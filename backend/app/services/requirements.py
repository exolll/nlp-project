"""Requirement extraction/revision lifecycle in the existing owner-scoped CAS aggregate."""
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import anyio
import httpx
from fastapi import BackgroundTasks, Request
from pydantic import ValidationError

from app.core.settings import ExtractionSettings
from app.persistence.input_state import APIError, encode, fail, utcnow
from app.schemas.documents import Contract
from app.schemas.requirements import (
    BUSINESS_FIELDS, DocumentRequirements, ExtractionJob, RequirementChange,
    RequirementEvidence, RequirementItem, RequirementRevision, RevisionEdit,
)
from app.services.extraction_provider import PROMPT_VERSION, ProviderError, build_payload, call_provider
from app.services.requirement_quality import validate_quality
from app.services.requirement_validation import (
    check, extraction_diagnostic, language_logic_explicit, resolve_spans, signature, validate_coverage, validate_extraction, validate_links,
)
from app.services.storage import StorageError

ACTIVE = {'queued', 'extracting', 'validating'}
GLOBAL_BUDGET_KEY = 'guards/requirement-extractions.json'


def aggregate(task):
    return task.setdefault('requirements', {'extractions': [], 'revisions': [], 'edit_fingerprints': {}})


def document_task(session, document_id):
    for task in session['tasks'].values():
        if task.get('document') and task['document']['id'] == str(document_id):
            return task
    fail(404, 'NOT_FOUND', '文档不存在。')


def revision_task(session, revision_id):
    for task in session['tasks'].values():
        for revision in task.get('requirements', {}).get('revisions', []):
            if revision['revision_id'] == str(revision_id):
                return task, revision
    fail(404, 'NOT_FOUND', '需求版本不存在。')


def expire(agg, now):
    for record in agg['extractions']:
        if record['job']['status'] in ACTIVE and datetime.fromisoformat(record['deadline']) <= now:
            record['job'].update(status='failed', error={
                'error_code': 'EXTRACTION_DEADLINE', 'message': '提取执行期限已过，结果未知；不会自动续跑。', 'retryable': False,
            })


def extraction_view(record):
    """Project a stored record into the wire contract, exposing a sanitised diagnostic."""
    job = dict(record['job'])
    diagnostic = record.get('validation_diagnostic')
    if isinstance(diagnostic, dict):
        field = diagnostic.get('field')
        job['diagnostic'] = {
            'validator': str(diagnostic.get('validator') or 'unknown'),
            'field': field if field in BUSINESS_FIELDS else None,
        }
    return ExtractionJob.model_validate(job)


def snapshot(task, now):
    agg = aggregate(task)
    expire(agg, now)
    return DocumentRequirements(
        extraction=extraction_view(agg['extractions'][-1]) if agg['extractions'] else None,
        revisions=agg['revisions'],
        latest_revision_id=agg['revisions'][-1]['revision_id'] if agg['revisions'] else None,
    )


def reserve_global(store, extraction_id):
    def reserve(value):
        value = value or {'count': 0, 'reservations': []}
        # A prior reservation, including an unknown write, never authorizes replay.
        if extraction_id in value['reservations']:
            return value, False
        if value['count'] >= store.settings.extraction_max_calls:
            fail(429, 'EXTRACTION_GLOBAL_LIMIT', '开发环境累计模型调用预留已达上限。')
        value['count'] += 1
        value['reservations'].append(extraction_id)
        return value, True
    return store.update(GLOBAL_BUDGET_KEY, reserve)


def update_job(store, identity, task_id, extraction_id, expected, status, *, error=None, usage=None, revision=None, diagnostic=None):
    now = utcnow()
    def transition(session):
        agg = aggregate(store.task(session, task_id))
        expire(agg, now)
        record = next((r for r in agg['extractions'] if r['job']['extraction_id'] == extraction_id), None)
        if record is None or record['job']['status'] not in expected:
            return False
        if revision is not None:
            if len(agg['revisions']) >= store.settings.max_requirement_revisions:
                fail(409, 'REVISION_LIMIT', '需求版本数已达上限，不会截断历史。')
            check(not agg['revisions'])
            agg['revisions'].append(revision)
        record['job'].update(status=status, error=error, usage=usage)
        if diagnostic is not None:
            record['validation_diagnostic'] = diagnostic
        return True
    return store.session(identity, transition)


