"""Explicitly authorized live acceptance over one existing SearchRun and Bundle."""
import argparse
import hashlib
import json
import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / 'data' / 'acceptance' / '3a-ui' / 'd198dbe9-583f-4764-9d80-d02cc9fdfcaa.json'
AUTHORIZATION_SCOPE = (
    '用户批准阶段三真实验收：优先复用已有稳定 SearchRun 和 EvidenceBundle；'
    'DeepSeek 最多调用 5 次，每仓库最多一次，不自动重试。'
)
BUDGET = {
    'max_repositories': 5,
    'max_model_calls': 5,
    'max_calls_per_repository': 1,
    'max_chunks_per_repository': 16,
    'max_input_bytes_upper_bound': 24000,
    'max_output_tokens': 2000,
    'automatic_retries': 0,
}
TERMINAL = ('completed', 'partial', 'failed', 'unknown')


def now():
    return datetime.now(timezone.utc)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def acceptance_path(run_id):
    return ROOT / 'data' / 'acceptance' / 'deep-review' / f'{run_id}.json'


def save_acceptance(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.pending')
    temporary.write_bytes(encode(value))
    temporary.replace(path)


def upgrade_bundle(context):
    from app.services.retrieval import WARNING

    bundle = deepcopy(context['source_bundle'])
    if bundle.get('version') == 'evidence-bundle-v2' and bundle.get('schema_version') == 2:
        return bundle
    if bundle.get('version') != 'evidence-bundle-v1' or bundle.get('schema_version') is not None:
        raise ValueError('Unsupported source Bundle version')
    operations = {row['snapshot_id']: row
                  for row in context['manifest']['readme_persistence_operations'].values()
                  if row['operation_status'] == 'completed'}
    for candidate in bundle['candidates']:
        snapshot_id = candidate['snapshot_id']
        operation = operations.get(snapshot_id)
        if operation is None or str(operation['repository_id']) != str(candidate['repository_id']):
            raise ValueError('DEEP_REVIEW_SNAPSHOT_BINDING')
        raw, _ = context['storage'].read_version(operation['storage_key'])
        if raw is None:
            raise ValueError('DEEP_REVIEW_SNAPSHOT_UNAVAILABLE')
        content_hash = hashlib.sha256(raw).hexdigest()
        blob_sha = hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        if content_hash != operation['content_hash'] or blob_sha != operation['blob_sha']:
            raise ValueError('DEEP_REVIEW_SNAPSHOT_INTEGRITY')
        trace = {key: operation[key] for key in
                 ('commit_sha', 'blob_sha', 'content_hash', 'readme_path', 'collected_at')}
        if any(key in candidate and candidate[key] != value for key, value in trace.items()):
            raise ValueError('DEEP_REVIEW_SNAPSHOT_BINDING')
        candidate.update(trace)
        for item in candidate['items']:
            item['status'] = 'retrieved' if item['chunks'] else 'empty'
            item['relevance'] = 'unverified'
            if not item['chunks'] and not item.get('empty_reason'):
                item['empty_reason'] = 'no_match_in_saved_snapshot'
    bundle['version'] = 'evidence-bundle-v2'
    bundle['schema_version'] = 2
    bundle['warning'] = WARNING
    return bundle


def load_context(source, run_id=None):
    from app.core.settings import ExtractionSettings, Settings
    from app.persistence.run_manifest import RunManifestStore
    from app.schemas.requirements import RequirementRevision
    from app.services.evidence_bundle import EvidenceBundleStore
    from app.services.storage import create_storage

    control = json.loads(source.read_bytes())
    token = control.get('token_a')
    if not isinstance(token, str) or not token:
        raise ValueError('Source acceptance control has no session token')
    owner = hashlib.sha256(token.encode()).hexdigest()
    run_id = run_id or control['run_id']
    settings = Settings()
    storage = create_storage(settings)
    session_key = f'sessions/{owner}.json'
    raw_session, _ = storage.read_version(session_key)
    if raw_session is None:
        raise ValueError('Source session no longer exists')
    session = json.loads(raw_session)
    task = session['tasks'][control['task_id']]
    record = next(row for row in task['search_runs'].values() if row['run_id'] == run_id)
    revision_value = next(row for row in task['requirements']['revisions']
                          if row['revision_id'] == control['revision_id'])
    revision = RequirementRevision.model_validate(revision_value)
    manifest = RunManifestStore(storage).read(owner, run_id)
    source_bundle, source_retrieval = EvidenceBundleStore(storage).read(owner, run_id, revision)
    source_raw, _ = storage.read_version(source_retrieval['storage_key'])
    if source_raw is None or digest(source_raw) != source_retrieval['bundle_hash']:
        raise ValueError('Source Bundle hash differs')
    context = {
        'control': control, 'token': token, 'owner': owner, 'run_id': run_id,
        'task_id': control['task_id'], 'revision': revision, 'record': record,
        'manifest': manifest, 'source_bundle': source_bundle, 'source_bundle_raw': source_raw,
        'source_bundle_key': source_retrieval['storage_key'],
        'source_bundle_hash': source_retrieval['bundle_hash'],
        'settings': settings, 'provider': ExtractionSettings(), 'storage': storage,
        'session_key': session_key, 'session_expires_at': session['expires_at'],
    }
    if run_id != control['run_id']:
        if record.get('acceptance_clone_of') != control['run_id']:
            raise ValueError('Selected run is not an acceptance clone of the source run')
        context['source_run_id'] = control['run_id']
        context['clone'] = {'source_run_id': control['run_id'], 'run_id': run_id}
        context['source_bundle_hash'] = record.get('acceptance_source_bundle_hash',
                                                    source_retrieval['bundle_hash'])
        context['source_bundle_version'] = 'evidence-bundle-v1'
    context['bundle'] = upgrade_bundle(context)
    context['bundle_hash'] = digest(encode(context['bundle']))
    context['bundle_key'] = (context['source_bundle_key'] if context['bundle'] == context['source_bundle']
                             else f'deep-review-acceptance/{run_id}/bundle-v2.json')
    context['retrieval'] = {**source_retrieval, 'bundle_hash': context['bundle_hash'],
                            'source_bundle_hash': context['source_bundle_hash'],
                            'source_version': source_bundle.get('version')}
    return context


def snapshot_reader(context):
    operations = {row['snapshot_id']: row['storage_key']
                  for row in context['manifest']['readme_persistence_operations'].values()
                  if row['operation_status'] == 'completed'}

    def read(owner, run_id, snapshot_id):
        if owner != context['owner'] or run_id != context['run_id']:
            raise ValueError('Snapshot binding differs')
        key = operations.get(snapshot_id)
        if key is None:
            raise ValueError('Snapshot has no completed operation')
        raw, _ = context['storage'].read_version(key)
        if raw is None:
            raise ValueError('Snapshot bytes are missing')
        return raw

    return read


def put_immutable(storage, key, data):
    existing, _ = storage.read_version(key)
    if existing is not None:
        if existing != data:
            raise ValueError('Immutable acceptance object already differs')
        return
    storage.put(key, data, 'application/json')
    stored, _ = storage.read_version(key)
    if stored != data:
        raise ValueError('Immutable acceptance object was not confirmed')


def preflight(context):
    from app.services.deep_review_logic import operation_repository_budget, repository_budget
    from app.services.deep_review_pipeline import DeepReviewPipeline
    from app.services.requirement_quality import validate_quality

    revision, bundle, manifest = context['revision'], context['bundle'], context['manifest']
    quality = validate_quality(revision)
    if not quality['ok']:
        raise ValueError('DEEP_REVIEW_REQUIREMENT_QUALITY')
    stable = (manifest['dispatch_closed'] and not manifest['integrity_blocked']
              and manifest['plan_publication_status'] == 'published'
              and all(row['operation_status'] in ('completed', 'interrupted')
                      for row in manifest['readme_persistence_operations'].values()))
    if not stable:
        raise ValueError('DEEP_REVIEW_NOT_READY')
    if context['retrieval']['status'] not in ('ready', 'partial'):
        raise ValueError('DEEP_REVIEW_NOT_READY')
    expected = {
        'version': 'evidence-bundle-v2', 'schema_version': 2, 'owner': context['owner'],
        'task_id': context['task_id'], 'run_id': context['run_id'],
        'confirmed_revision_id': str(revision.revision_id),
        'revision_hash': quality['revision_hash'], 'plan_hash': manifest['plan']['plan_hash'],
    }
    if any(bundle.get(key) != value for key, value in expected.items()):
        raise ValueError('DEEP_REVIEW_BINDING_MISMATCH')
    if (context['retrieval']['revision_hash'] != quality['revision_hash']
            or context['retrieval']['plan_hash'] != manifest['plan']['plan_hash']):
        raise ValueError('DEEP_REVIEW_BINDING_MISMATCH')
    budget_error = operation_repository_budget(len(bundle['candidates']))
    if budget_error:
        raise ValueError(budget_error['error_code'])
    pipeline = DeepReviewPipeline(context['settings'], context['provider'], snapshot_reader(context))
    prepared = pipeline._prepare(context['owner'], context['run_id'], revision, bundle)
    rows = []
    for row in prepared:
        candidate = row['candidate']
        budget_error = repository_budget(len(row['chunks']), row['input_bytes'], BUDGET['max_output_tokens'])
        rows.append({
            'repository_id': str(candidate['repository_id']),
            'repository': (candidate.get('repository') or {}).get('full_name'),
            'snapshot_id': str(candidate['snapshot_id']),
            'coverage': row['coverage'], 'chunk_count': len(row['chunks']),
            'input_bytes_upper_bound': row['input_bytes'],
            'eligible': bool(row['chunks']) and budget_error is None,
            'budget_error': None if budget_error is None else budget_error['error_code'],
        })
    eligible = sum(row['eligible'] for row in rows)
    if not eligible:
        raise ValueError('DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES')
    return {
        'run_id': context['run_id'], 'task_id': context['task_id'],
        'revision_id': str(revision.revision_id), 'requirement_items': len(revision.requirement_items),
        'source_bundle_version': context.get('source_bundle_version', context['source_bundle'].get('version')),
        'source_bundle_hash': context['source_bundle_hash'],
        'derived_bundle_version': bundle.get('version'),
        'derived_bundle_hash': context['bundle_hash'],
        'bundle_status': context['retrieval']['status'], 'candidates': rows,
        'eligible_candidates': eligible,
        'max_model_calls': min(eligible, BUDGET['max_model_calls']),
        'model': context['provider'].deepseek_model,
        'model_configured': bool(context['provider'].deepseek_api_key.get_secret_value().strip()),
        'session_expires_at': context['session_expires_at'],
        'session_expired': datetime.fromisoformat(context['session_expires_at']) <= now(),
        'existing_operation': context['manifest'].get('deep_review_operation'),
        'dry_run': True, 'external_calls': 0,
    }


class LocalAPI:
    def __init__(self, base_url, token, timeout):
        self.client = httpx.Client(base_url=base_url.rstrip('/') + '/api/v1', timeout=timeout,
                                   trust_env=False)
        self.headers = {'Authorization': f'Bearer {token}'}

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, headers=self.headers, **kwargs)
        if response.status_code >= 400:
            try:
                error = response.json()
            except ValueError:
                error = {}
            raise RuntimeError(f"LOCAL_API_{response.status_code}:{error.get('error_code', 'UNKNOWN')}")
        return response.json() if response.content else None


