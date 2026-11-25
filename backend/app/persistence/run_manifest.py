"""Callers authorize owners; pure transitions replay only after explicit CAS conflicts."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
import re
from uuid import UUID, uuid5

from app.schemas.search import SearchIntegrityError, SearchStateError
from app.services.search_contracts import (
    DEEP_CHECK_VERSION, SNAPSHOT_KEY_ALGORITHM, deep_check_findings, deep_check_recommendation,
    deep_check_sort_key, snapshot_key)
from app.services.storage import CASConflict, StorageError

_STAGES = ('prepared', 'staged', 'object_verified', 'snapshot_committed', 'completed')
_TERMINALS = ('interrupted', 'integrity_failed')
_IDENTITIES = ('owner', 'run_id', 'task_id', 'confirmed_revision_id')
_OP_IDENTITY = ('operation_id', 'repository_id', 'commit_sha', 'readme_path',
                'blob_sha', 'content_hash', 'snapshot_key', 'storage_key',
                'staging_key', 'byte_length', 'created_at', 'collected_at',
                'snapshot_key_algorithm')
_ERRORS = ('integrity', 'terminal', 'transient', 'outcome_unknown')
_DEEP_KEYS = {'operation_id', 'version', 'revision_hash', 'plan_hash', 'status', 'model_alias',
              'prompt_version', 'input_hash', 'prompt_hash', 'config_hash', 'response_key',
              'response_hash', 'report_key', 'report_hash', 'report', 'usage', 'provider_usage',
              'published', 'error_code', 'created_at', 'updated_at'}
_DEEP_STATUSES = ('dispatched', 'recorded', 'completed', 'failed')
_DEEP_ADVANCE = {'dispatched': ('recorded', 'completed', 'failed'),
                 'recorded': ('completed', 'failed'), 'completed': (), 'failed': ()}
_DEEP_MUTABLE = ('status', 'updated_at', 'error_code', 'response_hash', 'provider_usage',
                 'report', 'report_hash', 'usage', 'published')
_DEEP_BASIS = ('model', 'no_response', 'citation_rejected', 'not_fed', 'snapshot_unavailable')
_DEEP_ITEM_FIELDS = ('requirement_item_id', 'text', 'modality', 'operator', 'logic_group_id')
_DEEP_TOKENS = frozenset({'prompt_tokens', 'completion_tokens', 'total_tokens',
                          'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'})
_DEEP_REJECTIONS = ('out_of_range', 'too_long', 'too_many', 'duplicated', 'no_valid_citation')
_DEEP_ANOMALIES = ('unknown_candidate', 'duplicate_candidate', 'unknown_item', 'duplicate_item')
_DEEP_MODALITIES = ('required', 'preferred', 'prohibited', 'informational')
_DEEP_REVIEW_KEYS = {'operation_id', 'status', 'revision_id', 'revision_hash', 'bundle_hash',
                     'model', 'prompt_version', 'consent', 'authorized_at', 'deadline',
                     'report_key', 'report_hash', 'error_code'}
_DEEP_REVIEW_STATUSES = ('running', 'completed', 'partial', 'failed', 'unknown')
_DEEP_REVIEW_TERMINALS = ('completed', 'partial', 'failed', 'unknown')
_DEEP_REVIEW_IMMUTABLE = ('operation_id', 'revision_id', 'revision_hash', 'bundle_hash',
                          'model', 'prompt_version', 'consent', 'authorized_at', 'deadline')


def _require(condition, message, error=SearchIntegrityError):
    if not condition:
        raise error(message)


def _uuid(value):
    try:
        _require(type(value) is str and str(UUID(value)) == value, 'Noncanonical UUID')
    except (ValueError, TypeError, AttributeError) as exc:
        raise SearchIntegrityError('Noncanonical UUID') from exc


def _hex(value, length=64):
    _require(type(value) is str and re.fullmatch(r'[0-9a-f]{%d}' % length, value),
             'Invalid lowercase hexadecimal identity')


def _time(value):
    _require(type(value) is str, 'Timestamp must be an ISO string')
    try:
        dt = datetime.fromisoformat(value)
        _require(dt.utcoffset() is not None and dt.utcoffset().total_seconds() == 0,
                 'Timestamp must be UTC')
        return dt
    except (ValueError, TypeError) as exc:
        raise SearchIntegrityError('Invalid UTC timestamp') from exc


def _encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SearchIntegrityError('Manifest is not strict JSON') from exc


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, 'Duplicate JSON key')
        result[key] = value
    return result


def _plan(ref, revision):
    _require(type(ref) is dict and set(ref) == {
        'plan_id', 'plan_storage_key', 'plan_hash', 'confirmed_revision_id'},
        'Invalid plan reference')
    _uuid(ref['plan_id'])
    _hex(ref['plan_hash'])
    _require(ref['confirmed_revision_id'] == revision, 'Plan revision mismatch')
    _require(ref['plan_storage_key'] == f"search-plans/{ref['plan_id']}.json",
             'Invalid plan object key')


def _operation(op):
    _require(type(op) is dict and set(_OP_IDENTITY).union({
        'snapshot_id', 'operation_status', 'updated_at'}).issubset(op),
        'Incomplete operation')
    _uuid(op['operation_id'])
    _uuid(op['snapshot_id'])
    for field in ('content_hash', 'snapshot_key'):
        _hex(op[field])
    for field in ('commit_sha', 'blob_sha'):
        _hex(op[field], 40)
    _require(op['snapshot_key_algorithm'] == SNAPSHOT_KEY_ALGORITHM,
             'Unsupported snapshot algorithm')
    try:
        expected = snapshot_key(op['repository_id'], op['commit_sha'],
                                op['readme_path'], op['blob_sha'])
    except ValueError as exc:
        raise SearchIntegrityError('Invalid snapshot identity') from exc
    _require(op['snapshot_key'] == expected, 'Snapshot identity hash mismatch')
    _require(op['storage_key'] == f'repository-snapshots/{expected}/readme.raw',
             'Invalid snapshot object key')
    _require(op['staging_key'] == f"staging/readmes/{op['operation_id']}.raw",
             'Invalid staging object key')
    _require(type(op['byte_length']) is int and op['byte_length'] >= 0,
             'Invalid byte length')
    for field in ('created_at', 'updated_at', 'collected_at'):
        _time(op[field])
    _require(op['operation_status'] in _STAGES + _TERMINALS, 'Invalid operation status')
    history = op.get('transitions', {})
    _require(type(history) is dict, 'Invalid transition history')
    ids = set()
    for stage, fact in history.items():
        _require(stage in _STAGES[1:] and type(fact) is dict and
                 set(fact) == {'transition_id', 'snapshot_id'}, 'Invalid transition record')
        _require(type(fact['transition_id']) is str and bool(fact['transition_id']),
                 'Missing transition ID')
        _require(fact['transition_id'] not in ids, 'Reused transition ID')
        ids.add(fact['transition_id'])
        _uuid(fact['snapshot_id'])
    rank = (_STAGES.index(op['operation_status']) if op['operation_status'] in _STAGES
            else len(history))
    _require(set(history) == set(_STAGES[1:rank + 1]), 'Incomplete transition history')
    _require(op['operation_status'] not in _TERMINALS or rank < 4,
             'Completed facts cannot become a failure terminal')
    if history:
        _require(op.get('transition_id') == history[_STAGES[rank]]['transition_id'],
                 'Latest transition ID mismatch')
    else:
        _require(op.get('transition_id') is None, 'Prepared operation has a transition ID')
    if 'object_verified' in history:
        _require(history['staged']['snapshot_id'] == history['object_verified']['snapshot_id'],
                 'Pre-commit snapshot identity changed')
    if 'snapshot_committed' not in history and history:
        _require(history['staged']['snapshot_id'] == op['snapshot_id'],
                 'Pre-commit snapshot identity mismatch')
    if 'snapshot_committed' in history:
        _require(history['snapshot_committed']['snapshot_id'] == op['snapshot_id'],
                 'Committed snapshot identity mismatch')
    if 'completed' in history:
        _require(history['completed']['snapshot_id'] == op['snapshot_id'],
                 'Completed snapshot identity mismatch')


def _deep(m):
    """Recompute the whole deep check contract; stored facts must re-derive exactly."""
    op = m['deep_check_operation']
    _require(type(op) is dict and set(op) == _DEEP_KEYS, 'Invalid deep check fields')
    _uuid(op['operation_id'])
    _require(op['operation_id'] == str(uuid5(UUID(m['run_id']), DEEP_CHECK_VERSION)),
             'Invalid deep check operation ID')
    _require(op['version'] == op['prompt_version'] == DEEP_CHECK_VERSION, 'Invalid deep check version')
    for field in ('revision_hash', 'plan_hash', 'input_hash', 'prompt_hash', 'config_hash'):
        _hex(op[field])
    for field in ('response_hash', 'report_hash'):
        if op[field] is not None:
            _hex(op[field])
    _require(op['response_key'] == f"deep-check-responses/{op['operation_id']}.json" and
             op['report_key'] == f"deep-check-reports/{op['operation_id']}.json",
             'Invalid deep check object keys')
    _require(op['status'] in _DEEP_STATUSES and type(op['published']) is bool, 'Invalid deep check state')
    _require(type(op['model_alias']) is str and 0 < len(op['model_alias']) <= 128,
             'Invalid deep check model alias')
    for field in ('created_at', 'updated_at'):
        _time(op[field])
    _require(m['plan_publication_status'] == 'published' and op['plan_hash'] == m['plan']['plan_hash'] and
             m['dispatch_closed'] and all(row['operation_status'] in ('completed', 'interrupted')
             for row in m['readme_persistence_operations'].values()), 'Unstable deep check sources')
    if 'planner_operation' in m:
        _require(op['revision_hash'] == m['planner_operation']['revision_hash'],
                 'Deep check revision mismatch')
    for field in ('candidate_screening', 'evidence_operation'):
        if field in m:
            _require(op['revision_hash'] == m[field]['revision_hash'], 'Deep check revision mismatch')
    completed = {row['snapshot_id'] for row in m['readme_persistence_operations'].values()
                 if row['operation_status'] == 'completed'}
    _require(type(op['usage']) is dict and set(op['usage']) <= completed and
             all(type(value) is bool for value in op['usage'].values()), 'Invalid deep check usage')
    _require(type(op['provider_usage']) is dict and
             all(key in _DEEP_TOKENS and type(value) is int and 0 <= value <= 2**63 - 1
                 for key, value in op['provider_usage'].items()), 'Invalid deep check token usage')
    if op['status'] == 'dispatched':
        _require(op['response_hash'] is None and op['report'] is None and op['report_hash'] is None and
                 not op['provider_usage'] and not op['published'] and not any(op['usage'].values()) and
                 op['error_code'] is None, 'Deep check facts arrived too early')
    elif op['status'] == 'recorded':
        _require(op['response_hash'] is not None and op['report'] is None and
                 not op['published'] and not any(op['usage'].values()) and
                 op['error_code'] is None, 'Deep check facts arrived too early')
    elif op['status'] == 'failed':
        _require(op['report'] is None and op['report_hash'] is None and not op['published'] and
                 not any(op['usage'].values()) and type(op['error_code']) is str and op['error_code'],
                 'Failed deep check has facts')
    else:
        _require(op['report_hash'] is not None and type(op['report']) is dict and
                 op['error_code'] is None, 'Incomplete deep check report')
    report = op['report']
    if report is None:
        return
    _require(type(report) is dict and set(report) == set(_IDENTITIES).union({
        'version', 'warning', 'requirement_items', 'candidates', 'anomalies'}) and
        report['version'] == DEEP_CHECK_VERSION and
        all(report.get(key) == m[key] for key in _IDENTITIES) and
        type(report['warning']) is str and bool(report['warning']) and
        hashlib.sha256(_encode(report)).hexdigest() == op['report_hash'], 'Deep check report identity differs')
    items = report['requirement_items']
    _require(type(items) is list and bool(items), 'Invalid deep check requirements')
    seen = set()
    for item in items:
        _require(type(item) is dict and set(item) == set(_DEEP_ITEM_FIELDS) and
                 type(item['text']) is str and bool(item['text']) and
                 item['modality'] in _DEEP_MODALITIES and
                 item['operator'] in ('ALL', 'ANY', 'not_applicable'), 'Invalid deep check requirement')
        _uuid(item['requirement_item_id'])
        _require(item['requirement_item_id'] not in seen, 'Duplicate deep check requirement')
        seen.add(item['requirement_item_id'])
    rows = report['candidates']
    _require(type(rows) is list and len(rows) == len(m['candidates']) and
             {row['repository_id'] for row in rows} == set(m['candidates']),
             'Deep check candidate coverage differs')
    fed = set()
    for rank, row in enumerate(rows):
        _require(type(row) is dict and set(row) == {'repository_id', 'rank', 'availability',
                 'input_coverage', 'input_bytes', 'source', 'recommendation', 'missing_features',
                 'risk_notes', 'items'}, 'Invalid deep check candidate')
        _require(row['rank'] == rank and type(row['input_bytes']) is int and row['input_bytes'] >= 0,
                 'Invalid deep check ranking')
        _require(row['availability'] in ('available', 'no_snapshot', 'non_utf8') and
                 row['input_coverage'] in ('full', 'truncated', 'not_fed'), 'Invalid deep check source state')
        candidate = m['candidates'][row['repository_id']]
        source = row['source']
        _require((source is not None) == (row['availability'] != 'no_snapshot'),
                 'Deep check source availability differs')
        _require((row['input_bytes'] > 0) == (row['input_coverage'] != 'not_fed'),
                 'Deep check input bytes differ')
        _require(row['availability'] == 'available' or row['input_coverage'] == 'not_fed',
                 'Deep check fed an unreadable snapshot')
        if source is None:
            _require(candidate.get('collection_status') != 'completed', 'Missing deep check source')
        else:
            formal = m['readme_persistence_operations'].get(candidate.get('operation_id'), {})
            _require(formal.get('operation_status') == 'completed' and
                     all(source.get(key) == formal.get(key) for key in
                         ('snapshot_id', 'commit_sha', 'readme_path', 'blob_sha', 'content_hash')),
                     'Deep check source differs')
            if row['input_coverage'] != 'not_fed':
                fed.add(source['snapshot_id'])
        _require([{key: item.get(key) for key in _DEEP_ITEM_FIELDS} for item in row['items']] == items,
                 'Deep check item coverage differs')
        for item in row['items']:
            _require(type(item) is dict and
                     set(item) == set(_DEEP_ITEM_FIELDS).union({'verdict', 'basis', 'citations', 'rejections'}) and
                     item['verdict'] in ('supports', 'contradicts', 'insufficient') and
                     item['basis'] in _DEEP_BASIS and type(item['citations']) is list and
                     len(item['citations']) <= 2 and type(item['rejections']) is list and
                     all(reason in _DEEP_REJECTIONS for reason in item['rejections']),
                     'Invalid deep check verdict')
            _require(item['verdict'] == 'insufficient' or (item['citations'] and item['basis'] == 'model'),
                     'Unsupported deep check verdict')
            _require(item['basis'] == 'model' or not item['citations'], 'Deep check citation without a verdict')
            if row['input_coverage'] == 'not_fed':
                _require(item['verdict'] == 'insufficient' and not item['citations'] and
                         item['basis'] == ('not_fed' if row['availability'] == 'available' else 'snapshot_unavailable'),
                         'Deep check verdict without input')
            else:
                _require(item['basis'] in ('model', 'no_response', 'citation_rejected'),
                         'Invalid deep check basis')
            for quote in item['citations']:
                _require(type(quote) is dict and
                         set(quote) == {'text', 'byte_start', 'byte_end', 'line_start', 'line_end'} and
                         type(quote['text']) is str and bool(quote['text']) and
                         all(type(quote[key]) is int for key in
                             ('byte_start', 'byte_end', 'line_start', 'line_end')) and
                         0 <= quote['byte_start'] < quote['byte_end'] and
                         quote['byte_end'] - quote['byte_start'] == len(quote['text'].encode('utf-8')) and
                         1 <= quote['line_start'] <= quote['line_end'], 'Invalid deep check citation')
        _require(row['recommendation'] == deep_check_recommendation(
            row['items'], availability=row['availability'],
            input_coverage=row['input_coverage']), 'Deep check recommendation differs')
        findings = deep_check_findings(row['items'])
        _require(row['missing_features'] == findings['missing_features'] and
                 row['risk_notes'] == findings['risk_notes'], 'Deep check findings differ')
    _require([deep_check_sort_key(row) for row in rows] == sorted(deep_check_sort_key(row) for row in rows),
             'Deep check ranking is not ordered')
    # A recorded provider response exists exactly when the model was actually given content.
    _require((op['response_hash'] is not None) == bool(fed), 'Deep check response coverage differs')
    anomalies = report['anomalies']
    _require(type(anomalies) is list and
             len({(row.get('repository_id'), row.get('code')) for row in anomalies}) == len(anomalies) and
             all(type(row) is dict and set(row) == {'repository_id', 'code'} and
                 type(row['repository_id']) is str and re.fullmatch(r'[1-9][0-9]*', row['repository_id']) and
                 row['code'] in _DEEP_ANOMALIES and
                 (row['code'] == 'unknown_candidate') == (row['repository_id'] not in m['candidates'])
                 for row in anomalies), 'Invalid deep check anomalies')
    _require(set(op['usage']) == fed and
             (not op['published'] or (op['status'] == 'completed' and all(op['usage'].values()))),
             'Invalid deep check publication')


def _deep_review(m):
    op = m['deep_review_operation']
    _require(type(op) is dict and set(op) == _DEEP_REVIEW_KEYS, 'Invalid deep review fields')
    _uuid(op['operation_id'])
    _uuid(op['revision_id'])
    _require(op['revision_id'] == m['confirmed_revision_id'], 'Deep review revision mismatch')
    for field in ('revision_hash', 'bundle_hash'):
        _hex(op[field])
    _require(type(op['model']) is str and 0 < len(op['model']) <= 128, 'Invalid deep review model')
    _require(op['prompt_version'] == 'deep-review-v1' and op['consent'] is True,
             'Invalid deep review authorization')
    authorized, deadline = _time(op['authorized_at']), _time(op['deadline'])
    _require(deadline > authorized, 'Invalid deep review deadline')
    _require(op['status'] in _DEEP_REVIEW_STATUSES, 'Invalid deep review state')
    if op['report_key'] is None:
        _require(op['report_hash'] is None, 'Deep review report hash without key')
    else:
        _require(op['report_key'] == f"deep-reviews/{op['operation_id']}/report.json" and
                 op['report_hash'] is not None, 'Invalid deep review report identity')
        _hex(op['report_hash'])
    if op['error_code'] is not None:
        _require(type(op['error_code']) is str and
                 re.fullmatch(r'[A-Za-z0-9_]{1,128}', op['error_code']), 'Invalid deep review error')
    if op['status'] == 'running':
        _require(op['report_key'] is None and op['error_code'] is None,
                 'Running deep review carries terminal facts')
    elif op['status'] in ('completed', 'partial'):
        _require(op['report_key'] is not None and op['error_code'] is None,
                 'Completed deep review requires a report')
    else:
        _require(op['report_key'] is not None or op['error_code'] is not None,
                 'Failed deep review requires an outcome')
    retrieval = m.get('evidence_bundle_operation')
    _require(retrieval is not None and retrieval['status'] in ('ready', 'partial') and
             m['dispatch_closed'] and not m['integrity_blocked'] and
             m['plan_publication_status'] == 'published' and
             all(row['operation_status'] in ('completed', 'interrupted')
                 for row in m['readme_persistence_operations'].values()),
             'Unstable deep review sources')
    _require(op['bundle_hash'] == retrieval['bundle_hash'] and
             op['revision_hash'] == retrieval['revision_hash'], 'Deep review bundle mismatch')


_CHUNK_VERSION = 'chunk-index-v1'
_CHUNK_STATUSES = ('pending', 'embedding', 'ready', 'failed')
_CHUNK_KEYS = {'operation_id', 'version', 'index_key', 'revision_hash', 'plan_hash', 'model',
               'dimension', 'preprocess_version', 'config_hash', 'status', 'chunk_manifest_key',
               'chunk_manifest_hash', 'usage', 'error_code', 'failed_operation_ids',
               'created_at', 'updated_at'}
_CHUNK_ADVANCE = {'pending': ('embedding', 'ready', 'failed'),
                  'embedding': ('ready', 'failed')}


def _chunk_index(m):
    op = m['chunk_index_operation']
    _require(type(op) is dict and set(op) == _CHUNK_KEYS, 'Invalid chunk index fields')
    _uuid(op['operation_id'])
    _uuid(op['index_key'])
    _require(op['version'] == _CHUNK_VERSION, 'Invalid chunk index version')
    _require(type(op['model']) is str and 0 < len(op['model']) <= 128 and
             type(op['dimension']) is int and op['dimension'] > 0 and
             type(op['preprocess_version']) is str and 0 < len(op['preprocess_version']) <= 64,
             'Invalid chunk index embedding identity')
    for field in ('revision_hash', 'plan_hash', 'config_hash'):
        _hex(op[field])
    if op['chunk_manifest_hash'] is not None:
        _hex(op['chunk_manifest_hash'])
    _require(op['status'] in _CHUNK_STATUSES and
             (op['error_code'] is None or (type(op['error_code']) is str and 0 < len(op['error_code']) <= 64)),
             'Invalid chunk index state')
    _require(m['plan_publication_status'] == 'published' and op['plan_hash'] == m['plan']['plan_hash'] and
             m['dispatch_closed'] and all(row['operation_status'] in ('completed', 'interrupted')
             for row in m['readme_persistence_operations'].values()), 'Unstable chunk index sources')
    for field in ('planner_operation', 'candidate_screening', 'evidence_operation', 'deep_check_operation'):
        if field in m:
            _require(op['revision_hash'] == m[field]['revision_hash'], 'Chunk index revision mismatch')
    completed = {row['snapshot_id'] for row in m['readme_persistence_operations'].values()
                 if row['operation_status'] == 'completed'}
    _require(type(op['usage']) is dict and set(op['usage']) <= completed and
             all(type(value) is bool for value in op['usage'].values()), 'Invalid chunk index usage')
    history = op['failed_operation_ids']
    _require(type(history) is list and len(set(history)) == len(history) and
             op['operation_id'] not in history, 'Invalid chunk index history')
    for value in history:
        _uuid(value)
    for field in ('created_at', 'updated_at'):
        _time(op[field])
    key = f"chunk-index/{op['index_key']}/manifest.json"
    if op['chunk_manifest_key'] is not None:
        _require(op['chunk_manifest_key'] == key, 'Invalid chunk index object key')
    if op['status'] == 'ready':
        _require(op['chunk_manifest_hash'] is not None and op['chunk_manifest_key'] == key and
                 op['error_code'] is None and set(op['usage']) == completed and
                 all(op['usage'].values()), 'Incomplete chunk index readiness')
    elif op['status'] == 'failed':
        _require(op['error_code'] is not None and not any(op['usage'].values()),
                 'Failed chunk index has published facts')
    else:
        _require(op['error_code'] is None, 'Pending chunk index carries an error')


_RETRIEVAL_VERSIONS = ('evidence-bundle-v1', 'evidence-bundle-v2')
_RETRIEVAL_KEYS = {'operation_id', 'version', 'revision_hash', 'plan_hash', 'chunk_index_key',
                   'model', 'dimension', 'config_hash', 'storage_key', 'bundle_hash',
                   'status', 'error_code', 'created_at', 'updated_at'}


def _retrieval(m):
    op = m['evidence_bundle_operation']
    _require(type(op) is dict and set(op) == _RETRIEVAL_KEYS, 'Invalid retrieval bundle fields')
    _uuid(op['operation_id'])
    _uuid(op['chunk_index_key'])
    _require(op['version'] in _RETRIEVAL_VERSIONS, 'Invalid retrieval bundle version')
    _require(type(op['model']) is str and 0 < len(op['model']) <= 128 and
             type(op['dimension']) is int and op['dimension'] > 0, 'Invalid retrieval embedding identity')
    for field in ('revision_hash', 'plan_hash', 'config_hash', 'bundle_hash'):
        _hex(op[field])
    _require(op['storage_key'] == f"retrieval-bundles/{op['operation_id']}.json",
             'Invalid retrieval bundle key')
    _require(op['status'] in ('ready', 'partial', 'failed') and
             (op['error_code'] is None or (type(op['error_code']) is str and 0 < len(op['error_code']) <= 64)),
             'Invalid retrieval bundle state')
    _require((op['status'] == 'failed') == (op['error_code'] is not None),
             'Retrieval bundle error state differs')
    for field in ('created_at', 'updated_at'):
        _time(op[field])
    _require(m['dispatch_closed'] and not m['integrity_blocked'] and
             m['plan_publication_status'] == 'published' and
             all(row['operation_status'] in ('completed', 'interrupted')
                 for row in m['readme_persistence_operations'].values()),
             'Unstable retrieval bundle sources')
    for field in ('planner_operation', 'candidate_screening', 'evidence_operation',
                  'deep_check_operation', 'deep_review_operation', 'chunk_index_operation'):
        if field in m:
            _require(op['revision_hash'] == m[field]['revision_hash'], 'Retrieval revision mismatch')
    if 'chunk_index_operation' in m:
        chunk = m['chunk_index_operation']
        if chunk['status'] == 'ready':
            _require(op['status'] != 'failed' and op['chunk_index_key'] == chunk['index_key'],
                     'Retrieval bundle does not match a ready chunk index')
            _require(op['model'] == chunk['model'] and op['dimension'] == chunk['dimension'],
                     'Retrieval embedding identity differs')
        else:
            _require(op['status'] == 'failed', 'Retrieval bundle has no ready chunk index')


def _validate(m, owner, run_id):
    _require(type(m) is dict and set(_IDENTITIES).union({
        'run_status', 'plan_publication_status', 'plan', 'candidates',
        'readme_persistence_operations', 'requests', 'dispatch_closed',
        'integrity_blocked'}).issubset(m), 'Incomplete manifest')
    _hex(m['owner'])
    for field in _IDENTITIES[1:]:
        _uuid(m[field])
    _require(m['owner'] == owner and m['run_id'] == run_id, 'Manifest ownership mismatch')
    _require(m['run_status'] in ('queued', 'running', 'failed', 'completed'), 'Invalid run status')
    _require(m['plan_publication_status'] in ('preparing', 'published', 'failed'),
             'Invalid publication status')
    for field in ('dispatch_closed', 'integrity_blocked'):
        _require(type(m[field]) is bool, 'Invalid manifest flag')
    for field in ('candidates', 'readme_persistence_operations', 'requests'):
        _require(type(m[field]) is dict, 'Invalid manifest mapping')
    if m['plan'] is not None:
        _plan(m['plan'], m['confirmed_revision_id'])
    _require(m['plan_publication_status'] != 'published' or m['plan'] is not None,
             'Published plan is missing')
    seen = set()
    for key, op in m['readme_persistence_operations'].items():
        _operation(op)
        _require(key == op['operation_id'], 'Operation key mismatch')
        _require(op['snapshot_key'] not in seen, 'Duplicate snapshot operation')
        seen.add(op['snapshot_key'])
        _require(op['operation_status'] != 'integrity_failed' or m['integrity_blocked'],
                 'Integrity failure must block the run')
    for key, issued_at in m['requests'].items():
        _uuid(key)
        _time(issued_at)
    for key, candidate in m['candidates'].items():
        _require(type(key) is str and re.fullmatch(r'[1-9][0-9]*', key) and
                 int(key) <= 2**64 - 1 and type(candidate) is dict, 'Invalid candidate')
        if candidate.get('collection_status') == 'completed':
            op = m['readme_persistence_operations'].get(candidate.get('operation_id'))
            _require(op is not None and op['operation_status'] == 'completed' and
                     str(op['repository_id']) == key and
                     op['snapshot_id'] == candidate.get('snapshot_id'), 'Invalid collected reference')
    for op in m['readme_persistence_operations'].values():
        if op['operation_status'] == 'completed':
            candidate = m['candidates'].get(str(op['repository_id']), {})
            _require(candidate.get('collection_status') == 'completed',
                     'Completed operation has no collected candidate')
    if m['run_status'] == 'completed':
        _require(m['dispatch_closed'] and m['plan_publication_status'] == 'published' and
                 all(op['operation_status'] == 'completed'
                     for op in m['readme_persistence_operations'].values()), 'Premature run completion')
    for field in ('candidate_screening', 'evidence_operation'):
        if field not in m:
            continue
        review = m[field]
        _require(type(review) is dict, 'Invalid review')
        _hex(review.get('revision_hash'))
        _require(m['plan_publication_status'] == 'published' and
                 review.get('plan_hash') == m['plan']['plan_hash'], 'Review plan mismatch')
        if 'planner_operation' in m:
            _require(review['revision_hash'] == m['planner_operation']['revision_hash'], 'Review revision mismatch')
    if 'candidate_screening' in m:
        screening = m['candidate_screening']
        _require(set(screening) == {'version', 'revision_hash', 'plan_hash', 'candidates', 'warning'} and
                 screening['version'] == 'candidate-screening-v1', 'Invalid screening version')
        rows = screening['candidates']
        _require(type(rows) is list and len(rows) == len(m['candidates']) and
                 {row['repository_id'] for row in rows} == set(m['candidates']), 'Screening coverage differs')
        for row in rows:
            _require(set(row) == {'repository_id', 'eligible', 'metadata_mentions', 'priority', 'reason'} and
                     row['eligible'] is True and type(row['metadata_mentions']) is list and
                     type(row['priority']) is int and row['priority'] == len(row['metadata_mentions']) and
                     row['reason'] in ('literal_metadata_mentions', 'unknown_retained'), 'Invalid screening result')
    if 'evidence_operation' in m:
        op = m['evidence_operation']
        _require(set(op) == {'operation_id', 'version', 'revision_hash', 'plan_hash', 'storage_key',
                            'report_hash', 'report', 'usage', 'published'}, 'Invalid evidence operation')
        _uuid(op['operation_id'])
        _hex(op['report_hash'])
        _require(op['version'] == 'evidence-review-v1' and
                 op['storage_key'] == f"evidence-reviews/{op['operation_id']}.json", 'Invalid evidence identity')
        _require(m['dispatch_closed'] and all(value['operation_status'] in ('completed', 'interrupted')
                 for value in m['readme_persistence_operations'].values()), 'Unstable evidence sources')
        if 'candidate_screening' in m:
            _require(op['revision_hash'] == m['candidate_screening']['revision_hash'], 'Review revisions differ')
        report = op['report']
        _require(type(report) is dict and set(report) == set(_IDENTITIES).union({
            'version', 'warning', 'requirement_items', 'candidates'}) and
            report['version'] == 'evidence-review-v1', 'Invalid evidence report schema')
        items = report['requirement_items']
        _require(type(items) is list, 'Invalid evidence requirements')
        item_ids = set()
        for item in items:
            _require(type(item) is dict and set(item) == {'requirement_item_id', 'text', 'modality',
                     'operator', 'logic_group_id'}, 'Invalid evidence requirement')
            _uuid(item['requirement_item_id'])
            _require(item['requirement_item_id'] not in item_ids and type(item['text']) is str and
                     bool(item['text']) and item['modality'] in ('required', 'preferred', 'prohibited', 'informational') and
                     item['operator'] in ('ALL', 'ANY', 'not_applicable'), 'Invalid evidence requirement identity')
            item_ids.add(item['requirement_item_id'])
        _require(type(report) is dict and all(report.get(key) == m[key] for key in _IDENTITIES) and
                 hashlib.sha256(_encode(report)).hexdigest() == op['report_hash'], 'Evidence report identity differs')
        rows = report.get('candidates')
        _require(type(rows) is list and len(rows) == len(m['candidates']) and
                 {row['repository_id'] for row in rows} == set(m['candidates']), 'Evidence candidate coverage differs')
        sources = set()
        for row in rows:
            _require(type(row) is dict and set(row) == {'repository_id', 'availability', 'source', 'items'} and
                     type(row['items']) is list, 'Invalid evidence candidate')
            candidate = m['candidates'][row['repository_id']]
            source = row['source']
            if source is not None:
                formal = m['readme_persistence_operations'].get(candidate.get('operation_id'), {})
                _require(formal.get('operation_status') == 'completed' and
                         all(source.get(key) == formal.get(key) for key in
                             ('snapshot_id', 'commit_sha', 'readme_path', 'blob_sha', 'content_hash')),
                         'Evidence source differs')
                sources.add(source['snapshot_id'])
            else:
                _require(candidate.get('collection_status') != 'completed' and row['availability'] == 'no_snapshot',
                         'Missing evidence source')
            _require(row['availability'] in ('available', 'no_snapshot', 'non_utf8'), 'Invalid availability')
            fields = ('requirement_item_id', 'text', 'modality', 'operator', 'logic_group_id')
            _require([{key: item.get(key) for key in fields} for item in row['items']] == report['requirement_items'],
                     'Evidence item coverage differs')
            for item in row['items']:
                _require(set(item) == set(fields).union({'status', 'quotes'}) and type(item['quotes']) is list,
                         'Invalid evidence item schema')
                for quote in item['quotes']:
                    _require(type(quote) is dict and set(quote) == {'text', 'byte_start', 'byte_end', 'line_start', 'line_end'} and
                             type(quote['text']) is str and bool(quote['text']) and
                             all(type(quote[key]) is int for key in ('byte_start', 'byte_end', 'line_start', 'line_end')) and
                             0 <= quote['byte_start'] < quote['byte_end'] and
                             quote['byte_end'] - quote['byte_start'] == len(quote['text'].encode('utf-8')) and
                             1 <= quote['line_start'] <= quote['line_end'], 'Invalid evidence quote')
                _require(row['availability'] == 'available' or not item['quotes'], 'Unavailable source has quotes')
                _require(item['status'] in ('mention_found', 'unconfirmed') and
                         (item['status'] == 'mention_found') == bool(item['quotes']) and
                         len(item['quotes']) <= 3, 'Invalid evidence conclusion')
        _require(type(op['usage']) is dict and set(op['usage']) == sources and
                 all(type(value) is bool for value in op['usage'].values()) and
                 type(op['published']) is bool and
                 (not op['published'] or all(op['usage'].values())), 'Invalid evidence publication')
    if 'deep_check_operation' in m:
        _deep(m)
    if 'chunk_index_operation' in m:
        _chunk_index(m)
    if 'evidence_bundle_operation' in m:
        _retrieval(m)
    if 'deep_review_operation' in m:
        _deep_review(m)
    _encode(m)


def _invariants(old, new):
    if 'candidate_screening' in old:
        _require(new.get('candidate_screening') == old['candidate_screening'], 'Immutable screening changed')
    elif 'candidate_screening' in new:
        _require(not old['dispatch_closed'] and not old['readme_persistence_operations'] and
                 not old.get('search_execution', {}).get('readme_targets'), 'Screening registered too late')
    if 'evidence_operation' in old:
        previous, current = old['evidence_operation'], new.get('evidence_operation', {})
        _require(all(current.get(key) == value for key, value in previous.items()
                     if key not in ('usage', 'published')), 'Immutable evidence identity changed')
        _require(not previous['published'] or current['published'], 'Evidence publication reversed')
        _require(all(not value or current['usage'][key] for key, value in previous['usage'].items()),
                 'Evidence usage reversed')
        _require(old['candidates'] == new['candidates'] and
                 old['readme_persistence_operations'].keys() == new['readme_persistence_operations'].keys() and
                 all(all(op[field] == new['readme_persistence_operations'][key][field]
                         for field in (*_OP_IDENTITY, 'snapshot_id', 'operation_status'))
                     for key, op in old['readme_persistence_operations'].items()), 'Evidence sources changed')
        if current != previous:
            _require(not new['integrity_blocked'], 'Integrity blocks evidence publication')
    elif 'evidence_operation' in new:
        _require(old['dispatch_closed'] and not old['integrity_blocked'] and
                 not new['evidence_operation']['published'] and
                 not any(new['evidence_operation']['usage'].values()), 'Invalid initial evidence operation')
    if 'deep_check_operation' in old:
        previous, current = old['deep_check_operation'], new.get('deep_check_operation', {})
        _require(all(current.get(key) == value for key, value in previous.items()
                     if key not in _DEEP_MUTABLE), 'Immutable deep check identity changed')
        _require(previous['status'] == current.get('status') or
                 current['status'] in _DEEP_ADVANCE[previous['status']],
                 'Illegal deep check transition', SearchStateError)
        _require(not previous['published'] or current.get('published'), 'Deep check publication reversed')
        _require(all(not value or current['usage'][key] for key, value in previous['usage'].items()),
                 'Deep check usage reversed')
        if previous['response_hash'] is not None:
            _require(current.get('response_hash') == previous['response_hash'] and
                     current.get('provider_usage') == previous['provider_usage'],
                     'Recorded deep check response changed')
        if previous['report'] is not None:
            _require(current.get('report') == previous['report'] and
                     current.get('report_hash') == previous['report_hash'], 'Published deep check report changed')
        _require(old['candidates'] == new['candidates'] and
                 old['readme_persistence_operations'].keys() == new['readme_persistence_operations'].keys() and
                 all(all(op[field] == new['readme_persistence_operations'][key][field]
                         for field in (*_OP_IDENTITY, 'snapshot_id', 'operation_status'))
                     for key, op in old['readme_persistence_operations'].items()), 'Deep check sources changed')
        if current != previous:
            _require(not new['integrity_blocked'], 'Integrity blocks deep check publication')
    elif 'deep_check_operation' in new:
        created = new['deep_check_operation']
        _require(old['dispatch_closed'] and not old['integrity_blocked'] and
                 old['plan_publication_status'] == 'published' and created['status'] == 'dispatched' and
                 not created['published'] and not any(created['usage'].values()),
                 'Invalid initial deep check operation')
    if 'deep_review_operation' in old:
        previous, current = old['deep_review_operation'], new.get('deep_review_operation')
        _require(current is not None, 'Deep review operation removed')
        _require(all(current[key] == previous[key] for key in _DEEP_REVIEW_IMMUTABLE),
                 'Immutable deep review identity changed')
        if previous['status'] in _DEEP_REVIEW_TERMINALS:
            _require(current == previous, 'Terminal deep review cannot change')
        else:
            _require(current['status'] == 'running' or current['status'] in _DEEP_REVIEW_TERMINALS,
                     'Illegal deep review transition', SearchStateError)
        if previous['report_hash'] is not None:
            _require(current['report_key'] == previous['report_key'] and
                     current['report_hash'] == previous['report_hash'], 'Deep review report changed')
        _require(old['candidates'] == new['candidates'] and
                 old['readme_persistence_operations'].keys() == new['readme_persistence_operations'].keys() and
                 all(all(op[field] == new['readme_persistence_operations'][key][field]
                         for field in (*_OP_IDENTITY, 'snapshot_id', 'operation_status'))
                     for key, op in old['readme_persistence_operations'].items()),
                 'Deep review sources changed')
        if current != previous:
            _require(not new['integrity_blocked'], 'Integrity blocks deep review completion')
    elif 'deep_review_operation' in new:
        created = new['deep_review_operation']
        _require(old['dispatch_closed'] and not old['integrity_blocked'] and
                 old['plan_publication_status'] == 'published' and created['status'] == 'running' and
                 created['report_key'] is None and created['report_hash'] is None and
                 created['error_code'] is None, 'Invalid initial deep review operation')
    if 'evidence_bundle_operation' in old:
        _require(new.get('evidence_bundle_operation') == old['evidence_bundle_operation'],
                 'Retrieval bundle is immutable')
    elif 'evidence_bundle_operation' in new:
        created = new['evidence_bundle_operation']
        _require(old['dispatch_closed'] and not old['integrity_blocked'] and
                 'chunk_index_operation' in old and
                 (created['status'] == 'failed' or
                  (old['chunk_index_operation']['status'] == 'ready' and
                   created['chunk_index_key'] == old['chunk_index_operation']['index_key'])),
                 'Invalid initial retrieval bundle')
    if 'chunk_index_operation' in old:
        previous, current = old['chunk_index_operation'], new.get('chunk_index_operation')
        _require(current is not None, 'Chunk index operation removed')
        identity = ('index_key', 'revision_hash', 'plan_hash', 'model', 'dimension',
                    'preprocess_version', 'config_hash')
        if previous['status'] == 'ready':
            _require(current == previous, 'Ready chunk index is immutable')
        elif previous['status'] == 'failed':
            if current != previous:
                _require(current['operation_id'] != previous['operation_id'],
                         'Failed chunk index cannot be reopened')
                _require(previous['operation_id'] in current['failed_operation_ids'],
                         'Failed chunk index restart is not recorded')
                _require(current['status'] in ('pending', 'embedding') and
                         all(current[key] == previous[key] for key in identity),
                         'Chunk index restart identity changed')
        else:
            _require(current['operation_id'] == previous['operation_id'],
                     'Chunk index operation changed')
            _require(current['status'] == previous['status'] or
                     current['status'] in _CHUNK_ADVANCE[previous['status']],
                     'Illegal chunk index transition')
            _require(all(current[key] == previous[key] for key in identity + ('created_at',)) and
                     current['failed_operation_ids'] == previous['failed_operation_ids'],
                     'Chunk index identity changed')
            _require(all(not value or current['usage'][key] for key, value in previous['usage'].items()),
                     'Chunk index usage reversed')
            if previous['chunk_manifest_hash'] is not None:
                _require(current['chunk_manifest_hash'] == previous['chunk_manifest_hash'],
                         'Chunk index manifest changed')
    elif 'chunk_index_operation' in new:
        created = new['chunk_index_operation']
        _require(old['dispatch_closed'] and not old['integrity_blocked'] and
                 old['plan_publication_status'] == 'published' and
                 created['status'] in ('pending', 'embedding') and
                 not any(created['usage'].values()) and not created['failed_operation_ids'] and
                 created['error_code'] is None, 'Invalid initial chunk index operation')
    for field in _IDENTITIES:
        _require(old[field] == new[field], 'Immutable manifest identity changed')
    if old['plan'] is not None:
        _require(old['plan'] == new['plan'], 'Immutable plan reference changed')
    elif new['plan'] is not None:
        _require(old['run_status'] in ('queued', 'running') and not old['dispatch_closed']
                 and old['plan_publication_status'] == 'preparing',
                 'Run cannot bind a new plan', SearchStateError)
    for field, allowed in (
        ('run_status', {'queued': ('running', 'failed'), 'running': ('completed', 'failed')}),
        ('plan_publication_status', {'preparing': ('published', 'failed')}),
    ):
        _require(new[field] == old[field] or new[field] in allowed.get(old[field], ()),
                 'Illegal state transition', SearchStateError)
    if old['plan_publication_status'] != 'published' and new['plan_publication_status'] == 'published':
        _require(not new['integrity_blocked'], 'Integrity blocks publication', SearchStateError)
    for field in ('dispatch_closed', 'integrity_blocked'):
        _require(not old[field] or new[field], 'Monotonic flag cleared', SearchStateError)
    if old['run_status'] != 'completed' and new['run_status'] == 'completed':
        _require(not new['integrity_blocked'], 'Integrity blocks completion', SearchStateError)
    for key, value in old['requests'].items():
        _require(new['requests'].get(key) == value, 'Request reservation changed')
    if new['requests'].keys() - old['requests'].keys():
        _require(old['run_status'] == 'running', 'Run is not running', SearchStateError)
    before, after = old['readme_persistence_operations'], new['readme_persistence_operations']
    _require(before.keys() <= after.keys(), 'Operation removed')
    for key, op in after.items():
        if key not in before:
            _require(old['run_status'] == 'running' and not old['dispatch_closed'],
                     'Dispatch is closed', SearchStateError)
            _require(op['operation_status'] == 'prepared', 'New operation must be prepared')
            continue
        previous = before[key]
        for field in _OP_IDENTITY:
            _require(op[field] == previous[field], 'Immutable operation identity changed')
        a, b = previous['operation_status'], op['operation_status']
        if a != b:
            allowed = () if a in _TERMINALS or a == 'completed' else (
                _STAGES[_STAGES.index(a) + 1], *_TERMINALS)
            _require(b in allowed, 'Illegal operation transition', SearchStateError)
        if op['snapshot_id'] != previous['snapshot_id']:
            _require(a == 'object_verified' and b == 'snapshot_committed',
                     'Snapshot ID can change only at SQL commit')
        history, updated = previous.get('transitions', {}), op.get('transitions', {})
        _require(all(updated.get(k) == v for k, v in history.items()), 'Transition history changed')
        if a == b or b in _TERMINALS:
            _require(history == updated, 'Transition history changed without advancement')
        else:
            _require(set(updated) - set(history) == {b} and
                     updated[b]['snapshot_id'] == op['snapshot_id'], 'Invalid advancement facts')
        if b == 'completed' and a != b:
            candidate = new['candidates'].get(str(op['repository_id']), {})
            _require(candidate.get('snapshot_id') == op['snapshot_id'] and
                     candidate.get('operation_id') == key and
                     candidate.get('collection_status') == 'completed', 'Completion is not atomic')
    for key, previous in old['candidates'].items():
        if previous.get('collection_status') != 'completed':
            continue
        candidate = new['candidates'].get(key, {})
        fields = ('snapshot_id', 'operation_id', 'collection_status')
        if any(candidate.get(k) != previous.get(k) for k in fields):
            replacement = candidate.get('operation_id')
            _require(replacement in before and replacement in after and
                     before[replacement]['operation_status'] == 'snapshot_committed' and
                     after[replacement]['operation_status'] == 'completed' and
                     str(after[replacement]['repository_id']) == key,
                     'Completed candidate reference changed without new completion')


class RunManifestStore:
    def __init__(self, storage):
        self.storage = storage

    @staticmethod
    def _key(owner, run_id):
        _hex(owner)
        _uuid(run_id)
        return f'runs/{owner}/{run_id}.json'

    def _load(self, owner, run_id):
        raw, version = self.storage.read_version(self._key(owner, run_id))
        if raw is None:
            return None, raw, version
        try:
            value = json.loads(raw, object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(
                                   SearchIntegrityError('Nonfinite JSON number')))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise SearchIntegrityError('Corrupt manifest JSON') from exc
        _validate(value, owner, run_id)
        return value, raw, version

    def _write(self, key, version, payload):
        try:
            self.storage.compare_and_swap(key, version, payload)
        except CASConflict:
            raise
        except StorageError as exc:
            # Only exact expected bytes establish success. A later concurrent
            # write is not proof that this transition committed.
            try:
                actual, _ = self.storage.read_version(key)
            except StorageError:
                raise exc
            if actual != payload:
                raise exc

    def create(self, owner: str, run_id: str, task_id: str, confirmed_revision_id: str) -> dict:
        key = self._key(owner, run_id)
        _uuid(task_id)
        _uuid(confirmed_revision_id)
        initial = dict(owner=owner, run_id=run_id, task_id=task_id,
                       confirmed_revision_id=confirmed_revision_id, run_status='queued',
                       plan_publication_status='preparing', plan=None, candidates={},
                       readme_persistence_operations={}, requests={}, dispatch_closed=False,
                       integrity_blocked=False)
        for _ in range(64):
            existing, _, version = self._load(owner, run_id)
            if existing is not None:
                _require(all(existing[k] == initial[k] for k in _IDENTITIES),
                         'Run already belongs to a different identity')
                return existing
            try:
                self._write(key, version, _encode(initial))
                return initial
            except CASConflict:
                continue
        raise StorageError('Manifest CAS retry budget exhausted')

    def read(self, owner, run_id) -> dict:
        value, _, _ = self._load(owner, run_id)
        _require(value is not None, 'Run does not exist', SearchStateError)
        return value

    def update(self, owner, run_id, transition) -> dict:
        key = self._key(owner, run_id)
        for _ in range(64):
            value, raw, version = self._load(owner, run_id)
            _require(value is not None, 'Run does not exist', SearchStateError)
            before = deepcopy(value)
            transition(value)
            _validate(value, owner, run_id)
            _invariants(before, value)
            payload = _encode(value)
            if payload == raw:
                return value
            try:
                self._write(key, version, payload)
                return value
            except CASConflict:
                continue
        raise StorageError('Manifest CAS retry budget exhausted')

    def create_deep_review(self, owner, run_id, operation: dict):
        incoming = deepcopy(operation)
        created = False
        def apply(m):
            nonlocal created
            existing = m.get('deep_review_operation')
            if existing is not None:
                _require(existing['revision_id'] == incoming['revision_id'],
                         'Deep review revision conflict', SearchStateError)
                created = False
                return
            _require('evidence_bundle_operation' in m and
                     m['evidence_bundle_operation']['status'] in ('ready', 'partial'),
                     'Retrieval bundle is not ready', SearchStateError)
            m['deep_review_operation'] = incoming
            created = True
        result = self.update(owner, run_id, apply)
        return result['deep_review_operation'], created

    def complete_deep_review(self, owner, run_id, operation_id, status, *,
                             report_key=None, report_hash=None, error_code=None):
        _uuid(operation_id)
        _require(status in _DEEP_REVIEW_TERMINALS, 'Invalid deep review terminal', SearchStateError)
        if error_code is not None:
            _require(type(error_code) is str and re.fullmatch(r'[A-Za-z0-9_]{1,128}', error_code),
                     'Invalid deep review error')
        def apply(m):
            op = m.get('deep_review_operation')
            _require(op is not None and op['operation_id'] == operation_id,
                     'Deep review operation mismatch', SearchStateError)
            if op['status'] in _DEEP_REVIEW_TERMINALS:
                _require((op['status'], op['report_key'], op['report_hash'], op['error_code']) ==
                         (status, report_key, report_hash, error_code),
                         'Terminal deep review outcome differs')
                return
            op.update(status=status, report_key=report_key, report_hash=report_hash,
                      error_code=error_code)
        return self.update(owner, run_id, apply)

    def bind_plan(self, owner, run_id, plan_ref: dict):
        ref = deepcopy(plan_ref)
        def apply(m):
            _plan(ref, m['confirmed_revision_id'])
            _require(m['plan'] is None or m['plan'] == ref, 'Plan binding conflict')
            m['plan'] = deepcopy(ref)
        return self.update(owner, run_id, apply)

    def publish_plan(self, owner, run_id, plan_ref):
        ref = deepcopy(plan_ref)
        def apply(m):
            _plan(ref, m['confirmed_revision_id'])
            _require(m['plan'] == ref, 'Published reference differs from bound plan')
            _require(m['plan_publication_status'] in ('preparing', 'published'),
                     'Plan publication failed', SearchStateError)
            _require(not m['integrity_blocked'], 'Integrity blocks publication', SearchStateError)
            m['plan_publication_status'] = 'published'
        return self.update(owner, run_id, apply)

    def start(self, owner, run_id):
        def apply(m):
            _require(m['run_status'] in ('queued', 'running'), 'Run is terminal', SearchStateError)
            m['run_status'] = 'running'
        return self.update(owner, run_id, apply)

    def fail(self, owner, run_id, code, error_class='terminal'):
        self._error_fields(code, error_class, False)
        def apply(m):
            if m['run_status'] in ('queued', 'running'):
                m['run_status'] = 'failed'
            m.update(error_code=code, error_class=error_class)
            if error_class == 'integrity':
                m['integrity_blocked'] = True
        return self.update(owner, run_id, apply)

    def seal_dispatch(self, owner, run_id):
        return self.update(owner, run_id, lambda m: m.update(dispatch_closed=True))

    def finish(self, owner, run_id):
        def apply(m):
            _require(m['run_status'] in ('running', 'completed') and m['dispatch_closed']
                     and m['plan_publication_status'] == 'published' and not m['integrity_blocked']
                     and all(o['operation_status'] == 'completed'
                             for o in m['readme_persistence_operations'].values()),
                     'Run is not ready for completion', SearchStateError)
            m['run_status'] = 'completed'
        return self.update(owner, run_id, apply)

    def register_operation(self, owner, run_id, operation: dict) -> dict:
        incoming = deepcopy(operation)
        _operation(incoming)
        _require(incoming['operation_status'] == 'prepared', 'Expected prepared operation', SearchStateError)
        def apply(m):
            ops = m['readme_persistence_operations']
            prior = ops.get(incoming['operation_id'])
            if prior is not None:
                _require(all(prior[k] == incoming[k] for k in _OP_IDENTITY),
                         'Operation identity conflict')
                allocated = prior.get('transitions', {}).get('staged', {}).get(
                    'snapshot_id', prior['snapshot_id'])
                _require(allocated == incoming['snapshot_id'], 'Preallocated snapshot ID conflict')
                return
            for op in ops.values():
                if op['snapshot_key'] == incoming['snapshot_key']:
                    _require(all(op[k] == incoming[k] for k in (
                        'repository_id', 'commit_sha', 'readme_path', 'blob_sha',
                        'content_hash', 'storage_key', 'byte_length', 'snapshot_key_algorithm')),
                        'Snapshot identity conflict')
                    return
            _require(m['run_status'] == 'running' and not m['dispatch_closed'],
                     'Cannot register new work', SearchStateError)
            ops[incoming['operation_id']] = deepcopy(incoming)
        return self.update(owner, run_id, apply)

    def advance_operation(self, owner, run_id, operation_id, status, transition_id: str, **facts) -> dict:
        _uuid(operation_id)
        _require(status in _STAGES[1:], 'Invalid target stage', SearchStateError)
        _require(type(transition_id) is str and bool(transition_id), 'Missing transition ID')
        _require(set(facts) <= {'snapshot_id'}, 'Only snapshot_id facts are accepted')
        _require(not facts or status == 'snapshot_committed', 'Snapshot ID requires commit stage')
        if facts:
            _uuid(facts['snapshot_id'])
        now = datetime.now(timezone.utc).isoformat()
        def apply(m):
            op = m['readme_persistence_operations'].get(operation_id)
            _require(op is not None, 'Unknown operation', SearchStateError)
            current = op['operation_status']
            _require(current not in _TERMINALS, 'Operation is terminal', SearchStateError)
            history = op.setdefault('transitions', {})
            for stage, record in history.items():
                _require(record['transition_id'] != transition_id or stage == status,
                         'Transition ID belongs to another stage')
            if _STAGES.index(current) >= _STAGES.index(status):
                if facts:
                    _require(history[status]['snapshot_id'] == facts['snapshot_id'],
                             'Replay result differs from committed snapshot')
                return
            _require(_STAGES.index(status) == _STAGES.index(current) + 1,
                     'Operation stages cannot be skipped', SearchStateError)
            op.update(facts)
            op['operation_status'] = status
            op['updated_at'] = now
            history[status] = {'transition_id': transition_id, 'snapshot_id': op['snapshot_id']}
            op['transition_id'] = transition_id
            if status == 'completed':
                candidate = m['candidates'].setdefault(str(op['repository_id']), {})
                candidate.update(snapshot_id=op['snapshot_id'], operation_id=operation_id,
                                 collection_status='completed')
        return self.update(owner, run_id, apply)

    @staticmethod
    def _error_fields(code, error_class, retryable):
        _require(type(code) is str and re.fullmatch(r'[A-Za-z0-9_]{1,128}', code),
                 'Error code must be a safe identifier')
        _require(error_class in _ERRORS and type(retryable) is bool, 'Invalid error classification')

    def record_error(self, owner, run_id, operation_id, code, error_class, retryable=False) -> dict:
        _uuid(operation_id)
        self._error_fields(code, error_class, retryable)
        now = datetime.now(timezone.utc).isoformat()
        def apply(m):
            op = m['readme_persistence_operations'].get(operation_id)
            _require(op is not None, 'Unknown operation', SearchStateError)
            op.update(error_code=code, error_class=error_class, retryable=retryable,
                      last_error_at=now)
            if error_class == 'integrity':
                m['integrity_blocked'] = True
            if op['operation_status'] not in _TERMINALS + ('completed',):
                if error_class == 'integrity':
                    op['operation_status'] = 'integrity_failed'
                elif error_class == 'terminal':
                    op['operation_status'] = 'interrupted'
        return self.update(owner, run_id, apply)

    def reserve_request(self, owner, run_id, request_id: str, issued_at: datetime) -> dict:
        _uuid(request_id)
        _require(isinstance(issued_at, datetime) and issued_at.utcoffset() is not None,
                 'Request time must be timezone-aware')
        stamp = issued_at.astimezone(timezone.utc).isoformat()
        def apply(m):
            if request_id in m['requests']:
                _require(m['requests'][request_id] == stamp, 'Request timestamp conflict')
                return
            _require(m['run_status'] == 'running', 'Run is not running', SearchStateError)
            m['requests'][request_id] = stamp
        return self.update(owner, run_id, apply)
