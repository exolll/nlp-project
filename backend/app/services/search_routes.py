"""Explicit, one-shot HTTP authorization for paid calls; GET handlers never write or dispatch."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import anyio
from fastapi import BackgroundTasks, Request
from pydantic import Field, StrictBool, model_validator

from app.persistence.input_state import APIError, encode, fail, utcnow
from app.persistence.run_manifest import RunManifestStore
from app.schemas.documents import Contract
from app.schemas.requirements import RequirementRevision
from app.schemas.search import SearchStateError
from app.services.query_planner import PlannerError
from app.services.requirements import revision_task


class SearchBudget(Contract):
    model_calls: int = Field(ge=1, le=1, strict=True)
    github_requests: int = Field(ge=1, le=20, strict=True)
    search_requests: int = Field(default=5, ge=1, le=9, strict=True)
    readme_candidates: int = Field(ge=0, le=5, strict=True)
    per_page: int = Field(default=5, ge=1, le=5, strict=True)
    max_pages: int = Field(default=1, ge=1, le=1, strict=True)

    @model_validator(mode='after')
    def enough_requests(self):
        if self.github_requests < self.search_requests + 2 * self.readme_candidates:
            raise ValueError('Total budget must cover searches and commit/README pairs')
        return self


class SearchRequest(Contract):
    revision_id: UUID
    consent: StrictBool
    budget: SearchBudget


class DeepCheckRequest(Contract):
    """Independent consent: a search run never implies authorization for the deep check call."""
    revision_id: UUID
    consent: StrictBool


class BackendFactory:
    """Lazy imports/configuration. Preflight does not create clients or connect SQL."""
    def preflight(self, settings, *, external):
        if settings.environment != 'development':
            fail(503, 'SEARCH_DISABLED', '搜索运行尚未开放；测试必须注入离线服务。')
        from app.persistence.search_database import SearchDatabaseSettings
        database = SearchDatabaseSettings()
        provider = github = None
        if external:
            from app.core.settings import ExtractionSettings
            from app.services.github_client import GitHubSettings
            provider, github = ExtractionSettings(), GitHubSettings()
            if not provider.deepseek_api_key.get_secret_value().strip() or not github.github_token.get_secret_value().strip():
                raise ValueError('Missing credentials')
        return database, provider, github

    @contextmanager
    def open(self, store, config, budget=None, app_settings=None):
        from app.persistence.search_database import create_search_engine
        from app.persistence.search_repository import SearchRepository
        from app.services.candidate_review import EvidenceReview
        from app.services.deep_check import DeepCheck, DeepCheckSettings
        from app.services.search_persistence import SearchPersistence
        database, provider, github_config = config
        engine, github = create_search_engine(database), None
        try:
            persistence = SearchPersistence(store.storage, SearchRepository(engine))
            backend = SimpleNamespace(persistence=persistence, review=EvidenceReview(persistence),
                                      deep_check=DeepCheck(persistence, provider,
                                                           settings=DeepCheckSettings()))
            if budget is not None:
                from app.core.settings import SiliconFlowSettings, Settings
                from app.services.chunk_index import ChunkIndexer
                from app.services.chroma_store import ChromaStore
                from app.services.embedding_service import EmbeddingService
                from app.services.evidence_bundle import EvidenceBundleService
                from app.services.siliconflow_client import SiliconFlowClient
                embedding = SiliconFlowSettings()
                if embedding.embedding_enabled:
                    service = EmbeddingService(SiliconFlowClient(embedding), store.storage, embedding)
                    chroma = ChromaStore.persistent((app_settings or Settings()).chroma_path,
                                                    model=embedding.embedding_model,
                                                    dimension=embedding.embedding_dimension,
                                                    version=embedding.version())
                    backend.chunk_indexer = ChunkIndexer(persistence, service, embedding,
                                                         chroma_factory=lambda: chroma)
                    backend.evidence_bundle = EvidenceBundleService(persistence, backend.chunk_indexer,
                                                                    embedding)
                from app.services.github_client import GitHubService
                from app.services.query_planner import QueryPlanner
                from app.services.search_execution import SearchExecutionSettings, SearchExecutor
                github = GitHubService(github_config)
                backend.planner = QueryPlanner(persistence, provider)
                execution = SearchExecutionSettings(
                    _env_file=None, github_search_per_page=budget.per_page,
                    github_search_max_pages=1, github_search_max_attempts=1,
                    github_search_max_requests=budget.github_requests,
                    github_readme_max_candidates=budget.readme_candidates,
                    github_search_max_candidates=budget.search_requests * budget.per_page,
                )
                backend.executor = lambda identity, run_id: SearchExecutor(
                    persistence, github, persistence.initialize_request_clock([(identity, run_id)]), execution)
            yield backend
        finally:
            try:
                if github is not None:
                    github.close()
            finally:
                engine.dispose()


def stable(manifest):
    return (manifest['dispatch_closed'] and not manifest['integrity_blocked']
            and manifest['plan_publication_status'] == 'published'
            and all(op['operation_status'] in ('completed', 'interrupted')
                    for op in manifest['readme_persistence_operations'].values()))


_DEEP_STATUSES = frozenset({'not_started', 'dispatching', 'dispatched', 'recorded', 'completed',
                            'failed', 'interrupted'})
_DEEP_CODES = frozenset({'DEEP_CHECK_PROVIDER_FAILED', 'DEEP_CHECK_INVALID_DRAFT', 'DEEP_CHECK_INPUT_LIMIT',
                         'DEEP_CHECK_CONFIG_CHANGED', 'DEEP_CHECK_RESPONSE_LOST', 'DEEP_CHECK_OUTCOME_UNKNOWN',
                         'DEEP_CHECK_UNAVAILABLE', 'DEEP_CHECK_INTERRUPTED', 'DEEP_CHECK_INTEGRITY'})
_DEEP_LABELS = {'verify_further': '值得进一步核验', 'partial_reference': '局部参考',
                'insufficient': '信息不足', 'not_recommended': '不建议'}
_DEEP_REASONS = {
    'hard_requirement_conflict': '已保存的 README 中出现与硬性需求直接冲突的证据。',
    'all_hard_requirements_supported': '每条硬性需求都找到了可复核的行号证据，仍需人工进一步核验。',
    'partial_hard_support': '只有部分硬性需求有证据支持。',
    'no_hard_requirement_evidence': '硬性需求在已保存的 README 中没有决定性证据。',
    'no_hard_requirement': '本版本没有可判定的硬性需求，无法据此推荐。',
    'snapshot_unavailable': '该候选没有可用且完整的 README 快照，未纳入核对。',
    'input_truncated': '输入被截断，无法证明需求已完整覆盖。',
}
_DEEP_VERDICTS = {'supports': '有证据支持', 'contradicts': '有证据冲突', 'insufficient': '信息不足'}
_DEEP_BASIS = {'model': '判定已通过行号复核', 'citation_rejected': '引用行号未通过复核，按信息不足处理',
               'no_response': '模型未给出该项判定', 'not_fed': '未提供给模型核对',
               'snapshot_unavailable': '没有可读的 README 快照'}
_DEEP_AVAILABILITY = {'available': '快照可读', 'non_utf8': '快照不是 UTF-8 文本', 'no_snapshot': '没有保存的快照'}
_DEEP_COVERAGE = {'full': '全文输入', 'truncated': '输入被截断', 'not_fed': '未输入'}
_DEEP_FINDINGS = {'contradicts': '证据与需求冲突', 'insufficient': '未见决定性证据',
                  'prohibited_violation': '出现禁止项证据', 'preferred_conflict': '与偏好项冲突'}
_DEEP_ANOMALIES = {'unknown_candidate': '模型返回了未知候选，已忽略',
                   'duplicate_candidate': '模型重复返回同一候选，已忽略后一次',
                   'unknown_item': '模型引用了未知需求项，已忽略',
                   'duplicate_item': '同一需求项出现两次判定，按信息不足处理'}
_DEEP_NOTICE = ('以下是模型对已保存 README 行号证据的分析，不代表需求已被满足。'
                '「信息不足」只说明没有找到决定性证据，不等于该需求失败。')


def _deep_code(code):
    if code is None:
        return None
    return code if code in _DEEP_CODES else 'DEEP_CHECK_FAILED'


def _deep_projection(record, manifest):
    """Closed-vocabulary status for polling: raw codes and provider text never reach the client."""
    operation = (manifest or {}).get('deep_check_operation')
    blocked = bool((manifest or {}).get('integrity_blocked'))
    if operation is None:
        binding = record.get('deep_check') or {}
        status = binding.get('pipeline_status', 'not_started')
        return {'status': status if status in _DEEP_STATUSES else 'interrupted',
                'published': False, 'integrity_blocked': blocked,
                'error_code': _deep_code(binding.get('pipeline_error_code'))}
    return {'status': operation['status'], 'published': bool(operation['published']),
            'integrity_blocked': blocked, 'error_code': _deep_code(operation['error_code'])}


def deep_check_view(report):
    # Fixed Chinese templates over deterministic verdicts: no model prose is ever relayed.
    candidates = []
    for row in report['candidates']:
        items = []
        for item in row['items']:
            items.append({key: deepcopy(item[key]) for key in
                          ('requirement_item_id', 'text', 'modality', 'operator', 'logic_group_id',
                           'verdict', 'basis', 'rejections')}
                         | {'verdict_label': _DEEP_VERDICTS[item['verdict']],
                            'basis_label': _DEEP_BASIS[item['basis']],
                            'citations': [{key: citation[key] for key in
                                           ('text', 'line_start', 'line_end', 'byte_start', 'byte_end')}
                                          for citation in item['citations']]})
        required = [item for item in row['items'] if item['modality'] == 'required']
        supported = sum(1 for item in required if item['verdict'] == 'supports')
        conflicted = sum(1 for item in required if item['verdict'] == 'contradicts')
        violations = sum(1 for item in row['items']
                         if item['modality'] == 'prohibited' and item['verdict'] == 'contradicts')
        summary = ['硬性需求 %d/%d 项有证据支持' % (supported, len(required)),
                   '%d 项信息不足' % (len(required) - supported - conflicted)]
        if conflicted:
            summary.append('%d 项存在冲突证据' % conflicted)
        if violations:
            summary.append('%d 项禁止项出现违规证据' % violations)
        recommendation = row['recommendation']
        candidates.append({'repository_id': row['repository_id'], 'rank': row['rank'],
                           'availability': row['availability'],
                           'availability_label': _DEEP_AVAILABILITY[row['availability']],
                           'input_coverage': row['input_coverage'],
                           'input_coverage_label': _DEEP_COVERAGE[row['input_coverage']],
                           'input_bytes': row['input_bytes'],
                           'source': deepcopy(row['source']),
                           'recommendation': {'code': recommendation['label'],
                                              'label': _DEEP_LABELS[recommendation['label']],
                                              'reason_code': recommendation['reason'],
                                              'reason': _DEEP_REASONS[recommendation['reason']]},
                           'summary': '，'.join(summary) + '。',
                           'missing_features': [{key: finding[key] for key in ('requirement_item_id', 'text', 'modality')}
                                                | {'reason': _DEEP_FINDINGS[finding['reason']]}
                                                for finding in row['missing_features']],
                           'risk_notes': [{key: finding[key] for key in ('requirement_item_id', 'text', 'modality')}
                                          | {'reason': _DEEP_FINDINGS[finding['reason']]}
                                          for finding in row['risk_notes']],
                           'items': items})
    return {key: deepcopy(report[key]) for key in ('version', 'task_id', 'run_id', 'confirmed_revision_id')} | {
        'notice': _DEEP_NOTICE,
        'requirement_items': deepcopy(report['requirement_items']),
        'candidates': candidates,
        'anomalies': [{'repository_id': row['repository_id'], 'code': row['code'],
                       'message': _DEEP_ANOMALIES[row['code']]} for row in report['anomalies']]}


def evidence_view(report):
    # Explicit projections: never expose owner, storage paths or provider payloads.
    return {key: deepcopy(report[key]) for key in (
        'version', 'task_id', 'run_id', 'confirmed_revision_id', 'warning', 'requirement_items', 'candidates')}


def retrieval_view(bundle, repository_id=None, requirement_item_id=None):
    data = {key: deepcopy(bundle[key]) for key in (
        'version', 'task_id', 'run_id', 'confirmed_revision_id', 'status', 'warning',
        'created_at', 'model', 'dimension', 'config', 'empty_reason')}
    data['schema_version'] = bundle.get('schema_version', 1)
    data['query_usage'] = deepcopy(bundle.get('query_usage'))
    requirements = deepcopy(bundle['requirements'])
    candidates = deepcopy(bundle['candidates'])
    if requirement_item_id is not None:
        wanted = str(requirement_item_id)
        if not any(row['requirement_item_id'] == wanted for row in requirements):
            fail(404, 'RETRIEVAL_FILTER_NOT_FOUND', '该需求条目不在本次运行中。')
        requirements = [row for row in requirements if row['requirement_item_id'] == wanted]
        candidates = [{**row, 'items': [item for item in row['items']
                                        if item['requirement_item_id'] == wanted]}
                      for row in candidates]
    if repository_id is not None:
        if not any(row['repository_id'] == str(repository_id) for row in candidates):
            fail(404, 'RETRIEVAL_FILTER_NOT_FOUND', '该仓库不在本次运行中。')
        candidates = [row for row in candidates if row['repository_id'] == str(repository_id)]
    data['requirements'] = requirements
    data['candidates'] = candidates
    data['filters'] = {'repository_id': None if repository_id is None else str(repository_id),
                       'requirement_item_id': None if requirement_item_id is None else str(requirement_item_id)}
    data['notice'] = ('以下为已保存 README 中召回的候选片段；相关性尚未核查，不代表功能已满足；'
                      '相似度距离与 RRF 排名不构成满足百分比。')
    return data


def run_view(record, manifest=None):
    result = {key: record[key] for key in ('run_id', 'task_id', 'confirmed_revision_id', 'status')}
    result['retryable'] = False
    result['pipeline_status'] = record.get('pipeline_status', record['status'])
    result['pipeline_error_code'] = record.get('pipeline_error_code')
    if manifest is not None:
        result['search_status'] = manifest.get('search_execution', {}).get('status', 'not_started')
        allowed_reasons = {'PAGE_BUDGET', 'REQUEST_BUDGET', 'DEADLINE', 'RATE_LIMIT', 'AUTHENTICATION'}
        result['search_stop_reasons'] = [code if code in allowed_reasons else 'SEARCH_STOPPED'
                                       for code in manifest.get('search_execution', {}).get('stop_reasons', [])]
        code = manifest.get('error_code')
        result['error_code'] = (code if code in {'SEARCH_BUDGET', 'SEARCH_PARTIAL', 'SEARCH_INTEGRITY'}
                                else 'SEARCH_FAILED' if code else None)
        result['operation_failures'] = [
            dict(operation='readme_persistence', status=op['operation_status'], error_code='PERSISTENCE_INTERRUPTED')
            for op in manifest['readme_persistence_operations'].values()
            if op['operation_status'] in ('interrupted', 'integrity_failed')]

        result.update(status=manifest['run_status'], plan_status=manifest['plan_publication_status'],
                      dispatch_closed=manifest['dispatch_closed'])
        result['evidence_status'] = 'published' if manifest.get('evidence_operation', {}).get('published') else 'not_ready'
        retrieval = manifest.get('evidence_bundle_operation')
        result['retrieval_status'] = retrieval['status'] if retrieval else 'not_started'
        result['retrieval_error_code'] = (retrieval.get('error_code')
                                          if retrieval and retrieval['status'] == 'failed' else None)
        screening = manifest.get('candidate_screening')
        result['screening'] = ({key: deepcopy(screening[key]) for key in ('version', 'warning', 'candidates')}
                               if screening else None)
        result['candidates'] = [dict(repository_id=repo_id, collection_status=row.get('collection_status'))
                                for repo_id, row in manifest['candidates'].items()]
        # Planner provenance is derived from the executed queries; surface the normalisation
        # instead of leaving the warning stranded in the manifest.
        operation = manifest.get('planner_operation') or {}
        result['plan_warnings'] = [
            {'group': str(row.get('group')), 'declared': list(row.get('declared') or []),
             'used': list(row.get('used') or [])}
            for row in (operation.get('source_normalization') or []) if isinstance(row, dict)]
    result['deep_check'] = _deep_projection(record, manifest)
    return result


def install_routes(app, state, owner, settings, factory=None):
    factory = factory or BackendFactory()

    def config(external):
        try:
            return factory.preflight(settings, external=external)
        except APIError:
            raise
        except Exception:
            fail(503, 'SEARCH_UNAVAILABLE', '搜索服务配置不完整或暂时不可用。')

    def revision(session, store, task_id, revision_id):
        task = store.task(session, task_id)
        bound_task, value = revision_task(session, revision_id)
        if bound_task['id'] != task['id'] or value['task_id'] != str(task_id):
            fail(404, 'NOT_FOUND', '需求版本不存在。')
        if value['status'] != 'confirmed' or not value.get('confirmed_at'):
            fail(409, 'REVISION_UNCONFIRMED', '必须显式选择已确认的需求版本。')
        return RequirementRevision.model_validate(deepcopy(value))

    def authorized(store, identity, task_id, run_id):
        def read(session):
            task = store.task(session, task_id)
            record = next((r for r in task.get('search_runs', {}).values() if r['run_id'] == str(run_id)), None)
            if record is None:
                fail(404, 'NOT_FOUND', '搜索运行不存在。')
            return deepcopy(record), revision(session, store, task_id, record['confirmed_revision_id'])
        return store.session(identity, read)

    def manifest_for(store, identity, record):
        manifests = RunManifestStore(store.storage)
        # A durable reservation can outlive a worker that never created its manifest.
        raw, _ = store.storage.read_version(manifests._key(identity, record['run_id']))
        if raw is None:
            return None
        value = manifests.read(identity, record['run_id'])
        if value['task_id'] != record['task_id'] or value['confirmed_revision_id'] != record['confirmed_revision_id']:
            fail(409, 'SEARCH_STATE', '搜索运行状态不可用。')
        return value

    def pipeline(store, identity, record, confirmed, budget, configuration):
        # Only annotate the reservation; never overwrite an unknown manifest outcome.
        def outcome(status, code=None):
            def update(session):
                runs = store.task(session, record['task_id']).get('search_runs', {})
                for value in runs.values():
                    if value['run_id'] == record['run_id']:
                        value.update(status=status, pipeline_status=status, pipeline_error_code=code)
                        break
            store.session(identity, update)

        try:
            outcome('running')
            with factory.open(store, configuration, budget, settings) as backend:
                p = backend.persistence
                p.create_run(identity, record['task_id'], confirmed, record['run_id'])
                plan = anyio.run(backend.planner.plan_and_publish, identity, record['task_id'], confirmed, record['run_id'])
                if sum(len(g.queries) for g in plan.groups) > budget.search_requests:
                    p.manifests.fail(identity, record['run_id'], 'SEARCH_BUDGET', 'terminal')
                    outcome('failed', 'SEARCH_BUDGET')
                    return
                executor = backend.executor(identity, record['run_id'])
                executor.execute(identity, record['run_id'], confirmed_revision=confirmed)
                if stable(p.manifests.read(identity, record['run_id'])):
                    backend.review.review(identity, record['task_id'], record['run_id'], confirmed)
                    if getattr(backend, 'chunk_indexer', None) is not None:
                        # Retrieval is additive: failures are recorded, never roll back published evidence.
                        try:
                            backend.chunk_indexer.index(identity, record['task_id'], record['run_id'], confirmed)
                        except Exception:
                            pass
                        try:
                            backend.evidence_bundle.publish(identity, record['task_id'], record['run_id'], confirmed)
                        except Exception:
                            pass
            outcome('completed')
        except PlannerError as exc:
            try:
                outcome('interrupted', exc.code)
            except Exception:
                pass
        except Exception:
            # No replay, even if this separate annotation has an unknown write outcome.
            try:
                outcome('interrupted', 'PIPELINE_INTERRUPTED')
            except Exception:
                pass

    def deep_pipeline(store, identity, record, confirmed, configuration):
        from app.schemas.search import SearchIntegrityError
        from app.services.deep_check import DeepCheckError

        def outcome(status, code=None):
            try:
                def update(session):
                    for value in store.task(session, record['task_id']).get('search_runs', {}).values():
                        if value['run_id'] == record['run_id']:
                            value['deep_check'] = {**(value.get('deep_check') or {}),
                                                   'pipeline_status': status, 'pipeline_error_code': code}
                            break
                store.session(identity, update)
            except Exception:
                pass

        try:
            with factory.open(store, configuration) as backend:
                anyio.run(backend.deep_check.check, identity, record['task_id'], record['run_id'], confirmed)
            outcome('completed')
        except DeepCheckError as exc:
            # Recorded failures are terminal: this path never replays a possibly-charged call.
            outcome('failed', _deep_code(exc.code))
        except SearchIntegrityError:
            outcome('failed', 'DEEP_CHECK_INTEGRITY')
        except Exception:
            outcome('interrupted', 'DEEP_CHECK_INTERRUPTED')

    @app.post('/api/v1/tasks/{task_id}/search-runs', status_code=202)
    def create(task_id: UUID, body: SearchRequest, request: Request, background: BackgroundTasks):
        if not body.consent:
            fail(422, 'CONSENT_REQUIRED', '必须明确同意本次模型与 GitHub 外部调用及预算。')
        try:
            key = str(UUID(request.headers.get('idempotency-key', '')))
        except ValueError:
            fail(422, 'INVALID_IDEMPOTENCY_KEY', '必须提供 UUID 格式的 Idempotency-Key。')
        store, identity = state(), owner(request)
        digest = hashlib.sha256(encode(body.model_dump(mode='json'))).hexdigest()
        def check(session):
            confirmed = revision(session, store, task_id, body.revision_id)
            existing = store.task(session, task_id).get('search_runs', {}).get(key)
            if existing and existing['request_hash'] != digest:
                fail(409, 'IDEMPOTENCY_CONFLICT', '同一幂等键不能用于不同请求。')
            return deepcopy(existing), confirmed
        existing, confirmed = store.session(identity, check)
        if existing:
            return run_view(existing, manifest_for(store, identity, existing))
        configuration = config(True)
        record = dict(run_id=str(uuid4()), task_id=str(task_id), confirmed_revision_id=str(body.revision_id),
                      request_hash=digest, status='reserved')
        def claim(session):
            existing, _ = check(session)
            if existing:
                return existing, False
            runs = store.task(session, task_id).setdefault('search_runs', {})
            if len(runs) >= 10:
                fail(429, 'SEARCH_RUN_LIMIT', '每个任务最多允许十次搜索运行。')
            store.write_rate(session, utcnow())
            runs[key] = deepcopy(record)
            return record, True
        result, claimed = store.session(identity, claim)
        if claimed:
            background.add_task(pipeline, store, identity, record, confirmed, body.budget, configuration)
        return run_view(result)

    @app.get('/api/v1/tasks/{task_id}/search-runs/{run_id}')
    def get(task_id: UUID, run_id: UUID, request: Request):
        store, identity = state(), owner(request)
        record, _ = authorized(store, identity, task_id, run_id)
        return run_view(record, manifest_for(store, identity, record))

    @app.get('/api/v1/tasks/{task_id}/search-runs/{run_id}/evidence')
    def get_evidence(task_id: UUID, run_id: UUID, request: Request):
        store, identity = state(), owner(request)
        record, confirmed = authorized(store, identity, task_id, run_id)
        try:
            manifest = manifest_for(store, identity, record)
            operation = (manifest or {}).get('evidence_operation', {})
            if not operation.get('published') or not stable(manifest):
                fail(409, 'EVIDENCE_NOT_READY', '证据报告尚未就绪。')
            with factory.open(store, config(False)) as backend:
                return evidence_view(backend.review.read(identity, str(task_id), str(run_id), confirmed))
        except APIError:
            raise
        except SearchStateError:
            fail(409, 'EVIDENCE_NOT_READY', '证据状态尚未就绪。')
        except Exception:
            fail(503, 'EVIDENCE_UNAVAILABLE', '证据读取结果未确认；不会发起外部请求。')

    @app.get('/api/v1/tasks/{task_id}/search-runs/{run_id}/retrieval-evidence')
    def get_retrieval_evidence(task_id: UUID, run_id: UUID, request: Request,
                               repository_id: str | None = None,
                               requirement_item_id: UUID | None = None):
        from app.schemas.search import SearchIntegrityError
        from app.services.evidence_bundle import EvidenceBundleError, EvidenceBundleStore

        store, identity = state(), owner(request)
        record, confirmed = authorized(store, identity, task_id, run_id)
        manifest = manifest_for(store, identity, record)
        operation = (manifest or {}).get('evidence_bundle_operation')
        if operation is None:
            fail(409, 'RETRIEVAL_NOT_READY', '检索证据尚未生成，请稍后查询状态。')
        if operation['status'] == 'failed':
            fail(409, 'RETRIEVAL_FAILED', '检索证据生成失败；已保存的搜索与深度核对结果仍可查看。')
        try:
            bundle, _ = EvidenceBundleStore(store.storage).read(identity, str(run_id), confirmed)
        except EvidenceBundleError:
            fail(409, 'RETRIEVAL_NOT_READY', '检索证据尚未就绪。')
        except SearchIntegrityError:
            fail(409, 'RETRIEVAL_INTEGRITY', '检索证据完整性校验失败。')
        except SearchStateError:
            fail(409, 'RETRIEVAL_NOT_READY', '检索证据尚未就绪。')
        return retrieval_view(bundle, repository_id, requirement_item_id)

    @app.post('/api/v1/tasks/{task_id}/search-runs/{run_id}/evidence')
    def review(task_id: UUID, run_id: UUID, body: Contract, request: Request):
        store, identity = state(), owner(request)
        record, confirmed = authorized(store, identity, task_id, run_id)
        manifest = manifest_for(store, identity, record)
        if manifest is None or not stable(manifest):
            fail(409, 'EVIDENCE_NOT_READY', '正式快照尚未稳定，不会续跑外部调用。')
        configuration = config(False)
        try:
            with factory.open(store, configuration) as backend:
                return evidence_view(backend.review.review(identity, str(task_id), str(run_id), confirmed))
        except SearchStateError:
            fail(409, 'EVIDENCE_NOT_READY', '证据状态尚未就绪。')
        except Exception:
            fail(503, 'EVIDENCE_UNAVAILABLE', '证据读取结果未确认；不会发起外部请求。')

    @app.get('/api/v1/tasks/{task_id}/search-runs/{run_id}/deep-check')
    def get_deep_check(task_id: UUID, run_id: UUID, request: Request):
        store, identity = state(), owner(request)
        record, confirmed = authorized(store, identity, task_id, run_id)
        try:
            manifest = manifest_for(store, identity, record)
            operation = (manifest or {}).get('deep_check_operation')
            if not operation or not operation['published'] or not stable(manifest):
                fail(409, 'DEEP_CHECK_NOT_READY', '深度核对结果尚未就绪。')
            with factory.open(store, config(False)) as backend:
                return deep_check_view(backend.deep_check.read(identity, str(task_id), str(run_id), confirmed))
        except APIError:
            raise
        except SearchStateError:
            fail(409, 'DEEP_CHECK_NOT_READY', '深度核对状态尚未就绪。')
        except Exception:
            fail(503, 'DEEP_CHECK_UNAVAILABLE', '深度核对结果未确认；不会发起模型调用。')

    @app.post('/api/v1/tasks/{task_id}/search-runs/{run_id}/deep-check', status_code=202)
    def deep_check(task_id: UUID, run_id: UUID, body: DeepCheckRequest, request: Request, background: BackgroundTasks):
        if not body.consent:
            fail(422, 'CONSENT_REQUIRED', '必须单独明确同意本次深度核对的模型调用。')
        try:
            key = str(UUID(request.headers.get('idempotency-key', '')))
        except ValueError:
            fail(422, 'INVALID_IDEMPOTENCY_KEY', '必须提供 UUID 格式的 Idempotency-Key。')
        store, identity = state(), owner(request)
        digest = hashlib.sha256(encode(body.model_dump(mode='json'))).hexdigest()

        def check(session):
            record = next((r for r in store.task(session, task_id).get('search_runs', {}).values()
                           if r['run_id'] == str(run_id)), None)
            if record is None:
                fail(404, 'NOT_FOUND', '搜索运行不存在。')
            confirmed = revision(session, store, task_id, body.revision_id)
            binding = record.get('deep_check')
            if binding and (binding.get('idempotency_key') != key or binding.get('request_hash') != digest):
                fail(409, 'IDEMPOTENCY_CONFLICT', '一个搜索运行只允许一次深度核对授权。')
            return deepcopy(record), confirmed

        record, confirmed = store.session(identity, check)
        manifest = manifest_for(store, identity, record)
        if manifest is None or not stable(manifest):
            fail(409, 'DEEP_CHECK_NOT_READY', '正式快照尚未稳定，不会发起模型调用。')
        configuration = config(True)

        def claim(session):
            check(session)
            target = next(r for r in store.task(session, task_id).get('search_runs', {}).values()
                          if r['run_id'] == str(run_id))
            if target.get('deep_check') is None:
                store.write_rate(session, utcnow())
                target['deep_check'] = {'idempotency_key': key, 'request_hash': digest,
                                        'pipeline_status': 'dispatching', 'pipeline_error_code': None}
            return deepcopy(target)

        # The same key may be re-submitted after a crash: only recorded bytes can be reused.
        record = store.session(identity, claim)
        background.add_task(deep_pipeline, store, identity, record, confirmed, configuration)
        return run_view(record, manifest)