def review_path(context):
    return f"/tasks/{context['task_id']}/search-runs/{context['run_id']}/deep-review"


def validate_operation(value, context):
    operation = value
    if (operation.get('revision_id') != str(context['revision'].revision_id)
            or operation.get('consent') is not True
            or operation.get('bundle_hash') != context['bundle_hash']
            or operation.get('status') not in ('running', *TERMINAL)):
        raise ValueError('Deep review response binding differs')
    report = operation.get('report')
    if report is not None and (
        report.get('task_id') != context['task_id']
        or report.get('run_id') != context['run_id']
        or report.get('confirmed_revision_id') != str(context['revision'].revision_id)
        or report.get('operation_id') != operation['operation_id']
        or report.get('bundle_hash') != operation['bundle_hash']
    ):
        raise ValueError('Deep review report binding differs')
    return operation


def read_operation(api, context, missing_ok=False):
    try:
        return validate_operation(api.request('GET', review_path(context)), context)
    except RuntimeError as exc:
        if missing_ok and 'LOCAL_API_404:DEEP_REVIEW_NOT_FOUND' in str(exc):
            return None
        raise


def poll(api, context, operation, timeout):
    deadline = time.monotonic() + timeout
    while operation['status'] == 'running':
        operation_deadline = datetime.fromisoformat(operation['deadline'])
        if now() >= operation_deadline + timedelta(seconds=30):
            return read_operation(api, context)
        if time.monotonic() >= deadline:
            raise TimeoutError('Deep review polling timed out; no POST was repeated')
        time.sleep(2.5)
        operation = read_operation(api, context)
    return operation


