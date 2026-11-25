import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from app.core.settings import ExtractionSettings, Settings
from app.schemas.requirements import (RequirementEvidence, RequirementItem, RequirementJSON,
                                      RequirementRevision)
from app.services.deep_review_logic import (DeepReviewInputError, line_table,
                                            normalized_readme_bytes)
from app.services.deep_review_models import MAX_INPUT_BYTES, MAX_OUTPUT_TOKENS
from app.services.deep_review_pipeline import DeepReviewPipeline, NOTICE
from app.services.requirement_quality import revision_hash

OWNER = 'student-1'
RUN_ID = 'run-1'
OPERATION_ID = str(uuid4())
BUNDLE_HASH = 'a' * 64
PLAN_HASH = 'b' * 64
USAGE = {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}


def make_revision(specs=None, *, confirmed=True):
    specs = specs or [{'text': '支持任务管理'}]
    now = datetime.now(timezone.utc)
    task_id, revision_id = uuid4(), uuid4()
    values = {
        'project_topic': None, 'project_description': None, 'project_type': None, 'domain': None,
        'required_features': None, 'preferred_features': None, 'programming_languages': None,
        'required_technologies': None, 'preferred_technologies': None,
    }
    items, evidence = [], []
    list_fields = {'required_features', 'preferred_features', 'programming_languages',
                   'required_technologies', 'preferred_technologies'}
    for index, spec in enumerate(specs):
        item_id = spec.get('requirement_item_id', uuid4())
        field = spec.get('field', 'required_features')
        text = spec['text']
        if field in list_fields:
            values[field] = (values[field] or []) + [text]
            value_path = f'{field}[{len(values[field]) - 1}]'
        else:
            values[field] = text
            value_path = field
        evidence_id = f'E-{index + 1}'
        items.append(RequirementItem(
            requirement_item_id=str(item_id), field=field, value_path=value_path, text=text,
            modality=spec.get('modality', 'required'), source_kind='user_primary_text',
            evidence_ids=[evidence_id], logic_group_id=spec.get('logic_group_id'),
            operator=spec.get('operator', 'not_applicable'), supersedes_item_id=None))
        evidence.append(RequirementEvidence(
            evidence_id=evidence_id, field=field, original_text=f'任务原文：{text}。',
            source_kind='user_primary_text', source_location='用户输入正文',
            requirement_item_id=str(item_id), segment_id='SEG-1'))
    return RequirementRevision(
        revision_id=revision_id, task_id=task_id, revision_number=1, parent_revision_id=None,
        status='confirmed' if confirmed else 'draft',
        requirements=RequirementJSON(
            **values, missing_information=[], uncertain_information=None, source_evidence=evidence),
        requirement_items=items, change_log=[], created_at=now,
        confirmed_at=now if confirmed else None)


def make_chunk(raw, chunk_id, line_start=1, line_end=None):
    data = normalized_readme_bytes(raw)
    table = line_table(data)
    if line_end is None:
        line_end = len(data.split(b'\n')) - (1 if data.endswith(b'\n') else 0)
    start, end = table[line_start - 1][0], table[line_end - 1][1]
    return {
        'chunk_id': chunk_id, 'text': data[start:end].decode('utf-8'),
        'line_start': line_start, 'line_end': line_end,
        'byte_start': start, 'byte_end': end, 'split': 'bytes',
    }


def make_candidate(raw, index, item_ids, chunks, *, repository_id=None, snapshot_id=None,
                   coverage='full', commit_sha=None):
    repository_id = repository_id or str(uuid4())
    snapshot_id = snapshot_id or str(uuid4())
    blob_sha = hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
    return {
        'repository_id': repository_id, 'snapshot_id': snapshot_id, 'coverage': coverage,
        'readme_path': 'README.md', 'commit_sha': commit_sha or f'{index + 1:040x}',
        'blob_sha': blob_sha, 'content_hash': hashlib.sha256(raw).hexdigest(),
        'collected_at': datetime.now(timezone.utc).isoformat(),
        'repository': {'name': f'repo-{index}', 'language': 'Python', 'stargazers_count': 10},
        'status': 'retrieved', 'error_code': None,
        'items': [{
            'requirement_item_id': item_id, 'text': '', 'chunks': copy.deepcopy(chunks),
            'status': 'retrieved' if chunks else 'empty', 'relevance': 'unverified',
            'empty_reason': None if chunks else 'no_chunks',
        } for item_id in item_ids],
    }


