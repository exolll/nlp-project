"""One bounded aggregate per anonymous session; callbacks are pure state transitions."""
import hashlib
import json
from datetime import datetime, timezone

from app.services.storage import CASConflict, StorageError


class APIError(Exception):
    def __init__(self, status, code, message, retryable=False):
        self.status, self.code, self.message, self.retryable = status, code, message, retryable


def fail(status, code, message, retryable=False):
    raise APIError(status, code, message, retryable)


def utcnow():
    return datetime.now(timezone.utc)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


class InputState:
    def __init__(self, storage, settings):
        self.storage, self.settings = storage, settings

    def update(self, key, transition):
        for _ in range(64):
            raw, version = self.storage.read_version(key)
            try:
                value = json.loads(raw) if raw is not None else None
            except (ValueError, UnicodeError) as exc:
                raise StorageError('存储状态损坏。') from exc
            value, result = transition(value)
            updated = encode(value)
            if updated == raw:
                return result
            try:
                self.storage.compare_and_swap(key, version, updated)
                return result
            except CASConflict:
                continue
            # All other failures are unknown outcomes: never replay the callback/write.
        raise StorageError('存储繁忙，请稍后重试。')

    @staticmethod
    def session_key(owner):
        return 'sessions/' + owner + '.json'

    def create_session(self, owner, now, expires):
        def create(value):
            if value is not None:
                raise StorageError('会话键冲突。')
            return {'version': 1, 'created_at': now.isoformat(), 'expires_at': expires.isoformat(),
                    'tasks': {}, 'rate': {}}, None
        self.update(self.session_key(owner), create)

    def session(self, owner, transition=lambda value: value):
        now = utcnow()
        def apply(value):
            if value is None or datetime.fromisoformat(value['expires_at']) <= now:
                fail(401, 'UNAUTHORIZED', '会话无效或已过期。')
            for task in value['tasks'].values():
                doc = task.get('document')
                if task['status'] == 'processing' and (now - datetime.fromisoformat(doc['created_at'])).total_seconds() > self.settings.parse_timeout_seconds + 120:
                    task['status'] = 'failed'
                    doc['error'] = {'error_code': 'PROCESSING_TIMEOUT', 'message': '处理已超时，请新建任务重试。', 'retryable': True}
            return value, transition(value)
        return self.update(self.session_key(owner), apply)

    @staticmethod
    def rate_value(window, limit, now):
        if now >= window.get('expires', 0):
            window.update(count=0, expires=now + 60)
        if window['count'] >= limit:
            fail(429, 'RATE_LIMIT', '操作过于频繁，请稍后重试。', True)
        window['count'] += 1

    def ip_rate(self, ip):
        now = utcnow().timestamp()
        key = 'rates/' + hashlib.sha256(ip.encode()).hexdigest() + '.json'
        def apply(value):
            value = value or {'version': 1}
            self.rate_value(value, self.settings.session_requests_per_minute, now)
            return value, None
        self.update(key, apply)

    def write_rate(self, session, now):
        self.rate_value(session['rate'], self.settings.write_requests_per_minute, now.timestamp())

    @staticmethod
    def task(session, task_id):
        task = session['tasks'].get(str(task_id))
        if task is None:
            fail(404, 'NOT_FOUND', '任务不存在。')
        return task

    def complete(self, owner, task_id, document_id, result, error):
        def apply(session):
            task = self.task(session, task_id)
            doc = task.get('document')
            if task['status'] == 'processing' and doc['id'] == document_id:
                task['status'] = 'failed' if error else 'completed'
                doc.update(result=result, error=error)
        self.session(owner, apply)