def line_of(data, offset):
    return data[:offset].count(b'\n') + 1


def verify_report(context, operation):
    from app.services.deep_review_models import DeepReviewReport
    from app.services.deep_review_logic import line_table, normalized_readme_bytes
    from app.services.extraction_provider import strict_json

    failures, citations_checked, samples = [], 0, []
    key, expected_hash = operation.get('report_key'), operation.get('report_hash')
    if not key or not expected_hash:
        return {'passed': False, 'failed': ['terminal operation has no immutable report'],
                'citations_checked': 0, 'citation_failures': []}
    raw, _ = context['storage'].read_version(key)
    if raw is None or digest(raw) != expected_hash:
        return {'passed': False, 'failed': ['stored report hash differs'],
                'citations_checked': 0, 'citation_failures': []}
    bundle_raw, _ = context['storage'].read_version(context['bundle_key'])
    if bundle_raw is None or digest(bundle_raw) != context['bundle_hash']:
        failures.append('derived Bundle hash differs')
    try:
        report = DeepReviewReport.model_validate(strict_json(raw))
    except (ValueError, TypeError, RecursionError):
        return {'passed': False, 'failed': ['stored report schema is invalid'],
                'citations_checked': 0, 'citation_failures': []}
    expected_identity = {
        'owner': context['owner'], 'task_id': context['task_id'], 'run_id': context['run_id'],
        'confirmed_revision_id': str(context['revision'].revision_id),
        'operation_id': operation['operation_id'], 'revision_hash': context['retrieval']['revision_hash'],
        'plan_hash': context['manifest']['plan']['plan_hash'],
        'bundle_hash': context['bundle_hash'], 'model': context['provider'].deepseek_model,
        'prompt_version': 'deep-review-v1', 'status': operation['status'],
    }
    if any(getattr(report, key) != value for key, value in expected_identity.items()):
        failures.append('report identity or status differs from operation')
    if report.summary.usage.calls > BUDGET['max_model_calls']:
        failures.append('model call budget exceeded')
    attempted = [row for row in report.repositories if row.status != 'skipped']
    if report.summary.usage.calls != len(attempted):
        failures.append('model call count differs from attempted repositories')
    known_usage = [row.usage for row in attempted if row.usage.known]
    if report.summary.usage.unknown_calls != len(attempted) - len(known_usage):
        failures.append('unknown usage count differs')
    if report.cost_status != 'not_calculated' or any(
        value is not None for value in (report.estimated_cost, report.currency, report.pricing_source)
    ):
        failures.append('unapproved pricing was recorded')
    requirement_ids = {str(item.requirement_item_id) for item in context['revision'].requirement_items}
    bundle_candidates = {str(row['repository_id']): row for row in context['bundle']['candidates']}
    snapshot_keys = {row['snapshot_id']: row['storage_key']
                     for row in context['manifest']['readme_persistence_operations'].values()
                     if row['operation_status'] == 'completed'}
    for repository in report.repositories:
        candidate = bundle_candidates.get(repository.repository_id)
        if candidate is None or str(candidate['snapshot_id']) != repository.snapshot_id:
            failures.append(f"repository {repository.repository_id}: bundle ownership differs")
            continue
        if any(getattr(repository.trace, key) != candidate.get(key) for key in
               ('commit_sha', 'blob_sha', 'content_hash', 'readme_path', 'collected_at')):
            failures.append(f"repository {repository.repository_id}: snapshot trace differs")
        expected_chunks = [chunk['chunk_id'] for item in candidate['items'] for chunk in item['chunks']]
        if repository.input.chunk_ids != list(dict.fromkeys(expected_chunks)):
            failures.append(f"repository {repository.repository_id}: model input chunks differ")
        if repository.status != 'completed':
            if repository.requirements:
                failures.append(f"repository {repository.repository_id}: incomplete repository has conclusions")
            continue
        if {row.requirement_item_id for row in repository.requirements} != requirement_ids:
            failures.append(f"repository {repository.repository_id}: requirement coverage differs")
        if len(repository.requirements) != len(requirement_ids):
            failures.append(f"repository {repository.repository_id}: duplicate requirement conclusions")
        source_key = snapshot_keys.get(repository.snapshot_id)
        source_raw, _ = context['storage'].read_version(source_key) if source_key else (None, None)
        if source_raw is None:
            failures.append(f"repository {repository.repository_id}: snapshot bytes missing")
            continue
        if hashlib.sha256(source_raw).hexdigest() != repository.trace.content_hash:
            failures.append(f"repository {repository.repository_id}: content hash differs")
        blob_sha = hashlib.sha1(b'blob ' + str(len(source_raw)).encode('ascii') + b'\0' + source_raw).hexdigest()
        if blob_sha != repository.trace.blob_sha:
            failures.append(f"repository {repository.repository_id}: blob SHA differs")
        data = normalized_readme_bytes(source_raw)
        table = line_table(data)
        chunks = {chunk['chunk_id']: chunk for item in candidate['items'] for chunk in item['chunks']}
        for item in repository.requirements:
            if item.verdict in ('supports', 'contradicts') and not item.citations:
                failures.append(f"repository {repository.repository_id}: {item.verdict} has no citation")
            if item.verdict == 'insufficient' and item.citations:
                failures.append(f"repository {repository.repository_id}: insufficient has citations")
            for citation in item.citations:
                citations_checked += 1
                chunk = chunks.get(citation.chunk_id)
                if chunk is None:
                    failures.append(f"repository {repository.repository_id}: citation chunk is not in bundle")
                    continue
                if not (chunk['byte_start'] <= citation.byte_start < citation.byte_end <= chunk['byte_end']
                        and chunk['line_start'] <= citation.line_start <= citation.line_end <= chunk['line_end']):
                    failures.append(f"repository {repository.repository_id}: citation is outside its chunk")
                if data[citation.byte_start:citation.byte_end].decode('utf-8', 'replace') != citation.quote:
                    failures.append(f"repository {repository.repository_id}: citation bytes differ")
                elif (line_of(data, citation.byte_start) != citation.line_start
                      or line_of(data, citation.byte_end - 1) != citation.line_end
                      or not (1 <= citation.line_start <= citation.line_end <= len(table))):
                    failures.append(f"repository {repository.repository_id}: citation lines differ")
                if len(samples) < 5:
                    samples.append({'repository_id': repository.repository_id,
                                    'requirement_item_id': item.requirement_item_id,
                                    'verdict': item.verdict, 'line_start': citation.line_start,
                                    'line_end': citation.line_end, 'quote': citation.quote[:160]})
    return {
        'passed': not failures, 'failed': failures, 'citations_checked': citations_checked,
        'citation_failures': failures, 'citation_samples': samples,
        'report_hash': expected_hash, 'report_status': report.status,
        'usage': report.summary.usage.model_dump(mode='json'),
        'cost_status': report.cost_status, 'estimated_cost': report.estimated_cost,
        'currency': report.currency, 'pricing_source': report.pricing_source,
        'repositories': report.summary.model_dump(mode='json'),
    }


