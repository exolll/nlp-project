"""Isolated parsing and durable processing transitions."""
import multiprocessing as mp
import logging

from app.services.storage import StorageError
from app.persistence.input_state import APIError
from app.schemas.documents import NormalizedDocument
from app.services.parsing import ParseError, parse_content


def _worker(pipe, data, filename, text, limits):
    try:
        pipe.send(('ok', parse_content(data, filename, text, limits)))
    except ParseError as exc:
        pipe.send(('error', (exc.code, exc.message)))
    except Exception:
        pipe.send(('error', ('PARSE_FAILED', '文档解析失败，请检查内容后重试。')))
    finally:
        pipe.close()


def parse_isolated(data, filename, text, settings):
    context = mp.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, data, filename, text, settings.parser_limits()))
    try:
        process.start()
        sender.close()
        # Drain the pipe before joining: large results must not block the child.
        if not receiver.poll(settings.parse_timeout_seconds):
            raise ParseError('PARSE_TIMEOUT', '文档解析超时，请缩小文档后重试。')
        try:
            kind, value = receiver.recv()
        except EOFError:
            raise ParseError('PARSE_FAILED', '文档解析进程异常结束。') from None
        if kind != 'ok':
            raise ParseError(*value)
        return value
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()


def process_document(state, settings, owner, task_id, document, data, filename, text):
    result, error = None, None
    try:
        parsed = parse_isolated(data, filename, text, settings)
        parsed.update(document_id=document['id'], task_id=task_id, created_at=document['created_at'],
                      mime_type=parsed.get('mime_type') or 'text/plain', file_size=document['file_size'])
        result = NormalizedDocument.model_validate(parsed).model_dump(mode='json')
    except Exception as exc:
        error = {'error_code': 'PROCESSING_FAILED', 'message': '文档处理失败，请稍后重试。', 'retryable': True}
        if isinstance(exc, ParseError):
            error.update(error_code=exc.code, message=exc.message)
    try:
        state.complete(owner, task_id, document['id'], result, error)
    except (StorageError, APIError):
        # Unknown write outcome: do not follow it with another transition.
        # Polling expires orphaned processing claims after the deadline.
        logging.getLogger(__name__).warning('处理状态未确认；由后续状态查询恢复。')