def make_context(raws, specs=None):
    revision = make_revision(specs)
    item_ids = [str(item.requirement_item_id) for item in revision.requirement_items]
    candidates, snapshots = [], {}
    for index, raw in enumerate(raws):
        chunk = make_chunk(raw, f'chunk-{index}-0')
        candidate = make_candidate(raw, index, item_ids, [chunk])
        candidates.append(candidate)
        snapshots[candidate['snapshot_id']] = raw
    bundle = {
        'version': 'evidence-bundle-v2', 'schema_version': 2, 'owner': OWNER,
        'task_id': str(revision.task_id), 'run_id': RUN_ID,
        'confirmed_revision_id': str(revision.revision_id),
        'revision_hash': revision_hash(revision), 'plan_hash': PLAN_HASH,
        'candidates': candidates,
    }
    return {'revision': revision, 'bundle': bundle, 'snapshots': snapshots, 'reads': []}


def execute(context, handler, *, bundle=None, revision=None, bundle_hash=BUNDLE_HASH,
            timeout=90, reader=None):
    revision = revision or context['revision']
    bundle = bundle or context['bundle']
    snapshots, reads = context['snapshots'], context['reads']

    def default_reader(owner, run_id, snapshot_id):
        reads.append((owner, run_id, snapshot_id))
        return snapshots[snapshot_id]

    settings = Settings(environment='test', _env_file=None).model_copy(
        update={'extraction_timeout_seconds': timeout})
    provider = ExtractionSettings(deepseek_api_key='test-only', _env_file=None)
    pipeline = DeepReviewPipeline(settings, provider, reader or default_reader,
                                  httpx.MockTransport(handler))
    return asyncio.run(pipeline.generate(
        OWNER, revision.task_id, RUN_ID, revision, bundle, bundle_hash, OPERATION_ID))


def request_payload(request):
    return json.loads(request.content)


def request_envelope(request):
    payload = request_payload(request)
    return json.loads(payload['messages'][1]['content'])


def assessment(item_id, verdict='supports', *, chunk_id=None, line_start=1, line_end=1,
               cite=True):
    reason = {'supports': 'supported_by_citation', 'contradicts': 'conflicted_by_citation',
              'insufficient': 'not_found_in_provided_evidence'}[verdict]
    citations = []
    if cite and verdict != 'insufficient':
        citations = [{'chunk_id': chunk_id, 'line_start': line_start, 'line_end': line_end}]
    return {'requirement_item_id': str(item_id), 'verdict': verdict,
            'reason_code': reason, 'citations': citations}


def response(output=None, *, content=None, usage=None, finish_reason='stop'):
    if content is None:
        content = json.dumps(output, ensure_ascii=False)
    return httpx.Response(200, json={
        'choices': [{'message': {'content': content}, 'finish_reason': finish_reason}],
        'usage': usage,
    })