def extend_session(context):
    from app.services.storage import CASConflict

    for _ in range(64):
        raw, version = context['storage'].read_version(context['session_key'])
        if raw is None:
            raise ValueError('Source session no longer exists')
        session = json.loads(raw)
        original = session['expires_at']
        if datetime.fromisoformat(original) > now():
            return {'extended': False, 'original_expires_at': original}
        extended = (now() + timedelta(hours=context['settings'].session_hours)).isoformat()
        session['expires_at'] = extended
        try:
            context['storage'].compare_and_swap(context['session_key'], version, encode(session))
            return {'extended': True, 'original_expires_at': original,
                    'extended_expires_at': extended}
        except CASConflict:
            continue
    raise ValueError('Session extension CAS retry budget exhausted')


def restore_session(context, target):
    if not target.exists():
        raise ValueError('No live acceptance record exists')
    control = json.loads(target.read_bytes())
    extension = control.get('session_extension')
    if not extension or not extension.get('extended'):
        return {'restored': False, 'reason': 'session was not extended'}
    from app.services.storage import CASConflict

    for _ in range(64):
        raw, version = context['storage'].read_version(context['session_key'])
        if raw is None:
            raise ValueError('Source session no longer exists')
        session = json.loads(raw)
        if session['expires_at'] == extension['original_expires_at']:
            control['session_restored_at'] = now().isoformat()
            save_acceptance(target, control)
            return {'restored': True, 'expires_at': session['expires_at']}
        if session['expires_at'] != extension['extended_expires_at']:
            raise ValueError('Session expiry changed outside this acceptance; not overwriting it')
        session['expires_at'] = extension['original_expires_at']
        try:
            context['storage'].compare_and_swap(context['session_key'], version, encode(session))
            control['session_restored_at'] = now().isoformat()
            save_acceptance(target, control)
            return {'restored': True, 'expires_at': session['expires_at']}
        except CASConflict:
            continue
    raise ValueError('Session restore CAS retry budget exhausted')


