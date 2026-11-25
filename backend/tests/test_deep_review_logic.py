from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.requirements import RequirementItem
from app.services.deep_review_logic import (DraftAssessment, DraftCitation, DraftRepositoryOutput,
                                            build_ranking_results, chunk_is_usable,
                                            compile_requirement_reviews,
                                            estimate_input_bytes_upper_bound, hard_check,
                                            hard_constraint_summary, line_table, logic_group_results,
                                            normalized_readme_bytes, operation_repository_budget,
                                            repository_budget, truth_table, validate_citation,
                                            DeepReviewInputError)
from app.services.deep_review_models import (BudgetLimits, Citation, DeepReviewReport, HardConstraintSummary, ReportWarning,
                                             HardCounts, PreferredCounts, ReportSummary, RepositoryInput,
                                             RepositoryReview, RepositoryUsage, RequirementReview,
                                             SnapshotTrace, SummaryUsage)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
REPO, SNAP = '7', 's1'


def uid():
    return str(uuid4())


def requirement(text='Build a project tracker.', modality='required', operator='not_applicable',
                group=None, item_id=None, field='required_features'):
    return RequirementItem(requirement_item_id=item_id or uid(), field=field,
                           value_path=f'{field}[0]', text=text, modality=modality,
                           source_kind='assignment_document', evidence_ids=[], logic_group_id=group,
                           operator=operator, supersedes_item_id=None)


def chunks(raw='第一行\r\nEmoji \U0001F600 line\r\nthird\n'):
    data = normalized_readme_bytes(raw.encode())
    table = line_table(data)
    rows = []
    for start_line, end_line in ((1, 1), (2, 2), (1, 2)):
        start, end = table[start_line - 1][0], table[end_line - 1][1]
        rows.append({'chunk_id': f'c{start_line}{end_line}', 'repository_id': REPO,
                     'snapshot_id': SNAP, 'byte_start': start, 'byte_end': end,
                     'line_start': start_line, 'line_end': end_line, 'split': 'block',
                     'text': data[start:end].decode('utf-8')})
    return raw.encode(), data, table, rows


def compile_output(output, items, rows, raw, repository_id=REPO, snapshot_id=SNAP):
    return compile_requirement_reviews(output, items, rows, raw,
                                       repository_id=repository_id, snapshot_id=snapshot_id)


def test_utf8_bytes_lines_emoji_and_chunk_identity():
    raw, data, table, rows = chunks()
    valid, rejection = validate_citation(DraftCitation(chunk_id='c22', line_start=2, line_end=2),
                                         rows[1], data, table)
    assert rejection is None
    assert valid.chunk_id == 'c22' and valid.quote == 'Emoji 😀 line'
    dumped = valid.model_dump()
    assert dumped['chunk_id'] == 'c22' and dumped['quote'] == 'Emoji 😀 line'
    assert data[valid.byte_start:valid.byte_end].decode() == valid.quote
    assert valid.byte_start >= rows[1]['byte_start'] and valid.byte_end <= rows[1]['byte_end']


def test_citation_chunk_identity_is_preserved_in_report():
    raw, data, table, rows = chunks()
    item = requirement()
    output = DraftRepositoryOutput(assessments=[DraftAssessment(
        requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
        citations=[DraftCitation(chunk_id='c11', line_start=1, line_end=1)])])
    review = compile_output(output, [item], rows, raw)['requirements'][0]
    assert review['citations'][0]['chunk_id'] == 'c11'
    assert review['citations'][0]['quote'] == '第一行'


def test_last_line_without_newline_and_cr_only():
    for raw_text, expected in (('first\nlast', 'last'), ('first\rlast\r', 'last')):
        raw = raw_text.encode()
        data = normalized_readme_bytes(raw)
        table = line_table(data)
        row = {'chunk_id': 'c2', 'repository_id': REPO, 'snapshot_id': SNAP,
               'byte_start': table[1][0], 'byte_end': table[1][1], 'line_start': 2, 'line_end': 2,
               'split': 'block', 'text': data[table[1][0]:table[1][1]].decode()}
        valid, rejection = validate_citation(DraftCitation(chunk_id='c2', line_start=2, line_end=2),
                                             row, data, table)
        assert rejection is None and valid.quote == expected


def whole_chunk(raw_text, chunk_id='whole'):
    data = normalized_readme_bytes(raw_text.encode('utf-8'))
    return data, {'chunk_id': chunk_id, 'repository_id': REPO, 'snapshot_id': SNAP,
                  'byte_start': 0, 'byte_end': len(data), 'line_start': 1,
                  'line_end': len(data.split(b'\n')), 'split': 'block',
                  'text': data.decode('utf-8')}