async def run_extraction(store, identity, task_id, extraction_id, document, payload, provider, transport):
    """Only confirmed CAS transitions authorize side effects; unknown writes stop execution."""
    async def threaded(function, *args, **kwargs):
        return await anyio.to_thread.run_sync(lambda: function(*args, **kwargs))
    usage, diagnostic = None, None
    try:
        if not await threaded(update_job, store, identity, task_id, extraction_id, {'queued'}, 'extracting'):
            return
        if not await threaded(reserve_global, store, extraction_id):
            return
        # Recheck deadline/session AFTER a potentially slow global CAS, before payment.
        checked_at = utcnow()
        def live(session):
            agg = aggregate(store.task(session, task_id))
            expire(agg, checked_at)
            return any(r['job']['extraction_id'] == extraction_id and r['job']['status'] == 'extracting'
                       for r in agg['extractions'])
        if not await threaded(store.session, identity, live):
            return
        raw, usage = await call_provider(payload, store.settings, provider, transport)
        if not await threaded(update_job, store, identity, task_id, extraction_id, {'extracting'}, 'validating', usage=usage):
            return
        try:
            requirements, items = validate_extraction(raw, document)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            diagnostic = extraction_diagnostic(exc)
            raise ProviderError('EXTRACTION_INVALID', '模型需求未通过字段、业务规则或原文证据校验。', usage) from None
        now = utcnow()
        revision = RequirementRevision(
            revision_id=uuid4(), task_id=task_id, revision_number=1, parent_revision_id=None,
            status='extracted', requirements=requirements, requirement_items=items,
            change_log=[], created_at=now, confirmed_at=None,
        ).model_dump(mode='json')
        await threaded(update_job, store, identity, task_id, extraction_id, {'validating'}, 'completed',
                       usage=usage, revision=revision)
    except StorageError:
        # Never write failed after an unknown reservation/claim/completion outcome.
        return
    except (ProviderError, APIError) as exc:
        error = exc.public() if isinstance(exc, ProviderError) else {
            'error_code': exc.code, 'message': exc.message, 'retryable': False,
        }
        if isinstance(exc, ProviderError) and exc.usage is not None:
            usage = exc.usage
        try:
            await threaded(update_job, store, identity, task_id, extraction_id, ACTIVE, 'failed',
                           error=error, usage=usage, diagnostic=diagnostic)
        except (StorageError, APIError):
            return
    except Exception:
        # Unexpected executor loss is recovered by the durable deadline. No repr/logs
        # and no blind compensating write that could overwrite a completed result.
        return