def clone_run(context, replace_failed_preprovider=False):
    from app.persistence.run_manifest import RunManifestStore
    from app.services.storage import CASConflict

    source_run_id = context['run_id']
    if context['source_bundle'].get('version') != 'evidence-bundle-v1':
        raise ValueError('Clone preparation is only needed for a v1 source Bundle')
    for _ in range(64):
        raw, version = context['storage'].read_version(context['session_key'])
        if raw is None:
            raise ValueError('Source session no longer exists')
        session = json.loads(raw)
        task = session['tasks'][context['task_id']]
        existing = next((row for row in task.get('search_runs', {}).values()
                         if row.get('acceptance_clone_of') == source_run_id), None)
        if existing:
            if not replace_failed_preprovider:
                raise ValueError(f"Acceptance clone already exists: {existing['run_id']}")
            failed_manifest = RunManifestStore(context['storage']).read(context['owner'], existing['run_id'])
            failed_operation = failed_manifest.get('deep_review_operation') or {}
            if not (failed_operation.get('status') == 'failed'
                    and failed_operation.get('error_code') == 'DEEP_REVIEW_SNAPSHOT_UNAVAILABLE'
                    and failed_operation.get('report_key') is None):
                raise ValueError('Only a failed pre-provider acceptance clone may be replaced')
        if len(task.get('search_runs', {})) >= 3:
            raise ValueError('SearchRun limit reached; no clone was created')
        run_id = str(uuid4())
        operation_id = str(uuid4())
        bundle = deepcopy(context['bundle'])
        bundle['run_id'] = run_id
        bundle_hash = digest(encode(bundle))
        bundle_key = f'retrieval-bundles/{operation_id}.json'
        timestamp = now().isoformat()
        retrieval = {
            'operation_id': operation_id, 'version': 'evidence-bundle-v2',
            'revision_hash': context['retrieval']['revision_hash'],
            'plan_hash': context['retrieval']['plan_hash'],
            'chunk_index_key': context['retrieval']['chunk_index_key'],
            'model': context['retrieval']['model'], 'dimension': context['retrieval']['dimension'],
            'config_hash': digest(encode(bundle['config'])), 'storage_key': bundle_key,
            'bundle_hash': bundle_hash, 'status': context['retrieval']['status'],
            'error_code': context['retrieval']['error_code'],
            'created_at': timestamp, 'updated_at': timestamp,
        }
        manifest = {
            'owner': context['owner'], 'task_id': context['task_id'], 'run_id': run_id,
            'confirmed_revision_id': str(context['revision'].revision_id),
            'run_status': 'completed', 'plan_publication_status': 'published',
            'plan': deepcopy(context['manifest']['plan']),
            'candidates': deepcopy(context['manifest']['candidates']),
            'readme_persistence_operations': deepcopy(context['manifest']['readme_persistence_operations']),
            'chunk_index_operation': deepcopy(context['manifest']['chunk_index_operation']),
            'evidence_bundle_operation': retrieval,
            'requests': {}, 'dispatch_closed': True, 'integrity_blocked': False,
        }
        record = {
            'run_id': run_id, 'task_id': context['task_id'],
            'confirmed_revision_id': str(context['revision'].revision_id),
            'request_hash': digest(encode({'acceptance_clone_of': source_run_id,
                                           'source_bundle_hash': context['source_bundle_hash']})),
            'status': 'completed', 'pipeline_status': 'completed', 'pipeline_error_code': None,
            'acceptance_clone_of': source_run_id,
            'acceptance_source_bundle_hash': context['source_bundle_hash'],
        }
        put_immutable(context['storage'], bundle_key, encode(bundle))
        put_immutable(context['storage'], f'runs/{context["owner"]}/{run_id}.json', encode(manifest))
        RunManifestStore(context['storage']).read(context['owner'], run_id)
        task.setdefault('search_runs', {})[run_id] = record
        try:
            context['storage'].compare_and_swap(context['session_key'], version, encode(session))
            break
        except CASConflict:
            continue
    else:
        raise ValueError('Clone session CAS retry budget exhausted')
    old_target = acceptance_path(source_run_id)
    if old_target.exists():
        old_control = json.loads(old_target.read_bytes())
        context['session_extension'] = old_control.get('session_extension')
    context.update({
        'source_run_id': source_run_id, 'run_id': run_id, 'bundle': bundle,
        'bundle_hash': bundle_hash, 'bundle_key': bundle_key, 'retrieval': retrieval,
        'manifest': manifest, 'record': record, 'clone': {
            'source_run_id': source_run_id, 'run_id': run_id,
            'bundle_operation_id': operation_id,
            'source_bundle_hash': context['source_bundle_hash'],
        },
    })
    return context


