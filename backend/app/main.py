import hashlib
import json
import secrets
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from app.core.settings import Settings
from app.persistence.input_state import APIError, InputState, fail, utcnow
from app.schemas.documents import Contract, InputConfig, InputStatus, NormalizedDocument, SessionResponse, TaskList, TaskResponse
from app.services.processing import process_document
from app.services.storage import StorageError, create_storage


class RequestBoundary:
    """Bound every body before routing or multipart parsing, including chunked bodies."""
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request_id = str(uuid4())
        scope.setdefault('state', {})['request_id'] = request_id
        async def tagged(message):
            if message['type'] == 'http.response.start':
                message.setdefault('headers', []).append((b'x-request-id', request_id.encode()))
            await send(message)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > self.limit:
                response = JSONResponse(status_code=413, content={'error_code': 'BODY_TOO_LARGE', 'message': '请求体超过允许上限。', 'retryable': False, 'request_id': request_id})
                return await response(scope, receive, tagged)
            chunks.append(message.get('body', b''))
            if not message.get('more_body', False):
                break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()
        await self.app(scope, bounded_receive, tagged)


def create_app(settings=None, storage=None, *, extraction_settings=None, extraction_transport=None):
    settings = settings or Settings()
    app = FastAPI()
    app.state.processor = process_document
    app.state.storage = storage

    def state():
        # Lazy initialization keeps import/startup independent of storage availability.
        return InputState(app.state.storage if app.state.storage is not None else create_storage(settings), settings)
    app.add_middleware(RequestBoundary, limit=settings.max_request_bytes)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=['GET', 'POST'], allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key'],
                       expose_headers=['X-Request-ID'])

    def response_error(request, status, code, message, retryable=False):
        return JSONResponse(status_code=status, content={'error_code': code, 'message': message,
                            'retryable': retryable, 'request_id': getattr(request.state, 'request_id', None)})

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return response_error(request, exc.status, exc.code, exc.message, exc.retryable)

    @app.exception_handler(StorageError)
    async def storage_error(request, exc):
        return response_error(request, 503, 'STORAGE_UNAVAILABLE', '存储暂时不可用或写入结果未确认，请稍后查询状态。', True)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return response_error(request, 422, 'INVALID_REQUEST', '请求字段或格式不正确。')

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return response_error(request, exc.status_code, 'INVALID_REQUEST', '请求无效或资源不存在。')

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return response_error(request, 500, 'INTERNAL_ERROR', '服务暂时无法完成请求。', True)

    def owner(request):
        parts = request.headers.get('authorization', '').split()
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            fail(401, 'UNAUTHORIZED', '请先创建有效会话。')
        return hashlib.sha256(parts[1].encode()).hexdigest()

    def status(task):
        doc = task.get('document')
        return InputStatus(task_id=task['id'], document_id=doc['id'] if doc else None,
                           status=task['status'], error=doc.get('error') if doc else None)

    def task_response(task):
        return TaskResponse(task_id=task['id'], created_at=task['created_at'], status=task['status'],
                            document_id=task['document']['id'] if task.get('document') else None)

    @app.post('/api/v1/sessions', response_model=SessionResponse, status_code=201)
    def sessions(body: Contract, request: Request):
        store = state()
        store.ip_rate(request.client.host if request.client else 'unknown')
        token, now = secrets.token_urlsafe(32), utcnow()
        expires = now + timedelta(hours=settings.session_hours)
        store.create_session(hashlib.sha256(token.encode()).hexdigest(), now, expires)
        return SessionResponse(access_token=token, expires_at=expires)

    @app.get('/api/v1/config', response_model=InputConfig)
    def config():
        return InputConfig(**settings.model_dump(include=set(InputConfig.model_fields)))

    @app.get('/api/v1/tasks', response_model=TaskList)
    def tasks(request: Request):
        session = state().session(owner(request))
        rows = sorted(session['tasks'].values(), key=lambda t: (t['created_at'], t['id']), reverse=True)
        return TaskList(tasks=[task_response(t) for t in rows])

    @app.post('/api/v1/tasks', response_model=TaskResponse, status_code=201)
    def new_task(body: Contract, request: Request):
        store, identity, now = state(), owner(request), utcnow()
        task_id = str(uuid4())
        def create(session):
            if len(session['tasks']) >= settings.max_session_tasks:
                fail(429, 'TASK_LIMIT', '会话任务数已达上限，请创建新会话。')
            store.write_rate(session, now)
            task = {'id': task_id, 'created_at': now.isoformat(), 'status': 'empty', 'document': None}
            session['tasks'][task_id] = task
            return task_response(task)
        return store.session(identity, create)

    async def read_input(request):
        try:
            form = await request.form(max_files=1, max_fields=1, max_part_size=settings.max_request_bytes)
        except Exception:
            fail(422, 'INVALID_MULTIPART', '表单格式无效，只允许一个文件和一个文本字段。')
        try:
            items = list(form.multi_items())
            if any(k not in {'file', 'text'} for k, _ in items) or len({k for k, _ in items}) != len(items):
                fail(422, 'INVALID_FIELDS', '只允许 file 和 text 字段，且不得重复。')
            upload, text = form.get('file'), form.get('text', '')
            if not isinstance(text, str) or (upload is not None and not isinstance(upload, UploadFile)):
                fail(422, 'INVALID_FIELDS', '文件或文本字段类型错误。')
            return await upload.read() if upload else None, upload.filename if upload else None, text
        finally:
            await form.close()

    @app.post('/api/v1/tasks/{task_id}/input', response_model=InputStatus, status_code=202)
    def submit(task_id: UUID, request: Request, background: BackgroundTasks):
        from anyio import from_thread

        try:
            key = str(UUID(request.headers.get('idempotency-key', '')))
        except ValueError:
            fail(422, 'INVALID_IDEMPOTENCY_KEY', '必须提供 UUID 格式的 Idempotency-Key。')
        if not request.headers.get('content-type', '').lower().startswith('multipart/form-data'):
            fail(415, 'INVALID_CONTENT_TYPE', '请使用 multipart/form-data 提交。')
        data, filename, text = from_thread.run(read_input, request)
        if data is not None and len(data) > settings.max_file_bytes:
            fail(413, 'FILE_SIZE_LIMIT', '文件大小超过允许上限。')
        if filename and len(filename) > 255:
            fail(422, 'INVALID_FILENAME', '文件名过长。')
        identity = {'file_sha256': hashlib.sha256(data).hexdigest() if data is not None else None, 'text': text, 'filename': filename}
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        store, owner_key = state(), owner(request)

        def existing_status(session):
            task = store.task(session, task_id)
            existing = task.get('document')
            if existing:
                if existing['idempotency_key'] != key or existing['request_hash'] != digest:
                    fail(409, 'INPUT_CONFLICT', '任务已有不同的输入，请新建任务。')
                return status(task)
            return None

        existing = store.session(owner_key, existing_status)
        if existing is not None:
            return existing
        object_key = 'originals/' + uuid4().hex if data is not None else None
        if object_key:
            # Upload first. An uncertain upload is retained, never blindly deleted.
            store.storage.put(object_key, data, 'application/octet-stream')
        now = utcnow()
        doc = {'id': str(uuid4()), 'idempotency_key': key, 'request_hash': digest,
               'original_filename': filename, 'object_key': object_key,
               'file_hash': identity['file_sha256'], 'file_size': len(data) if data is not None else 0,
               'created_at': now.isoformat(), 'result': None, 'error': None}

        def claim(session):
            existing = existing_status(session)
            if existing is not None:
                return existing, False
            store.write_rate(session, now)
            if sum(t['status'] == 'processing' for t in session['tasks'].values()) >= settings.max_processing_tasks:
                fail(429, 'PROCESSING_LIMIT', '正在处理的任务过多，请稍后重试。', True)
            task = store.task(session, task_id)
            task.update(status='processing', document=doc)
            return status(task), True

        def clean_unclaimed():
            if object_key:
                try:
                    store.storage.delete(object_key)
                except StorageError:
                    pass

        try:
            result, claimed = store.session(owner_key, claim)
        except APIError:
            # Pure callback rejected before a write; previous retries only had explicit CAS conflicts.
            clean_unclaimed()
            raise
        # StorageError intentionally propagates without cleanup: commit may have succeeded.
        if not claimed:
            clean_unclaimed()
        else:
            background.add_task(app.state.processor, store, settings, owner_key, str(task_id), doc, data, filename, text)
        return result

    @app.get('/api/v1/tasks/{task_id}/input/status', response_model=InputStatus)
    def input_status(task_id: UUID, request: Request):
        store = state()
        return store.session(owner(request), lambda session: status(store.task(session, task_id)))

    @app.get('/api/v1/tasks/{task_id}/input', response_model=NormalizedDocument)
    def get_input(task_id: UUID, request: Request):
        store = state()
        task = store.session(owner(request), lambda session: store.task(session, task_id))
        result = task['document']['result'] if task['status'] == 'completed' else None
        if result is None:
            fail(409, 'INPUT_NOT_READY', '文档尚未完成处理，请查询状态。', True)
        return result

    from app.services.requirements import install_routes
    install_routes(app, state, owner, settings, extraction_settings, extraction_transport)
    return app


app = create_app()
