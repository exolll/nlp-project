from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.requirements import (RequirementEvidence, RequirementItem, RequirementJSON,
                                      RequirementRevision)
from app.services.requirement_quality import (canonical, evidence_mismatches, quality_warnings,
                                              revision_hash, validate_quality)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def uid():
    return str(uuid4())


def revision(pairs):
    """pairs: [(field, text, modality, evidence_text)]"""
    evidence, items = [], []
    for index, (field, text, modality, original) in enumerate(pairs, 1):
        item_id, evidence_id = uid(), f'E{index}'
        items.append(RequirementItem(requirement_item_id=item_id, field=field,
                                     value_path=f'{field}[{index}]' if modality else field,
                                     text=text, modality=modality, source_kind='assignment_document',
                                     evidence_ids=[evidence_id], logic_group_id=None,
                                     operator='not_applicable', supersedes_item_id=None))
        evidence.append(RequirementEvidence(evidence_id=evidence_id, field=field,
                                            original_text=original, source_kind='assignment_document',
                                            source_location='第 1 页', requirement_item_id=item_id,
                                            segment_id='SEG-001', start_offset=0, end_offset=len(original)))
    requirements = RequirementJSON(
        project_topic=pairs[0][1] if pairs[0][0] == 'project_topic' else None,
        project_description=None, project_type=None, domain=None,
        required_features=[text for field, text, _, _ in pairs if field == 'required_features'] or None,
        preferred_features=None, programming_languages=None, required_technologies=None,
        preferred_technologies=None, missing_information=[], uncertain_information=None,
        source_evidence=evidence)
    return RequirementRevision(revision_id=uid(), task_id=uid(), revision_number=1,
                               parent_revision_id=None, status='confirmed',
                               requirements=requirements, requirement_items=items, change_log=[],
                               created_at=NOW, confirmed_at=NOW)


def test_duplicate_project_goal_warns_without_deleting_evidence():
    rev = revision([
        ('project_topic', 'Build a project tracker', 'informational', 'Build a project tracker.'),
        ('required_features', 'Build a project tracker', 'required', 'Build a project tracker.'),
    ])
    result = quality_warnings(rev)
    codes = [warning['code'] for warning in result['warnings']]
    assert 'DUPLICATE_PROJECT_GOAL' in codes
    assert len(rev.requirement_items) == 2
    assert len(rev.requirements.source_evidence) == 2
    assert result['revision_hash'] == revision_hash(rev)


def test_duplicate_across_other_fields():
    rev = revision([
        ('required_features', 'Export transcripts', 'required', 'Export transcripts.'),
        ('preferred_features', 'Export transcripts', 'preferred', 'Export transcripts.'),
    ])
    codes = [warning['code'] for warning in quality_warnings(rev)['warnings']]
    assert 'DUPLICATE_REQUIREMENT' in codes


def test_verbatim_check_rejects_synonyms_but_allows_punctuation_case_space():
    ok = revision([('required_features', 'build a project tracker', 'required',
                    'Build a project tracker.')])
    assert evidence_mismatches(ok) == []
    changed = revision([('required_features', 'Create a task manager', 'required',
                         'Build a project tracker.')])
    mismatches = evidence_mismatches(changed)
    assert mismatches and mismatches[0]['reason'] == 'not_verbatim'


def test_quality_validation_blocks_only_mismatch():
    rev = revision([
        ('project_topic', 'Build a project tracker', 'informational', 'Build a project tracker.'),
        ('required_features', 'Build a project tracker', 'required', 'Build a project tracker.'),
    ])
    result = validate_quality(rev)
    assert result['ok'] is True and result['warnings']
    bad = revision([('required_features', 'Unrelated feature', 'required', 'Build a tracker.')])
    assert validate_quality(bad)['ok'] is False


def test_canonical_normalizes_width_case_spaces_and_trailing_punctuation():
    assert canonical('  ＢＵＩＬＤ   a tracker. ') == canonical('build a tracker')

def _rebind(rev, *, evidence_ids=None, evidence_id=None, requirement_item_id=None,
            evidence_requirement_item_id=None):
    item = rev.requirement_items[0]
    evidence = rev.requirements.source_evidence[0]
    new_item = item.model_copy(update={'evidence_ids': evidence_ids if evidence_ids is not None else item.evidence_ids})
    new_evidence = evidence.model_copy(update={
        'evidence_id': evidence_id or evidence.evidence_id,
        'requirement_item_id': (evidence_requirement_item_id
                                if evidence_requirement_item_id is not None
                                else evidence.requirement_item_id)})
    return rev.model_copy(update={
        'requirement_items': [new_item],
        'requirements': rev.requirements.model_copy(update={'source_evidence': [new_evidence]})})