def run_live(context, target, source):
    import anyio
    from app.persistence.run_manifest import RunManifestStore
    from app.services.deep_review_logic import DeepReviewInputError
    from app.services.deep_review_pipeline import DeepReviewPipeline, PROMPT_VERSION

    class CountingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport(retries=0)
            self.calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    if not context['provider'].deepseek_api_key.get_secret_value().strip():
        raise ValueError('DeepSeek credentials are not configured')
    if target.exists():
        raise ValueError('Acceptance record already exists; use verify instead of spending again')
    if context['manifest'].get('deep_review_operation') is not None:
        raise ValueError('An operation already exists; no new model call was authorized')
    extension = context.get('session_extension') or extend_session(context)
    control = {
        'schema_version': 1, 'source_control': str(source.relative_to(ROOT)),
        'run_id': context['run_id'], 'source_run_id': context.get('source_run_id', context['run_id']),
        'task_id': context['task_id'], 'revision_id': str(context['revision'].revision_id),
        'reused_existing_search_run': not bool(context.get('clone')),
        'reused_existing_bundle': True, 'acceptance_clone': context.get('clone'),
        'source_bundle_version': context.get('source_bundle_version', context['source_bundle'].get('version')),
        'source_bundle_hash': context['source_bundle_hash'],
        'derived_bundle_version': context['bundle'].get('version'),
        'derived_bundle_hash': context['bundle_hash'], 'derived_bundle_key': context['bundle_key'],
        'bundle_derivation': 'offline metadata completion only; candidate chunks unchanged',
        'operation_creation_path': 'acceptance_direct_manifest_storage_snapshot_reader',
        'authorization_scope': AUTHORIZATION_SCOPE, 'authorized_at': now().isoformat(),
        'budget': BUDGET, 'model': context['provider'].deepseek_model,
        'prompt_version': PROMPT_VERSION, 'http_post_attempts': 0,
        'provider_call_attempts': 0, 'provider_calls_started': False,
        'pipeline_started': False, 'operation': None, 'session_extension': extension,
    }
    save_acceptance(target, control)
    authorized = now()
    operation = {
        'operation_id': str(uuid4()), 'status': 'running',
        'revision_id': str(context['revision'].revision_id),
        'revision_hash': context['retrieval']['revision_hash'],
        'bundle_hash': context['bundle_hash'], 'model': context['provider'].deepseek_model,
        'prompt_version': PROMPT_VERSION, 'consent': True,
        'authorized_at': authorized.isoformat(),
        'deadline': (authorized + timedelta(
            seconds=5 * context['settings'].extraction_timeout_seconds + 120)).isoformat(),
        'report_key': None, 'report_hash': None, 'error_code': None,
    }
    manifests = RunManifestStore(context['storage'])
    operation, created = manifests.create_deep_review(context['owner'], context['run_id'], operation)
    control['operation'] = operation
    control['operation_created'] = created
    save_acceptance(target, control)
    if not created:
        raise ValueError('Deep review operation was created concurrently; no model call was made')
    transport = CountingTransport()
    pipeline = DeepReviewPipeline(context['settings'], context['provider'], snapshot_reader(context), transport)
    control['pipeline_started'] = True
    save_acceptance(target, control)
    try:
        report = anyio.run(
            pipeline.generate, context['owner'], context['task_id'], context['run_id'],
            context['revision'], context['bundle'], context['bundle_hash'], operation['operation_id'])
    except DeepReviewInputError as exc:
        control['provider_call_attempts'] = transport.calls
        control['provider_calls_started'] = transport.calls > 0
        manifests.complete_deep_review(context['owner'], context['run_id'], operation['operation_id'],
                                       'failed', error_code=exc.code)
        control['execution_outcome'] = 'failed_before_provider'
        control['error_code'] = exc.code
        save_acceptance(target, control)
        anyio.run(transport.aclose)
        raise
    except Exception as exc:
        control['provider_call_attempts'] = transport.calls
        control['provider_calls_started'] = transport.calls > 0
        control['execution_outcome'] = 'unknown'
        control['error_type'] = type(exc).__name__
        save_acceptance(target, control)
        anyio.run(transport.aclose)
        raise
    anyio.run(transport.aclose)
    data = encode(report.model_dump(mode='json'))
    report_key = f"deep-reviews/{operation['operation_id']}/report.json"
    try:
        put_immutable(context['storage'], report_key, data)
        manifests.complete_deep_review(context['owner'], context['run_id'], operation['operation_id'],
                                       report.status, report_key=report_key, report_hash=digest(data))
    except Exception as exc:
        control['provider_call_attempts'] = transport.calls
        control['provider_calls_started'] = transport.calls > 0
        control['execution_outcome'] = 'unknown'
        control['error_type'] = type(exc).__name__
        save_acceptance(target, control)
        raise
    control['provider_call_attempts'] = transport.calls
    control['provider_calls_started'] = transport.calls > 0
    control['execution_outcome'] = report.status
    control['report_key'] = report_key
    control['report_hash'] = digest(data)
    control['finished_at'] = now().isoformat()
    save_acceptance(target, control)
    operation = manifests.read(context['owner'], context['run_id'])['deep_review_operation']
    return operation