def build_edit(parent, body, revision_id, now, document, revision_segments):
    """Called outside CAS; result is immutable input to a pure append transition."""
    requirements = body.requirements.model_copy(deep=True)
    old_items = [RequirementItem.model_validate(item) for item in parent['requirement_items']]
    old_by_id = {item.requirement_item_id: item for item in old_items}
    old_evidence = {e['evidence_id']: RequirementEvidence.model_validate(e) for e in parent['requirements']['source_evidence']}
    items, evidence, mapping, used = [], {}, {}, set()
    new_segments = {}
    validate_coverage(requirements, body.requirement_items)
    for proposed in body.requirement_items:
        old = None
        if proposed.requirement_item_id is not None:
            check(proposed.requirement_item_id in old_by_id, '不能伪造已有需求项 ID。')
            old = old_by_id[proposed.requirement_item_id]
            check(proposed.supersedes_item_id in (old.supersedes_item_id, old.requirement_item_id))
        elif proposed.supersedes_item_id is not None:
            check(proposed.supersedes_item_id in old_by_id, '替换项必须属于当前父版本。')
            old = old_by_id[proposed.supersedes_item_id]
        else:
            matches = [item for item in old_items if signature(item) == signature(proposed)
                       and item.requirement_item_id not in used]
            if len(matches) == 1:
                old = matches[0]
        if old is not None:
            check(old.requirement_item_id not in used, '同一旧需求项不能重复使用。')
            used.add(old.requirement_item_id)
        unchanged = old is not None and signature(old) == signature(proposed)
        if unchanged:
            item = old.model_copy(update={'value_path': proposed.value_path})
            for eid in old.evidence_ids:
                evidence[eid] = old_evidence[eid].model_copy(deep=True)
        else:
            item_id, evidence_id = str(uuid4()), str(uuid4())
            item = RequirementItem(
                **proposed.model_dump(exclude={'requirement_item_id', 'source_kind', 'evidence_ids', 'supersedes_item_id'}),
                requirement_item_id=item_id, source_kind='user_revision', evidence_ids=[evidence_id],
                supersedes_item_id=old.requirement_item_id if old else None,
            )
            segment_id = 'REV-' + str(uuid4())
            location = f'revision:{revision_id}:{item.value_path}'
            # Actual user-submitted text, durably retained with the revision in the
            # same CAS. Offsets have the same original_content semantics as input.
            new_segments[segment_id] = {
                'segment_id': segment_id, 'original_content': proposed.text,
                'source_kind': 'user_revision', 'source_location': location,
            }
            evidence[evidence_id] = RequirementEvidence(
                evidence_id=evidence_id, field=item.field, original_text=proposed.text,
                source_kind='user_revision', source_location=location,
                requirement_item_id=item_id, segment_id=segment_id, start_offset=0, end_offset=len(proposed.text),
            )
        if old is not None:
            mapping[old.requirement_item_id] = item.requirement_item_id
        items.append(item)
    # Client source_evidence/source_kind/evidence_ids never authorize provenance.
    # Rebuild uncertainty references from surviving items, not arbitrary client quotes.
    by_id = {item.requirement_item_id: item for item in items}
    for uncertain in requirements.uncertain_information or []:
        uncertain.requirement_item_ids = [mapping.get(rid, rid) for rid in uncertain.requirement_item_ids]
        check(set(uncertain.requirement_item_ids) <= set(by_id), '歧义引用已删除或不存在的需求项。')
        uncertain.evidence_ids = list(dict.fromkeys(
            eid for rid in uncertain.requirement_item_ids for eid in by_id[rid].evidence_ids
        ))
    requirements.source_evidence = list(evidence.values())
    resolve_spans(requirements, {'source_segments': [
        *document['source_segments'], *revision_segments.values(), *new_segments.values(),
    ]})
    validate_coverage(requirements, items)
    validate_links(requirements, items)
    logs = []
    old_req = parent['requirements']
    new_req = requirements.model_dump(mode='json')
    for field in (*BUSINESS_FIELDS, 'missing_information', 'uncertain_information'):
        old_meta = [item.model_dump() for item in old_items if item.field == field]
        new_meta = [item.model_dump() for item in items if item.field == field]
        if old_req[field] != new_req[field] or old_meta != new_meta:
            logs.append(RequirementChange(
                field=field, old_value={'value': old_req[field], 'items': old_meta},
                new_value={'value': new_req[field], 'items': new_meta}, reason=body.change_reason,
                original_text=encode({'field': field, 'value': new_req[field], 'change_reason': body.change_reason}).decode(),
                created_at=now, requirement_item_ids=[item.requirement_item_id for item in items if item.field == field],
            ))
    return RequirementRevision(
        revision_id=revision_id, task_id=parent['task_id'], revision_number=parent['revision_number'] + 1,
        parent_revision_id=parent['revision_id'], status='draft', requirements=requirements,
        requirement_items=items, change_log=logs, created_at=now, confirmed_at=None,
    ).model_dump(mode='json'), new_segments


