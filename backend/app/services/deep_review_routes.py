"""One authorized deep-review operation per run; GET only reads durable facts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

import anyio
import httpx
from fastapi import BackgroundTasks, Request
from pydantic import StrictBool, ValidationError

from app.core.settings import ExtractionSettings
from app.persistence.input_state import APIError, fail, utcnow
from app.persistence.run_manifest import RunManifestStore
from app.schemas.documents import Contract
from app.schemas.requirements import RequirementRevision
from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.deep_review_logic import DeepReviewInputError, operation_repository_budget
from app.services.deep_review_models import DeepReviewReport
from app.services.deep_review_pipeline import DeepReviewPipeline, PROMPT_VERSION, extract_candidate_chunks
from app.services.extraction_provider import strict_json
from app.services.requirement_quality import validate_quality
from app.services.requirements import revision_task
from app.services.search_routes import BackendFactory


class DeepReviewRequest(Contract):
    revision_id: UUID
    consent: StrictBool


def stable(manifest):
    return (manifest['dispatch_closed'] and not manifest['integrity_blocked']
            and manifest['plan_publication_status'] == 'published'
            and all(op['operation_status'] in ('completed', 'interrupted')
                    for op in manifest['readme_persistence_operations'].values()))


def confirmed_revision(session, store, task_id, revision_id):
    task = store.task(session, task_id)
    bound_task, value = revision_task(session, revision_id)
    if bound_task['id'] != task['id'] or value['task_id'] != str(task_id):
        fail(404, 'NOT_FOUND', '需求版本不存在。')
    if value['status'] != 'confirmed' or not value.get('confirmed_at'):
        fail(409, 'REVISION_UNCONFIRMED', '必须显式选择已确认的需求版本。')
    return RequirementRevision.model_validate(deepcopy(value))


def report_view(raw, operation, manifest, identity, task_id, run_id):
    from app.services.search_persistence import _digest
    if _digest(raw) != operation['report_hash']:
        fail(409, 'DEEP_REVIEW_INTEGRITY', '深度核查报告完整性校验失败。')
    try:
        value = strict_json(raw)
        report = DeepReviewReport.model_validate(value)
    except (ValueError, TypeError, ValidationError, RecursionError):
        fail(409, 'DEEP_REVIEW_INTEGRITY', '深度核查报告格式或完整性校验失败。')
    expected = {
        'owner': identity, 'task_id': str(task_id), 'run_id': str(run_id),
        'confirmed_revision_id': operation['revision_id'],
        'operation_id': operation['operation_id'], 'revision_hash': operation['revision_hash'],
        'bundle_hash': operation['bundle_hash'], 'model': operation['model'],
        'prompt_version': operation['prompt_version'], 'status': operation['status'],
        'plan_hash': manifest['plan']['plan_hash'],
    }
    if any(getattr(report, key) != value for key, value in expected.items()):
        fail(409, 'DEEP_REVIEW_INTEGRITY', '深度核查报告与已授权运行不一致。')
    data = report.model_dump(mode='json')
    data.pop('owner')
    return data


def operation_view(store, identity, task_id, run_id, manifest, now=None):
    operation = manifest['deep_review_operation']
    status = operation['status']
    if status == 'running':
        deadline = datetime.fromisoformat(operation['deadline'])
        if (now or utcnow()) >= deadline:
            status = 'unknown'
    report = None
    if operation['report_key'] is not None:
        raw, _ = store.storage.read_version(operation['report_key'])
        if raw is None:
            fail(409, 'DEEP_REVIEW_INTEGRITY', '深度核查报告不存在或尚未确认写入。')
        report = report_view(raw, operation, manifest, identity, task_id, run_id)
    return {
        'operation_id': operation['operation_id'], 'status': status,
        'persisted_status': operation['status'], 'revision_id': operation['revision_id'],
        'revision_hash': operation['revision_hash'], 'bundle_hash': operation['bundle_hash'],
        'model': operation['model'], 'prompt_version': operation['prompt_version'],
        'consent': operation['consent'], 'authorized_at': operation['authorized_at'],
        'deadline': operation['deadline'], 'report_key': operation['report_key'],
        'report_hash': operation['report_hash'], 'error_code': operation['error_code'],
        'report': report,
    }


def install_routes(app, state, owner, settings, provider_settings=None, transport=None,
                   search_backend_factory=None):
    if transport is not None and (settings.environment != 'test'
                                  or not isinstance(transport, httpx.MockTransport)):
        raise ValueError('Injected model transport is permitted only for tests with MockTransport.')
    factory = search_backend_factory or BackendFactory()

    def provider_config():
        if settings.environment == 'production':
            fail(503, 'DEEP_REVIEW_NOT_PUBLIC', '付费深度核查尚未开放生产环境闸门。')
        if settings.environment == 'test' and (transport is None or provider_settings is None):
            fail(503, 'DEEP_REVIEW_DISABLED', '测试环境仅允许显式 MockTransport，不进行真实调用。')
        try:
            provider = provider_settings if provider_settings is not None else ExtractionSettings()
            provider = ExtractionSettings.model_validate(provider.model_dump())
            if not provider.deepseek_api_key.get_secret_value().strip():
                fail(503, 'DEEP_REVIEW_DISABLED', '深度核查尚未配置模型凭据。')
            return provider
        except ValidationError:
            fail(503, 'DEEP_REVIEW_MISCONFIGURED', '深度核查模型配置无效。')

    def configuration():
        try:
            return factory.preflight(settings, external=False)
        except APIError:
            raise
        except Exception:
            fail(503, 'DEEP_REVIEW_UNAVAILABLE', '深度核查服务配置不完整或暂时不可用。')

    def load_run(store, identity, task_id, run_id, revision_id):
        def read(session):
            record = next((row for row in store.task(session, task_id).get('search_runs', {}).values()
                           if row['run_id'] == str(run_id)), None)
            if record is None:
                fail(404, 'NOT_FOUND', '搜索运行不存在。')
            if record['confirmed_revision_id'] != str(revision_id):
                fail(409, 'DEEP_REVIEW_REVISION_CONFLICT', '深度核查必须绑定搜索运行已确认的需求版本。')
            confirmed = confirmed_revision(session, store, task_id, revision_id)
            return deepcopy(record), confirmed
        return store.session(identity, read)

    def manifest_for(store, identity, record):
        manifests = RunManifestStore(store.storage)
        raw, _ = store.storage.read_version(manifests._key(identity, record['run_id']))
        if raw is None:
            return None
        try:
            manifest = manifests.read(identity, record['run_id'])
        except SearchStateError:
            fail(409, 'DEEP_REVIEW_NOT_READY', '搜索运行尚未就绪。')
        if (manifest['task_id'] != record['task_id']
                or manifest['confirmed_revision_id'] != record['confirmed_revision_id']):
            fail(409, 'DEEP_REVIEW_NOT_READY', '搜索运行与需求版本绑定不一致。')
        return manifest

    def execute(store, identity, record, confirmed, provider, config):
        from app.services.evidence_bundle import EvidenceBundleStore
        from app.services.search_persistence import _digest, _encode
        manifests = RunManifestStore(store.storage)
        operation = manifests.read(identity, record['run_id'])['deep_review_operation']
        try:
            with factory.open(store, config) as backend:
                persistence = backend.persistence
                bundle, retrieval = EvidenceBundleStore(store.storage).read(
                    identity, record['run_id'], confirmed)
                pipeline = DeepReviewPipeline(
                    settings, provider,
                    lambda owner_key, run_id, snapshot_id: persistence.read_snapshot(
                        owner_key, run_id, snapshot_id, read_only=True),
                    transport=transport)
                report = anyio.run(pipeline.generate, identity, record['task_id'], record['run_id'],
                                   confirmed, bundle, retrieval['bundle_hash'],
                                   operation['operation_id'])
                data = _encode(report.model_dump(mode='json'))
                key = f"deep-reviews/{operation['operation_id']}/report.json"
                persistence._put_immutable(key, data, 'application/json')
                manifests.complete_deep_review(identity, record['run_id'], operation['operation_id'],
                                               report.status, report_key=key, report_hash=_digest(data))
        except DeepReviewInputError as exc:
            # Preflight failures happen before any provider call and are safe to terminate.
            try:
                manifests.complete_deep_review(identity, record['run_id'], operation['operation_id'],
                                               'failed', error_code=exc.code)
            except Exception:
                pass
        except Exception:
            # An uncertain write or interrupted worker must never authorize a replay.
            return

    @app.post('/api/v1/tasks/{task_id}/search-runs/{run_id}/deep-review', status_code=202)
    def create(task_id: UUID, run_id: UUID, body: DeepReviewRequest, request: Request,
               background: BackgroundTasks):
        if not body.consent:
            fail(422, 'CONSENT_REQUIRED', '必须单独明确同意本次深度核查的模型调用与费用。')
        store, identity = state(), owner(request)
        record, confirmed = load_run(store, identity, task_id, run_id, body.revision_id)
        manifest = manifest_for(store, identity, record)
        operation = (manifest or {}).get('deep_review_operation')
        if operation is not None:
            if operation['revision_id'] != str(body.revision_id):
                fail(409, 'DEEP_REVIEW_REVISION_CONFLICT', '该搜索运行已授权其他需求版本。')
            return operation_view(store, identity, task_id, run_id, manifest)
        if manifest is None or not stable(manifest):
            fail(409, 'DEEP_REVIEW_NOT_READY', '检索 Bundle 尚未稳定，不能授权深度核查。')
        retrieval = manifest.get('evidence_bundle_operation')
        if retrieval is None or retrieval['status'] not in ('ready', 'partial'):
            fail(409, 'DEEP_REVIEW_NOT_READY', '检索 Bundle 尚未就绪。')
        from app.services.evidence_bundle import EvidenceBundleError, EvidenceBundleStore
        try:
            bundle, _ = EvidenceBundleStore(store.storage).read(identity, str(run_id), confirmed)
        except EvidenceBundleError:
            fail(409, 'DEEP_REVIEW_NOT_READY', '检索 Bundle 尚未就绪。')
        except SearchIntegrityError:
            fail(409, 'DEEP_REVIEW_INTEGRITY', '检索 Bundle 完整性校验失败。')
        except SearchStateError:
            fail(409, 'DEEP_REVIEW_NOT_READY', '搜索运行尚未就绪。')
        quality = validate_quality(confirmed)
        if not quality['ok']:
            fail(422, 'DEEP_REVIEW_REQUIREMENT_QUALITY', '已确认需求的证据校验未通过。')
        binding = {'version': 'evidence-bundle-v2', 'schema_version': 2, 'owner': identity,
                   'task_id': str(task_id), 'run_id': str(run_id),
                   'confirmed_revision_id': str(confirmed.revision_id),
                   'revision_hash': quality['revision_hash'],
                   'plan_hash': manifest['plan']['plan_hash']}
        if (any(bundle.get(key) != value for key, value in binding.items())
                or retrieval['revision_hash'] != quality['revision_hash']
                or retrieval['plan_hash'] != manifest['plan']['plan_hash']):
            fail(409, 'DEEP_REVIEW_INTEGRITY', '检索 Bundle 与已确认需求版本不一致。')
        candidates = bundle.get('candidates')
        if not isinstance(candidates, list):
            fail(422, 'DEEP_REVIEW_INVALID_BUNDLE', '检索 Bundle 候选格式无效。')
        budget = operation_repository_budget(len(candidates))
        if budget:
            fail(budget['http'], budget['error_code'], '候选仓库数量不满足深度核查预算。')
        try:
            requirement_ids = {str(item.requirement_item_id) for item in confirmed.requirement_items}
            usable = any(extract_candidate_chunks(candidate, requirement_ids) for candidate in candidates)
        except DeepReviewInputError as exc:
            fail(422, exc.code, '检索 Bundle 中没有可用于深度核查的有效片段。')
        if not usable:
            fail(422, 'DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES', '没有可用于深度核查的仓库片段。')
        provider = provider_config()
        config = configuration()
        authorized = utcnow()
        operation = {
            'operation_id': str(uuid4()), 'status': 'running',
            'revision_id': str(body.revision_id), 'revision_hash': retrieval['revision_hash'],
            'bundle_hash': retrieval['bundle_hash'], 'model': provider.deepseek_model,
            'prompt_version': PROMPT_VERSION, 'consent': True,
            'authorized_at': authorized.isoformat(),
            'deadline': (authorized + timedelta(seconds=5 * settings.extraction_timeout_seconds + 120)).isoformat(),
            'report_key': None, 'report_hash': None, 'error_code': None,
        }
        manifests = RunManifestStore(store.storage)
        try:
            current, created = manifests.create_deep_review(identity, str(run_id), operation)
        except SearchStateError:
            latest = manifests.read(identity, str(run_id)).get('deep_review_operation')
            if latest is not None and latest['revision_id'] == str(body.revision_id):
                return operation_view(store, identity, task_id, run_id,
                                      manifests.read(identity, str(run_id)))
            fail(409, 'DEEP_REVIEW_REVISION_CONFLICT', '该搜索运行已授权其他需求版本。')
        if created:
            background.add_task(execute, store, identity, record, confirmed, provider, config)
        return operation_view(store, identity, task_id, run_id,
                              manifests.read(identity, str(run_id)))

    @app.get('/api/v1/tasks/{task_id}/search-runs/{run_id}/deep-review')
    def get(task_id: UUID, run_id: UUID, request: Request):
        store, identity = state(), owner(request)
        # A pure session read avoids InputState.session's timeout-repair write on GET.
        raw, _ = store.storage.read_version(store.session_key(identity))
        if raw is None:
            fail(401, 'UNAUTHORIZED', '会话无效或已过期。')
        try:
            session = json.loads(raw)
            if datetime.fromisoformat(session['expires_at']) <= utcnow():
                fail(401, 'UNAUTHORIZED', '会话无效或已过期。')
            record = next((row for row in store.task(session, task_id).get('search_runs', {}).values()
                           if row['run_id'] == str(run_id)), None)
            if record is None:
                fail(404, 'NOT_FOUND', '搜索运行不存在。')
            confirmed_revision(session, store, task_id, record['confirmed_revision_id'])
        except APIError:
            raise
        except (ValueError, TypeError, KeyError):
            fail(401, 'UNAUTHORIZED', '会话无效或已过期。')
        manifests = RunManifestStore(store.storage)
        raw, _ = store.storage.read_version(manifests._key(identity, str(run_id)))
        if raw is None:
            fail(404, 'DEEP_REVIEW_NOT_FOUND', '该搜索运行尚未授权深度核查。')
        try:
            manifest = manifests.read(identity, str(run_id))
        except SearchStateError:
            fail(409, 'DEEP_REVIEW_NOT_READY', '深度核查状态不可用。')
        if (manifest['task_id'] != str(task_id)
                or manifest['confirmed_revision_id'] != record['confirmed_revision_id']
                or 'deep_review_operation' not in manifest):
            fail(404, 'DEEP_REVIEW_NOT_FOUND', '该搜索运行尚未授权深度核查。')
        return operation_view(store, identity, task_id, run_id, manifest)