def verify_live(context, api, target, timeout):
    if not target.exists():
        raise ValueError('No live acceptance record exists')
    control = json.loads(target.read_bytes())
    if control['run_id'] != context['run_id']:
        raise ValueError('Acceptance record belongs to another run')
    operation = read_operation(api, context)
    if operation['status'] == 'running':
        operation = poll(api, context, operation, timeout)
    before = digest(encode(operation))
    repeated = validate_operation(api.request(
        'POST', review_path(context),
        json={'revision_id': str(context['revision'].revision_id), 'consent': True},
    ), context)
    after_operation = read_operation(api, context)
    checks = {
        'same_operation_after_repeated_click': repeated['operation_id'] == operation['operation_id'],
        'operation_unchanged_after_repeated_click': before == digest(encode(after_operation)),
        'terminal_status': operation['status'] in TERMINAL,
        'model_call_budget': (operation.get('report') or {}).get('summary', {}).get('usage', {}).get('calls', 0)
                             <= BUDGET['max_model_calls'],
    }
    local = verify_report(context, operation)
    control['operation'] = operation
    control['http_post_attempts'] = control.get('http_post_attempts', 0) + 1
    control['verified_at'] = now().isoformat()
    control['verification'] = {**checks, **local}
    save_acceptance(target, control)
    return control['verification']