@pytest.mark.parametrize('raw_text,cited,expected_quote,expected_lines', [
    ('alpha\nbeta\ngamma', (1, 3), 'alpha\nbeta\ngamma', (1, 3)),
    ('alpha\nbeta\n', (1, 3), 'alpha\nbeta', (1, 2)),
    ('alpha\nbeta\n\n\n', (1, 5), 'alpha\nbeta', (1, 2)),
    ('alpha\n\nbeta', (1, 3), 'alpha\n\nbeta', (1, 3)),
    ('第一行\r\nEmoji \U0001F600\r\n\r\n', (1, 4), '第一行\nEmoji \U0001F600', (1, 2)),
])
def test_citation_line_end_excludes_trailing_blank_lines(raw_text, cited, expected_quote,
                                                         expected_lines):
    data, chunk = whole_chunk(raw_text)
    valid, rejection = validate_citation(
        DraftCitation(chunk_id='whole', line_start=cited[0], line_end=cited[1]),
        chunk, data, line_table(data))
    assert rejection is None
    assert (valid.line_start, valid.line_end) == expected_lines
    expected_bytes = expected_quote.encode('utf-8')
    assert valid.quote == expected_quote
    assert (valid.byte_start, valid.byte_end) == (0, len(expected_bytes))
    assert data[valid.byte_start:valid.byte_end] == expected_bytes


def test_volga_trailing_blank_line_regression():
    quote = ('```json\n'
             '{\n'
             '  "segments": [\n'
             '    {"id": 0, "start": 0.0, "end": 5.08}\n'
             '  ]\n'
             '}\n'
             '```')
    raw_text = '## Output\n' + quote + '\n\n'
    data, chunk = whole_chunk(raw_text)
    valid, rejection = validate_citation(
        DraftCitation(chunk_id='whole', line_start=2, line_end=9),
        chunk, data, line_table(data))
    assert rejection is None
    assert (valid.line_start, valid.line_end) == (2, 8)
    assert valid.quote == quote
    expected_start = len('## Output\n'.encode('utf-8'))
    assert (valid.byte_start, valid.byte_end) == (
        expected_start, expected_start + len(quote.encode('utf-8')))


def test_citation_respects_chunk_bounds_after_trimming():
    data = normalized_readme_bytes(b'one\ntwo\nthree')
    table = line_table(data)
    chunk = {'chunk_id': 'c', 'repository_id': REPO, 'snapshot_id': SNAP,
             'byte_start': table[1][0], 'byte_end': table[2][1], 'line_start': 2,
             'line_end': 3, 'split': 'block', 'text': 'two\nthree'}
    valid, rejection = validate_citation(
        DraftCitation(chunk_id='c', line_start=2, line_end=3), chunk, data, table)
    assert rejection is None and valid.quote == 'two\nthree'
    _, rejection = validate_citation(
        DraftCitation(chunk_id='c', line_start=1, line_end=1), chunk, data, table)
    assert rejection.reason == 'line_out_of_range'


def test_citation_of_only_blank_lines_is_rejected():
    data, chunk = whole_chunk('a\n\n\n')
    table = line_table(data)
    for start, end in ((2, 4), (3, 4), (4, 4)):
        valid, rejection = validate_citation(
            DraftCitation(chunk_id='whole', line_start=start, line_end=end), chunk, data, table)
        assert valid is None and rejection.reason == 'empty_quote'


def test_empty_line_and_inconsistent_chunk_are_rejected():
    data = normalized_readme_bytes(b'a\n\nb')
    table = line_table(data)
    empty = {'chunk_id': 'e', 'repository_id': REPO, 'snapshot_id': SNAP,
             'byte_start': table[0][0], 'byte_end': table[2][1], 'line_start': 1, 'line_end': 3,
             'split': 'block', 'text': 'a\n\nb'}
    _, rejection = validate_citation(DraftCitation(chunk_id='e', line_start=2, line_end=2),
                                     empty, data, table)
    assert rejection.reason == 'empty_quote'
    broken = {**empty, 'line_start': 2}
    usable, _ = chunk_is_usable(broken, data, table)
    assert usable is False


def test_cross_repository_and_cross_snapshot_are_rejected():
    raw, data, table, rows = chunks()
    item = requirement()
    for rows_variant, reason in (( [{**row, 'repository_id': '8'} for row in rows], 'cross_repository'),
                                 ( [{**row, 'snapshot_id': 'other'} for row in rows], 'cross_snapshot')):
        output = DraftRepositoryOutput(assessments=[DraftAssessment(
            requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
            citations=[DraftCitation(chunk_id='c11', line_start=1, line_end=1)])])
        review = compile_output(output, [item], rows_variant, raw)['requirements'][0]
        assert review['verdict'] == 'insufficient' and review['reason_code'] == 'citation_rejected'
        assert review['rejected_citations'][0]['reason'] == reason


def test_chunk_outside_input_set_is_rejected():
    raw, data, table, rows = chunks()
    item = requirement()
    output = DraftRepositoryOutput(assessments=[DraftAssessment(
        requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
        citations=[DraftCitation(chunk_id='not-input', line_start=1, line_end=1)])])
    review = compile_output(output, [item], rows, raw)['requirements'][0]
    assert review['verdict'] == 'insufficient'
    assert review['rejected_citations'][0]['reason'] == 'unknown_chunk'


