"""Private object storage with conditional, non-replayed state writes."""
import hashlib
import os
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Protocol

from filelock import FileLock
from app.core.settings import Settings


class StorageError(Exception):
    """A write may have committed. Callers must not replay or delete its inputs."""


class CASConflict(StorageError):
    """The provider explicitly confirmed that the conditional write lost."""


class StorageService(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def read_version(self, key: str) -> tuple[bytes | None, str | None]: ...
    def compare_and_swap(self, key: str, expected: str | None, data: bytes) -> None: ...


def _comparable_path(path: Path) -> Path:
    # On Windows, non-strict realpath may retain the extended prefix when a
    # concurrent mkdir changes ERROR_PATH_NOT_FOUND to ERROR_FILE_NOT_FOUND.
    # Normalize equivalent resolved roots for containment, without resolving again.
    value = str(path)
    if os.name == 'nt':
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\') and value[5:7] == ':\\':
            value = value[4:]
    return Path(value)


class LocalStorage:
    def __init__(self, directory: Path):
        self.directory = _comparable_path(directory.resolve())

    def _path(self, key: str):
        path = (self.directory / key).resolve()
        if not _comparable_path(path).is_relative_to(self.directory) or '..' in PurePosixPath(key).parts:
            raise StorageError('非法对象路径。')
        return path

    def _read_unlocked(self, key):
        try:
            data = self._path(key).read_bytes()
            return data, hashlib.sha256(data).hexdigest()
        except FileNotFoundError:
            return None, None

    def read_version(self, key):
        try:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Windows readers must also cooperate: an open handle can block replace.
            with FileLock(str(path) + '.lock', timeout=30):
                return self._read_unlocked(key)
        except Exception as exc:
            raise StorageError('无法读取存储状态。') from exc

    def compare_and_swap(self, key, expected, data):
        temporary = None
        try:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Persistent lock file: never unlink it (another process may hold it).
            with FileLock(str(path) + '.lock', timeout=30):
                _, version = self._read_unlocked(key)
                if version != expected:
                    raise CASConflict('存储状态已更改。')
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.pending-', delete=False) as output:
                    temporary = output.name
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                for attempt in range(4):
                    try:
                        os.replace(temporary, path)
                        break
                    except OSError as exc:
                        if os.name != 'nt' or getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 3:
                            raise
                        # Retry only an unchanged, uncommitted payload under the same lock.
                        time.sleep(0.01 * 2 ** attempt)
                        if Path(temporary).read_bytes() != data or self._read_unlocked(key)[1] != expected:
                            raise
                temporary = None
        except CASConflict:
            raise
        except Exception as exc:
            raise StorageError('无法保存存储状态。') from exc
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def put(self, key, data, content_type):
        self.compare_and_swap(key, None, data)

    def get(self, key):
        data, _ = self.read_version(key)
        if data is None:
            raise StorageError('无法读取原始文件。')
        return data

    def delete(self, key):
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError('无法清理上传文件。') from exc


class S3Storage:
    def __init__(self, settings: Settings):
        import boto3
        from botocore.config import Config
        if not all([settings.s3_bucket, settings.s3_access_key_id, settings.s3_secret_access_key]):
            raise StorageError('对象存储尚未配置，请设置私有Bucket及凭据。')
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            's3', endpoint_url=settings.s3_endpoint_url or None, region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(connect_timeout=5, read_timeout=15, retries={'total_max_attempts': 1}),
        )

    def _write(self, key, expected, data, content_type):
        condition = {'IfNoneMatch': '*'} if expected is None else {'IfMatch': expected}
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                   ContentType=content_type, **condition)
        except Exception as exc:
            response = getattr(exc, 'response', {})
            code = response.get('Error', {}).get('Code')
            status = response.get('ResponseMetadata', {}).get('HTTPStatusCode')
            if (status == 412 and code == 'PreconditionFailed') or (status == 409 and code == 'ConditionalRequestConflict'):
                raise CASConflict('存储状态已更改。') from exc
            # Unsupported conditional writes also fail closed. No unconditional fallback.
            raise StorageError('对象存储写入结果未知，请稍后查询状态。') from exc

    def compare_and_swap(self, key, expected, data):
        self._write(key, expected, data, 'application/json')

    def put(self, key, data, content_type):
        self._write(key, None, data, content_type)

    def read_version(self, key):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            with response['Body'] as body:
                data = body.read()
            return data, response['ETag']
        except Exception as exc:
            response = getattr(exc, 'response', {})
            if response.get('Error', {}).get('Code') == 'NoSuchKey':
                return None, None
            raise StorageError('对象存储读取失败。') from exc

    def get(self, key):
        data, _ = self.read_version(key)
        if data is None:
            raise StorageError('无法读取原始文件。')
        return data

    def delete(self, key):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError('对象存储清理失败。') from exc


def create_storage(settings: Settings) -> StorageService:
    if settings.storage_backend == 's3':
        return S3Storage(settings)
    return LocalStorage(settings.local_storage_directory)