def test_successful_report_uses_full_prompt_and_byte_accurate_crlf_citations():
    raw = '第一行：支持任务管理。\r\n第二行：使用 Python 和 emoji 🚀。\r\n'.encode('utf-8')
    context = make_context([raw])
    item_id = context['revision'].requirement_items[0].requirement_item_id
    seen = []

    def handler(request):
        payload = request_payload(request)
        envelope = request_envelope(request)
        seen.append(payload)
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        assert request.headers['Authorization'] == 'Bearer test-only'
        assert payload['model'] == 'deepseek-flash'
        assert payload['max_tokens'] == MAX_OUTPUT_TOKENS
        assert payload['stream'] is False
        assert payload['thinking'] == {'type': 'disabled'}
        assert 'UNTRUSTED_README_CHUNKS' in payload['messages'][0]['content']
        assert len(envelope['chunks']) == 1
        assert envelope['chunks'][0]['text'].startswith('第一行')
        return response({'assessments': [assessment(item_id, chunk_id='chunk-0-0')]}, usage=USAGE)

    report = execute(context, handler)
    repository = report.repositories[0]
    review = repository.requirements[0]
    citation = review.citations[0]
    line = '第一行：支持任务管理。'

    assert report.status == 'completed'
    assert report.operation_id == OPERATION_ID
    assert report.prompt_version == 'deep-review-v1'
    assert report.notice == NOTICE
    assert repository.status == 'completed'
    assert review.verdict == 'supports'
    assert review.reason_code == 'supported_by_citation'
    assert citation.quote == line
    assert (citation.line_start, citation.line_end) == (1, 1)
    assert (citation.byte_start, citation.byte_end) == (0, len(line.encode('utf-8')))
    assert repository.trace.content_hash == hashlib.sha256(raw).hexdigest()
    assert repository.trace.blob_sha == hashlib.sha1(
        b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
    assert repository.input.input_bytes_upper_bound == (
        len(json.dumps(seen[0], ensure_ascii=False).encode('utf-8')) + 256)
    assert repository.input.input_bytes_upper_bound <= MAX_INPUT_BYTES
    assert repository.input.budget.max_input_bytes == MAX_INPUT_BYTES
    assert repository.usage.known is True
    assert repository.usage.total_tokens == 10
    assert report.summary.usage.calls == 1
    assert report.summary.usage.total_tokens == 10
    assert report.summary.usage.unknown_calls == 0
    assert report.ranking is not None
    assert report.ranking.repositories[0].hard_constraint_tier == 'meets_requirements'
    assert report.ranking.repositories[0].recommendation.reasons[0].citations[0].chunk_id == 'chunk-0-0'
    assert report.cost_status == 'not_calculated'
    assert report.estimated_cost is None and report.currency is None and report.pricing_source is None
    assert repository.response_hash is not None
    assert context['reads'] == [(OWNER, RUN_ID, repository.snapshot_id)]
    serialized = report.model_dump_json()
    assert 'max_input_tokens' not in serialized
    assert 'input_tokens_estimate' not in serialized


def test_candidate_chunks_are_deduplicated_without_truncation():
    raw = '支持任务管理。\n支持导出报表。'.encode('utf-8')
    specs = [{'text': '支持任务管理'}, {'text': '支持导出报表'}]
    context = make_context([raw], specs)
    revision = context['revision']
    first, second = [str(item.requirement_item_id) for item in revision.requirement_items]
    chunks = [make_chunk(raw, 'chunk-a', 1, 1), make_chunk(raw, 'chunk-b', 2, 2)]
    candidate = make_candidate(raw, 0, [first, second], [])
    candidate['items'][0]['chunks'] = copy.deepcopy(chunks)
    candidate['items'][1]['chunks'] = copy.deepcopy(chunks[:1])
    context['bundle']['candidates'] = [candidate]
    context['snapshots'] = {candidate['snapshot_id']: raw}

    def handler(request):
        envelope = request_envelope(request)
        assert [chunk['chunk_id'] for chunk in envelope['chunks']] == ['chunk-a', 'chunk-b']
        return response({'assessments': [
            assessment(first, chunk_id='chunk-a'),
            assessment(second, chunk_id='chunk-b', line_start=2, line_end=2)]})

    report = execute(context, handler)
    assert report.repositories[0].input.chunk_ids == ['chunk-a', 'chunk-b']
    assert report.repositories[0].input.chunk_count == 2
    assert report.summary.requirements_total == 2
    assert report.summary.supports == 2


def test_input_byte_budget_skip_does_not_call_or_truncate():
    raw = b'x' * 30000
    context = make_context([raw])

    def handler(request):
        raise AssertionError('over-budget repository must not call the provider')

    report = execute(context, handler)
    repository = report.repositories[0]
    assert repository.status == 'skipped'
    assert repository.error_code == 'DEEP_REVIEW_INPUT_BYTES_BUDGET'
    assert repository.input.input_bytes_upper_bound > MAX_INPUT_BYTES
    assert repository.input.chunk_count == 1
    assert report.status == 'failed'
    assert report.summary.usage.calls == 0


def test_chunk_budget_skip_does_not_call_or_truncate():
    raw = '\n'.join(f'line {index}' for index in range(17)).encode('utf-8')
    context = make_context([raw])
    revision = context['revision']
    item_id = str(revision.requirement_items[0].requirement_item_id)
    chunks = [make_chunk(raw, f'chunk-{index}', index + 1, index + 1) for index in range(17)]
    candidate = make_candidate(raw, 0, [item_id], chunks)
    context['bundle']['candidates'] = [candidate]
    context['snapshots'] = {candidate['snapshot_id']: raw}

    def handler(request):
        raise AssertionError('over-budget repository must not call the provider')

    report = execute(context, handler)
    repository = report.repositories[0]
    assert repository.status == 'skipped'
    assert repository.error_code == 'DEEP_REVIEW_CHUNK_BUDGET'
    assert repository.input.chunk_count == 17
    assert len(repository.input.chunk_ids) == 17
    assert report.status == 'failed'


def test_five_repositories_make_exactly_five_calls():
    context = make_context([f'README {index}'.encode('utf-8') for index in range(5)])
    item_id = context['revision'].requirement_items[0].requirement_item_id
    calls = []

    def handler(request):
        envelope = request_envelope(request)
        calls.append(envelope['repository']['repository_id'])
        return response({'assessments': [assessment(
            item_id, chunk_id=envelope['chunks'][0]['chunk_id'])]})

    report = execute(context, handler)
    assert len(calls) == len(set(calls)) == 5
    assert report.status == 'completed'
    assert report.summary.repositories_completed == 5
    assert report.summary.usage.calls == 5
    assert report.ranking is not None
    assert report.ranking.ranking_config_version == 'hard-tier-score-v2'
    assert len(report.ranking.repositories) == 5


def test_more_than_five_repositories_is_rejected_before_any_call():
    context = make_context([f'README {index}'.encode('utf-8') for index in range(6)])

    def handler(request):
        raise AssertionError('operation over budget must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler)
    assert error.value.code == 'DEEP_REVIEW_REPOSITORY_BUDGET'
    assert context['reads'] == []


def test_mixed_empty_and_usable_repository_is_partial_not_requirement_partial():
    context = make_context([b'empty', b'README evidence'])
    revision = context['revision']
    item_id = revision.requirement_items[0].requirement_item_id
    empty = context['bundle']['candidates'][0]
    for item in empty['items']:
        item['chunks'] = []
        item['status'] = 'empty'
    context['snapshots'].pop(empty['snapshot_id'])

    def handler(request):
        return response({'assessments': [assessment(item_id, chunk_id='chunk-1-0')]})

    report = execute(context, handler)
    assert [repository.status for repository in report.repositories] == ['skipped', 'completed']
    assert report.repositories[0].error_code == 'DEEP_REVIEW_NO_CHUNKS'
    assert report.repositories[1].requirements[0].verdict == 'supports'
    assert report.status == 'partial'
    assert len(context['reads']) == 1


def test_no_eligible_repository_is_rejected_before_any_call():
    context = make_context([b'README'])
    for item in context['bundle']['candidates'][0]['items']:
        item['chunks'] = []

    def handler(request):
        raise AssertionError('no eligible repository must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler)
    assert error.value.code == 'DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES'


@pytest.mark.parametrize('status,code,repository_status,top_status,stop_code', [
    (401, 'PROVIDER_AUTH', 'failed', 'failed', 'DEEP_REVIEW_STOPPED_AFTER_PROVIDER_ERROR'),
    (402, 'PROVIDER_BALANCE', 'failed', 'failed', 'DEEP_REVIEW_STOPPED_AFTER_PROVIDER_ERROR'),
    (429, 'PROVIDER_RATE_LIMIT', 'unknown', 'unknown', 'DEEP_REVIEW_STOPPED_AFTER_UNKNOWN'),
    (500, 'PROVIDER_UNAVAILABLE', 'unknown', 'unknown', 'DEEP_REVIEW_STOPPED_AFTER_UNKNOWN'),
])
def test_provider_status_stops_without_retry(status, code, repository_status, top_status, stop_code):
    context = make_context([b'one', b'two', b'three'])
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='secret-body-credential')

    report = execute(context, handler)
    assert len(calls) == 1
    assert report.status == top_status
    assert report.repositories[0].status == repository_status
    assert report.repositories[0].error_code == code
    assert [repository.status for repository in report.repositories[1:]] == ['skipped', 'skipped']
    assert all(repository.error_code == stop_code for repository in report.repositories[1:])
    assert report.summary.usage.calls == 1
    assert report.summary.usage.unknown_calls == 1
    assert 'secret-body' not in report.model_dump_json()
    assert 'test-only' not in report.model_dump_json()


def test_completed_results_survive_a_later_unknown_outcome():
    context = make_context([b'one', b'two', b'three'])
    item_id = context['revision'].requirement_items[0].requirement_item_id
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(500, text='secret-body')
        chunk_id = 'chunk-0-0' if len(calls) == 1 else 'chunk-2-0'
        return response({'assessments': [assessment(item_id, chunk_id=chunk_id)]}, usage=USAGE)

    report = execute(context, handler)
    assert len(calls) == 2
    assert report.status == 'unknown'
    assert [repository.status for repository in report.repositories] == ['completed', 'unknown', 'skipped']
    assert report.repositories[0].requirements[0].verdict == 'supports'
    assert report.repositories[0].usage.total_tokens == 10
    assert report.repositories[1].error_code == 'PROVIDER_UNAVAILABLE'
    assert report.repositories[2].error_code == 'DEEP_REVIEW_STOPPED_AFTER_UNKNOWN'
    assert report.summary.repositories_completed == 1
    assert report.summary.repositories_unknown == 1
    assert report.summary.repositories_skipped == 1
    assert 'secret-body' not in report.model_dump_json()


@pytest.mark.parametrize('exception,code', [
    (httpx.ReadTimeout, 'PROVIDER_TIMEOUT'),
    (httpx.ConnectError, 'PROVIDER_NETWORK'),
])
def test_timeout_and_network_errors_are_unknown_single_attempt(exception, code):
    context = make_context([b'one', b'two'])
    calls = []

    def handler(request):
        calls.append(request)
        raise exception('Authorization: test-only secret-body')

    report = execute(context, handler)
    assert len(calls) == 1
    assert report.status == 'unknown'
    assert report.repositories[0].status == 'unknown'
    assert report.repositories[0].error_code == code
    assert report.repositories[1].status == 'skipped'
    assert 'secret-body' not in report.model_dump_json()
    assert 'test-only' not in report.model_dump_json()


@pytest.mark.parametrize('status', [302, 400])
def test_other_http_errors_fail_one_repository_and_continue(status):
    context = make_context([b'one', b'two'])
    item_id = context['revision'].requirement_items[0].requirement_item_id
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, text='bad request', headers={'Location': 'https://evil.invalid'})
        return response({'assessments': [assessment(item_id, chunk_id='chunk-1-0')]})

    report = execute(context, handler)
    assert len(calls) == 2
    assert str(calls[1].url) == 'https://api.deepseek.com/chat/completions'
    assert report.status == 'partial'
    assert report.repositories[0].status == 'failed'
    assert report.repositories[0].error_code == 'PROVIDER_HTTP_ERROR'
    assert report.repositories[1].status == 'completed'


def test_invalid_json_and_schema_fail_without_retry_then_continue():
    context = make_context([b'one', b'two', b'three'])
    item_id = context['revision'].requirement_items[0].requirement_item_id
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return response(content='not-json', usage=USAGE)
        if len(calls) == 2:
            return response({'assessments': 'invalid'}, usage=USAGE)
        return response({'assessments': [assessment(item_id, chunk_id='chunk-2-0')]}, usage=USAGE)

    report = execute(context, handler)
    assert len(calls) == 3
    assert report.status == 'partial'
    assert [repository.status for repository in report.repositories] == ['failed', 'failed', 'completed']
    assert report.repositories[0].error_code == 'PROVIDER_INVALID_JSON'
    assert report.repositories[1].error_code == 'DEEP_REVIEW_INVALID_OUTPUT'
    assert report.repositories[0].usage.total_tokens == 10
    assert report.repositories[1].usage.total_tokens == 10
    assert report.summary.usage.total_tokens == 30


def test_truncated_output_preserves_usage_and_does_not_retry():
    context = make_context([b'one', b'two'])
    item_id = context['revision'].requirement_items[0].requirement_item_id
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return response({'assessments': [assessment(item_id, chunk_id='chunk-0-0')]},
                            usage=USAGE, finish_reason='length')
        return response({'assessments': [assessment(item_id, chunk_id='chunk-1-0')]}, usage=USAGE)

    report = execute(context, handler)
    assert len(calls) == 2
    assert report.status == 'partial'
    assert report.repositories[0].status == 'failed'
    assert report.repositories[0].error_code == 'PROVIDER_OUTPUT_LIMIT'
    assert report.repositories[0].usage.total_tokens == 10
    assert report.repositories[1].status == 'completed'


@pytest.mark.parametrize('usage', [None, {}, {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 9}])
def test_unknown_usage_stays_null_without_changing_success_status(usage):
    context = make_context([b'README'])
    item_id = context['revision'].requirement_items[0].requirement_item_id

    def handler(request):
        return response({'assessments': [assessment(item_id, chunk_id='chunk-0-0')]}, usage=usage)

    report = execute(context, handler)
    repository = report.repositories[0]
    assert report.status == 'completed'
    assert repository.status == 'completed'
    assert repository.usage.known is False
    assert repository.usage.prompt_tokens is None
    assert repository.usage.completion_tokens is None
    assert repository.usage.total_tokens is None
    assert report.summary.usage.calls == 1
    assert report.summary.usage.unknown_calls == 1
    assert report.summary.usage.total_tokens == 0


def test_summary_usage_adds_only_known_calls():
    context = make_context([b'one', b'two'])
    item_id = context['revision'].requirement_items[0].requirement_item_id
    calls = []

    def handler(request):
        calls.append(request)
        chunk_id = f'chunk-{len(calls) - 1}-0'
        return response({'assessments': [assessment(item_id, chunk_id=chunk_id)]},
                        usage=None if len(calls) == 1 else USAGE)

    report = execute(context, handler)
    assert report.status == 'completed'
    assert report.summary.usage.calls == 2
    assert report.summary.usage.unknown_calls == 1
    assert report.summary.usage.prompt_tokens == 7
    assert report.summary.usage.completion_tokens == 3
    assert report.summary.usage.total_tokens == 10


@pytest.mark.parametrize('kind,rejection', [
    ('unknown_chunk', 'unknown_chunk'), ('line', 'line_out_of_range'), ('missing', None),
])
def test_invalid_citations_downgrade_to_insufficient_but_repository_completes(kind, rejection):
    context = make_context([b'README evidence'])
    item_id = context['revision'].requirement_items[0].requirement_item_id

    def handler(request):
        if kind == 'unknown_chunk':
            draft = assessment(item_id, chunk_id='invented')
        elif kind == 'line':
            draft = assessment(item_id, chunk_id='chunk-0-0', line_start=99, line_end=99)
        else:
            draft = assessment(item_id, cite=False)
        return response({'assessments': [draft]})

    report = execute(context, handler)
    repository = report.repositories[0]
    review = repository.requirements[0]
    assert report.status == 'completed'
    assert repository.status == 'completed'
    assert review.verdict == 'insufficient'
    assert review.reason_code == 'citation_rejected'
    assert review.citations == []
    if rejection:
        assert review.rejected_citations[0].reason == rejection
    else:
        assert review.rejected_citations == []
    assert report.summary.insufficient == 1


@pytest.mark.parametrize('operator,verdicts,expected', [
    ('ALL', ['supports', 'contradicts', 'insufficient'], 'fail'),
    ('ANY', ['supports', 'insufficient', 'insufficient'], 'pass'),
])
def test_all_any_truth_tables_are_computed_by_python(operator, verdicts, expected):
    specs = [{'text': f'需求 {index}', 'operator': operator, 'logic_group_id': 'group-1'}
             for index in range(3)]
    context = make_context([b'README evidence'], specs)
    items = context['revision'].requirement_items

    def handler(request):
        return response({'assessments': [
            assessment(item.requirement_item_id, verdict, chunk_id='chunk-0-0')
            for item, verdict in zip(items, verdicts)]})

    report = execute(context, handler)
    result = report.repositories[0].logic_group_results[0]
    assert result.logic_group_id == 'group-1'
    assert result.operator == operator
    assert result.result == expected
    assert report.status == 'completed'


def test_prohibited_absence_is_insufficient_and_unknown_hard_check():
    context = make_context([b'README evidence'], [{'text': '不得使用 PHP', 'modality': 'prohibited'}])
    item_id = context['revision'].requirement_items[0].requirement_item_id

    def handler(request):
        return response({'assessments': [assessment(item_id, 'insufficient')]})

    report = execute(context, handler)
    review = report.repositories[0].requirements[0]
    assert review.verdict == 'insufficient'
    assert review.hard_constraint_check == 'unknown'
    assert report.repositories[0].hard_constraint_summary.prohibited.unknown == 1
    assert report.status == 'completed'


def test_omitted_duplicate_and_unknown_assessments_do_not_mark_repository_partial():
    specs = [{'text': '需求一'}, {'text': '需求二'}]
    context = make_context([b'README evidence'], specs)
    first, second = [item.requirement_item_id for item in context['revision'].requirement_items]

    def handler(request):
        return response({'assessments': [
            assessment(first, chunk_id='chunk-0-0'),
            assessment(first, chunk_id='chunk-0-0'),
            assessment(uuid4(), 'insufficient'),
        ]})

    report = execute(context, handler)
    assert report.status == 'completed'
    assert report.repositories[0].status == 'completed'
    assert [review.verdict for review in report.repositories[0].requirements] == [
        'insufficient', 'insufficient']
    assert report.repositories[0].requirements[1].model_omitted is True
    assert report.summary.model_omitted == 1
    assert report.summary.duplicates == 1
    assert report.summary.unknown_items == 1
    assert {'duplicate_item', 'model_omitted', 'unknown_item'} <= {
        warning.code for warning in report.warnings}


@pytest.mark.parametrize('field', ['content_hash', 'blob_sha'])
def test_snapshot_integrity_mismatch_is_rejected_before_provider_call(field):
    context = make_context([b'README'])
    context['bundle']['candidates'][0][field] = '0' * (64 if field == 'content_hash' else 40)

    def handler(request):
        raise AssertionError('integrity failure must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler)
    assert error.value.code == 'DEEP_REVIEW_SNAPSHOT_INTEGRITY'


@pytest.mark.parametrize('raw,code', [(b'\xff', 'DEEP_REVIEW_NON_UTF8'),
                                     (b'', 'DEEP_REVIEW_EMPTY_README')])
def test_non_utf8_and_empty_snapshots_are_rejected_before_provider_call(raw, code):
    context = make_context([b'README'])
    revision = context['revision']
    item_id = str(revision.requirement_items[0].requirement_item_id)
    chunk = {'chunk_id': 'bad', 'text': 'x', 'line_start': 1, 'line_end': 1,
             'byte_start': 0, 'byte_end': 1, 'split': 'bytes'}
    candidate = make_candidate(raw, 0, [item_id], [chunk])
    context['bundle']['candidates'] = [candidate]
    context['snapshots'] = {candidate['snapshot_id']: raw}

    def handler(request):
        raise AssertionError('invalid snapshot must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler)
    assert error.value.code == code


@pytest.mark.parametrize('key,value,code', [
    ('version', 'wrong', 'DEEP_REVIEW_BINDING_MISMATCH'),
    ('schema_version', 3, 'DEEP_REVIEW_BINDING_MISMATCH'),
    ('owner', 'other', 'DEEP_REVIEW_BINDING_MISMATCH'),
    ('task_id', str(uuid4()), 'DEEP_REVIEW_BINDING_MISMATCH'),
    ('run_id', 'other', 'DEEP_REVIEW_BINDING_MISMATCH'),
    ('confirmed_revision_id', str(uuid4()), 'DEEP_REVIEW_BINDING_MISMATCH'),
    ('revision_hash', '0' * 64, 'DEEP_REVIEW_BINDING_MISMATCH'),
    ('plan_hash', '', 'DEEP_REVIEW_INVALID_BUNDLE'),
])
def test_bundle_binding_mismatch_is_rejected_before_provider_call(key, value, code):
    context = make_context([b'README'])
    context['bundle'][key] = value

    def handler(request):
        raise AssertionError('binding mismatch must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler)
    assert error.value.code == code


@pytest.mark.parametrize('bundle_hash', ['short', 'g' * 64])
def test_invalid_bundle_hash_is_rejected_before_provider_call(bundle_hash):
    context = make_context([b'README'])

    def handler(request):
        raise AssertionError('invalid bundle hash must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler, bundle_hash=bundle_hash)
    assert error.value.code == 'DEEP_REVIEW_INVALID_BUNDLE_HASH'


def test_unconfirmed_revision_is_rejected_before_provider_call():
    context = make_context([b'README'])
    revision = make_revision(confirmed=False)

    def handler(request):
        raise AssertionError('unconfirmed revision must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler, revision=revision)
    assert error.value.code == 'DEEP_REVIEW_REVISION_NOT_CONFIRMED'


def test_requirement_quality_failure_is_rejected_before_provider_call():
    context = make_context([b'README'])
    revision = context['revision'].model_copy(deep=True)
    revision.requirement_items[0].evidence_ids = []

    def handler(request):
        raise AssertionError('invalid requirement evidence must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler, revision=revision)
    assert error.value.code == 'DEEP_REVIEW_REQUIREMENT_QUALITY'


def test_conflicting_duplicate_chunk_is_rejected_before_provider_call():
    specs = [{'text': '需求一'}, {'text': '需求二'}]
    context = make_context([b'README'], specs)
    revision = context['revision']
    first, second = [str(item.requirement_item_id) for item in revision.requirement_items]
    chunk = make_chunk(context['snapshots'][context['bundle']['candidates'][0]['snapshot_id']], 'same')
    candidate = make_candidate(b'README', 0, [first, second], [])
    candidate['items'][0]['chunks'] = [chunk]
    candidate['items'][1]['chunks'] = [{**chunk, 'text': 'conflict'}]
    context['bundle']['candidates'] = [candidate]
    context['snapshots'] = {candidate['snapshot_id']: b'README'}

    def handler(request):
        raise AssertionError('conflicting chunks must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler)
    assert error.value.code == 'DEEP_REVIEW_CHUNK_CONFLICT'


def test_unknown_bundle_requirement_item_is_rejected_before_provider_call():
    context = make_context([b'README'])
    context['bundle']['candidates'][0]['items'][0]['requirement_item_id'] = str(uuid4())

    def handler(request):
        raise AssertionError('unknown bundle item must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler)
    assert error.value.code == 'DEEP_REVIEW_UNKNOWN_BUNDLE_ITEM'


def test_string_chunk_coordinates_are_rejected_before_provider_call():
    context = make_context([b'README'])
    chunk = context['bundle']['candidates'][0]['items'][0]['chunks'][0]
    chunk['byte_start'] = '0'

    def handler(request):
        raise AssertionError('invalid chunk coordinates must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler)
    assert error.value.code == 'DEEP_REVIEW_INVALID_CHUNK'


def test_character_split_chunk_is_rejected_before_provider_call():
    context = make_context([b'README'])
    context['bundle']['candidates'][0]['items'][0]['chunks'][0]['split'] = 'chars'

    def handler(request):
        raise AssertionError('character split chunk must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler)
    assert error.value.code == 'DEEP_REVIEW_INVALID_CHUNK'


def test_duplicate_repository_candidate_is_rejected_before_provider_call():
    context = make_context([b'one', b'two'])
    first, second = context['bundle']['candidates']
    second['repository_id'] = first['repository_id']

    def handler(request):
        raise AssertionError('duplicate repository must not call the provider')

    with pytest.raises(DeepReviewInputError) as error:
        execute(context, handler)
    assert error.value.code == 'DEEP_REVIEW_DUPLICATE_REPOSITORY'