def test_evidence_must_be_in_evidence_ids_even_with_same_item_id():
    rev = revision([('required_features', 'Build a project tracker', 'required',
                     'Build a project tracker.')])
    unlinked = _rebind(rev, evidence_ids=['E-missing'])
    assert evidence_mismatches(unlinked)[0]['reason'] == 'evidence_missing'
    empty = _rebind(rev, evidence_ids=[])
    assert evidence_mismatches(empty)[0]['reason'] == 'empty_evidence_ids'


def test_evidence_bound_to_another_item_is_invalid():
    rev = revision([('required_features', 'Build a project tracker', 'required',
                     'Build a project tracker.')])
    other = _rebind(rev, evidence_requirement_item_id=str(uuid4()))
    assert evidence_mismatches(other)[0]['reason'] == 'evidence_not_bound'


def test_one_valid_binding_among_invalid_is_enough():
    rev = revision([('required_features', 'Build a project tracker', 'required',
                     'Build a project tracker.')])
    good = rev.requirements.source_evidence[0]
    bad = good.model_copy(update={'evidence_id': 'E2', 'requirement_item_id': str(uuid4())})
    new_item = rev.requirement_items[0].model_copy(update={'evidence_ids': ['E2', good.evidence_id]})
    mixed = rev.model_copy(update={
        'requirement_items': [new_item],
        'requirements': rev.requirements.model_copy(update={'source_evidence': [bad, good]})})
    assert evidence_mismatches(mixed) == []


def test_three_duplicate_items_all_warn():
    rev = revision([
        ('required_features', 'Export transcripts', 'required', 'Export transcripts.'),
        ('required_features', 'Export transcripts', 'required', 'Export transcripts.'),
        ('preferred_features', 'Export transcripts', 'preferred', 'Export transcripts.'),
    ])
    warnings = [w for w in quality_warnings(rev)['warnings'] if w['code'] == 'DUPLICATE_REQUIREMENT']
    assert len(warnings) == 3

def test_duplicate_evidence_id_is_not_silently_overwritten():
    rev = revision([('required_features', 'Build a project tracker', 'required',
                     'Build a project tracker.')])
    item = rev.requirement_items[0]
    evidence = rev.requirements.source_evidence[0]
    other = evidence.model_copy(update={'requirement_item_id': str(uuid4())})
    first = rev.model_copy(update={
        'requirements': rev.requirements.model_copy(update={'source_evidence': [evidence, other]})})
    second = rev.model_copy(update={
        'requirements': rev.requirements.model_copy(update={'source_evidence': [other, evidence]})})
    a = evidence_mismatches(first)
    b = evidence_mismatches(second)
    assert a == b
    reasons = {row['reason'] for row in a}
    assert 'duplicate_evidence_id' in reasons
    assert any(row['requirement_item_id'] == item.requirement_item_id for row in a
               if row['reason'] == 'duplicate_evidence_id')
    assert validate_quality(first)['ok'] is False


def test_duplicate_evidence_cannot_bypass_owner_check():
    rev = revision([('required_features', 'Build a project tracker', 'required',
                     'Build a project tracker.')])
    evidence = rev.requirements.source_evidence[0]
    foreign = evidence.model_copy(update={'requirement_item_id': str(uuid4())})
    mixed = rev.model_copy(update={
        'requirements': rev.requirements.model_copy(update={'source_evidence': [foreign, evidence]})})
    reasons = [row['reason'] for row in evidence_mismatches(mixed)]
    assert 'duplicate_evidence_id' in reasons

def test_informational_summary_may_paraphrase_its_evidence():
    """project_description and friends are summaries; a paraphrase is not an evidence mismatch."""
    rev = revision([('project_description', '做一个任务管理工具，支持截止日期', 'informational',
                     '课程作业要求实现一个支持截止日期的任务管理工具。')])
    assert evidence_mismatches(rev) == []
    assert validate_quality(rev)['ok'] is True


def test_requirement_field_still_requires_a_verbatim_slice():
    rev = revision([('required_features', '支持任务截止日期提醒', 'required', '系统必须支持任务截止日期。')])
    mismatches = evidence_mismatches(rev)
    assert mismatches and mismatches[0]['reason'] == 'not_verbatim'
    assert validate_quality(rev)['ok'] is False


def test_null_reverse_link_is_bound_by_item_evidence_ids():
    """item.evidence_ids is authoritative: a null evidence.requirement_item_id is not a mismatch."""
    rev = revision([('required_features', '实现检索', 'required', '系统必须实现检索。')])
    rev.requirements.source_evidence[0].requirement_item_id = None
    assert evidence_mismatches(rev) == []
    assert validate_quality(rev)['ok'] is True
