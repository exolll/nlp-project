"""Deterministic 3b logic: byte-accurate citations, coverage, truth tables, budgets."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.requirements import UUIDString
from app.services.deep_review_models import (Citation, EvidenceCoverage, RejectedCitation,
                                             ReasonCode, LogicGroupResult,
                                             MAX_CHUNKS_PER_REPOSITORY, MAX_INPUT_BYTES,
                                             MAX_OUTPUT_TOKENS, RANKING_CONFIG_VERSION,
                                             ExcludedRepository, HardConstraintUnit,
                                             PreferredRankingSummary, RecommendationReason,
                                             RecommendationResult, RankingResults,
                                             RepositoryRanking, ScoreDimension)

MAX_REPOSITORIES = 5
MAX_CITATIONS = 2
SCORE_DIMENSIONS = (
    ('required_features', 'Required Feature Match', 0.35),
    ('topic', 'Topic Match', 0.15),
    ('technology', 'Technology Match', 0.15),
    ('language', 'Programming Language Match', 0.10),
    ('project_type', 'Project Type Match', 0.10),
    ('preferred', 'Preferred Match', 0.10),
    ('activity', 'Activity / Popularity', 0.05),
)
SCORE_WEIGHTS = {key: weight for key, _label, weight in SCORE_DIMENSIONS}
FIELD_DIMENSIONS = {
    'project_topic': 'topic', 'domain': 'topic', 'project_description': 'topic',
    'required_features': 'required_features', 'required_technologies': 'technology',
    'programming_languages': 'language', 'project_type': 'project_type',
    'preferred_features': 'preferred', 'preferred_technologies': 'preferred',
}
ACTIVITY_STAR_CAP = 1000
ACTIVITY_FORK_CAP = 200
ACTIVITY_FRESH_DAYS = 365


class DeepReviewInputError(ValueError):
    """Deterministic input rejection before any citation or budget evaluation."""

    def __init__(self, code, details=None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class DraftCitation(_Strict):
    chunk_id: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class DraftAssessment(_Strict):
    requirement_item_id: UUIDString
    verdict: Literal['supports', 'contradicts', 'insufficient']
    reason_code: ReasonCode
    citations: list[DraftCitation] = Field(default_factory=list, max_length=MAX_CITATIONS)


class DraftRepositoryOutput(_Strict):
    assessments: list[DraftAssessment] = Field(default_factory=list, max_length=200)


def normalized_readme_bytes(raw: bytes) -> bytes:
    text = raw.decode('utf-8')
    return text.replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')


def line_table(data: bytes):
    """Global 1-based line table over LF-normalized UTF-8 bytes; end excludes newline."""
    rows, start = [], 0
    for line in data.split(b'\n'):
        rows.append((start, start + len(line)))
        start += len(line) + 1
    return rows


def chunk_is_usable(chunk, data, table):
    if chunk.get('split') == 'chars':
        return False, 'chunk_split_chars'
    try:
        start, end = int(chunk['byte_start']), int(chunk['byte_end'])
        line_start, line_end = int(chunk['line_start']), int(chunk['line_end'])
    except (KeyError, TypeError, ValueError):
        return False, 'invalid_chunk'
    if not (0 <= start < end <= len(data)) or not (1 <= line_start <= line_end <= len(table)):
        return False, 'invalid_chunk'
    if (table[line_start - 1][0] != start or table[line_end - 1][1] != end
            or data[start:end].decode('utf-8') != chunk.get('text')):
        return False, 'invalid_chunk'
    return True, None


def _citation_line_end(table, line_start, line_end):
    # Trailing blank lines carry only a line terminator, never content.
    while line_end > line_start and table[line_end - 1][0] == table[line_end - 1][1]:
        line_end -= 1
    return line_end


def validate_citation(citation, chunk, data, table):
    if chunk.get('split') == 'chars':
        return None, RejectedCitation(chunk_id=citation.chunk_id, reason='chunk_split_chars')
    usable, _ = chunk_is_usable(chunk, data, table)
    if not usable:
        return None, RejectedCitation(chunk_id=citation.chunk_id, reason='invalid_utf8')
    if not (1 <= citation.line_start <= citation.line_end <= len(table)):
        return None, RejectedCitation(chunk_id=citation.chunk_id, reason='line_out_of_range')
    if not (chunk['line_start'] <= citation.line_start and citation.line_end <= chunk['line_end']):
        return None, RejectedCitation(chunk_id=citation.chunk_id, reason='line_out_of_range')
    line_start = citation.line_start
    line_end = _citation_line_end(table, line_start, citation.line_end)
    start = table[line_start - 1][0]
    end = table[line_end - 1][1]
    if start == end:
        return None, RejectedCitation(chunk_id=citation.chunk_id, reason='empty_quote')
    if not (chunk['byte_start'] <= start < end <= chunk['byte_end']):
        return None, RejectedCitation(chunk_id=citation.chunk_id, reason='byte_out_of_range')
    try:
        text = data[start:end].decode('utf-8')
    except UnicodeDecodeError:
        return None, RejectedCitation(chunk_id=citation.chunk_id, reason='invalid_utf8')
    if not text.strip():
        return None, RejectedCitation(chunk_id=citation.chunk_id, reason='empty_quote')
    return Citation(chunk_id=citation.chunk_id, quote=text, line_start=line_start,
                    line_end=line_end, byte_start=start, byte_end=end), None


def hard_check(modality, verdict):
    if modality == 'required':
        return {'supports': 'pass', 'contradicts': 'fail'}.get(verdict, 'unknown')
    if modality == 'prohibited':
        return {'supports': 'pass', 'contradicts': 'fail'}.get(verdict, 'unknown')
    return 'not_applicable'


def compile_requirement_reviews(output, requirements, chunk_rows, raw, *, repository_id, snapshot_id):
    """Coverage, duplicates, citation coupling and hard checks. No model calls.

    Precondition: chunk_rows are exactly this repository+snapshot's model input.
    """
    data = normalized_readme_bytes(raw)
    table = line_table(data)
    counts = {}
    for row in chunk_rows:
        chunk_id = row.get('chunk_id')
        counts[chunk_id] = counts.get(chunk_id, 0) + 1
    duplicates = sorted(str(chunk_id) for chunk_id, count in counts.items() if count > 1)
    if duplicates:
        raise DeepReviewInputError('DEEP_REVIEW_DUPLICATE_CHUNK_ID', {'chunk_ids': duplicates})
    chunks = {chunk['chunk_id']: chunk for chunk in chunk_rows}
    assessments = output.assessments
    by_id = {}
    duplicate_ids, unknown_ids = set(), []
    known = {str(item.requirement_item_id) for item in requirements}
    for assessment in assessments:
        item_id = assessment.requirement_item_id
        if item_id not in known:
            if item_id not in unknown_ids:
                unknown_ids.append(item_id)
            continue
        if item_id in by_id:
            duplicate_ids.add(item_id)
        else:
            by_id[item_id] = assessment
    warnings, reviews = [], []
    counts = {'supports': 0, 'contradicts': 0, 'insufficient': 0, 'model_omitted': 0,
              'duplicates': len(duplicate_ids), 'unknown_items': len(unknown_ids)}
    for item in requirements:
        item_id = str(item.requirement_item_id)
        projection = {'requirement_item_id': item_id, 'field': item.field, 'text': item.text,
                      'modality': item.modality, 'operator': item.operator,
                      'logic_group_id': item.logic_group_id}
        if item_id in duplicate_ids:
            reviews.append({**projection, 'verdict': 'insufficient',
                            'reason_code': 'model_output_incomplete', 'hard_constraint_check': 'unknown',
                            'citations': [], 'rejected_citations': [], 'model_omitted': False})
            warnings.append({'code': 'duplicate_item', 'requirement_item_id': item_id})
            counts['insufficient'] += 1
            continue
        assessment = by_id.get(item_id)
        if assessment is None:
            reviews.append({**projection, 'verdict': 'insufficient',
                            'reason_code': 'model_output_incomplete', 'hard_constraint_check': 'unknown',
                            'citations': [], 'rejected_citations': [], 'model_omitted': True})
            warnings.append({'code': 'model_omitted', 'requirement_item_id': item_id})
            counts['model_omitted'] += 1
            counts['insufficient'] += 1
            continue
        if assessment.verdict == 'insufficient':
            if assessment.citations:
                warnings.append({'code': 'citations_for_insufficient', 'requirement_item_id': item_id})
            reviews.append({**projection, 'verdict': 'insufficient',
                            'reason_code': 'not_found_in_provided_evidence',
                            'hard_constraint_check': 'unknown', 'citations': [],
                            'rejected_citations': [], 'model_omitted': False})
            counts['insufficient'] += 1
            continue
        citations, rejected, bad = [], [], False
        for citation in assessment.citations:
            chunk = chunks.get(citation.chunk_id)
            if chunk is None:
                rejected.append(RejectedCitation(chunk_id=citation.chunk_id, reason='unknown_chunk').model_dump())
                bad = True
                continue
            if str(chunk.get('repository_id')) != str(repository_id):
                rejected.append(RejectedCitation(chunk_id=citation.chunk_id, reason='cross_repository').model_dump())
                bad = True
                continue
            if str(chunk.get('snapshot_id')) != str(snapshot_id):
                rejected.append(RejectedCitation(chunk_id=citation.chunk_id, reason='cross_snapshot').model_dump())
                bad = True
                continue
            valid, rejection = validate_citation(citation, chunk, data, table)
            if rejection:
                rejected.append(rejection.model_dump())
                bad = True
            else:
                citations.append(valid.model_dump())
        if not citations:
            code = 'citations_rejected' if assessment.citations else 'citations_missing'
            warnings.append({'code': code, 'requirement_item_id': item_id})
            reviews.append({**projection, 'verdict': 'insufficient',
                            'reason_code': 'citation_rejected', 'hard_constraint_check': 'unknown',
                            'citations': [], 'rejected_citations': rejected, 'model_omitted': False})
            counts['insufficient'] += 1
            continue
        if bad:
            warnings.append({'code': 'partial_citations_rejected', 'requirement_item_id': item_id})
        verdict = assessment.verdict
        reviews.append({**projection, 'verdict': verdict,
                        'reason_code': 'supported_by_citation' if verdict == 'supports'
                        else 'conflicted_by_citation',
                        'hard_constraint_check': hard_check(item.modality, verdict),
                        'citations': citations, 'rejected_citations': rejected,
                        'model_omitted': False})
        counts[verdict] += 1
    for item_id in unknown_ids:
        warnings.append({'code': 'unknown_item', 'requirement_item_id': item_id})
    return {'requirements': reviews, 'warnings': warnings, 'counts': counts, 'status': 'completed'}


def truth_table(operator, checks):
    if operator == 'ALL':
        if 'fail' in checks:
            return 'fail'
        return 'pass' if checks and all(value == 'pass' for value in checks) else 'unknown'
    if operator == 'ANY':
        if 'pass' in checks:
            return 'pass'
        return 'fail' if checks and all(value == 'fail' for value in checks) else 'unknown'
    return 'unknown'


def logic_group_results(reviews):
    groups = {}
    for review in reviews:
        if review.get('logic_group_id') and review['modality'] in ('required', 'prohibited'):
            groups.setdefault(review['logic_group_id'], []).append(review)
    results, warnings = [], []
    for group_id, members in groups.items():
        operators = {member['operator'] for member in members}
        modalities = {member['modality'] for member in members}
        if len(operators) != 1 or operators.pop() not in ('ALL', 'ANY') or len(modalities) != 1:
            warnings.append({'code': 'logic_group_inconsistent', 'logic_group_id': group_id})
            continue
        operator = members[0]['operator']
        checks = [member['hard_constraint_check'] for member in members]
        results.append(LogicGroupResult(logic_group_id=group_id, operator=operator,
                                        member_item_ids=[member['requirement_item_id'] for member in members],
                                        result=truth_table(operator, checks)))
    return results, warnings


def hard_constraint_summary(reviews):
    summary = {'required': {'pass': 0, 'fail': 0, 'unknown': 0, 'total': 0},
               'prohibited': {'pass': 0, 'fail': 0, 'unknown': 0, 'total': 0},
               'preferred': {'evidence_found': 0, 'unknown': 0, 'total': 0}}
    for review in reviews:
        modality = review['modality']
        if modality in ('required', 'prohibited'):
            summary[modality]['total'] += 1
            summary[modality][review['hard_constraint_check']] += 1
        elif modality == 'preferred':
            summary['preferred']['total'] += 1
            key = 'evidence_found' if review['verdict'] == 'supports' else 'unknown'
            summary['preferred'][key] += 1
    return summary


_RANKING_STATUS_ALIASES = {'ok': 'completed', 'outcome_unknown': 'unknown'}
_TIER_ORDER = {'meets_requirements': 0, 'needs_review': 1, 'hard_conflict': 2}
_README_LIMITATION = '仅基于已保存 README，不代表源码审计、代码运行验证或部署可用性。'


def _ranking_status(status):
    return _RANKING_STATUS_ALIASES.get(status, status)


def _valid_citations(review, chunk_ids):
    return [citation for citation in review.citations if citation.chunk_id in chunk_ids]


def _hard_result(review, chunk_ids):
    declared = review.hard_constraint_check
    derived = hard_check(review.modality, review.verdict)
    citations = _valid_citations(review, chunk_ids)
    if declared == 'fail' or (derived == 'fail' and citations):
        return 'fail'
    if review.verdict in ('supports', 'contradicts') and not citations:
        return 'unknown'
    return declared if declared in ('pass', 'unknown') and declared == derived else 'unknown'


def _group_result(operator, members, chunk_ids):
    results = [_hard_result(member, chunk_ids) for member in members]
    if operator == 'ALL':
        if 'fail' in results:
            return 'fail'
        return 'pass' if all(result == 'pass' for result in results) else 'unknown'
    if 'pass' in results:
        return 'pass'
    return 'fail' if all(result == 'fail' for result in results) else 'unknown'


def _hard_constraint_units(repository):
    hard_reviews = [review for review in repository.requirements
                    if review.modality in ('required', 'prohibited')]
    chunk_ids = set(repository.input.chunk_ids)
    groups = {}
    for review in hard_reviews:
        if review.logic_group_id:
            groups.setdefault(review.logic_group_id, []).append(review)
    results = {result.logic_group_id: result for result in repository.logic_group_results}
    units, grouped_ids = [], set()
    for group_id in sorted(groups):
        members = groups[group_id]
        member_ids = {str(member.requirement_item_id) for member in members}
        grouped_ids.update(member_ids)
        result = results.get(group_id)
        valid = (result is not None and set(result.member_item_ids) == member_ids
                 and len({member.operator for member in members}) == 1
                 and result.operator == members[0].operator
                 and len({member.modality for member in members}) == 1)
        expected = _group_result(members[0].operator, members, chunk_ids)
        verified = valid and result.result == expected
        # An unreadable group is unknown, never an automatic conflict. A prohibited
        # member with its own valid counter-evidence still conflicts on its own merit.
        prohibited_fail = any(member.modality == 'prohibited'
                              and _hard_result(member, chunk_ids) == 'fail'
                              for member in members)
        value = expected if verified else ('fail' if prohibited_fail else 'unknown')
        units.append(HardConstraintUnit(
            kind='logic_group', source_id=group_id,
            member_item_ids=sorted(member_ids), result=value))
    for review in hard_reviews:
        item_id = str(review.requirement_item_id)
        if item_id in grouped_ids:
            continue
        units.append(HardConstraintUnit(
            kind=review.modality, source_id=item_id, member_item_ids=[item_id],
            result=_hard_result(review, chunk_ids)))
    return sorted(units, key=lambda unit: (unit.kind, unit.source_id))


def _hard_constraint_tier(repository, units):
    if any(unit.result == 'fail' for unit in units):
        return 'hard_conflict'
    if not units or _ranking_status(repository.status) == 'partial':
        return 'needs_review'
    if repository.coverage != 'full' or any(unit.result != 'pass' for unit in units):
        return 'needs_review'
    return 'meets_requirements'


def _preferred_summary(repository):
    reviews = [review for review in repository.requirements if review.modality == 'preferred']
    chunk_ids = set(repository.input.chunk_ids)
    supports = sum(review.verdict == 'supports' for review in reviews)
    contradicts = sum(review.verdict == 'contradicts' for review in reviews)
    insufficient = sum(review.verdict == 'insufficient' for review in reviews)
    status = None
    if reviews:
        status = ('mixed' if supports and contradicts else 'supports' if supports else
                  'contradicts' if contradicts else 'insufficient')
    return PreferredRankingSummary(
        applicable=bool(reviews), status=status, supports_count=supports,
        contradicts_count=contradicts, insufficient_count=insufficient,
        evidence_match_count=sum(review.verdict == 'supports' and bool(_valid_citations(review, chunk_ids))
                                 for review in reviews))


def _reason(reason_code, text, review=None, citations=None):
    return RecommendationReason(
        reason_code=reason_code,
        requirement_item_id=str(review.requirement_item_id) if review else None,
        text=text, citations=citations if citations is not None else review.citations if review else [])


def _recommendation(repository, tier, units):
    if tier == 'hard_conflict':
        reason_code = 'HARD_CONSTRAINT_CONFLICT'
    elif not units:
        reason_code = 'NO_HARD_CONSTRAINTS'
    elif repository.coverage != 'full':
        reason_code = 'README_COVERAGE_INCOMPLETE'
    elif any(unit.result == 'unknown' for unit in units):
        reason_code = 'HARD_CONSTRAINT_EVIDENCE_INSUFFICIENT'
    else:
        reason_code = 'HARD_CONSTRAINTS_MET'
    reasons = []
    if not units:
        reasons.append(_reason(
            'NO_HARD_CONSTRAINTS', '当前需求没有可核查的硬约束，不能确认项目完全符合要求。'))
    if repository.coverage != 'full':
        reasons.append(_reason('README_COVERAGE_INCOMPLETE', 'README 证据覆盖不完整，需进一步核查。'))
    by_id = {str(review.requirement_item_id): review for review in repository.requirements}
    chunk_ids = set(repository.input.chunk_ids)
    selected = []
    for unit in units:
        if unit.kind != 'logic_group':
            selected.append((by_id[unit.source_id], unit.result))
            continue
        members = [by_id[item_id] for item_id in unit.member_item_ids]
        member_results = [(member, _hard_result(member, chunk_ids)) for member in members]
        selected.extend(row for row in member_results if row[1] == unit.result)
        if not any(result == unit.result for _, result in member_results):
            reasons.append(_reason(
                'LOGIC_GROUP_RESULT_UNVERIFIED',
                f'逻辑组 {unit.source_id} 的结果缺失或与成员不一致，无法确认该组硬约束。'))
    for review, result in sorted(selected, key=lambda row: str(row[0].requirement_item_id)):
        citations = _valid_citations(review, chunk_ids)
        if result == 'pass' and citations:
            code = 'REQUIRED_SUPPORTED' if review.modality == 'required' else 'PROHIBITED_CONFIRMED'
            text = ('README 中有有效证据支持该必需需求。' if review.modality == 'required'
                    else 'README 中有有效证据支持该禁止约束。')
            reasons.append(_reason(code, text, review, citations))
        elif result == 'fail':
            text = ('README 中有有效证据与该必需需求冲突。' if review.modality == 'required'
                    else 'README 中有有效证据表明存在该禁止项。') if citations else (
                        '该硬约束记录为冲突，但没有可展示的有效反证引用。')
            reasons.append(_reason('HARD_CONSTRAINT_CONFLICT', text, review, citations))
        else:
            reasons.append(_reason(
                'HARD_CONSTRAINT_EVIDENCE_INSUFFICIENT',
                'README 证据不足，无法确认该硬约束。', review, citations))
    for review in sorted(repository.requirements, key=lambda item: str(item.requirement_item_id)):
        if review.modality == 'preferred':
            citations = _valid_citations(review, chunk_ids)
            if review.verdict == 'supports' and citations:
                reasons.append(_reason('PREFERRED_SUPPORTED', 'README 中有有效证据支持该偏好需求。', review, citations))
            elif review.verdict == 'contradicts' and citations:
                reasons.append(_reason('PREFERRED_CONTRADICTED', 'README 中有有效证据与该偏好需求冲突。', review, citations))
            else:
                reasons.append(_reason(
                    'PREFERRED_EVIDENCE_INSUFFICIENT', '该偏好需求的 README 证据不足。', review, citations))
    limitations = [_README_LIMITATION]
    if repository.coverage != 'full':
        limitations.append('README 证据覆盖不完整，不能据此确认项目完全符合要求。')
    status = {'meets_requirements': 'recommended', 'needs_review': 'needs_review',
              'hard_conflict': 'not_recommended'}[tier]
    return RecommendationResult(status=status, reason_code=reason_code,
                                reasons=reasons, limitations=limitations)


def _metadata_string(metadata, *keys):
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _metadata_int(metadata, *keys):
    for key in keys:
        value = metadata.get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _as_utc(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _activity_dimension(metadata, as_of):
    stars = _metadata_int(metadata, 'stars', 'stargazers_count')
    forks = _metadata_int(metadata, 'forks', 'forks_count')
    pushed = _as_utc(metadata.get('pushed_at') or metadata.get('updated_at'))
    age_days = None if pushed is None else max(0, (as_of - pushed).days)
    components = []
    if stars is not None:
        components.append((0.4, 100 * min(stars, ACTIVITY_STAR_CAP) / ACTIVITY_STAR_CAP))
    if forks is not None:
        components.append((0.3, 100 * min(forks, ACTIVITY_FORK_CAP) / ACTIVITY_FORK_CAP))
    if age_days is not None:
        components.append((0.3, max(0.0, 1 - age_days / ACTIVITY_FRESH_DAYS) * 100))
    missing = len(components) < 3
    detail = (f"stars={stars if stars is not None else 'missing'} (cap {ACTIVITY_STAR_CAP}); "
              f"forks={forks if forks is not None else 'missing'} (cap {ACTIVITY_FORK_CAP}); "
              f"pushed_at={pushed.date().isoformat() if pushed else 'missing'}; "
              f"age_days={age_days if age_days is not None else 'missing'}; "
              f"valid_components={len(components)}; reference_date={as_of.date().isoformat()}")
    if not components:
        return ScoreDimension(
            key='activity', label='Activity / Popularity', weight=SCORE_WEIGHTS['activity'],
            applicable=False, score=None, units_total=0, supports=0, contradicts=0,
            insufficient=0, missing_data=True, detail=detail)
    weight_total = sum(weight for weight, _score in components)
    score = min(100.0, max(0.0, sum(weight * value for weight, value in components) / weight_total))
    return ScoreDimension(
        key='activity', label='Activity / Popularity', weight=SCORE_WEIGHTS['activity'],
        applicable=True, score=score, units_total=0, supports=0, contradicts=0,
        insufficient=0, missing_data=missing, detail=detail)


def _group_dimension(members):
    dimensions = {FIELD_DIMENSIONS.get(member.field) for member in members
                  if member.modality != 'prohibited'}
    dimensions.discard(None)
    return dimensions.pop() if len(dimensions) == 1 else 'required_features'


def _score_units(repository):
    """One unit per hard constraint (groups counted once) plus preferred/informational items."""
    reviews = {str(review.requirement_item_id): review for review in repository.requirements}
    hard_units = _hard_constraint_units(repository)
    units, grouped = [], set()
    for unit in hard_units:
        # Every hard-constraint member is settled here, even when the unit is
        # unscorable; the fallback loop below must never count it again.
        grouped.update(unit.member_item_ids)
        if unit.kind == 'prohibited':
            continue
        members = [reviews[item_id] for item_id in unit.member_item_ids if item_id in reviews]
        dimension = None if (
            not members or any(member.modality == 'prohibited' for member in members)
        ) else _group_dimension(members)
        if dimension is None:
            # Mixed or unmapped groups still count as one unresolved evidence unit,
            # so skipping scoring never inflates the total or coverage.
            units.append({'dimension': None, 'result': 'insufficient'})
            continue
        units.append({'dimension': dimension,
                      'result': {'pass': 'supports', 'fail': 'contradicts',
                                 'unknown': 'insufficient'}[unit.result]})
    for review in repository.requirements:
        item_id = str(review.requirement_item_id)
        if item_id in grouped or review.modality == 'prohibited':
            continue
        dimension = ('preferred' if review.modality == 'preferred'
                     else FIELD_DIMENSIONS.get(review.field))
        if dimension is None:
            continue
        units.append({'dimension': dimension, 'result': review.verdict})
    return units


def _dimension_score(key, label, weight, units):
    rows = [unit for unit in units if unit['dimension'] == key]
    supports = sum(unit['result'] == 'supports' for unit in rows)
    contradicts = sum(unit['result'] == 'contradicts' for unit in rows)
    insufficient = sum(unit['result'] == 'insufficient' for unit in rows)
    applicable = bool(rows)
    return ScoreDimension(
        key=key, label=label, weight=weight, applicable=applicable,
        score=(100 * supports / len(rows)) if rows else None,
        units_total=len(rows), supports=supports, contradicts=contradicts,
        insufficient=insufficient)


def _ranking_explanation(tier, total_score, coverage, dimensions):
    strengths = [row.label for row in dimensions
                 if row.applicable and row.score is not None and row.score >= 75]
    gaps = [row.label for row in dimensions if row.applicable and row.insufficient > 0]
    not_applicable = [row.label for row in dimensions if not row.applicable]
    parts = [f'硬约束 {tier}', f'综合得分 {total_score:.1f}/100',
             f'证据覆盖率 {coverage * 100:.0f}%']
    if strengths:
        parts.append('优势：' + '、'.join(strengths))
    if gaps:
        parts.append('证据不足：' + '、'.join(gaps))
    if not_applicable:
        parts.append('不适用：' + '、'.join(not_applicable))
    return '；'.join(parts)


def _score_repository(repository, metadata, as_of, tier):
    units = _score_units(repository)
    dimensions = []
    for key, label, weight in SCORE_DIMENSIONS:
        if key == 'activity':
            dimensions.append(_activity_dimension(metadata, as_of))
        else:
            dimensions.append(_dimension_score(key, label, weight, units))
    applicable = [row for row in dimensions if row.applicable]
    weight_total = sum(row.weight for row in applicable)
    total = (min(100.0, max(0.0, sum(row.weight * row.score for row in applicable) / weight_total))
             if weight_total else 0.0)
    supported = sum(unit['result'] == 'supports' for unit in units)
    contradicted = sum(unit['result'] == 'contradicts' for unit in units)
    insufficient = sum(unit['result'] == 'insufficient' for unit in units)
    coverage = (supported + contradicted) / len(units) if units else 0.0
    return total, dimensions, EvidenceCoverage(
        applicable_units=len(units), supported=supported, contradicted=contradicted,
        insufficient=insufficient, coverage=coverage,
        not_applicable_dimensions=[row.label for row in dimensions if not row.applicable],
    ), _ranking_explanation(tier, total, coverage, dimensions)


def _ranking_sort_key(tier, total_score, coverage, repository_id):
    return (_TIER_ORDER[tier], -total_score, -coverage, str(repository_id))


def build_ranking_results(repositories, metadata_by_id=None, *, as_of=None):
    metadata_by_id = metadata_by_id or {}
    reference = _as_utc(as_of) or datetime.now(timezone.utc)
    entries, excluded = [], []
    for repository in repositories:
        repository_id = str(repository.repository_id)
        status = _ranking_status(repository.status)
        if status not in ('completed', 'partial'):
            reason_code = {'failed': 'REPOSITORY_FAILED',
                           'unknown': 'REPOSITORY_OUTCOME_UNKNOWN',
                           'skipped': 'REPOSITORY_SKIPPED'}.get(status, 'REPOSITORY_NOT_COMPLETED')
            excluded.append(ExcludedRepository(
                repository_id=repository_id, status=repository.status,
                error_code=repository.error_code, reason_code=reason_code))
            continue
        units = _hard_constraint_units(repository)
        tier = _hard_constraint_tier(repository, units)
        preferred = _preferred_summary(repository)
        metadata = metadata_by_id.get(repository_id) or {}
        total, breakdown, coverage, explanation = _score_repository(
            repository, metadata, reference, tier)
        entries.append({
            'repository': repository, 'units': units, 'tier': tier, 'preferred': preferred,
            'recommendation': _recommendation(repository, tier, units),
            'full_name': _metadata_string(metadata, 'full_name', 'name'),
            'html_url': _metadata_string(metadata, 'html_url'),
            'total': total, 'breakdown': breakdown, 'coverage': coverage,
            'explanation': explanation,
        })
    entries.sort(key=lambda entry: _ranking_sort_key(
        entry['tier'], entry['total'], entry['coverage'].coverage,
        entry['repository'].repository_id))
    ranked = [RepositoryRanking(
        repository_id=str(entry['repository'].repository_id), full_name=entry['full_name'],
        html_url=entry['html_url'], rank=index, hard_constraint_tier=entry['tier'],
        hard_constraint_units=entry['units'], preferred=entry['preferred'],
        recommendation=entry['recommendation'], total_score=entry['total'],
        score_breakdown=entry['breakdown'], evidence_coverage=entry['coverage'],
        ranking_tier=entry['tier'], ranking_explanation=entry['explanation'])
        for index, entry in enumerate(entries, 1)]
    excluded.sort(key=lambda row: row.repository_id)
    return RankingResults(ranking_config_version=RANKING_CONFIG_VERSION,
                          scored_at=reference.isoformat(),
                          repositories=ranked, excluded_repositories=excluded)


def estimate_input_bytes_upper_bound(*parts):
    """UTF-8 byte length used as a conservative upper bound on tokens.

    This is NOT a tokenizer count and must never be reported as actual token usage.
    """
    return sum(len(str(part).encode('utf-8')) for part in parts)


def repository_budget(chunk_count, input_bytes_upper_bound, output_tokens=MAX_OUTPUT_TOKENS):
    """Pre-call business budget. Output limit is the request max_tokens, not a tokenizer count."""
    if chunk_count > MAX_CHUNKS_PER_REPOSITORY:
        return {'status': 'skipped', 'error_code': 'DEEP_REVIEW_CHUNK_BUDGET'}
    if input_bytes_upper_bound > MAX_INPUT_BYTES:
        return {'status': 'skipped', 'error_code': 'DEEP_REVIEW_INPUT_BYTES_BUDGET'}
    if output_tokens > MAX_OUTPUT_TOKENS or output_tokens < 1:
        return {'status': 'skipped', 'error_code': 'DEEP_REVIEW_OUTPUT_TOKENS_BUDGET'}
    return None


def operation_repository_budget(count):
    if count == 0:
        return {'status': 'rejected', 'http': 422, 'error_code': 'DEEP_REVIEW_NO_ELIGIBLE_REPOSITORIES'}
    if count > MAX_REPOSITORIES:
        return {'status': 'rejected', 'http': 422, 'error_code': 'DEEP_REVIEW_REPOSITORY_BUDGET'}
    return None
