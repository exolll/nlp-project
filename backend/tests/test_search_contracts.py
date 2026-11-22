"""Independent byte fixtures for the phase-one search contracts (offline only)."""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID

import pytest

from app.schemas.requirements import RequirementJSON, RequirementRevision
from app.services.search_contracts import (
    HASH_ENCODING_VERSION, QUERY_HASH_ALGORITHM, SNAPSHOT_KEY_ALGORITHM,
    LIST_SOURCE_FIELDS, SCALAR_SOURCE_FIELDS, SearchContractError,
    canonicalize_query, encode_hash_value, query_hash,
    resolve_requirement_pointer, snapshot_key, validate_source_requirement_pointers,
)

TASK = UUID('11111111-1111-4111-8111-111111111111')
REVISION = UUID('22222222-2222-4222-8222-222222222222')
OTHER = UUID('33333333-3333-4333-8333-333333333333')
BINDING = dict(task_id=TASK, confirmed_revision_id=REVISION)


@pytest.fixture
def revision():
    requirements = RequirementJSON(
        project_topic='搜索', project_description='不要商业依赖',
        project_type='web', domain='NLP', required_features=['搜索', '导出'],
        preferred_features=['离线优先'], programming_languages=['Python'],
        required_technologies=['SQLite'], preferred_technologies=['FastAPI'],
        missing_information=[], uncertain_information=None, source_evidence=[],
    )
    return RequirementRevision(
        revision_id=REVISION, task_id=TASK, revision_number=1,
        parent_revision_id=None, status='confirmed', requirements=requirements,
        requirement_items=[], change_log=[], created_at=datetime.now(timezone.utc),
        confirmed_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(('value', 'expected'), [
    (None, b'n'), ('', b's0:'), ([], b'a0:'),
    (['中', None, ''], bytes.fromhex('61333a73333ae4b8ad6e73303a')),
    (['e\u0301', 'é', ['😀']], b'a3:s3:e\xcc\x81s2:\xc3\xa9a1:s4:\xf0\x9f\x98\x80'),
])
def test_encoding_anchors(value, expected):
    assert encode_hash_value(value) == expected


@pytest.mark.parametrize('value', [True, 0, 1.2, {}, (), b'x', '\ud800', ['\udfff'], [False]])
def test_encoding_rejects_other_types(value):
    with pytest.raises(SearchContractError):
        encode_hash_value(value)


def test_version_constants():
    assert HASH_ENCODING_VERSION == 'pp-hash-v1'
    assert QUERY_HASH_ALGORITHM == 'projectpilot-query-v1'
    assert SNAPSHOT_KEY_ALGORITHM == 'projectpilot-snapshot-v1'


@pytest.mark.parametrize(('text', 'language', 'qualifiers', 'encoded'), [
    ('hello', None, {}, b'a4:s21:projectpilot-query-v1s5:hellona0:'),
    ('中 e\u0301', 'C++', {'topic': ['é', '中', 'é'], 'stars': ['>=10']},
     b'a4:s21:projectpilot-query-v1s7:\xe4\xb8\xad e\xcc\x81s3:C++a2:'
     b'a2:s5:starsa1:s4:>=10a2:s5:topica2:s2:\xc3\xa9s3:\xe4\xb8\xad'),
])
def test_query_golden_bytes(text, language, qualifiers, encoded):
    # The complete byte literals are independent of the production encoder.
    expected = sha256(encoded).hexdigest()
    assert expected == {
        'hello': 'a5dd88354edb25205fb19db747cc4ecd1624464a8ecc8015a75d90a2377cc8ae',
        '中 e\u0301': 'df1f26b57989d26a54a4ef99ff20c01db36eba4316ace369088c52f3a1a7b00b',
    }[text]
    assert query_hash(text, language_filter=language, qualifiers=qualifiers) == expected


def test_canonicalization_and_no_implicit_filters():
    source = {'topic': ['中', 'é', '中'], 'in': ['readme', 'name']}
    result = canonicalize_query('"two  words" OR 搜索', language_filter='Custom Language', qualifiers=source)
    assert result == {'query_text': '"two  words" OR 搜索',
                      'language_filter': 'Custom Language',
                      'qualifiers': {'in': ['name', 'readme'], 'topic': ['é', '中']}}
    assert list(result['qualifiers']) == ['in', 'topic']
    assert source == {'topic': ['中', 'é', '中'], 'in': ['readme', 'name']}
    result['qualifiers']['topic'].append('x')
    assert 'x' not in source['topic']
    assert canonicalize_query('hello', qualifiers={})['qualifiers'] == {}
    assert query_hash('hello', qualifiers=source) == query_hash(
        'hello', qualifiers={'in': ['name', 'readme'], 'topic': ['é', '中']})


@pytest.mark.parametrize(('left', 'right'), [
    ('Hello', 'hello'), ('é', 'e\u0301'), ('"a  b"', '"a b"'), ('a OR b', 'b OR a'),
])
def test_query_preserves_exact_text(left, right):
    assert query_hash(left, qualifiers={}) != query_hash(right, qualifiers={})


@pytest.mark.parametrize('text', ['', ' ', '\u2003', ' x', 'x ', '\tx', 'x\n', 'a\tb', 'a\x00b', 'a\x7fb', 'a\x85b', '\ud800', None, 1, True])
def test_invalid_query_text(text):
    with pytest.raises(SearchContractError):
        query_hash(text, qualifiers={})


@pytest.mark.parametrize('qualifiers', [
    None, [], {'Stars': ['1']}, {'language': ['Python']}, {'owner': ['x']},
    {'tópic': ['x']}, {1: ['x']}, {'topic': None}, {'topic': []},
    {'topic': 'x'}, {'topic': [1.2]}, {'topic': [True]}, {'topic': [None]},
    {'topic': [{}]}, {'topic': ['']}, {'topic': ['\u2003']},
    {'topic': [' x']}, {'topic': ['a\nb']}, {'topic': ['\udfff']},
])
def test_invalid_qualifiers(qualifiers):
    with pytest.raises(SearchContractError):
        query_hash('hello', qualifiers=qualifiers)


@pytest.mark.parametrize('language', ['', ' ', ' Python', 'Python ', '\udfff', 3, False])
def test_invalid_language(language):
    with pytest.raises(SearchContractError):
        query_hash('hello', language_filter=language, qualifiers={})


def test_missing_fields_rejected():
    with pytest.raises(TypeError):
        query_hash('hello')
    with pytest.raises(TypeError):
        query_hash(qualifiers={})
    with pytest.raises(TypeError):
        snapshot_key(1, 'a' * 40, 'README')


def test_all_qualifiers_supported():
    values = {'in': ['readme'], 'stars': ['>=5'], 'forks': ['1..10'],
              'license': ['mit'], 'archived': ['false'], 'topic': ['nlp']}
    assert canonicalize_query('搜索', qualifiers=values)['qualifiers'] == values


def test_snapshot_golden_bytes_large_id():
    encoded = (b'a5:s24:projectpilot-snapshot-v1s16:9007199254740993'
               b's40:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
               b's14:\xe4\xb8\xad/README.md '
               b's40:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')
    expected = sha256(encoded).hexdigest()
    assert expected == 'eb9eb5e7e44474241d20608cabc81b440bb60ce43f760a475c24115c0b990991'
    assert snapshot_key('9007199254740993', 'A' * 40, '中/README.md ', 'B' * 40) == expected
    assert snapshot_key(9007199254740993, 'a' * 40, '中/README.md ', 'b' * 40) == expected


@pytest.mark.parametrize('repository_id', [0, -1, 2**64, True, 1.0, 9007199254740992.0, None, '', '0', '01', '+1', ' 1', '1 ', '１', '1e3', str(2**64), '9' * 5000])
def test_invalid_repository_id(repository_id):
    with pytest.raises(SearchContractError):
        snapshot_key(repository_id, 'a' * 40, 'README', 'b' * 40)


@pytest.mark.parametrize('repository_id', [1, 2**64 - 1])
def test_uint64_boundaries(repository_id):
    assert snapshot_key(repository_id, 'a' * 40, 'R', 'b' * 40) == snapshot_key(str(repository_id), 'A' * 40, 'R', 'B' * 40)


@pytest.mark.parametrize('sha', ['', 'a' * 39, 'a' * 41, 'g' * 40, 'a' * 39 + '\n', None, 1])
def test_invalid_sha(sha):
    with pytest.raises(SearchContractError):
        snapshot_key(1, sha, 'README', 'b' * 40)
    with pytest.raises(SearchContractError):
        snapshot_key(1, 'a' * 40, 'README', sha)


@pytest.mark.parametrize('path', ['', None, '\udfff', 1])
def test_invalid_path(path):
    with pytest.raises(SearchContractError):
        snapshot_key(1, 'a' * 40, path, 'b' * 40)


def test_snapshot_identity_preserves_every_field():
    paths = ['README', 'Readme', 'README ', './README', '/README', '\\README', 'é', 'e\u0301', ' ']
    hashes = {snapshot_key(1, 'a' * 40, path, 'b' * 40) for path in paths}
    assert len(hashes) == len(paths)
    base = snapshot_key(1, 'a' * 40, 'README', 'b' * 40)
    assert base != snapshot_key(2, 'a' * 40, 'README', 'b' * 40)
    assert base != snapshot_key(1, 'c' * 40, 'README', 'b' * 40)
    assert base != snapshot_key(1, 'a' * 40, 'README', 'c' * 40)


@pytest.mark.parametrize('field', sorted(SCALAR_SOURCE_FIELDS | LIST_SOURCE_FIELDS))
def test_actual_flat_schema_sources(revision, field):
    pointer = '/' + field + ('/0' if field in LIST_SOURCE_FIELDS else '')
    value = getattr(revision.requirements, field)
    assert resolve_requirement_pointer(revision, pointer, **BINDING) == (value[0] if isinstance(value, list) else value)


@pytest.mark.parametrize('pointer', [
    '', '/', 'project_topic', '#/project_topic', '/requirements/project_topic',
    '/requirement_items/0', '/functional_requirements/required_features/0',
    '/project_overview/project_topic', '/source_evidence/0/original_text',
    '/missing_information/0', '/uncertain_information/0', '/revision_id',
    '/required_features', '/project_topic/0', '/required_features/2',
    '/required_features/-', '/required_features/-1', '/required_features/+1',
    '/required_features/00', '/required_features/01', '/required_features/1.0',
    '/required_features/１', '/required_features/ 0', '/required_features/0/',
    '/required_features/' + '9' * 5000, '/required_features/~2', '/project_topic~',
    '/project~1topic', '/project~0topic', '/required_features/~01',
    '/required_features/0~1', '/required_features/0\n', None, 1, '\udfff',
])
def test_invalid_pointer(revision, pointer):
    with pytest.raises(SearchContractError):
        resolve_requirement_pointer(revision, pointer, **BINDING)


@pytest.mark.parametrize('value', [None, '', ' ', '\ud800', 1, []])
def test_source_must_be_real_nonblank_string(revision, value):
    # model_copy(update=...) deliberately tests unvalidated/malformed inputs.
    revision.requirements = revision.requirements.model_copy(update={'project_topic': value})
    with pytest.raises(SearchContractError):
        resolve_requirement_pointer(revision, '/project_topic', **BINDING)


@pytest.mark.parametrize('value', [None, [], [None], [''], ['\u2003'], ['\udfff']])
def test_null_empty_or_invalid_list_source(revision, value):
    revision.requirements = revision.requirements.model_copy(update={'required_features': value})
    with pytest.raises(SearchContractError):
        resolve_requirement_pointer(revision, '/required_features/0', **BINDING)


@pytest.mark.parametrize('patch', [{'status': 'draft'}, {'status': 'extracted'}, {'task_id': OTHER}, {'revision_id': OTHER}])
def test_binding_rejects_wrong_revision_task_status(revision, patch):
    with pytest.raises(SearchContractError):
        resolve_requirement_pointer(revision.model_copy(update=patch), '/project_topic', **BINDING)
    with pytest.raises(SearchContractError):
        validate_source_requirement_pointers(revision.model_copy(update=patch), [], group_status='skipped', **BINDING)


def test_binding_accepts_uuid_strings_and_no_latest_fallback(revision):
    assert resolve_requirement_pointer(revision, '/project_topic', task_id=str(TASK), confirmed_revision_id=str(REVISION)) == '搜索'
    with pytest.raises(SearchContractError):
        resolve_requirement_pointer(revision, '/project_topic', task_id=TASK, confirmed_revision_id=OTHER)
    with pytest.raises(SearchContractError):
        resolve_requirement_pointer(revision, '/project_topic', task_id='invalid', confirmed_revision_id=REVISION)


def test_sources_dedup_subset_and_input_unchanged(revision):
    before = revision.model_dump(mode='json')
    sources = ['/project_description', '/preferred_features/0', '/project_description']
    assert validate_source_requirement_pointers(revision, sources, **BINDING) == sources[:2]
    assert validate_source_requirement_pointers(revision, sources[:1], group_pointers=sources, **BINDING) == sources[:1]
    assert validate_source_requirement_pointers(revision, [], group_status='skipped', **BINDING) == []
    assert revision.model_dump(mode='json') == before
    assert len(sources) == 3
    assert resolve_requirement_pointer(revision, '/project_description', **BINDING) == '不要商业依赖'
    assert resolve_requirement_pointer(revision, '/preferred_features/0', **BINDING) == '离线优先'


@pytest.mark.parametrize(('sources', 'options'), [
    ([], {}), (None, {}), ('/project_topic', {}),
    (['/project_topic'], {'group_status': 'unknown'}),
    (['/project_topic'], {'group_pointers': ['/domain']}),
    (['/project_topic'], {'group_pointers': []}),
    ([], {'group_status': 'skipped', 'group_pointers': ['/domain']}),
])
def test_invalid_source_collections(revision, sources, options):
    with pytest.raises(SearchContractError):
        validate_source_requirement_pointers(revision, sources, **options, **BINDING)