def read_live(context, api, target, timeout):
    if not target.exists():
        raise ValueError('No live acceptance record exists')
    control = json.loads(target.read_bytes())
    operation = read_operation(api, context)
    if operation['status'] == 'running':
        operation = poll(api, context, operation, timeout)
    before = digest(encode(operation))
    after_operation = read_operation(api, context)
    checks = {
        'read_only_get': True,
        'operation_unchanged_across_gets': before == digest(encode(after_operation)),
        'terminal_status': operation['status'] in TERMINAL,
    }
    local = verify_report(context, operation)
    control['operation'] = operation
    control['last_read_verification'] = {**checks, **local, 'checked_at': now().isoformat()}
    save_acceptance(target, control)
    return control['last_read_verification']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--phase', choices=('inspect', 'prepare', 'run', 'verify', 'read', 'restore'), required=True)
    parser.add_argument('--allow-paid', action='store_true')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--run-id')
    parser.add_argument('--new-clone', action='store_true')
    arguments = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if arguments.phase == 'run' and not arguments.allow_paid:
        raise SystemExit('The run phase makes real paid model calls and needs --allow-paid')
    context = load_context(arguments.source.resolve(), arguments.run_id)
    target = acceptance_path(context['run_id'])
    if arguments.phase == 'inspect':
        result = preflight(context)
    elif arguments.phase == 'prepare':
        preflight(context)
        context = clone_run(context, arguments.new_clone)
        result = {'phase': 'prepare', 'clone': context['clone'],
                  'derived_bundle_hash': context['bundle_hash'],
                  'manifest_validated': True, 'external_calls': 0}
    elif arguments.phase == 'restore':
        result = restore_session(context, target)
    else:
        api = LocalAPI(arguments.base_url, context['token'], arguments.timeout)
        try:
            if arguments.phase == 'verify':
                result = verify_live(context, api, target, arguments.timeout)
            elif arguments.phase == 'read':
                result = read_live(context, api, target, arguments.timeout)
            else:
                if not arguments.run_id and context['source_bundle'].get('version') == 'evidence-bundle-v1':
                    raise ValueError('Prepare an acceptance clone first, then pass its --run-id')
                preflight(context)
                operation = run_live(context, target, arguments.source.resolve())
                result = {'phase': 'run', 'operation_id': operation['operation_id'],
                          'status': operation['status'], 'report_key': operation.get('report_key'),
                          'usage': (operation.get('report') or {}).get('summary', {}).get('usage'),
                          'acceptance': str(target)}
        finally:
            api.close()
    print(json.dumps(result, ensure_ascii=False))
    if arguments.phase == 'verify' and (not all(result[key] for key in (
        'same_operation_after_repeated_click', 'operation_unchanged_after_repeated_click',
        'terminal_status', 'model_call_budget', 'passed'))):
        raise SystemExit(1)
    if arguments.phase == 'read' and (not result['read_only_get']
                                     or not result['operation_unchanged_across_gets']
                                     or not result['terminal_status'] or not result['passed']):
        raise SystemExit(1)


def control_key(operation_id):
    return f'deep-reviews/{operation_id}/report.json'


if __name__ == '__main__':
    main()