def install_routes(app, state, owner, settings, provider_settings=None, transport=None):
    if transport is not None and (settings.environment != 'test' or not isinstance(transport, httpx.MockTransport)):
        raise ValueError('Injected model transport is permitted only for tests with MockTransport.')

    def provider_config():
        if settings.environment == 'production':
            fail(503, 'EXTRACTION_NOT_PUBLIC', '付费提取尚未开放生产环境闸门，仅开发环境可用。')
        if settings.environment == 'test' and (transport is None or provider_settings is None):
            fail(503, 'EXTRACTION_DISABLED', '测试环境仅允许显式 MockTransport，不进行真实调用。')
        try:
            config = provider_settings if provider_settings is not None else ExtractionSettings()
            config = ExtractionSettings.model_validate(config.model_dump())
            if not config.deepseek_api_key.get_secret_value().strip():
                fail(503, 'EXTRACTION_DISABLED', '需求提取尚未配置模型凭据；输入、编辑和查询仍可使用。')
            return config
        except ValidationError:
            fail(503, 'EXTRACTION_MISCONFIGURED', '需求提取配置无效，仅支持官方 HTTPS 端点及固定模型。')

    @app.post('/api/v1/documents/{document_id}/requirement-extractions', response_model=ExtractionJob, status_code=202)
    def extract(document_id: UUID, body: Contract, request: Request, background: BackgroundTasks):
        try:
            key = str(UUID(request.headers.get('idempotency-key', '')))
        except ValueError:
            fail(422, 'INVALID_IDEMPOTENCY_KEY', '必须提供 UUID 格式的 Idempotency-Key。')
        store, identity, now = state(), owner(request), utcnow()

        def preflight(session):
            task = document_task(session, document_id)
            agg = aggregate(task)
            expire(agg, now)
            for record in agg['extractions']:
                if record['idempotency_key'] == key:
                    return extraction_view(record), None
            if agg['revisions'] or any(r['job']['status'] in ACTIVE for r in agg['extractions']):
                fail(409, 'EXTRACTION_CONFLICT', '已有有效提取或正在提取；请编辑版本或等待结果。')
            if len(agg['extractions']) >= settings.max_task_extractions:
                fail(429, 'TASK_EXTRACTION_LIMIT', '此任务提取次数已达上限。')
            if task['status'] != 'completed' or not task['document']['result']:
                fail(409, 'INPUT_NOT_READY', '文档尚未完成处理。')
            return None, deepcopy(task)

        existing, task = store.session(identity, preflight)
        if existing is not None:
            return existing
        provider = provider_config()
        try:
            payload = build_payload(task['document']['result'], settings, provider)
        except ProviderError as exc:
            fail(413, exc.code, exc.message)
        job = ExtractionJob(extraction_id=uuid4(), status='queued', error=None,
                            model=provider.deepseek_model, prompt_version=PROMPT_VERSION, usage=None, created_at=now)
        record = {'idempotency_key': key, 'job': job.model_dump(mode='json'),
                  'deadline': (now + timedelta(seconds=settings.extraction_timeout_seconds + 120)).isoformat()}

        def claim(session):
            for session_task in session['tasks'].values():
                if 'requirements' in session_task:
                    expire(session_task['requirements'], now)
            current, current_task = preflight(session)
            if current is not None:
                return current, False
            if session.get('extraction_calls', 0) >= settings.extraction_max_calls:
                fail(429, 'SESSION_EXTRACTION_LIMIT', '会话累计提取次数已达上限。')
            active = sum(r['job']['status'] in ACTIVE for t in session['tasks'].values()
                         for r in t.get('requirements', {}).get('extractions', []))
            if active >= settings.max_processing_tasks:
                fail(429, 'EXTRACTION_PROCESSING_LIMIT', '会话内正在提取的任务过多。')
            store.write_rate(session, now)
            session['extraction_calls'] = session.get('extraction_calls', 0) + 1
            aggregate(store.task(session, current_task['id']))['extractions'].append(deepcopy(record))
            return job, True

        result, claimed = store.session(identity, claim)
        if claimed:
            background.add_task(run_extraction, store, identity, task['id'], str(job.extraction_id),
                                task['document']['result'], payload, provider, transport)
        return result

    @app.get('/api/v1/documents/{document_id}/requirements', response_model=DocumentRequirements)
    def get_requirements(document_id: UUID, request: Request):
        now = utcnow()
        return state().session(owner(request), lambda session: snapshot(document_task(session, document_id), now))

    @app.post('/api/v1/requirement-revisions/{revision_id}/revisions', response_model=RequirementRevision, status_code=201)
    def edit(revision_id: UUID, body: RevisionEdit, request: Request):
        store, identity, now = state(), owner(request), utcnow()
        fingerprint = hashlib.sha256(encode({'parent': str(revision_id), 'body': body.model_dump()})).hexdigest()

        def preflight(session):
            task, parent = revision_task(session, revision_id)
            agg = aggregate(task)
            child_id = agg['edit_fingerprints'].get(fingerprint)
            if child_id:
                return next(r for r in agg['revisions'] if r['revision_id'] == child_id), None
            if agg['revisions'][-1]['revision_id'] != str(revision_id):
                fail(409, 'STALE_REVISION', '版本已更新，请刷新后基于最新版本编辑。')
            if len(agg['revisions']) >= settings.max_requirement_revisions:
                fail(409, 'REVISION_LIMIT', '需求版本数已达上限，不会截断历史。')
            return None, deepcopy({'parent': parent, 'document': task['document']['result'],
                                   'revision_segments': agg.get('revision_segments', {})})
        existing, context = store.session(identity, preflight)
        if existing:
            return existing
        if not body.change_reason.strip():
            fail(422, 'INVALID_REVISION', '请填写具体修改原因。')
        try:
            child, new_segments = build_edit(context['parent'], body, str(uuid4()), now,
                                             context['document'], context['revision_segments'])
        except (ValueError, TypeError, KeyError):
            fail(422, 'INVALID_REVISION', '需求项覆盖、身份、属性或引用无效。')
        def append(session):
            existing, _ = preflight(session)
            if existing:
                return existing
            task, _ = revision_task(session, revision_id)
            store.write_rate(session, now)
            agg = aggregate(task)
            agg['revisions'].append(deepcopy(child))
            agg.setdefault('revision_segments', {}).update(deepcopy(new_segments))
            agg['edit_fingerprints'][fingerprint] = child['revision_id']
            return child
        return store.session(identity, append)

    @app.post('/api/v1/requirement-revisions/{revision_id}/confirm', response_model=RequirementRevision)
    def confirm(revision_id: UUID, body: Contract, request: Request):
        store, identity, now = state(), owner(request), utcnow()
        def transition(session):
            task, revision = revision_task(session, revision_id)
            if aggregate(task)['revisions'][-1]['revision_id'] != str(revision_id):
                fail(409, 'STALE_REVISION', '只能确认最新需求版本。')
            if revision['status'] == 'confirmed':
                return revision
            items = [RequirementItem.model_validate(item) for item in revision['requirement_items']]
            if revision['requirements']['uncertain_information'] or not language_logic_explicit(items):
                fail(409, 'REQUIREMENTS_UNRESOLVED', '请先编辑消除歧义，并明确多语言硬要求的同组 ALL/ANY 关系。')
            quality = validate_quality(RequirementRevision.model_validate(revision))
            if not quality['ok']:
                fail(422, 'REQUIREMENT_EVIDENCE_MISMATCH',
                     '必需/偏好类需求文字与原文证据不一致（概述类字段不受此限），请修改后保存新版本再确认。',
                     details={'evidence_mismatches': quality['evidence_mismatches']})
            store.write_rate(session, now)
            revision.update(status='confirmed', confirmed_at=now.isoformat())
            return revision
        return store.session(identity, transition)
