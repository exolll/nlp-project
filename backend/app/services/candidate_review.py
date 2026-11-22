import re
from copy import deepcopy
from uuid import UUID, uuid5

from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.requirement_validation import validate_coverage
from app.services.search_persistence import _digest, _encode, _lock


def _bound(persistence, owner, task_id, run_id, revision, *, read_only=False):
    manifest = persistence.manifests.read(owner, run_id)
    if manifest['integrity_blocked']:
        raise SearchIntegrityError('Run is integrity blocked')
    if (manifest['task_id'] != str(task_id) or revision.status != 'confirmed'
            or str(revision.task_id) != str(task_id)
            or manifest['confirmed_revision_id'] != str(revision.revision_id)):
        raise SearchStateError('Review requires the bound confirmed revision')
    validate_coverage(revision.requirements, revision.requirement_items)
    ids = [item.requirement_item_id for item in revision.requirement_items]
    if len(ids) != len(set(ids)):
        raise SearchStateError('Duplicate requirement item')
    digest = _digest(_encode(revision.model_dump(mode='json')))
    for field in ('planner_operation', 'candidate_screening', 'evidence_operation'):
        if field in manifest and manifest[field]['revision_hash'] != digest:
            raise SearchIntegrityError('Confirmed revision changed')
    plan = persistence.read_plan(owner, run_id, read_only=read_only)
    plan.validate_revision(revision, str(task_id))
    return manifest, digest


def _tokens(text):
    # Explicit English filtering only; this is literal retrieval, not NLP inference.
    stopwords = set(('a an the and or but if then else as at by for from in into of on '
                     'to with without is are was were be been being do does did have has '
                     'had it its this that these those i we you they he she their our your '
                     'which what who will would can could may might shall should must '
                     'not no nor only also any all some such than use uses using used '
                     'support supports supported supporting require requires required '
                     'need needs needed feature features project application software '
                     'system tool tools implement implements provide provides').split())
    tokens = re.findall(r'\.?[^\W_]+(?:[.-][^\W_]+)*(?:\+\+|#)?', text, re.UNICODE)
    return sorted({token.casefold() for token in tokens
                   if token.casefold() not in stopwords and not token.isdecimal()})


def _mentions(text, item, cap=3):
    tokens = _tokens(item.text)
    if not tokens or cap <= 0:
        return []
    pattern = re.compile(r'(?<![\w.+#])(?:' + '|'.join(re.escape(token) for token in tokens)
                         + r')(?![\w+#]|\.[^\W_])', re.IGNORECASE)
    quotes = []
    byte_start = 0
    # Keep complete lines, including negation, rather than isolated matched words.
    # Oversized lines are left unconfirmed instead of quoting misleading fragments.
    for line_number, line in enumerate(text.split('\n'), 1):
        content = line.rstrip('\r\n')
        raw = content.encode('utf-8')
        if len(raw) <= 2000 and pattern.search(content):
            quotes.append({'text': content, 'byte_start': byte_start,
                           'byte_end': byte_start + len(raw),
                           'line_start': line_number, 'line_end': line_number})
            if len(quotes) >= min(cap, 3):
                break
        byte_start += len(line.encode('utf-8')) + 1
    return quotes


class CandidateReview:
    def __init__(self, persistence):
        self.persistence = persistence

    def screen(self, owner, task_id, run_id, revision):
        with _lock(run_id):
            m, digest = _bound(self.persistence, owner, task_id, run_id, revision)
            if 'candidate_screening' in m:
                return deepcopy(m['candidate_screening'])
            if m['dispatch_closed'] or m['run_status'] not in ('queued', 'running'):
                raise SearchStateError('Screening must precede README dispatch')
            if m['readme_persistence_operations'] or m.get('search_execution', {}).get('readme_targets'):
                raise SearchStateError('README selection has already begun')
            results = []
            scores = {}
            for repo_id, candidate in sorted(m['candidates'].items(), key=lambda pair: int(pair[0])):
                metadata = candidate.get('metadata', {})
                text = ' '.join(str(metadata.get(key) or '') for key in ('full_name', 'description', 'language', 'topics'))
                matched = [item for item in revision.requirement_items
                           if item.modality != 'prohibited' and _mentions(text, item, 1)]
                mentions = [item.requirement_item_id for item in matched]
                # One core mention outweighs all secondary hints combined.
                core_weight = len(revision.requirement_items) + 1
                priority = sum(core_weight if item.field == 'project_topic' or
                               (item.field == 'required_features' and item.modality == 'required')
                               else 1 for item in matched)
                scores[repo_id] = priority
                # The persisted priority retains its v1 mention-count contract;
                # weighted scoring determines the retrieval order only.
                results.append({'repository_id': repo_id, 'eligible': True,
                                'metadata_mentions': mentions, 'priority': len(mentions),
                                'reason': 'literal_metadata_mentions' if mentions else 'unknown_retained'})
            results.sort(key=lambda row: (-scores[row['repository_id']], int(row['repository_id'])))
            result = {'version': 'candidate-screening-v1', 'revision_hash': digest,
                      'plan_hash': m['plan']['plan_hash'], 'candidates': results,
                      'warning': 'Metadata mentions prioritize retrieval only; absence is not failure.'}
            self.persistence.manifests.update(owner, run_id, lambda value: value.update(candidate_screening=result))
            return deepcopy(result)


