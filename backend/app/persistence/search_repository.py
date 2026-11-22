"""Unknown SQL outcomes require reconciliation; only confirmed rollbacks may retry."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import sleep
from uuid import UUID
import re

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.persistence.search_models import (
    Repository, RepositorySnapshot, SearchGroup, SearchPlan, SearchQuery, SearchSnapshot,
)
from app.schemas.search import SearchIntegrityError, SearchPlanDocument, SearchStateError
from app.services.search_contracts import snapshot_key as calculate_snapshot_key
from app.services.storage import StorageError

UTC = timezone.utc
PLAN_FIELDS = ('plan_id', 'run_id', 'confirmed_revision_id', 'planner_version',
               'model_version', 'prompt_version', 'created_at')
GROUP_FIELDS = ('group_id', 'group_name', 'group_purpose', 'source_requirement_pointers',
                'group_status', 'skip_reason', 'created_at')
QUERY_FIELDS = ('query_id', 'query_text', 'query_type', 'language_filter', 'qualifiers',
                'source_requirement_pointers', 'query_hash', 'created_at')
SNAPSHOT_FIELDS = ('snapshot_id', 'repository_id', 'commit_sha', 'readme_path', 'blob_sha',
                   'content_hash', 'storage_key', 'snapshot_key', 'collected_at')
SNAPSHOT_IDENTITY = ('repository_id', 'commit_sha', 'readme_path', 'blob_sha',
                     'content_hash', 'storage_key', 'snapshot_key')
USAGE_FIELDS = ('id', 'run_id', 'plan_id', 'repository_id', 'snapshot_id', 'usage_type', 'used_at')
USAGE_KEY = ('run_id', 'repository_id', 'snapshot_id', 'usage_type')
TEXT_LIMITS = {'full_name': 255, 'html_url': 2048, 'language': 64,
               'license': 128, 'default_branch': 255}
PATCH_FIELDS = frozenset((*TEXT_LIMITS, 'description', 'topics', 'stars', 'forks',
                          'pushed_at', 'is_archived'))


def _time(value):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
        value = value.astimezone(UTC).replace(tzinfo=None)
        if value.year < 1000:  # MySQL DATETIME range, not Python's wider range.
            raise ValueError
        return value
    except (ValueError, TypeError, OverflowError):
        raise SearchIntegrityError('Expected an aware timestamp') from None


def _uint(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**64 - 1:
        raise SearchIntegrityError('Invalid unsigned integer')
    return value


def _text(value, limit, *, ascii_only=False, nonempty=False):
    if type(value) is not str or len(value) > limit or (nonempty and not value.strip()):
        raise SearchIntegrityError('Invalid text field')
    try:
        value.encode('ascii' if ascii_only else 'utf-8')
    except UnicodeError:
        raise SearchIntegrityError('Invalid text encoding') from None
    return value


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SearchIntegrityError('Invalid UUID') from None
    return value


def _hash(value, length=64):
    if type(value) is not str or re.fullmatch('[0-9a-f]{%d}' % length, value) is None:
        raise SearchIntegrityError('Invalid hash')
    return value


def _columns(document, fields):
    result = {field: deepcopy(document[field]) for field in fields}
    for field in ('created_at', 'collected_at', 'used_at'):
        if field in result:
            result[field] = _time(result[field])
    return result


def _assert_fields(row, expected):
    if any(getattr(row, field) != value for field, value in expected.items()):
        raise SearchIntegrityError('Stored identity or projection differs')


def _document(row):
    if row is None:
        return None
    result = {}
    for column in row.__table__.columns:
        value = deepcopy(getattr(row, column.name))
        if isinstance(value, datetime):
            value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
            value = value.isoformat().replace('+00:00', 'Z')
        result[column.name] = value
    return result


class RequestClock:
    """Single-worker request clock, seeded with SQL and Manifest watermarks."""

    def __init__(self, initial_values):
        self._lock = Lock()
        self._last = max((_time(value).replace(tzinfo=UTC) for value in initial_values),
                         default=datetime.min.replace(tzinfo=UTC))

    def issue(self) -> datetime:
        with self._lock:
            self._last = max(datetime.now(UTC), self._last + timedelta(microseconds=1))
            return self._last


class SearchRepository:
    def __init__(self, engine):
        self.engine = engine

    def _read(self, operation):
        try:
            with Session(self.engine, expire_on_commit=False) as session:
                return operation(session)
        except SQLAlchemyError:
            raise StorageError('Search storage read failed') from None

    def _write(self, operation):
        # MySQL 1205 may roll back one statement; require full rollback before retry.
        for attempt in range(3):
            try:
                with Session(self.engine, expire_on_commit=False) as session:
                    committing = False
                    try:
                        result = operation(session)
                        committing = True
                        session.commit()
                        return result
                    except SQLAlchemyError as error:
                        code = error.orig.args[0] if isinstance(error, DBAPIError) and error.orig.args else None
                        try:
                            session.rollback()
                        except SQLAlchemyError:
                            raise StorageError('Search storage rollback unconfirmed') from None
                        retryable = code in (1205, 1213) or (code == 1062 and not committing)
                        if retryable and attempt < 2:
                            pass
                        elif code == 1062 and not committing:
                            raise SearchIntegrityError('Conflicting unique identity') from None
                        else:
                            raise StorageError('Search storage outcome requires reconciliation') from None
                sleep(0.01 * (attempt + 1))
            except SQLAlchemyError:
                raise StorageError('Search storage operation failed') from None

    @staticmethod
    def _plan_values(plan, storage_key, plan_hash):
        try:
            document = SearchPlanDocument.model_validate(plan).model_dump(mode='json')
        except ValueError:
            raise SearchIntegrityError('Invalid search plan') from None
        parent = _columns(document, PLAN_FIELDS)
        parent.update(plan_storage_key=_text(storage_key, 255, ascii_only=True, nonempty=True),
                      plan_hash=_hash(plan_hash))
        groups, queries = [], []
        for group in document['groups']:
            groups.append(dict(_columns(group, GROUP_FIELDS), plan_id=parent['plan_id']))
            for query in group['queries']:
                queries.append(dict(_columns(query, QUERY_FIELDS), group_id=group['group_id']))
        return parent, groups, queries

    @staticmethod
    def _locked_plan(session, parent):
        row = session.scalar(select(SearchPlan).where(SearchPlan.plan_id == parent['plan_id']).with_for_update())
        if row is not None:
            _assert_fields(row, parent)
            if row.plan_status == 'failed':
                raise SearchStateError('Search plan failed')
            if row.plan_status not in ('pending', 'ready'):
                raise SearchStateError('Invalid search plan state')
        return row

    @staticmethod
    def _check_projection(session, plan_id, groups, queries):
        actual_groups = session.scalars(select(SearchGroup).where(SearchGroup.plan_id == plan_id)).all()
        actual_queries = session.scalars(select(SearchQuery).join(
            SearchGroup, SearchQuery.group_id == SearchGroup.group_id
        ).where(SearchGroup.plan_id == plan_id)).all()
        for actual, expected, key in ((actual_groups, groups, 'group_id'),
                                      (actual_queries, queries, 'query_id')):
            by_id = {getattr(row, key): row for row in actual}
            if len(actual) != len(expected) or set(by_id) != {row[key] for row in expected}:
                raise SearchIntegrityError('Stored projection is incomplete or different')
            for values in expected:
                _assert_fields(by_id[values[key]], values)

    def ensure_pending(self, plan: dict, storage_key: str, plan_hash: str) -> None:
        parent, groups, queries = self._plan_values(plan, storage_key, plan_hash)

        def write(session):
            row = self._locked_plan(session, parent)
            if row is None:
                session.add(SearchPlan(**parent, plan_status='pending'))
                session.flush()
            elif row.plan_status == 'ready':
                self._check_projection(session, parent['plan_id'], groups, queries)
        self._write(write)

    def publish_plan(self, plan: dict, storage_key: str, plan_hash: str) -> None:
        parent, groups, queries = self._plan_values(plan, storage_key, plan_hash)

        def write(session):
            row = self._locked_plan(session, parent)
            if row is None:
                raise SearchStateError('Pending search plan is missing')
            if row.plan_status == 'ready':
                self._check_projection(session, parent['plan_id'], groups, queries)
                return
            # Never repair a partial projection by deleting or overwriting evidence.
            self._check_projection(session, parent['plan_id'], [], [])
            session.add_all([SearchGroup(**values) for values in groups])
            session.flush()
            session.add_all([SearchQuery(**values) for values in queries])
            session.flush()
            self._check_projection(session, parent['plan_id'], groups, queries)
            row.plan_status = 'ready'
            session.flush()
        self._write(write)

    def plan_is_ready(self, plan: dict, storage_key: str, plan_hash: str) -> bool:
        parent, groups, queries = self._plan_values(plan, storage_key, plan_hash)

        def read(session):
            row = session.scalar(select(SearchPlan).where(SearchPlan.plan_id == parent['plan_id']))
            if row is None:
                return False
            _assert_fields(row, parent)
            if row.plan_status == 'failed':
                raise SearchStateError('Search plan failed')
            if row.plan_status == 'pending':
                self._check_projection(session, parent['plan_id'], [], [])
                return False
            if row.plan_status != 'ready':
                raise SearchStateError('Invalid search plan state')
            self._check_projection(session, parent['plan_id'], groups, queries)
            return True
        return self._read(read)

    def get_snapshot(self, snapshot_key: str) -> dict | None:
        _hash(snapshot_key)
        return self._read(lambda session: _document(session.scalar(select(RepositorySnapshot).where(
            RepositorySnapshot.snapshot_key == snapshot_key))))

    def get_snapshot_by_id(self, snapshot_id: str) -> dict | None:
        _uuid(snapshot_id)
        return self._read(lambda session: _document(session.get(RepositorySnapshot, snapshot_id)))

    def ensure_snapshot(self, snapshot: dict) -> dict:
        if set(snapshot) != set(SNAPSHOT_FIELDS):
            raise SearchIntegrityError('Invalid snapshot fields')
        values = _columns(snapshot, SNAPSHOT_FIELDS)
        _uuid(values['snapshot_id'])
        _uint(values['repository_id'], 1)
        for field in ('commit_sha', 'blob_sha'):
            _hash(values[field], 40)
        for field in ('snapshot_key', 'content_hash'):
            _hash(values[field])
        _text(values['storage_key'], 255, ascii_only=True, nonempty=True)
        _text(values['readme_path'], 65535)
        if not values['readme_path'] or len(values['readme_path'].encode('utf-8')) > 65535:
            raise SearchIntegrityError('Invalid README path')
        if calculate_snapshot_key(*(values[k] for k in ('repository_id', 'commit_sha', 'readme_path', 'blob_sha'))) != values['snapshot_key']:
            raise SearchIntegrityError('Snapshot key does not match identity')

        def write(session):
            by_id = session.get(RepositorySnapshot, values['snapshot_id'])
            if by_id is not None:
                _assert_fields(by_id, {key: values[key] for key in SNAPSHOT_IDENTITY})
            row = session.scalar(select(RepositorySnapshot).where(
                RepositorySnapshot.snapshot_key == values['snapshot_key']).with_for_update())
            if row is not None:
                _assert_fields(row, {key: values[key] for key in SNAPSHOT_IDENTITY})
                return _document(row)
            row = RepositorySnapshot(**values)
            session.add(row)
            session.flush()
            return _document(row)
        return self._write(write)

    def record_usage(self, usage: dict) -> dict:
        if set(usage) != set(USAGE_FIELDS):
            raise SearchIntegrityError('Invalid usage fields')
        values = _columns(usage, USAGE_FIELDS)
        for field in ('id', 'run_id', 'plan_id', 'snapshot_id'):
            _uuid(values[field])
        _uint(values['repository_id'], 1)
        if values['usage_type'] not in ('candidate_review', 'evidence_retrieval', 'deep_analysis'):
            raise SearchIntegrityError('Invalid snapshot usage type')

        def write(session):
            plan = session.scalar(select(SearchPlan).where(SearchPlan.plan_id == values['plan_id']).with_for_update())
            if plan is None:
                raise SearchStateError('Search plan is missing')
            _assert_fields(plan, {'run_id': values['run_id']})
            if plan.plan_status != 'ready':
                raise SearchStateError('Search plan is not ready')
            snapshot = session.get(RepositorySnapshot, values['snapshot_id'])
            if snapshot is None:
                raise SearchIntegrityError('Snapshot is missing')
            _assert_fields(snapshot, {'repository_id': values['repository_id']})
            identity = {key: values[key] for key in (*USAGE_KEY, 'plan_id')}
            by_id = session.get(SearchSnapshot, values['id'])
            if by_id is not None:
                _assert_fields(by_id, identity)
            row = session.scalar(select(SearchSnapshot).where(
                *(getattr(SearchSnapshot, key) == values[key] for key in USAGE_KEY)).with_for_update())
            if row is not None:
                _assert_fields(row, identity)
                return _document(row)
            row = SearchSnapshot(**values)
            session.add(row)
            session.flush()
            return _document(row)
        return self._write(write)

    @staticmethod
    def _validate_patch(patch):
        if type(patch) is not dict or set(patch) - PATCH_FIELDS:
            raise SearchIntegrityError('Unknown repository patch fields')
        values = deepcopy(patch)
        for field, value in values.items():
            if value is None:
                if field in ('full_name', 'html_url'):
                    raise SearchIntegrityError('Required repository field cannot be null')
                continue
            if field in TEXT_LIMITS:
                _text(value, TEXT_LIMITS[field], nonempty=field in ('full_name', 'html_url'))
            elif field == 'description':
                _text(value, 65535)
                if len(value.encode('utf-8')) > 65535:
                    raise SearchIntegrityError('Description exceeds SQL TEXT capacity')
            elif field in ('stars', 'forks'):
                _uint(value)
            elif field == 'topics':
                if type(value) is not list:
                    raise SearchIntegrityError('Topics must be an array')
                for topic in value:
                    _text(topic, 65535)
            elif field == 'is_archived':
                if type(value) is not bool:
                    raise SearchIntegrityError('Archived flag must be boolean')
            elif field == 'pushed_at':
                values[field] = _time(value)
        return values

    @staticmethod
    def github_patch(response):
        if type(response) is not dict or 'id' not in response:
            raise SearchIntegrityError('GitHub response lacks a repository identity')
        repository_id = _uint(response['id'], 1)
        mapping = {
            'full_name': 'full_name', 'html_url': 'html_url', 'description': 'description',
            'topics': 'topics', 'language': 'language', 'stargazers_count': 'stars',
            'forks_count': 'forks', 'default_branch': 'default_branch',
            'pushed_at': 'pushed_at', 'archived': 'is_archived',
        }
        patch = {target: deepcopy(response[source]) for source, target in mapping.items() if source in response}
        if 'license' in response:
            license_data = response['license']
            if license_data is None:
                patch['license'] = None
            elif type(license_data) is dict:
                identifier = license_data.get('spdx_id') or license_data.get('key')
                if type(identifier) is not str or not identifier.strip():
                    raise SearchIntegrityError('GitHub license has no explicit identifier')
                patch['license'] = identifier
            else:
                raise SearchIntegrityError('Invalid GitHub license metadata')
        values = SearchRepository._validate_patch(patch)
        if isinstance(values.get('pushed_at'), datetime):
            values['pushed_at'] = values['pushed_at'].replace(tzinfo=UTC).isoformat()
        return repository_id, values

    def update_repository(self, repository_id: int, patch: dict, fetched_at: datetime) -> bool:
        _uint(repository_id, 1)
        if not isinstance(fetched_at, datetime):
            raise SearchIntegrityError('Request timestamp must be an aware datetime')
        incoming = _time(fetched_at)
        values = self._validate_patch(patch)

        def write(session):
            row = session.scalar(select(Repository).where(
                Repository.repository_id == repository_id).with_for_update())
            if row is None:
                if not {'full_name', 'html_url'} <= set(values):
                    raise SearchIntegrityError('Initial repository requires name and URL')
                session.add(Repository(repository_id=repository_id, fetched_at=incoming, **values))
            else:
                if incoming < row.fetched_at:
                    return False
                if incoming == row.fetched_at:
                    _assert_fields(row, values)
                    return False
                for key, value in values.items():
                    setattr(row, key, value)
                row.fetched_at = incoming
            session.flush()
            return True
        return self._write(write)

    def get_repository(self, repository_id: int) -> dict | None:
        _uint(repository_id, 1)
        return self._read(lambda session: _document(session.get(Repository, repository_id)))

    def latest_fetched_at(self) -> datetime | None:
        value = self._read(lambda session: session.scalar(select(func.max(Repository.fetched_at))))
        return value.replace(tzinfo=UTC) if value is not None else None