def test_compile_keeps_valid_and_rejects_other():
    raw, data, table, rows = chunks()
    item = requirement()
    output = DraftRepositoryOutput(assessments=[DraftAssessment(
        requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
        citations=[DraftCitation(chunk_id='c11', line_start=1, line_end=1),
                   DraftCitation(chunk_id='c11', line_start=3, line_end=3)])])
    result = compile_output(output, [item], rows, raw)
    review = result['requirements'][0]
    assert review['verdict'] == 'supports' and review['hard_constraint_check'] == 'pass'
    assert len(review['citations']) == 1 and review['rejected_citations']
    assert result['status'] == 'completed'


def test_supports_without_citation_is_downgraded():
    raw, data, table, rows = chunks()
    item = requirement()
    output = DraftRepositoryOutput(assessments=[DraftAssessment(
        requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation', citations=[])])
    review = compile_output(output, [item], rows, raw)['requirements'][0]
    assert review['verdict'] == 'insufficient' and review['reason_code'] == 'citation_rejected'


def test_duplicate_assessment_is_untrusted_and_partial():
    raw, data, table, rows = chunks()
    item = requirement()
    output = DraftRepositoryOutput(assessments=[
        DraftAssessment(requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
                        citations=[DraftCitation(chunk_id='c11', line_start=1, line_end=1)]),
        DraftAssessment(requirement_item_id=item.requirement_item_id, verdict='contradicts', reason_code='conflicted_by_citation',
                        citations=[DraftCitation(chunk_id='c11', line_start=1, line_end=1)])])
    result = compile_output(output, [item], rows, raw)
    review = result['requirements'][0]
    assert review['reason_code'] == 'model_output_incomplete'
    assert any(w['code'] == 'duplicate_item' for w in result['warnings'])
    assert result['status'] == 'completed'


def test_missing_and_unknown_items():
    raw, data, table, rows = chunks()
    first, second = requirement(), requirement('Other feature')
    output = DraftRepositoryOutput(assessments=[
        DraftAssessment(requirement_item_id=first.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
                        citations=[DraftCitation(chunk_id='c11', line_start=1, line_end=1)]),
        DraftAssessment(requirement_item_id=uid(), verdict='supports', reason_code='supported_by_citation',
                        citations=[DraftCitation(chunk_id='c11', line_start=1, line_end=1)]),
        DraftAssessment(requirement_item_id=uid(), verdict='supports', reason_code='supported_by_citation',
                        citations=[DraftCitation(chunk_id='c11', line_start=1, line_end=1)])])
    result = compile_output(output, [first, second], rows, raw)
    assert result['requirements'][1]['model_omitted'] is True
    unknown = [w for w in result['warnings'] if w['code'] == 'unknown_item']
    assert len(unknown) == result['counts']['unknown_items'] == 2
    assert result['status'] == 'completed'


def test_prohibited_insufficient_is_unknown_not_pass():
    assert hard_check('prohibited', 'insufficient') == 'unknown'
    assert hard_check('prohibited', 'supports') == 'pass'
    assert hard_check('prohibited', 'contradicts') == 'fail'
    assert hard_check('preferred', 'supports') == 'not_applicable'


@pytest.mark.parametrize('operator,checks,expected', [
    ('ALL', ['pass', 'pass'], 'pass'), ('ALL', ['pass', 'fail'], 'fail'),
    ('ALL', ['pass', 'unknown'], 'unknown'), ('ALL', [], 'unknown'),
    ('ANY', ['pass', 'fail'], 'pass'), ('ANY', ['fail', 'fail'], 'fail'),
    ('ANY', ['fail', 'unknown'], 'unknown'), ('ANY', [], 'unknown'),
])
def test_truth_table(operator, checks, expected):
    assert truth_table(operator, checks) == expected


def test_logic_group_inconsistency_is_excluded():
    reviews = [
        {'logic_group_id': 'g1', 'operator': 'ALL', 'modality': 'required',
         'hard_constraint_check': 'pass', 'requirement_item_id': uid()},
        {'logic_group_id': 'g1', 'operator': 'ANY', 'modality': 'required',
         'hard_constraint_check': 'pass', 'requirement_item_id': uid()},
    ]
    results, warnings = logic_group_results(reviews)
    assert results == [] and warnings[0]['code'] == 'logic_group_inconsistent'


def test_hard_summary_counts_preferred_separately():
    base = {'verdict': 'supports', 'logic_group_id': None, 'operator': 'not_applicable'}
    reviews = [{**base, 'modality': 'required', 'hard_constraint_check': 'pass'},
               {**base, 'modality': 'prohibited', 'hard_constraint_check': 'unknown',
                'verdict': 'insufficient'},
               {**base, 'modality': 'preferred', 'hard_constraint_check': 'not_applicable'}]
    summary = hard_constraint_summary(reviews)
    assert summary['required'] == {'pass': 1, 'fail': 0, 'unknown': 0, 'total': 1}
    assert summary['prohibited'] == {'pass': 0, 'fail': 0, 'unknown': 1, 'total': 1}
    assert summary['preferred'] == {'evidence_found': 1, 'unknown': 0, 'total': 1}




def test_all_citations_rejected_warns_and_partial_status():
    raw, data, table, rows = chunks()
    item = requirement()
    output = DraftRepositoryOutput(assessments=[DraftAssessment(
        requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
        citations=[DraftCitation(chunk_id='c11', line_start=3, line_end=3)])])
    result = compile_output(output, [item], rows, raw)
    assert result['requirements'][0]['reason_code'] == 'citation_rejected'
    assert any(w['code'] == 'citations_rejected' for w in result['warnings'])
    assert result['status'] == 'completed'


def test_partial_citation_rejected_warns_and_partial_status():
    raw, data, table, rows = chunks()
    item = requirement()
    output = DraftRepositoryOutput(assessments=[DraftAssessment(
        requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
        citations=[DraftCitation(chunk_id='c11', line_start=1, line_end=1),
                   DraftCitation(chunk_id='c11', line_start=3, line_end=3)])])
    result = compile_output(output, [item], rows, raw)
    assert any(w['code'] == 'partial_citations_rejected' for w in result['warnings'])
    assert result['status'] == 'completed'


def test_all_citations_valid_is_ok_without_citation_warnings():
    raw, data, table, rows = chunks()
    item = requirement()
    output = DraftRepositoryOutput(assessments=[DraftAssessment(
        requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
        citations=[DraftCitation(chunk_id='c11', line_start=1, line_end=1)])])
    result = compile_output(output, [item], rows, raw)
    assert result['status'] == 'completed'
    assert not any(w['code'] in {'citations_rejected', 'citations_missing',
                                 'partial_citations_rejected'} for w in result['warnings'])


def test_missing_citations_warn_and_partial():
    raw, data, table, rows = chunks()
    item = requirement()
    output = DraftRepositoryOutput(assessments=[DraftAssessment(
        requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation', citations=[])])
    result = compile_output(output, [item], rows, raw)
    assert any(w['code'] == 'citations_missing' for w in result['warnings'])
    assert result['status'] == 'completed'


def test_byte_budget_uses_explicit_upper_bound_and_output_limit():
    assert estimate_input_bytes_upper_bound('中', 'ab') == 5
    assert repository_budget(1, 24000) is None
    assert repository_budget(1, 24001)['error_code'] == 'DEEP_REVIEW_INPUT_BYTES_BUDGET'
    assert repository_budget(1, 100, output_tokens=2001)['error_code'] == 'DEEP_REVIEW_OUTPUT_TOKENS_BUDGET'
    assert repository_budget(1, 100, output_tokens=2000) is None



def test_duplicate_chunk_id_is_rejected_regardless_of_order():
    raw, data, table, rows = chunks()
    item = requirement()
    first = rows[0]
    second = {**first, 'repository_id': '8'}
    for ordered in ([first, second], [second, first]):
        output = DraftRepositoryOutput(assessments=[DraftAssessment(
            requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
            citations=[DraftCitation(chunk_id=first['chunk_id'], line_start=1, line_end=1)])])
        with pytest.raises(DeepReviewInputError) as error:
            compile_output(output, [item], ordered, raw)
        assert error.value.code == 'DEEP_REVIEW_DUPLICATE_CHUNK_ID'
        assert error.value.details['chunk_ids'] == [first['chunk_id']]


def test_duplicate_chunk_across_snapshot_is_rejected():
    raw, data, table, rows = chunks()
    item = requirement()
    second = {**rows[0], 'snapshot_id': 'other'}
    with pytest.raises(DeepReviewInputError):
        compile_output(DraftRepositoryOutput(assessments=[]), [item], [rows[0], second], raw)


def test_duplicate_chunk_cannot_turn_cross_repository_citation_into_supports():
    raw, data, table, rows = chunks()
    item = requirement()
    own = rows[0]
    foreign = {**own, 'repository_id': '8'}
    output = DraftRepositoryOutput(assessments=[DraftAssessment(
        requirement_item_id=item.requirement_item_id, verdict='supports', reason_code='supported_by_citation',
        citations=[DraftCitation(chunk_id=own['chunk_id'], line_start=1, line_end=1)])])
    with pytest.raises(DeepReviewInputError):
        compile_output(output, [item], [own, foreign], raw)


def test_budget_limits_stay_strict_while_inputs_can_report_over_budget():
    with pytest.raises(Exception):
        BudgetLimits(max_chunks=17, max_input_bytes=24000, max_output_tokens=2000)
    with pytest.raises(Exception):
        BudgetLimits(max_chunks=16, max_input_bytes=24001, max_output_tokens=2000)
    with pytest.raises(Exception):
        BudgetLimits(max_chunks=16, max_input_bytes=24000, max_output_tokens=2001)
    oversized = RepositoryInput(
        chunk_ids=['a'], chunk_count=1, input_bytes_upper_bound=24001,
        budget=BudgetLimits(max_chunks=16, max_input_bytes=24000, max_output_tokens=2000))
    assert oversized.input_bytes_upper_bound == 24001
    assert repository_budget(1, 24001)['status'] == 'skipped'

def test_budgets_are_pre_call_rejections():
    assert repository_budget(16, 1000) is None
    assert repository_budget(17, 1000)['status'] == 'skipped'
    assert repository_budget(1, 24001)['error_code'] == 'DEEP_REVIEW_INPUT_BYTES_BUDGET'
    assert operation_repository_budget(0)['error_code'] == 'DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES'
    assert operation_repository_budget(6)['error_code'] == 'DEEP_REVIEW_REPOSITORY_BUDGET'
    assert operation_repository_budget(5) is None


def report():
    item = requirement()
    citation = Citation(chunk_id='c11', quote='第一行', line_start=1, line_end=1,
                        byte_start=0, byte_end=9)
    review = RequirementReview(requirement_item_id=item.requirement_item_id, field='required_features',
                               text=item.text, modality='required', operator='not_applicable',
                               logic_group_id=None, verdict='supports',
                               reason_code='supported_by_citation', hard_constraint_check='pass',
                               citations=[citation])
    summary = HardConstraintSummary(
        required=HardCounts(passed=1, failed=0, unknown=0, total=1),
        prohibited=HardCounts(passed=0, failed=0, unknown=0, total=0),
        preferred=PreferredCounts(evidence_found=0, unknown=0, total=0))
    repository = RepositoryReview(
        repository_id=REPO, snapshot_id=SNAP,
        trace=SnapshotTrace(commit_sha='c', blob_sha='b', content_hash='h',
                            readme_path='README.md', collected_at=NOW.isoformat()),
        coverage='full', status='completed',
        input=RepositoryInput(chunk_ids=['c11'], chunk_count=1, input_bytes_upper_bound=100,
                              budget=BudgetLimits(max_chunks=16, max_input_bytes=24000,
                                                  max_output_tokens=2000)),
        requirements=[review], hard_constraint_summary=summary, logic_group_results=[],
        usage=RepositoryUsage(known=True, prompt_tokens=1, completion_tokens=1, total_tokens=2))
    return DeepReviewReport(owner='a' * 64, task_id=uid(), run_id=uid(), confirmed_revision_id=uid(),
                            operation_id=uid(), revision_hash='r' * 64, plan_hash='p' * 64,
                            bundle_hash='b' * 64, model='deepseek-flash', prompt_version='v',
                            notice='docs only', created_at=NOW.isoformat(), status='partial',
                            repositories=[repository],
                            summary=ReportSummary(usage=SummaryUsage(calls=1, unknown_calls=0,
                                                                     prompt_tokens=1,
                                                                     completion_tokens=1,
                                                                     total_tokens=2)))


def test_report_models_serialize_with_chunk_id_and_quote():
    payload = report().model_dump(mode='json', by_alias=True)
    citation = payload['repositories'][0]['requirements'][0]['citations'][0]
    assert citation['chunk_id'] == 'c11' and citation['quote'] == '第一行'
    assert payload['status'] == 'partial'
    assert payload['summary']['repositories_completed'] == 0
    assert payload['cost_status'] == 'not_calculated'
    assert payload['estimated_cost'] is None and payload['currency'] is None
    assert payload['pricing_source'] is None
    with pytest.raises(Exception):
        DeepReviewReport(**{**report().model_dump(), 'estimated_cost': 1.0})
    with pytest.raises(Exception):
        DeepReviewReport(**{**report().model_dump(), 'cost_status': 'calculated'})


def test_report_serializes_pass_fail_keys_and_strict_warnings():
    payload = report().model_dump(mode='json')
    required = payload['repositories'][0]['hard_constraint_summary']['required']
    assert required['pass'] == 1 and required['fail'] == 0 and 'passed' not in required
    with pytest.raises(Exception):
        ReportWarning.model_validate({'code': 'x', 'extra': 1})
    with pytest.raises(Exception):
        RepositoryInput(chunk_ids=['a', 'a'], chunk_count=2, input_bytes_upper_bound=1,
                        budget=BudgetLimits(max_chunks=16, max_input_bytes=1, max_output_tokens=1))
    with pytest.raises(Exception):
        RepositoryUsage(known=True, prompt_tokens=1, completion_tokens=1, total_tokens=3)
    with pytest.raises(Exception):
        SummaryUsage(calls=1, unknown_calls=0, prompt_tokens=1, completion_tokens=1, total_tokens=3)


def test_nested_models_reject_unknown_missing_and_bad_types():
    with pytest.raises(Exception):
        SnapshotTrace(commit_sha='c', blob_sha='b', content_hash='h', readme_path='README.md',
                      collected_at='x', extra=True)
    with pytest.raises(Exception):
        SnapshotTrace(commit_sha='c', blob_sha='b', content_hash='h', readme_path='README.md')
    with pytest.raises(Exception):
        RepositoryUsage(known=False, prompt_tokens=1)
    with pytest.raises(Exception):
        HardCounts(passed=1, failed=0, unknown=0, total=2)
    with pytest.raises(Exception):
        RepositoryUsage(known='yes', prompt_tokens=None)
    with pytest.raises(Exception):
        DeepReviewReport(**{**report().model_dump(), 'status': 'running'})


def ranking_review(modality='required', verdict='supports', *, cite=True, group=None,
                   operator='not_applicable', item_id=None):
    item_id = item_id or uid()
    quote = f'evidence for {item_id}'
    citations = [Citation(chunk_id=f'chunk-{item_id}', quote=quote, line_start=1, line_end=1,
                          byte_start=0, byte_end=len(quote.encode('utf-8')))] if cite and verdict != 'insufficient' else []
    reason = {'supports': 'supported_by_citation', 'contradicts': 'conflicted_by_citation',
              'insufficient': 'not_found_in_provided_evidence'}[verdict]
    field = {'required': 'required_features', 'prohibited': 'required_technologies',
             'preferred': 'preferred_features', 'informational': 'project_topic'}[modality]
    return RequirementReview(
        requirement_item_id=item_id, field=field, text=f'{modality} {item_id}', modality=modality,
        operator=operator, logic_group_id=group, verdict=verdict, reason_code=reason,
        hard_constraint_check=hard_check(modality, verdict), citations=citations)


def ranking_repository(repository_id, reviews, *, coverage='full', status='completed'):
    dumped = [review.model_dump() for review in reviews]
    groups, _ = logic_group_results(dumped)
    chunk_ids = sorted({citation.chunk_id for review in reviews for citation in review.citations})
    return RepositoryReview(
        repository_id=repository_id, snapshot_id=f'snapshot-{repository_id}',
        trace=SnapshotTrace(commit_sha='c', blob_sha='b', content_hash='h',
                            readme_path='README.md', collected_at=NOW.isoformat()),
        coverage=coverage, status=status,
        input=RepositoryInput(chunk_ids=chunk_ids, chunk_count=len(chunk_ids),
                              input_bytes_upper_bound=100,
                              budget=BudgetLimits(max_chunks=16, max_input_bytes=24000,
                                                  max_output_tokens=2000)),
        requirements=reviews,
        hard_constraint_summary=HardConstraintSummary.model_validate(
            hard_constraint_summary(dumped)),
        logic_group_results=groups, usage=RepositoryUsage(known=False))


def test_ranking_three_tiers_and_preferred_cannot_offset_conflict():
    meets = ranking_repository('meets', [ranking_review('required', 'supports')])
    needs = ranking_repository('needs', [ranking_review('required', 'insufficient')] + [
        ranking_review('preferred', 'supports') for _ in range(5)])
    conflict = ranking_repository('conflict', [ranking_review('required', 'contradicts')] + [
        ranking_review('preferred', 'supports') for _ in range(9)])
    result = build_ranking_results([conflict, needs, meets])
    assert [row.repository_id for row in result.repositories] == ['meets', 'needs', 'conflict']
    assert [row.hard_constraint_tier for row in result.repositories] == [
        'meets_requirements', 'needs_review', 'hard_conflict']
    assert result.repositories[1].preferred.evidence_match_count == 5
    assert result.repositories[2].preferred.evidence_match_count == 9


def test_prohibited_conflict_reason_uses_its_own_citation():
    required = ranking_review('required', 'supports')
    prohibited = ranking_review('prohibited', 'contradicts')
    result = build_ranking_results([ranking_repository('repo', [required, prohibited])])
    ranked = result.repositories[0]
    assert ranked.hard_constraint_tier == 'hard_conflict'
    reason = next(row for row in ranked.recommendation.reasons
                  if row.requirement_item_id == prohibited.requirement_item_id)
    assert reason.reason_code == 'HARD_CONSTRAINT_CONFLICT'
    assert reason.citations == prohibited.citations


@pytest.mark.parametrize('operator,verdicts,expected', [
    ('ALL', ['supports', 'contradicts'], 'fail'),
    ('ANY', ['supports', 'insufficient'], 'pass'),
])
def test_ranking_uses_all_any_group_as_one_hard_unit(operator, verdicts, expected):
    reviews = [ranking_review('required', verdict, group='group-1', operator=operator)
               for verdict in verdicts]
    result = build_ranking_results([ranking_repository('repo', reviews)])
    ranked = result.repositories[0]
    assert len(ranked.hard_constraint_units) == 1
    assert ranked.hard_constraint_units[0].kind == 'logic_group'
    assert ranked.hard_constraint_units[0].result == expected
    assert ranked.hard_constraint_tier == ('hard_conflict' if expected == 'fail'
                                           else 'meets_requirements')


def test_logic_group_result_must_match_member_truth_table():
    reviews = [ranking_review('required', 'supports', group='group-1', operator='ALL'),
               ranking_review('required', 'contradicts', group='group-1', operator='ALL')]
    repository = ranking_repository('repo', reviews)
    repository.logic_group_results[0] = repository.logic_group_results[0].model_copy(
        update={'result': 'pass'})
    ranked = build_ranking_results([repository]).repositories[0]
    assert ranked.hard_constraint_units[0].result == 'fail'
    assert ranked.hard_constraint_tier == 'hard_conflict'


def test_any_group_pass_does_not_report_other_members_as_repository_conflicts():
    passed = ranking_review('required', 'supports', group='group-1', operator='ANY')
    failed = ranking_review('required', 'contradicts', group='group-1', operator='ANY')
    ranked = build_ranking_results([ranking_repository('repo', [passed, failed])]).repositories[0]
    assert ranked.hard_constraint_tier == 'meets_requirements'
    assert not any(reason.reason_code == 'HARD_CONSTRAINT_CONFLICT'
                   for reason in ranked.recommendation.reasons)
    supported = next(reason for reason in ranked.recommendation.reasons
                     if reason.requirement_item_id == passed.requirement_item_id)
    assert supported.citations == passed.citations


def test_ungrouped_explicit_fail_is_not_overridden_by_supports():
    review = ranking_review('required', 'supports').model_copy(
        update={'hard_constraint_check': 'fail'})
    ranked = build_ranking_results([ranking_repository('repo', [review])]).repositories[0]
    assert ranked.hard_constraint_units[0].result == 'fail'
    assert ranked.hard_constraint_tier == 'hard_conflict'


def test_no_hard_constraints_is_needs_review_with_explicit_reason():
    preferred = ranking_review('preferred', 'supports')
    result = build_ranking_results([ranking_repository('repo', [preferred])])
    ranked = result.repositories[0]
    assert ranked.hard_constraint_tier == 'needs_review'
    assert ranked.recommendation.reason_code == 'NO_HARD_CONSTRAINTS'
    assert any(row.reason_code == 'NO_HARD_CONSTRAINTS' and not row.citations
               for row in ranked.recommendation.reasons)


def test_partial_equivalent_is_capped_unless_confirmed_conflict_exists():
    passed = ranking_review('required', 'supports')
    truncated = ranking_repository('truncated', [passed], coverage='truncated')
    conflict = ranking_repository('conflict', [ranking_review('required', 'contradicts')],
                                  coverage='truncated')
    result = build_ranking_results([conflict, truncated])
    assert [row.hard_constraint_tier for row in result.repositories] == [
        'needs_review', 'hard_conflict']


@pytest.mark.parametrize('verdicts,expected', [
    (['supports'], 'supports'),
    (['contradicts'], 'contradicts'),
    (['insufficient'], 'insufficient'),
    (['supports', 'contradicts'], 'mixed'),
])
def test_preferred_summary_states(verdicts, expected):
    reviews = [ranking_review('preferred', verdict) for verdict in verdicts]
    result = build_ranking_results([ranking_repository('repo', reviews)])
    assert result.repositories[0].preferred.status == expected


def test_no_preferred_is_null_not_insufficient():
    result = build_ranking_results([ranking_repository(
        'repo', [ranking_review('required', 'supports')])])
    preferred = result.repositories[0].preferred
    assert preferred.applicable is False and preferred.status is None
    assert preferred.evidence_match_count == 0 and preferred.insufficient_count == 0


def test_preferred_supports_without_citation_does_not_count_as_evidence():
    review = ranking_review('preferred', 'supports', cite=False)
    result = build_ranking_results([ranking_repository('repo', [review])])
    preferred = result.repositories[0].preferred
    assert preferred.supports_count == 1 and preferred.evidence_match_count == 0
    assert all(not reason.citations for reason in result.repositories[0].recommendation.reasons)


def test_same_tier_and_count_uses_repository_id_string_order():
    first = ranking_repository('2', [ranking_review('required', 'supports')])
    second = ranking_repository('10', [ranking_review('required', 'supports')])
    expected = ['10', '2']
    assert [row.repository_id for row in build_ranking_results([first, second]).repositories] == expected
    assert [row.repository_id for row in build_ranking_results([second, first]).repositories] == expected


def test_recommendation_citations_stay_with_their_requirement():
    required = ranking_review('required', 'supports')
    preferred = ranking_review('preferred', 'supports')
    result = build_ranking_results([ranking_repository('repo', [required, preferred])])
    reasons = {row.requirement_item_id: row for row in result.repositories[0].recommendation.reasons}
    assert reasons[required.requirement_item_id].citations == required.citations
    assert reasons[preferred.requirement_item_id].citations == preferred.citations


def test_supports_without_valid_citation_cannot_pass_or_enter_reason():
    review = ranking_review('required', 'supports', cite=False)
    result = build_ranking_results([ranking_repository('repo', [review])])
    ranked = result.repositories[0]
    assert ranked.hard_constraint_tier == 'needs_review'
    assert all(not reason.citations for reason in ranked.recommendation.reasons)


def test_failed_unknown_and_skipped_repositories_are_excluded():
    repositories = [ranking_repository('failed', [], status='failed'),
                    ranking_repository('unknown', [], status='unknown'),
                    ranking_repository('skipped', [], status='skipped')]
    result = build_ranking_results(repositories)
    assert result.repositories == []
    assert {row.repository_id: row.reason_code for row in result.excluded_repositories} == {
        'failed': 'REPOSITORY_FAILED',
        'unknown': 'REPOSITORY_OUTCOME_UNKNOWN',
        'skipped': 'REPOSITORY_SKIPPED'}


def test_logic_group_operator_mismatch_is_not_trusted():
    reviews = [ranking_review('required', 'supports', group='group-1', operator='ALL'),
               ranking_review('required', 'insufficient', group='group-1', operator='ALL')]
    repository = ranking_repository('repo', reviews)
    repository.logic_group_results[0] = repository.logic_group_results[0].model_copy(
        update={'operator': 'ANY', 'result': 'pass'})
    ranked = build_ranking_results([repository]).repositories[0]
    assert ranked.hard_constraint_units[0].result == 'unknown'
    assert ranked.hard_constraint_tier == 'needs_review'


def test_missing_logic_group_with_failure_is_conservative_conflict():
    reviews = [ranking_review('required', 'contradicts', group='group-1', operator='ALL')]
    repository = ranking_repository('repo', reviews).model_copy(update={'logic_group_results': []})
    result = build_ranking_results([repository])
    assert result.repositories[0].hard_constraint_tier == 'hard_conflict'


@pytest.mark.parametrize('group_state', ['missing', 'mismatch'])
def test_unverified_logic_group_with_passing_members_has_explanation_without_citations(group_state):
    reviews = [ranking_review('required', 'supports', group='group-1', operator='ALL'),
               ranking_review('required', 'supports', group='group-1', operator='ALL')]
    repository = ranking_repository('repo', reviews)
    if group_state == 'missing':
        repository = repository.model_copy(update={'logic_group_results': []})
    else:
        repository = repository.model_copy(update={'logic_group_results': [
            repository.logic_group_results[0].model_copy(update={'result': 'unknown'})]})
    ranked = build_ranking_results([repository]).repositories[0]
    assert ranked.hard_constraint_tier == 'needs_review'
    assert ranked.hard_constraint_units[0].result == 'unknown'
    reason = next(row for row in ranked.recommendation.reasons
                  if row.reason_code == 'LOGIC_GROUP_RESULT_UNVERIFIED')
    assert reason.requirement_item_id is None
    assert reason.citations == []
    assert 'group-1' in reason.text and '缺失或与成员不一致' in reason.text


@pytest.mark.parametrize('group_state', ['missing', 'mismatch'])
def test_unverified_any_group_with_mixed_members_stays_needs_review(group_state):
    reviews = [ranking_review('required', 'supports', group='group-1', operator='ANY'),
               ranking_review('required', 'contradicts', group='group-1', operator='ANY')]
    repository = ranking_repository('repo', reviews)
    if group_state == 'missing':
        repository = repository.model_copy(update={'logic_group_results': []})
    else:
        repository = repository.model_copy(update={'logic_group_results': [
            repository.logic_group_results[0].model_copy(update={'result': 'fail'})]})
    ranked = build_ranking_results([repository]).repositories[0]
    assert ranked.hard_constraint_tier == 'needs_review'
    assert ranked.hard_constraint_units[0].result == 'unknown'
    assert any(row.reason_code == 'LOGIC_GROUP_RESULT_UNVERIFIED' for row in ranked.recommendation.reasons)


def test_preferred_evidence_requires_citation_from_repository_input_chunks():
    review = ranking_review('preferred', 'supports')
    repository = ranking_repository('repo', [review])
    repository = repository.model_copy(update={'input': RepositoryInput(
        chunk_ids=[], chunk_count=0, input_bytes_upper_bound=0, budget=repository.input.budget)})
    ranked = build_ranking_results([repository]).repositories[0]
    assert ranked.preferred.supports_count == 1
    assert ranked.preferred.evidence_match_count == 0
    reason = next(row for row in ranked.recommendation.reasons
                  if row.reason_code == 'PREFERRED_EVIDENCE_INSUFFICIENT')
    assert reason.citations == []


def test_hard_constraint_reason_drops_citation_outside_repository_input_chunks():
    review = ranking_review('required', 'supports')
    repository = ranking_repository('repo', [review])
    repository = repository.model_copy(update={'input': RepositoryInput(
        chunk_ids=[], chunk_count=0, input_bytes_upper_bound=0, budget=repository.input.budget)})
    ranked = build_ranking_results([repository]).repositories[0]
    assert ranked.hard_constraint_tier == 'needs_review'
    assert ranked.hard_constraint_units[0].result == 'unknown'
    assert all(reason.citations == [] for reason in ranked.recommendation.reasons)


def test_old_report_without_ranking_remains_compatible():
    payload = report().model_dump(mode='json')
    payload.pop('ranking')
    validated = DeepReviewReport.model_validate(payload)
    assert validated.ranking is None
    assert validated.model_dump(mode='json')['ranking'] is None