class EvidenceReview:
    def __init__(self, persistence):
        self.persistence = persistence

    def read(self, owner, task_id, run_id, revision):
        m, _ = _bound(self.persistence, owner, task_id, run_id, revision, read_only=True)
        operation = m.get('evidence_operation')
        if not operation or not operation['published']:
            raise SearchStateError('Evidence report is not published')
        raw, _ = self.persistence.storage.read_version(operation['storage_key'])
        expected = _encode(operation['report'])
        if raw != expected or _digest(expected) != operation['report_hash']:
            raise SearchIntegrityError('Evidence report content differs')
        self._verify_report(owner, run_id, revision, operation['report'], read_only=True)
        return deepcopy(operation['report'])

    def review(self, owner, task_id, run_id, revision):
        with _lock(run_id):
            m, digest = _bound(self.persistence, owner, task_id, run_id, revision)
            if not m['dispatch_closed'] or any(op['operation_status'] not in ('completed', 'interrupted')
                                             for op in m['readme_persistence_operations'].values()):
                raise SearchStateError('Formal snapshots are not stable')
            try:
                if 'evidence_operation' not in m:
                    report = self._report(owner, run_id, revision, m)
                    operation_id = str(uuid5(UUID(run_id), 'evidence-review-v1'))
                    operation = {'operation_id': operation_id, 'version': 'evidence-review-v1',
                                 'revision_hash': digest, 'plan_hash': m['plan']['plan_hash'],
                                 'storage_key': f'evidence-reviews/{operation_id}.json',
                                 'report_hash': _digest(_encode(report)), 'report': report,
                                 'usage': {row['source']['snapshot_id']: False for row in report['candidates']
                                           if row['source'] is not None}, 'published': False}
                    m = self.persistence.manifests.update(owner, run_id, lambda value: value.update(evidence_operation=operation))
                operation = m['evidence_operation']
                self._verify_report(owner, run_id, revision, operation['report'])
                raw = _encode(operation['report'])
                self.persistence._put_immutable(operation['storage_key'], raw, 'application/json')
                actual, _ = self.persistence.storage.read_version(operation['storage_key'])
                if actual != raw:
                    raise SearchIntegrityError('Evidence report readback differs')
                for snapshot_id, recorded in operation['usage'].items():
                    if not recorded:
                        self.persistence.record_usage(owner, run_id, snapshot_id, 'evidence_retrieval')
                        self.persistence.manifests.update(owner, run_id, lambda value: value['evidence_operation']['usage'].update({snapshot_id: True}))
                self.persistence.manifests.update(owner, run_id, lambda value: value['evidence_operation'].update(published=True))
                return deepcopy(operation['report'])
            except SearchIntegrityError:
                self.persistence._block(owner, run_id, 'EVIDENCE_INTEGRITY')
                raise

    def _verify_report(self, owner, run_id, revision, report, *, read_only=False):
        fields = ('requirement_item_id', 'text', 'modality', 'operator', 'logic_group_id')
        expected = [{key: getattr(item, key) for key in fields} for item in revision.requirement_items]
        if report['requirement_items'] != expected:
            raise SearchIntegrityError('Evidence requirement coverage differs')
        for row in report['candidates']:
            if row['source'] is None:
                continue
            raw = self.persistence.read_snapshot(owner, run_id, row['source']['snapshot_id'], read_only=read_only)
            try:
                raw.decode('utf-8')
            except UnicodeDecodeError:
                if row['availability'] != 'non_utf8':
                    raise SearchIntegrityError('Evidence encoding differs') from None
                continue
            if row['availability'] != 'available':
                raise SearchIntegrityError('Evidence availability differs')
            for item in row['items']:
                for quote in item['quotes']:
                    start, end = quote['byte_start'], quote['byte_end']
                    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw)
                            or raw[start:end] != quote['text'].encode('utf-8')
                            or raw[:start].count(b'\n') + 1 != quote['line_start']
                            or raw[:end].count(b'\n') + 1 != quote['line_end']):
                        raise SearchIntegrityError('Evidence quote offsets differ')

    def _report(self, owner, run_id, revision, manifest):
        rows = []
        for repo_id, candidate in sorted(manifest['candidates'].items(), key=lambda pair: int(pair[0])):
            source, text = None, None
            availability = 'no_snapshot'
            if candidate.get('collection_status') == 'completed':
                op = manifest['readme_persistence_operations'][candidate['operation_id']]
                raw = self.persistence.read_snapshot(owner, run_id, op['snapshot_id'])
                source = {key: op[key] for key in ('snapshot_id', 'commit_sha', 'readme_path', 'blob_sha', 'content_hash')}
                try:
                    text = raw.decode('utf-8')
                    availability = 'available'
                except UnicodeDecodeError:
                    availability = 'non_utf8'
            items = []
            for item in revision.requirement_items:
                quotes = _mentions(text, item) if text is not None else []
                items.append({'requirement_item_id': item.requirement_item_id, 'text': item.text,
                              'modality': item.modality, 'operator': item.operator,
                              'logic_group_id': item.logic_group_id,
                              'status': 'mention_found' if quotes else 'unconfirmed', 'quotes': quotes})
            rows.append({'repository_id': repo_id, 'availability': availability, 'source': source, 'items': items})
        return {'version': 'evidence-review-v1', 'owner': owner, 'task_id': manifest['task_id'],
                'run_id': run_id, 'confirmed_revision_id': str(revision.revision_id),
                'warning': 'Literal mentions are not satisfaction. Negation and ALL/ANY semantics cannot be proven.',
                'requirement_items': [{key: getattr(item, key) for key in
                                       ('requirement_item_id', 'text', 'modality', 'operator', 'logic_group_id')}
                                      for item in revision.requirement_items],
                'candidates': rows}
