"""Explicitly authorized integration runner; never targets the configured business database."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

import httpx
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.settings import PROJECT_ROOT
from app.persistence.search_database import SearchDatabaseSettings, create_search_engine
from app.persistence.search_models import SearchBase, SearchPlan, RepositorySnapshot, SearchSnapshot
from app.persistence.search_repository import SearchRepository
from app.schemas.requirements import RequirementJSON, RequirementRevision
from app.schemas.search import SearchPlanDocument
from app.services.github_client import GitHubService, GitHubSettings
from app.services.search_contracts import query_hash
from app.services.search_execution import SearchExecutionSettings, SearchExecutor
from app.services.search_persistence import SearchPersistence
from app.services.storage import LocalStorage


class SimulatedCrash(BaseException):
    pass


class CrashAfterSnapshot(SearchRepository):
    def ensure_snapshot(self, snapshot):
        super().ensure_snapshot(snapshot)
        raise SimulatedCrash()


def save(path, value):
    raw = json.dumps(value, ensure_ascii=True, indent=2).encode()
    temporary = path.with_suffix('.pending')
    temporary.write_bytes(raw)
    temporary.replace(path)


def fixture(service, owner, directory, label):
    task_id, revision_id, run_id = (str(uuid4()) for _ in range(3))
    now = datetime.now(timezone.utc)
    revision = RequirementRevision(
        revision_id=revision_id, task_id=task_id, revision_number=1, parent_revision_id=None,
        status='confirmed', requirements=RequirementJSON(
            project_topic='fastapi', project_description='Explicit integration test fixture, not an LLM output',
            project_type=None, domain=None, required_features=None, preferred_features=None,
            programming_languages=None, required_technologies=None, preferred_technologies=None,
            missing_information=[], uncertain_information=None, source_evidence=[]),
        requirement_items=[], change_log=[], created_at=now, confirmed_at=now,
    )
    query = dict(query_id=str(uuid4()), query_text='fastapi', query_type='primary', language_filter=None,
                 qualifiers={}, query_hash=query_hash('fastapi', qualifiers={}),
                 source_requirement_pointers=['/project_topic'], created_at=now)
    groups = [dict(group_id=str(uuid4()), group_name=name, group_purpose='Integration test fixture',
                   group_status='ready' if name == 'A' else 'skipped',
                   source_requirement_pointers=['/project_topic'] if name == 'A' else [],
                   skip_reason=None if name == 'A' else 'Not needed for the bounded integration test',
                   created_at=now, queries=[query] if name == 'A' else []) for name in 'ABC']
    plan = SearchPlanDocument(plan_id=str(uuid4()), run_id=run_id, confirmed_revision_id=revision_id,
                              planner_version='integration-fixture-v1', model_version='no-model-called',
                              prompt_version='manual-test-fixture', created_at=now, groups=groups)
    save(directory / f'{label}-revision.json', revision.model_dump(mode='json'))
    service.create_run(owner, task_id, revision, run_id)
    service.publish_plan(owner, task_id, revision, plan)
    return revision, plan


def run(args):
    if not args.allow_isolated_mysql or not re.fullmatch(r'[a-z0-9_]{1,32}', args.case):
        raise ValueError('Explicit isolated MySQL opt-in and safe case name required')
    database = 'projectpilot_verify_' + args.case
    settings = SearchDatabaseSettings()
    if settings.mysql_host not in ('localhost', '127.0.0.1', '::1') or database == settings.mysql_database:
        raise ValueError('Validation requires a distinct local database')
    directory = PROJECT_ROOT / 'data' / 'search-validation' / args.case
    control_path = directory / 'control.json'
    if args.phase == 'prepare':
        if directory.exists():
            raise ValueError('Case already exists; refusing to overwrite')
        directory.mkdir(parents=True)
        control = {'database': database, 'owner': hashlib.sha256(args.case.encode()).hexdigest(),
                   'created_at': datetime.now(timezone.utc).isoformat(), 'runs': {}, 'results': {}}
        save(control_path, control)
        admin = create_search_engine(settings)
        try:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
        finally:
            admin.dispose()
    else:
        control = json.loads(control_path.read_text())
        if control['database'] != database:
            raise ValueError('Isolated database identity differs')
    engine = create_search_engine(settings.model_copy(update={'mysql_database': database}))
    storage = LocalStorage(directory / 'objects')
    repository = SearchRepository(engine)
    service = SearchPersistence(storage, repository)
    owner = control['owner']
    try:
        with engine.connect() as connection:
            if connection.scalar(text('SELECT DATABASE()')) != database:
                raise ValueError('Refusing an unexpected target database')
        if args.phase == 'prepare':
            SearchBase.metadata.create_all(engine)
            tables = inspect(engine).get_table_names()
            assert set(tables) == set(SearchBase.metadata.tables)
            control['results']['prepare'] = {'six_tables': tables, 'mysql_version': engine.dialect.server_version_info}
        elif args.phase == 'mysql':
            if 'mysql' in control['results']:
                raise ValueError('MySQL phase already completed')
            revision, plan = fixture(service, owner, directory, 'mysql')
            control['runs']['mysql'] = plan.run_id
            save(control_path, control)
            service.publish_plan(owner, str(revision.task_id), revision, plan)
            assert service.read_plan(owner, plan.run_id).plan_id == plan.plan_id
            service.manifests.start(owner, plan.run_id)
            now = datetime.now(timezone.utc)
            repo_id = 2**63 + 101
            patch = {'full_name': 'integration/synthetic', 'html_url': 'https://github.com/integration/synthetic',
                     'description': 'synthetic fixture, not a GitHub response', 'stars': 0}
            repository.update_repository(repo_id, patch, now)
            def concurrent_update(number):
                return repository.update_repository(repo_id, {'stars': number}, now + timedelta(seconds=number))
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(concurrent_update, [4, 1, 3, 2]))
            assert repository.get_repository(repo_id)['stars'] == 4
            assert not repository.update_repository(repo_id, {'stars': 1}, now + timedelta(seconds=1))
            assert not repository.update_repository(repo_id, {'stars': 4}, now + timedelta(seconds=4))
            repository.update_repository(repo_id, {'description': None}, now + timedelta(seconds=5))
            assert repository.get_repository(repo_id)['stars'] == 4
            assert repository.get_repository(repo_id)['description'] is None
            with Session(engine) as session:
                try:
                    row = session.get(SearchPlan, plan.plan_id)
                    row.plan_status = 'failed'
                    session.flush()
                    raise SimulatedCrash()
                except SimulatedCrash:
                    session.rollback()
            assert service.read_plan(owner, plan.run_id).plan_id == plan.plan_id
            raw = b'# Explicit synthetic MySQL integration fixture\n'
            blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
            crashing = SearchPersistence(storage, CrashAfterSnapshot(engine))
            try:
                crashing.collect_readme(owner, plan.run_id, repo_id, 'a' * 40, 'README.md', blob, raw)
                raise AssertionError('Expected the injected crash')
            except SimulatedCrash:
                pass
            op = next(iter(service.manifests.read(owner, plan.run_id)['readme_persistence_operations'].values()))
            assert op['operation_status'] == 'object_verified'
            with Session(engine) as session:
                assert session.scalar(select(func.count()).select_from(RepositorySnapshot)) == 1
            control['results']['mysql'] = {'plan_publish_idempotent': True, 'concurrent_metadata_watermark': True,
                                           'missing_vs_null': True, 'actual_rollback': True,
                                           'snapshot_sql_committed_before_crash': True,
                                           'recovery_pending_for_new_process': True}
        elif args.phase == 'recover':
            run_id = control['runs']['mysql']
            results = service.recover_readmes(owner, run_id)
            manifest = service.manifests.read(owner, run_id)
            op = next(iter(manifest['readme_persistence_operations'].values()))
            assert op['operation_status'] == 'completed'
            assert service.read_snapshot(owner, run_id, op['snapshot_id']) == b'# Explicit synthetic MySQL integration fixture\n'
            first = service.record_usage(owner, run_id, op['snapshot_id'], 'candidate_review')
            second = service.record_usage(owner, run_id, op['snapshot_id'], 'candidate_review')
            assert first['id'] == second['id']
            snapshot = repository.get_snapshot_by_id(op['snapshot_id'])
            def concurrent_snapshot(_):
                incoming = dict(snapshot, snapshot_id=str(uuid4()))
                return repository.ensure_snapshot(incoming)['snapshot_id']
            with ThreadPoolExecutor(max_workers=4) as pool:
                assert set(pool.map(concurrent_snapshot, range(4))) == {op['snapshot_id']}
            with Session(engine) as session:
                assert session.scalar(select(func.count()).select_from(RepositorySnapshot)) == 1
                assert session.scalar(select(func.count()).select_from(SearchSnapshot)) == 1
            service.manifests.seal_dispatch(owner, run_id)
            service.manifests.finish(owner, run_id)
            control['results']['recover'] = {'fresh_process_no_http': True, 'raw_bytes_verified': True,
                                             'concurrent_snapshot_reuse': True, 'usage_idempotent': True,
                                             'recovered_operations': len(results)}
        elif args.phase == 'github':
            if not args.allow_github or 'github_started' in control:
                raise ValueError('Explicit GitHub opt-in required; this phase cannot be replayed')
            if 'recover' not in control['results']:
                raise ValueError('Complete real MySQL recovery checks before HTTP')
            revision, plan = fixture(service, owner, directory, 'github')
            control['runs']['github'] = plan.run_id
            control['github_started'] = datetime.now(timezone.utc).isoformat()
            control['github_wire_requests'] = 0
            save(control_path, control)
            class BoundedTransport(httpx.AsyncBaseTransport):
                def __init__(self):
                    self.delegate = httpx.AsyncHTTPTransport(retries=0)
                async def handle_async_request(self, request):
                    if control['github_wire_requests'] >= 5:
                        raise ValueError('Five-request wire budget exhausted')
                    control['github_wire_requests'] += 1
                    save(control_path, control)
                    return await self.delegate.handle_async_request(request)
                async def aclose(self):
                    await self.delegate.aclose()
            budget = SearchExecutionSettings(_env_file=None, github_search_per_page=2,
                                             github_search_max_pages=1, github_search_max_candidates=2,
                                             github_readme_max_candidates=2, github_search_max_requests=5,
                                             github_search_max_attempts=1, github_search_max_wait_seconds=0,
                                             github_search_deadline_seconds=90)
            refs = [(owner, run_id) for run_id in control['runs'].values()]
            clock = service.initialize_request_clock(refs)
            with GitHubService(GitHubSettings(), transport=BoundedTransport()) as github:
                manifest = SearchExecutor(service, github, clock, budget).execute(owner, plan.run_id)
            statuses = {key: candidate['collection_status'] for key, candidate in manifest['candidates'].items()}
            control['results']['github'] = {'wire_requests': control['github_wire_requests'],
                                            'execution_status': manifest['search_execution']['status'],
                                            'stop_reasons': manifest['search_execution']['stop_reasons'],
                                            'candidate_statuses': statuses,
                                            'attempt_errors': [a.get('error_code') for a in manifest['search_execution']['attempts'].values()]}
        elif args.phase == 'verify':
            run_id = control['runs']['github']
            manifest = service.manifests.read(owner, run_id)
            if manifest['search_execution']['status'] == 'dispatching':
                clock = service.initialize_request_clock([(owner, run_id)])
                manifest = SearchExecutor(service, None, clock, SearchExecutionSettings(_env_file=None)).recover(owner, run_id)
            verified = []
            for repo_id, candidate in manifest['candidates'].items():
                if candidate['collection_status'] != 'completed':
                    continue
                snapshot_id = candidate['snapshot_id']
                snapshot = repository.get_snapshot_by_id(snapshot_id)
                raw = service.read_snapshot(owner, run_id, snapshot_id)
                assert hashlib.sha256(raw).hexdigest() == snapshot['content_hash']
                assert hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == snapshot['blob_sha']
                assert snapshot['repository_id'] == int(repo_id)
                assert candidate['commit_sha'] == snapshot['commit_sha']
                assert repository.get_repository(int(repo_id)) is not None
                first = service.record_usage(owner, run_id, snapshot_id, 'candidate_review')
                second = service.record_usage(owner, run_id, snapshot_id, 'candidate_review')
                assert first['id'] == second['id']
                verified.append({'repository_id': int(repo_id), 'full_name': candidate['metadata']['full_name'],
                                 'snapshot_id': snapshot_id, 'commit_sha': snapshot['commit_sha'],
                                 'blob_sha': snapshot['blob_sha'], 'readme_path': snapshot['readme_path'],
                                 'content_hash': snapshot['content_hash'], 'byte_length': len(raw)})
            control['results']['verify'] = {'fresh_process_no_http': True, 'verified_snapshots': verified,
                                            'complete_real_chain_passed': len(verified) > 0}
        save(control_path, control)
        print(json.dumps({'phase': args.phase, 'database': database, 'report': str(control_path),
                          'result': control['results'][args.phase]}, ensure_ascii=True))
    finally:
        engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', required=True)
    parser.add_argument('--phase', choices=['prepare', 'mysql', 'recover', 'github', 'verify'], required=True)
    parser.add_argument('--allow-isolated-mysql', action='store_true')
    parser.add_argument('--allow-github', action='store_true')
    args = parser.parse_args()
    try:
        run(args)
    except Exception as error:
        code = None
        if isinstance(error, SQLAlchemyError):
            original = getattr(error, 'orig', None)
            arguments = getattr(original, 'args', ())
            if arguments and type(arguments[0]) is int:
                code = arguments[0]
        print(json.dumps({'phase': args.phase, 'failed': type(error).__name__, 'mysql_error_code': code}))
        sys.exit(1)
