"""Single-worker dispatch; restart recovery reconciles durable responses without HTTP."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from time import monotonic, sleep
from uuid import uuid4

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.settings import PROJECT_ROOT
from app.persistence.search_repository import SearchRepository
from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.github_client import GitHubClientError, render_query
from app.services.search_persistence import _lock
from app.services.storage import StorageError


class SearchExecutionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / '.env', extra='ignore', hide_input_in_errors=True)
    github_search_per_page: int = Field(30, ge=1, le=100)
    github_search_max_pages: int = Field(3, ge=1, le=10)
    github_search_max_queries_per_group: int = Field(3, ge=1, le=3)
    github_search_max_candidates: int = Field(100, ge=1, le=300)
    github_readme_max_candidates: int = Field(20, ge=0, le=30)
    github_search_max_requests: int = Field(100, ge=1, le=300)
    github_search_deadline_seconds: float = Field(120, ge=1, le=600)
    github_search_max_attempts: int = Field(3, ge=1, le=3)
    github_search_max_wait_seconds: float = Field(10, ge=0, le=60)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def search_fingerprint(query, *, per_page, sort=None, order=None, **extra):
    """Identity of the effective GitHub search request. `page` is applied uniformly per pass."""
    params = {'q': query, 'sort': sort, 'order': order, 'per_page': per_page, **extra}
    return json.dumps(params, sort_keys=True)


class SearchExecutor:
    def __init__(self, persistence, github, clock, settings=None, *, timer=monotonic, wait=sleep):
        self.persistence = persistence
        self.manifests = persistence.manifests
        self.github = github
        self.clock = clock
        self.settings = settings or SearchExecutionSettings()
        self.timer = timer
        self.wait = wait

    def execute(self, owner, run_id, confirmed_revision=None):
        with _lock(run_id):
            plan = self.persistence.read_plan(owner, run_id)
            manifest = self.manifests.read(owner, run_id)
            if confirmed_revision is not None:
                confirmed_revision = confirmed_revision.model_copy(deep=True)
                from app.services.candidate_review import _bound
                _bound(self.persistence, owner, manifest['task_id'], run_id, confirmed_revision)
            if 'search_execution' in manifest:
                return self.recover(owner, run_id)
            if manifest['run_status'] not in ('queued', 'running') or manifest['dispatch_closed']:
                raise SearchStateError('Run cannot dispatch search')
            queries = []
            unique = {}
            groups = sorted(plan.groups, key=lambda group: group.group_name)
            for group in groups:
                if len(group.queries) > self.settings.github_search_max_queries_per_group:
                    raise GitHubClientError('Search query budget exceeded')
            for index in range(self.settings.github_search_max_queries_per_group):
                for group in groups:
                    if index < len(group.queries):
                        query = group.queries[index]
                        rendered = render_query(query)
                        identity = search_fingerprint(
                            rendered, per_page=self.settings.github_search_per_page)
                        alias = {'group_id': group.group_id, 'query_id': query.query_id}
                        if identity in unique:
                            unique[identity]['aliases'].append(alias)
                        else:
                            entry = {'group_id': group.group_id, 'query_id': query.query_id,
                                     'query': rendered, 'aliases': [alias]}
                            unique[identity] = entry
                            queries.append(entry)
            budget = self.settings.model_dump()
            self.manifests.start(owner, run_id)
            started_at = _now()
            def begin(m):
                if 'search_execution' in m:
                    raise SearchStateError('Search already dispatched')
                m['search_execution'] = {'version': 'github-dispatch-v1', 'status': 'dispatching',
                                         'started_at': started_at, 'budget': budget, 'attempts': {},
                                         'queries': queries, 'stop_reasons': [], 'readme_targets': []}
            self.manifests.update(owner, run_id, begin)
            deadline = self.timer() + self.settings.github_search_deadline_seconds
            try:
                active = list(queries)
                for page in range(1, self.settings.github_search_max_pages + 1):
                    next_queries = []
                    for query in active:
                        args = dict(query, page=page, per_page=self.settings.github_search_per_page)
                        result = self._dispatch(owner, run_id, 'search', args, deadline)
                        if result is None:
                            continue
                        if result['error_code'] == 'AUTHENTICATION':
                            self._stop(owner, run_id, 'AUTHENTICATION')
                            return self._finish(owner, run_id)
                        if not result['error_code'] and result['has_next']:
                            next_queries.append(query)
                    if not next_queries:
                        break
                    active = next_queries
                    if page == self.settings.github_search_max_pages:
                        self._stop(owner, run_id, 'PAGE_BUDGET')
                manifest = self.manifests.read(owner, run_id)
                candidates = manifest['candidates']
                selected = []
                for query in queries:
                    for repo_id, candidate in candidates.items():
                        if any(hit['group_id'] == query['group_id'] for hit in candidate.get('hits', [])):
                            if repo_id not in selected:
                                selected.append(repo_id)
                                break
                selected.extend(repo_id for repo_id in candidates if repo_id not in selected)
                if confirmed_revision is not None:
                    from app.services.candidate_review import CandidateReview
                    screening = CandidateReview(self.persistence).screen(
                        owner, manifest['task_id'], run_id, confirmed_revision)
                    selected = [row['repository_id'] for row in screening['candidates'] if row['eligible']]
                selected = selected[:self.settings.github_readme_max_candidates]
                self.manifests.update(owner, run_id, lambda m: m['search_execution'].update(readme_targets=selected))
                for repo_id in selected:
                    candidate = self.manifests.read(owner, run_id)['candidates'][repo_id]
                    metadata = candidate['metadata']
                    if not metadata.get('default_branch'):
                        self._candidate_status(owner, run_id, repo_id, 'MISSING_DEFAULT_BRANCH')
                        self._stop(owner, run_id, 'COLLECTION_FAILED')
                        continue
                    args = {'repository_id': int(repo_id), 'full_name': metadata['full_name'],
                            'ref': metadata['default_branch']}
                    commit = self._dispatch(owner, run_id, 'commit', args, deadline)
                    if commit is None or commit['error_code']:
                        continue
                    args = {'repository_id': int(repo_id), 'full_name': metadata['full_name'],
                            'commit_sha': commit['data']['sha']}
                    self._dispatch(owner, run_id, 'readme', args, deadline)
                return self._finish(owner, run_id)
            except SearchIntegrityError:
                self.manifests.fail(owner, run_id, 'SEARCH_INTEGRITY', 'integrity')
                raise

    def _stop(self, owner, run_id, reason):
        def apply(m):
            reasons = m['search_execution']['stop_reasons']
            if reason not in reasons:
                reasons.append(reason)
        self.manifests.update(owner, run_id, apply)

    def _candidate_status(self, owner, run_id, repo_id, status):
        def apply(m):
            candidate = m['candidates'][str(repo_id)]
            if candidate.get('collection_status') != 'completed':
                candidate['collection_status'] = status
        self.manifests.update(owner, run_id, apply)

    def _dispatch(self, owner, run_id, kind, args, deadline):
        for number in range(self.settings.github_search_max_attempts):
            manifest = self.manifests.read(owner, run_id)
            stopped = set(manifest['search_execution']['stop_reasons']) & {'RATE_LIMIT', 'AUTHENTICATION'}
            if stopped:
                if kind != 'search':
                    self._candidate_status(owner, run_id, args['repository_id'], sorted(stopped)[0])
                return None
            remaining = deadline - self.timer()
            if remaining <= 0 or len(manifest['search_execution']['attempts']) >= self.settings.github_search_max_requests:
                reason = 'DEADLINE' if remaining <= 0 else 'REQUEST_BUDGET'
                self._stop(owner, run_id, reason)
                if kind != 'search':
                    self._candidate_status(owner, run_id, args['repository_id'], reason)
                return None
            request_id = str(uuid4())
            issued = self.persistence.reserve_request(owner, run_id, request_id, self.clock)
            key = f'github-responses/{request_id}.json'
            attempt = {'kind': kind, 'args': args, 'issued_at': issued.isoformat(),
                       'response_key': key, 'status': 'dispatched', 'attempt_number': number + 1}
            self.manifests.update(owner, run_id, lambda m: m['search_execution']['attempts'].update({request_id: deepcopy(attempt)}))
            start = self.timer()
            if start >= deadline:
                self.manifests.update(owner, run_id, lambda m: m['search_execution']['attempts'][request_id].update(status='not_sent'))
                self._stop(owner, run_id, 'DEADLINE')
                if kind != 'search':
                    self._candidate_status(owner, run_id, args['repository_id'], 'DEADLINE')
                return None
            timeout = min(10.0, deadline - start)
            if kind == 'search':
                result = self.github.search(args['query'], args['page'], args['per_page'], timeout=timeout)
            elif kind == 'commit':
                result = self.github.commit(args['full_name'], args['ref'], timeout=timeout)
            else:
                result = self.github.readme(args['full_name'], args['commit_sha'], timeout=timeout)
            payload = {'owner': owner, 'run_id': run_id, 'request_id': request_id, 'kind': kind, 'args': args, 'issued_at': issued.isoformat(),
                       'result': result, 'elapsed_seconds': max(0.0, self.timer() - start)}
            raw = _bytes(payload)
            self.persistence._put_immutable(key, raw, 'application/json')
            self._attach_response(owner, run_id, request_id, raw, payload)
            if result['error_code'] is None:
                self._apply(owner, run_id, request_id, payload)
                return result
            if not result['retryable'] or number + 1 == self.settings.github_search_max_attempts:
                self._apply(owner, run_id, request_id, payload)
                return result
            delay = max(result['retry_after'], float(2 ** number))
            if delay > self.settings.github_search_max_wait_seconds or self.timer() + delay >= deadline:
                self._apply(owner, run_id, request_id, payload)
                self._stop(owner, run_id, 'RETRY_BUDGET')
                return result
            self.manifests.update(owner, run_id, lambda m: m['search_execution']['attempts'][request_id].update(status='retry_wait'))
            self.wait(delay)
        return None

    def _attach_response(self, owner, run_id, request_id, raw, payload):
        digest = hashlib.sha256(raw).hexdigest()
        def apply(m):
            attempt = m['search_execution']['attempts'][request_id]
            if attempt.get('response_hash', digest) != digest:
                raise SearchIntegrityError('Stored response identity differs')
            attempt.update(response_hash=digest, status='received',
                           error_code=payload['result']['error_code'],
                           rate_limit=payload['result']['rate_limit'],
                           elapsed_seconds=payload['elapsed_seconds'])
        self.manifests.update(owner, run_id, apply)

    def _apply(self, owner, run_id, request_id, payload):
        result, args, kind = payload['result'], payload['args'], payload['kind']
        manifest = self.manifests.read(owner, run_id)
        if manifest['search_execution']['attempts'][request_id]['status'] == 'applied':
            return
        if result['error_code']:
            if result['error_code'] in ('RATE_LIMITED', 'SECONDARY_RATE_LIMIT'):
                self._stop(owner, run_id, 'RATE_LIMIT')
            if result['error_code'] == 'AUTHENTICATION':
                self._stop(owner, run_id, 'AUTHENTICATION')
            if kind != 'search':
                code = 'NO_README' if kind == 'readme' and result['error_code'] == 'NOT_FOUND' else result['error_code']
                self._candidate_status(owner, run_id, args['repository_id'], code)
            self._stop(owner, run_id, 'QUERY_FAILED' if kind == 'search' else 'COLLECTION_FAILED')
        elif kind == 'search':
            limit = manifest['search_execution']['budget']['github_search_max_candidates']
            issued = datetime.fromisoformat(payload['issued_at'])
            items = result['data']['items']
            for position, item in enumerate(items, 1):
                repo_id, patch = SearchRepository.github_patch(item)
                current = self.manifests.read(owner, run_id)
                if str(repo_id) not in current['candidates'] and len(current['candidates']) >= limit:
                    self._stop(owner, run_id, 'CANDIDATE_BUDGET')
                    continue
                self.persistence.repository.update_repository(repo_id, patch, issued)
                aliases = args.get('aliases') or [{'group_id': args['group_id'], 'query_id': args['query_id']}]
                for alias in aliases:
                    hit = {'group_id': alias['group_id'], 'query_id': alias['query_id'],
                           'query': args['query'], 'page': args['page'], 'position': position,
                           'request_id': request_id}
                    def merge(m, hit=hit):
                        candidate = m['candidates'].setdefault(str(repo_id), {'hits': [], 'collection_status': 'not_selected'})
                        if hit not in candidate['hits']:
                            candidate['hits'].append(hit)
                        if candidate.get('metadata_fetched_at', '') <= payload['issued_at']:
                            candidate.setdefault('metadata', {}).update(deepcopy(patch))
                            candidate['metadata_fetched_at'] = payload['issued_at']
                    self.manifests.update(owner, run_id, merge)
            if result['data']['incomplete_results']:
                self._stop(owner, run_id, 'INCOMPLETE_RESULTS')
        elif kind == 'commit':
            def remember(m):
                m['candidates'][str(args['repository_id'])]['commit_sha'] = result['data']['sha']
                m['candidates'][str(args['repository_id'])]['collection_status'] = 'commit_resolved'
            self.manifests.update(owner, run_id, remember)
        else:
            data = result['data']
            raw = base64.b64decode(data['content'].replace('\r', '').replace('\n', ''), validate=True)
            self.persistence.collect_readme(owner, run_id, args['repository_id'], args['commit_sha'],
                                            data['path'], data['sha'], raw)
        self.manifests.update(owner, run_id, lambda m: m['search_execution']['attempts'][request_id].update(status='applied'))

    def recover(self, owner, run_id):
        with _lock(run_id):
            self.persistence.read_plan(owner, run_id)
            manifest = self.manifests.read(owner, run_id)
            execution = manifest.get('search_execution')
            if execution is None:
                raise SearchStateError('No search execution to recover')
            if execution['status'] != 'dispatching':
                return manifest
            try:
                attempts = sorted(execution['attempts'].items(), key=lambda item: item[1]['issued_at'])
                for request_id, attempt in attempts:
                    if attempt['status'] in ('applied', 'retry_wait', 'not_sent'):
                        continue
                    if attempt['response_key'] != f'github-responses/{request_id}.json':
                        raise SearchIntegrityError('Invalid response object key')
                    raw, _ = self.persistence.storage.read_version(attempt['response_key'])
                    if raw is None:
                        self._stop(owner, run_id, 'REQUEST_INTERRUPTED')
                        if attempt['kind'] != 'search':
                            self._candidate_status(owner, run_id, attempt['args']['repository_id'], 'INTERRUPTED')
                        continue
                    if 'response_hash' in attempt and hashlib.sha256(raw).hexdigest() != attempt['response_hash']:
                        raise SearchIntegrityError('Durable response hash differs')
                    try:
                        payload = json.loads(raw)
                        if (payload['owner'] != owner or payload['run_id'] != run_id or payload['request_id'] != request_id
                                or any(payload[k] != attempt[k] for k in ('kind', 'args', 'issued_at'))):
                            raise ValueError
                    except (ValueError, KeyError, TypeError, UnicodeError):
                        raise SearchIntegrityError('Durable response identity differs') from None
                    self._attach_response(owner, run_id, request_id, raw, payload)
                    self._apply(owner, run_id, request_id, payload)
                self.persistence.recover_readmes(owner, run_id, max_operations=100)
                self._stop(owner, run_id, 'DISPATCH_INTERRUPTED')
                return self._finish(owner, run_id)
            except SearchIntegrityError:
                self.manifests.fail(owner, run_id, 'SEARCH_INTEGRITY', 'integrity')
                raise

    def _finish(self, owner, run_id):
        manifest = self.manifests.read(owner, run_id)
        execution = manifest['search_execution']
        partial = bool(execution['stop_reasons'])
        finished_at = _now()
        def finish(m):
            if m['integrity_blocked']:
                raise SearchIntegrityError('Run is blocked by an integrity failure')
            allowed = ('completed', 'interrupted') if partial else ('completed',)
            if any(op['operation_status'] not in allowed for op in m['readme_persistence_operations'].values()):
                raise SearchStateError('Persistence operations are unfinished')
            for candidate in m['candidates'].values():
                if candidate.get('collection_status') == 'commit_resolved':
                    candidate['collection_status'] = 'INTERRUPTED'
            m['search_execution'].update(status='partial' if partial else 'completed', finished_at=finished_at)
            m['dispatch_closed'] = True
            if m['run_status'] == 'running':
                m['run_status'] = 'failed' if partial else 'completed'
            if partial:
                m['error_code'] = 'SEARCH_PARTIAL'
                m['error_class'] = 'terminal'
        return self.manifests.update(owner, run_id, finish)
