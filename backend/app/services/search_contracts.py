"""Callers must authorize the task; source pointers do not prove semantic fidelity."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from uuid import UUID

from app.schemas.requirements import RequirementRevision

HASH_ENCODING_VERSION = 'pp-hash-v1'
QUERY_HASH_ALGORITHM = 'projectpilot-query-v1'
SNAPSHOT_KEY_ALGORITHM = 'projectpilot-snapshot-v1'
QUALIFIER_KEYS = frozenset({'in', 'stars', 'forks', 'license', 'archived', 'topic'})
SCALAR_SOURCE_FIELDS = frozenset({
    'project_topic', 'project_description', 'project_type', 'domain',
})
LIST_SOURCE_FIELDS = frozenset({
    'required_features', 'preferred_features', 'programming_languages',
    'required_technologies', 'preferred_technologies',
})
_ASCII_WHITESPACE = ' \t\n\r\v\f'


class SearchContractError(ValueError):
    """Invalid input to a deterministic search contract."""


def _utf8(value: str) -> bytes:
    if type(value) is not str:
        raise SearchContractError('Expected a string')
    try:
        return value.encode('utf-8', errors='strict')
    except UnicodeEncodeError as exc:
        raise SearchContractError('Invalid Unicode scalar value') from exc


def encode_hash_value(value: object) -> bytes:
    """E(null/string/list), with UTF-8 byte lengths and no JSON conversion."""
    if value is None:
        return b'n'
    if type(value) is str:
        data = _utf8(value)
        return b's' + str(len(data)).encode('ascii') + b':' + data
    if type(value) is list:
        return (b'a' + str(len(value)).encode('ascii') + b':'
                + b''.join(encode_hash_value(item) for item in value))
    raise SearchContractError('E accepts only null, strings, and lists')


def _nonblank(value: str) -> str:
    _utf8(value)
    if not value or not value.strip():
        raise SearchContractError('Expected a nonblank string')
    return value


def _query_string(value: str) -> str:
    _nonblank(value)
    if value[0] in _ASCII_WHITESPACE or value[-1] in _ASCII_WHITESPACE:
        raise SearchContractError('Leading/trailing ASCII whitespace is forbidden')
    if any(unicodedata.category(char) == 'Cc' for char in value):
        raise SearchContractError('Control characters are forbidden')
    return value


def canonicalize_query(
    query_text: str, *, language_filter: str | None = None,
    qualifiers: dict[str, list[str]],
) -> dict:
    """Wire normalization only; the query compiler must validate GitHub semantics."""
    _query_string(query_text)
    if language_filter is not None:
        _query_string(language_filter)
    if type(qualifiers) is not dict:
        raise SearchContractError('qualifiers must be an object, never null')
    for key, values in qualifiers.items():
        if type(key) is not str or key not in QUALIFIER_KEYS:
            raise SearchContractError('Unsupported qualifier key')
        if type(values) is not list or not values:
            raise SearchContractError('Qualifier values must be a nonempty list')
        for value in values:
            _query_string(value)
    canonical = {
        key: sorted(set(qualifiers[key]), key=_utf8)
        for key in sorted(qualifiers, key=lambda key: key.encode('ascii'))
    }
    return {'query_text': query_text, 'language_filter': language_filter,
            'qualifiers': canonical}


def query_hash(
    query_text: str, *, language_filter: str | None = None,
    qualifiers: dict[str, list[str]],
) -> str:
    """SHA-256 of the v1 canonical query payload (not JSON)."""
    query = canonicalize_query(query_text, language_filter=language_filter,
                               qualifiers=qualifiers)
    pairs = [[key, values] for key, values in query['qualifiers'].items()]
    payload = [QUERY_HASH_ALGORITHM, query['query_text'],
               query['language_filter'], pairs]
    return hashlib.sha256(encode_hash_value(payload)).hexdigest()


def snapshot_key(
    repository_id: int | str, commit_sha: str, readme_path: str, blob_sha: str,
) -> str:
    """Hash exact snapshot identity; IDs never pass through floating point."""
    if type(repository_id) is int:
        if not 1 <= repository_id <= 2**64 - 1:
            raise SearchContractError('Repository ID is outside uint64 range')
        decimal = str(repository_id)
    elif type(repository_id) is str:
        if not re.fullmatch(r'[1-9][0-9]{0,19}', repository_id):
            raise SearchContractError('Repository ID must be canonical decimal')
        if int(repository_id) > 2**64 - 1:
            raise SearchContractError('Repository ID is outside uint64 range')
        decimal = repository_id
    else:
        raise SearchContractError('Repository ID must be an exact integer or string')
    for sha in (commit_sha, blob_sha):
        if type(sha) is not str or not re.fullmatch(r'[0-9a-fA-F]{40}', sha):
            raise SearchContractError('Expected a 40-character hexadecimal SHA')
    _utf8(readme_path)
    if not readme_path:
        raise SearchContractError('README path must not be empty')
    payload = [SNAPSHOT_KEY_ALGORITHM, decimal, commit_sha.lower(),
               readme_path, blob_sha.lower()]
    return hashlib.sha256(encode_hash_value(payload)).hexdigest()


def _uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    if type(value) is not str:
        raise SearchContractError('Expected a UUID')
    try:
        return UUID(value)
    except ValueError as exc:
        raise SearchContractError('Expected a UUID') from exc


def _bind(revision: RequirementRevision, task_id: UUID | str,
          confirmed_revision_id: UUID | str) -> None:
    if not isinstance(revision, RequirementRevision):
        raise SearchContractError('Expected a RequirementRevision')
    if (revision.task_id != _uuid(task_id)
            or revision.revision_id != _uuid(confirmed_revision_id)
            or revision.status != 'confirmed'):
        raise SearchContractError('Revision is not the bound confirmed task revision')


def _resolve(revision: RequirementRevision, pointer: str) -> str:
    _utf8(pointer)
    if not pointer.startswith('/') or re.search(r'~(?![01])', pointer):
        raise SearchContractError('Invalid RFC 6901 pointer')
    tokens = [token.replace('~1', '/').replace('~0', '~')
              for token in pointer[1:].split('/')]
    field = tokens[0]
    if field in SCALAR_SOURCE_FIELDS and len(tokens) == 1:
        value = getattr(revision.requirements, field)
    elif field in LIST_SOURCE_FIELDS and len(tokens) == 2:
        index = tokens[1]
        if not re.fullmatch(r'0|[1-9][0-9]*', index):
            raise SearchContractError('Noncanonical array index')
        values = getattr(revision.requirements, field)
        # Compare decimal lengths before int conversion (also bounds huge inputs).
        if (type(values) is not list or not values
                or len(index) > len(str(len(values) - 1))
                or int(index) >= len(values)):
            raise SearchContractError('Array index does not exist')
        value = values[int(index)]
    else:
        raise SearchContractError('Pointer is not an allowed source path')
    return _nonblank(value)


def resolve_requirement_pointer(
    revision: RequirementRevision, pointer: str, *, task_id: UUID | str,
    confirmed_revision_id: UUID | str,
) -> str:
    """Resolve only an exact business string in the caller-authorized revision."""
    _bind(revision, task_id, confirmed_revision_id)
    return _resolve(revision, pointer)


def validate_source_requirement_pointers(
    revision: RequirementRevision, pointers: list[str], *, task_id: UUID | str,
    confirmed_revision_id: UUID | str, group_status: str = 'ready',
    group_pointers: list[str] | None = None,
) -> list[str]:
    """Passing group_pointers validates query sources against their ready group."""
    _bind(revision, task_id, confirmed_revision_id)
    if group_status not in ('ready', 'skipped'):
        raise SearchContractError('Invalid group status')
    if type(pointers) is not list:
        raise SearchContractError('Sources must be a list')
    result = []
    for pointer in pointers:
        _resolve(revision, pointer)
        if pointer not in result:
            result.append(pointer)
    if group_status == 'ready' and not result:
        raise SearchContractError('Ready sources must not be empty')
    if group_pointers is not None:
        if group_status != 'ready':
            raise SearchContractError('Skipped groups cannot have queries')
        parents = validate_source_requirement_pointers(
            revision, group_pointers, task_id=task_id,
            confirmed_revision_id=confirmed_revision_id)
        if not set(result).issubset(parents):
            raise SearchContractError('Query sources must be a subset of group sources')
    return result


DEEP_CHECK_VERSION = 'deep-check-v1'
DEEP_CHECK_HARD = frozenset({'required', 'prohibited'})
DEEP_CHECK_LABELS = ('verify_further', 'partial_reference', 'insufficient', 'not_recommended')
DEEP_CHECK_REASONS = frozenset({
    'hard_requirement_conflict', 'all_hard_requirements_supported', 'partial_hard_support',
    'no_hard_requirement_evidence', 'no_hard_requirement', 'snapshot_unavailable', 'input_truncated'})
DEEP_CHECK_FINDINGS = frozenset({'insufficient', 'contradicts', 'prohibited_violation', 'preferred_conflict'})


def deep_check_hard_results(items: list[dict]) -> list[str]:
    """ALL fails on any conflict, ANY passes on any support; anything else is unknown."""
    groups: dict[str, list[dict]] = {}
    for item in items:
        if item['modality'] not in DEEP_CHECK_HARD:
            continue
        groups.setdefault(item['logic_group_id'] or '#' + item['requirement_item_id'], []).append(item)
    results = []
    for members in groups.values():
        verdicts = {member['verdict'] for member in members}
        every = len(members) == 1 or members[0]['operator'] != 'ANY'
        if every:
            results.append('contradicts' if 'contradicts' in verdicts
                           else 'supports' if verdicts == {'supports'} else 'insufficient')
        else:
            results.append('supports' if 'supports' in verdicts
                           else 'contradicts' if verdicts == {'contradicts'} else 'insufficient')
    return results


def deep_check_recommendation(items: list[dict], *, availability: str, input_coverage: str) -> dict:
    """Label candidate review priority; this is never a claim that requirements are met."""
    if availability != 'available' or input_coverage == 'not_fed':
        return {'label': 'insufficient', 'reason': 'snapshot_unavailable'}
    results = deep_check_hard_results(items)
    if not results:
        return {'label': 'insufficient', 'reason': 'no_hard_requirement'}
    if 'contradicts' in results:
        label, reason = 'not_recommended', 'hard_requirement_conflict'
    elif all(value == 'supports' for value in results):
        label, reason = 'verify_further', 'all_hard_requirements_supported'
    elif 'supports' in results:
        label, reason = 'partial_reference', 'partial_hard_support'
    else:
        label, reason = 'insufficient', 'no_hard_requirement_evidence'
    # Incomplete input coverage can prove a conflict but never prove completeness.
    if input_coverage == 'truncated' and label != 'not_recommended':
        return {'label': 'insufficient', 'reason': 'input_truncated'}
    return {'label': label, 'reason': reason}


def deep_check_findings(items: list[dict]) -> dict:
    missing = [{'requirement_item_id': item['requirement_item_id'], 'text': item['text'],
                'modality': item['modality'],
                'reason': 'contradicts' if item['verdict'] == 'contradicts' else 'insufficient'}
               for item in items if item['modality'] == 'required' and item['verdict'] != 'supports']
    risks = [{'requirement_item_id': item['requirement_item_id'], 'text': item['text'],
              'modality': item['modality'],
              'reason': 'prohibited_violation' if item['modality'] == 'prohibited' else 'preferred_conflict'}
             for item in items if item['modality'] in ('prohibited', 'preferred')
             and item['verdict'] == 'contradicts']
    return {'missing_features': missing, 'risk_notes': risks}


def deep_check_sort_key(row: dict) -> tuple:
    supports = sum(1 for item in row['items']
                   if item['modality'] == 'required' and item['verdict'] == 'supports')
    return (DEEP_CHECK_LABELS.index(row['recommendation']['label']), -supports, int(row['repository_id']))
