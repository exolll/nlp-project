"""One explicitly authorized deep check over an already stable run: no new search, no GitHub.

Dry run (default) computes the exact request size and writes nothing. --allow-paid performs
at most one model HTTP call. --verify-only re-checks the published report from stored bytes.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

OWNER = 'e9b9a322101866a4eceff782b3f18fbcf8264c70bef2434b1eed94c9f1f0f547'
RUN = '287e4237-42df-4748-92a8-d114c418778e'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def line_of(data):
    offsets, position = [], 0
    for line in data.split(b'\n'):
        offsets.append(position)
        position += len(line) + 1
    return lambda index: max(i for i, offset in enumerate(offsets) if offset <= index) + 1


def verify(owner, run_id, revision, check, report, storage, engine):
    from sqlalchemy import func, select

    from app.persistence.search_database import search_transaction
    from app.persistence.search_models import SearchSnapshot

    persistence = check.persistence
    manifest = persistence.manifests.read(owner, run_id)
    op = manifest['deep_check_operation']
    stored_report, _ = storage.read_version(op['report_key'])
    stored_response, _ = storage.read_version(op['response_key'])
    checks = {
        'operation_completed': op['status'] == 'completed',
        'published': op['published'] is True,
        'report_hash_matches_stored': digest(stored_report) == op['report_hash'],
        'report_is_recomputed_report': stored_report == encode(op['report']),
        'response_hash_matches_stored': digest(stored_response) == op['response_hash'],
        'config_hash_is_current_defaults': op['config_hash'] == digest(encode(check.settings.model_dump())),
        'plan_bound': op['plan_hash'] == manifest['plan']['plan_hash'],
        'revision_bound': op['revision_hash'] == digest(encode(revision.model_dump(mode='json'))),
    }
    failures, cited, samples = [], 0, []
    for candidate in report['candidates']:
        if not candidate['source']:
            continue
        raw = persistence.read_snapshot(owner, run_id, candidate['source']['snapshot_id'], read_only=True)
        normalized = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
        locate = line_of(normalized)
        for item in candidate['items']:
            for citation in item['citations']:
                cited += 1
                if normalized[citation['byte_start']:citation['byte_end']].decode('utf-8') != citation['text']:
                    failures.append({'item': item['text'], 'why': 'bytes differ'})
                elif (locate(citation['byte_start']) != citation['line_start']
                      or locate(citation['byte_end'] - 1) != citation['line_end']):
                    failures.append({'item': item['text'], 'why': 'lines differ'})
                if len(samples) < 6:
                    samples.append({'repository_id': candidate['repository_id'], 'text': item['text'],
                                    'verdict': item['verdict'], 'line': citation['line_start'],
                                    'quote': citation['text'][:120]})
    checks['all_citations_reproduce'] = not failures
    checks['every_supported_verdict_cited'] = not [
        item['text'] for candidate in report['candidates'] if candidate['source']
        for item in candidate['items'] if item['verdict'] != 'insufficient' and not item['citations']]
    checks['insufficient_items_carry_no_quotes'] = not [
        item['text'] for candidate in report['candidates']
        for item in candidate['items'] if item['verdict'] == 'insufficient' and item['citations']]
    with search_transaction(engine) as db:
        counts = dict(db.execute(select(SearchSnapshot.usage_type, func.count())
                                 .group_by(SearchSnapshot.usage_type)).all())
    checks['mysql_deep_usage_rows'] = counts.get('deep_analysis') == len(op['usage'])
    summary = {
        'checks': checks, 'failed': [name for name, ok in checks.items() if not ok],
        'citations': cited, 'citation_failures': failures[:5], 'citation_samples': samples,
        'provider_usage': op['provider_usage'], 'model_alias': op['model_alias'],
        'mysql_usage_counts': counts,
        'labels': [{'repository_id': row['repository_id'], 'label': row['recommendation']['label'],
                    'reason': row['recommendation']['reason'], 'coverage': row['input_coverage']}
                   for row in report['candidates']],
        'item_verdicts': {row['repository_id']: [{'text': item['text'], 'modality': item['modality'],
                                                  'verdict': item['verdict'], 'basis': item['basis'],
                                                  'citations': len(item['citations'])}
                                                 for item in row['items']]
                          for row in report['candidates'] if row['source']},
    }
    print(json.dumps(summary, ensure_ascii=False))
    if summary['failed']:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-paid', action='store_true')
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--owner', default=OWNER)
    parser.add_argument('--run', default=RUN)
    arguments = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    import anyio

    from app.core.settings import ExtractionSettings, Settings
    from app.persistence.search_database import SearchDatabaseSettings, create_search_engine
    from app.persistence.search_repository import SearchRepository
    from app.schemas.requirements import RequirementRevision
    from app.services.deep_check import DeepCheck, DeepCheckSettings
    from app.services.search_persistence import SearchPersistence
    from app.services.storage import create_storage

    owner, run_id = arguments.owner, arguments.run
    storage = create_storage(Settings())
    raw, _ = storage.read_version(f'sessions/{owner}.json')
    session = json.loads(raw)
    task = next(t for t in session['tasks'].values()
                if any(r['run_id'] == run_id for r in (t.get('search_runs') or {}).values()))
    revision_value = next(r for r in task['requirements']['revisions']
                          if r['status'] == 'confirmed' and r['confirmed_at'])
    revision = RequirementRevision.model_validate(revision_value)
    provider = ExtractionSettings()
    engine = create_search_engine(SearchDatabaseSettings())
    try:
        persistence = SearchPersistence(storage, SearchRepository(engine))
        check = DeepCheck(persistence, provider, settings=DeepCheckSettings())
        manifest, operation = check._state(owner, task['id'], run_id, revision)
        print(json.dumps({'run': run_id, 'task': task['id'], 'revision': str(revision.revision_id),
                          'evidence_published': manifest.get('evidence_operation', {}).get('published'),
                          'deep_operation': operation is not None,
                          'requirement_items': len(revision.requirement_items),
                          'candidates': len(manifest['candidates'])}, ensure_ascii=False))
        if arguments.verify_only:
            if operation is None:
                raise SystemExit('No deep check operation exists to verify')
            verify(owner, run_id, revision, check, check.read(owner, task['id'], run_id, revision),
                   storage, engine)
            return
        if operation is not None:
            raise SystemExit('This run already has a deep check operation; refusing to spend again')
        entries = check._sources(owner, run_id, manifest)
        data, system = check._payload(revision, manifest, entries)
        fed = {rid: row for rid, row in entries.items() if row['coverage'] != 'not_fed'}
        print(json.dumps({'coverage': {rid: row['coverage'] for rid, row in entries.items()},
                          'fed_snapshots': len(fed), 'payload_bytes': len(data),
                          'system_bytes': len(system.encode('utf-8')),
                          'input_upper_bound_tokens': len(data) + len(system.encode('utf-8')) + 256,
                          'input_limit': check.settings.extraction_max_input_tokens,
                          'max_output_tokens': check.settings.extraction_max_output_tokens},
                         ensure_ascii=False))
        if not arguments.allow_paid:
            print('DRY RUN: nothing written, model not called.')
            return
        report = anyio.run(check.check, owner, task['id'], run_id, revision)
        assert digest(encode(check.read(owner, task['id'], run_id, revision))) == digest(encode(report))
        verify(owner, run_id, revision, check, report, storage, engine)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
