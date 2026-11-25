"""Strict report models for 3b deep review. No prose from the model is stored."""
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.requirements import UUIDString

VERDICTS = ('supports', 'contradicts', 'insufficient')
ReasonCode = Literal['supported_by_citation', 'conflicted_by_citation',
                     'not_found_in_provided_evidence', 'citation_rejected',
                     'model_output_incomplete']
REASON_CODES = get_args(ReasonCode)
HARD_CHECKS = ('pass', 'fail', 'unknown', 'not_applicable')
REPOSITORY_STATUSES = ('completed', 'failed', 'unknown', 'skipped')
MAX_CHUNKS_PER_REPOSITORY = 16
MAX_INPUT_BYTES = 24000
MAX_OUTPUT_TOKENS = 2000


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class RejectedCitation(_Strict):
    chunk_id: str = Field(min_length=1)
    reason: Literal['unknown_chunk', 'cross_repository', 'cross_snapshot', 'chunk_split_chars',
                    'line_out_of_range', 'byte_out_of_range', 'invalid_utf8', 'empty_quote']


class Citation(_Strict):
    chunk_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    byte_start: int = Field(ge=0)
    byte_end: int = Field(ge=1)

    @model_validator(mode='after')
    def ordered_range(self):
        if self.byte_start >= self.byte_end or self.line_start > self.line_end:
            raise ValueError('invalid citation range')
        return self


class RequirementReview(_Strict):
    requirement_item_id: UUIDString
    field: str = Field(min_length=1)
    text: str = Field(min_length=1)
    modality: Literal['required', 'preferred', 'prohibited', 'informational']
    operator: Literal['ALL', 'ANY', 'not_applicable']
    logic_group_id: str | None
    verdict: Literal['supports', 'contradicts', 'insufficient']
    reason_code: ReasonCode
    hard_constraint_check: Literal['pass', 'fail', 'unknown', 'not_applicable']
    citations: list[Citation] = Field(default_factory=list, max_length=2)
    rejected_citations: list[RejectedCitation] = Field(default_factory=list)
    model_omitted: bool = False


class LogicGroupResult(_Strict):
    logic_group_id: str = Field(min_length=1)
    operator: Literal['ALL', 'ANY']
    member_item_ids: list[UUIDString] = Field(min_length=1)
    result: Literal['pass', 'fail', 'unknown']


RANKING_CONFIG_VERSION = 'hard-tier-score-v2'
LEGACY_RANKING_CONFIG_VERSION = 'hard-tier-preferred-v1'


class HardConstraintUnit(_Strict):
    kind: Literal['required', 'prohibited', 'logic_group']
    source_id: str = Field(min_length=1)
    member_item_ids: list[UUIDString] = Field(default_factory=list)
    result: Literal['pass', 'fail', 'unknown']


class PreferredRankingSummary(_Strict):
    applicable: bool
    status: Literal['supports', 'contradicts', 'insufficient', 'mixed'] | None
    supports_count: int = Field(ge=0)
    contradicts_count: int = Field(ge=0)
    insufficient_count: int = Field(ge=0)
    evidence_match_count: int = Field(ge=0)

    @model_validator(mode='after')
    def status_matches_counts(self):
        counts = (self.supports_count, self.contradicts_count, self.insufficient_count)
        if not self.applicable:
            if self.status is not None or any(counts) or self.evidence_match_count:
                raise ValueError('inapplicable preferred summary must be empty')
            return self
        if self.status is None or not any(counts):
            raise ValueError('applicable preferred summary requires a status and items')
        expected = ('mixed' if self.supports_count and self.contradicts_count else
                    'supports' if self.supports_count else
                    'contradicts' if self.contradicts_count else 'insufficient')
        if self.status != expected:
            raise ValueError('preferred status does not match counts')
        if self.evidence_match_count > self.supports_count:
            raise ValueError('preferred evidence count exceeds supports')
        return self


class RecommendationReason(_Strict):
    reason_code: str = Field(min_length=1)
    requirement_item_id: UUIDString | None = None
    text: str = Field(min_length=1)
    citations: list[Citation] = Field(default_factory=list, max_length=2)

    @model_validator(mode='after')
    def citations_require_requirement(self):
        if self.citations and self.requirement_item_id is None:
            raise ValueError('citation reason requires a requirement_item_id')
        return self


class RecommendationResult(_Strict):
    status: Literal['recommended', 'needs_review', 'not_recommended']
    reason_code: str = Field(min_length=1)
    reasons: list[RecommendationReason]
    limitations: list[str] = Field(min_length=1)


class ScoreDimension(_Strict):
    key: Literal['required_features', 'topic', 'technology', 'language',
                 'project_type', 'preferred', 'activity']
    label: str = Field(min_length=1)
    weight: float = Field(ge=0, le=1)
    applicable: bool
    score: float | None = Field(default=None, ge=0, le=100)
    units_total: int = Field(ge=0)
    supports: int = Field(ge=0)
    contradicts: int = Field(ge=0)
    insufficient: int = Field(ge=0)
    missing_data: bool = False
    detail: str | None = None

    @model_validator(mode='after')
    def score_matches_applicability(self):
        if self.applicable != (self.score is not None):
            raise ValueError('applicable dimension must have a score')
        if self.units_total != self.supports + self.contradicts + self.insufficient:
            raise ValueError('dimension unit counts must add up')
        return self


class EvidenceCoverage(_Strict):
    applicable_units: int = Field(ge=0)
    supported: int = Field(ge=0)
    contradicted: int = Field(ge=0)
    insufficient: int = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    not_applicable_dimensions: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def counts_match(self):
        if self.applicable_units != self.supported + self.contradicted + self.insufficient:
            raise ValueError('coverage counts must add up')
        expected = 0.0 if not self.applicable_units else (
            (self.supported + self.contradicted) / self.applicable_units)
        if abs(self.coverage - expected) > 1e-9:
            raise ValueError('coverage does not match its counts')
        return self


class RepositoryRanking(_Strict):
    repository_id: str = Field(min_length=1)
    full_name: str | None = None
    html_url: str | None = None
    rank: int = Field(ge=1)
    hard_constraint_tier: Literal['meets_requirements', 'needs_review', 'hard_conflict']
    hard_constraint_units: list[HardConstraintUnit]
    preferred: PreferredRankingSummary
    recommendation: RecommendationResult
    total_score: float | None = Field(default=None, ge=0, le=100)
    score_breakdown: list[ScoreDimension] = Field(default_factory=list)
    evidence_coverage: EvidenceCoverage | None = None
    ranking_tier: Literal['meets_requirements', 'needs_review', 'hard_conflict'] | None = None
    ranking_explanation: str | None = None

    @model_validator(mode='after')
    def recommendation_matches_tier(self):
        expected = {'meets_requirements': 'recommended', 'needs_review': 'needs_review',
                    'hard_conflict': 'not_recommended'}[self.hard_constraint_tier]
        if self.recommendation.status != expected:
            raise ValueError('recommendation status does not match hard constraint tier')
        scored = self.total_score is not None
        if scored and (self.ranking_tier != self.hard_constraint_tier
                       or self.evidence_coverage is None or len(self.score_breakdown) != 7
                       or not self.ranking_explanation):
            raise ValueError('scored ranking requires tier, coverage, breakdown and explanation')
        if not scored and (self.ranking_tier is not None or self.evidence_coverage is not None
                           or self.score_breakdown or self.ranking_explanation is not None):
            raise ValueError('unscored ranking must not carry scoring fields')
        return self


class ExcludedRepository(_Strict):
    repository_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    error_code: str | None = None
    reason_code: str = Field(min_length=1)


class RankingResults(_Strict):
    ranking_config_version: Literal['hard-tier-preferred-v1',
                                    'hard-tier-score-v2'] = RANKING_CONFIG_VERSION
    scored_at: str | None = None
    repositories: list[RepositoryRanking]
    excluded_repositories: list[ExcludedRepository]

    @model_validator(mode='after')
    def ranks_are_contiguous(self):
        if [row.rank for row in self.repositories] != list(range(1, len(self.repositories) + 1)):
            raise ValueError('ranking ranks must be contiguous')
        return self


class SnapshotTrace(_Strict):
    commit_sha: str = Field(min_length=1)
    blob_sha: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    readme_path: str = Field(min_length=1)
    collected_at: str = Field(min_length=1)


class BudgetLimits(_Strict):
    max_chunks: int = Field(ge=1, le=MAX_CHUNKS_PER_REPOSITORY)
    max_input_bytes: int = Field(ge=1, le=MAX_INPUT_BYTES)
    max_output_tokens: int = Field(ge=1, le=MAX_OUTPUT_TOKENS)


class RepositoryInput(_Strict):
    chunk_ids: list[str]
    chunk_count: int = Field(ge=0)
    input_bytes_upper_bound: int = Field(
        ge=0, description='Conservative UTF-8 byte upper bound on the full prompt; not tokenizer usage')
    budget: BudgetLimits

    @model_validator(mode='after')
    def count_matches(self):
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError('duplicate chunk_id in input')
        if self.chunk_count != len(self.chunk_ids):
            raise ValueError('chunk_count mismatch')
        return self


class RepositoryUsage(_Strict):
    known: bool
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    reused: bool = False

    @model_validator(mode='after')
    def known_tokens(self):
        values = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        if self.known and any(value is None for value in values):
            raise ValueError('known usage requires token counts')
        if not self.known and any(value is not None for value in values):
            raise ValueError('unknown usage must not carry token counts')
        if self.known and self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError('usage total mismatch')
        return self


class HardCounts(_Strict):
    model_config = ConfigDict(extra='forbid', strict=True, populate_by_name=True)
    passed: int = Field(alias='pass', ge=0)
    failed: int = Field(alias='fail', ge=0)
    unknown: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode='after')
    def total_matches(self):
        if self.total != self.passed + self.failed + self.unknown:
            raise ValueError('summary total mismatch')
        return self


class PreferredCounts(_Strict):
    evidence_found: int = Field(ge=0)
    unknown: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode='after')
    def total_matches(self):
        if self.total != self.evidence_found + self.unknown:
            raise ValueError('preferred total mismatch')
        return self


class HardConstraintSummary(_Strict):
    required: HardCounts
    prohibited: HardCounts
    preferred: PreferredCounts


class RepositoryReview(_Strict):
    repository_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    trace: SnapshotTrace
    coverage: Literal['full', 'truncated', 'non_utf8', 'empty']
    status: Literal['completed', 'failed', 'unknown', 'skipped']
    error_code: str | None = None
    input: RepositoryInput
    requirements: list[RequirementReview]
    hard_constraint_summary: HardConstraintSummary
    logic_group_results: list[LogicGroupResult]
    usage: RepositoryUsage
    response_hash: str | None = None


class SummaryUsage(_Strict):
    calls: int = Field(ge=0)
    unknown_calls: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode='after')
    def total_matches(self):
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError('summary usage total mismatch')
        return self


class ReportSummary(_Strict):
    repositories_total: int = Field(default=0, ge=0)
    repositories_completed: int = Field(default=0, ge=0)
    repositories_failed: int = Field(default=0, ge=0)
    repositories_unknown: int = Field(default=0, ge=0)
    repositories_skipped: int = Field(default=0, ge=0)
    requirements_total: int = Field(default=0, ge=0)
    supports: int = Field(default=0, ge=0)
    contradicts: int = Field(default=0, ge=0)
    insufficient: int = Field(default=0, ge=0)
    model_omitted: int = Field(default=0, ge=0)
    duplicates: int = Field(default=0, ge=0)
    unknown_items: int = Field(default=0, ge=0)
    usage: SummaryUsage


class ReportWarning(_Strict):
    code: str = Field(min_length=1)
    requirement_item_id: str | None = None
    logic_group_id: str | None = None
    message: str | None = None


class DeepReviewReport(_Strict):
    schema_version: Literal[1] = 1
    version: Literal['deep-review-v1'] = 'deep-review-v1'
    owner: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    confirmed_revision_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    revision_hash: str = Field(min_length=1)
    plan_hash: str = Field(min_length=1)
    bundle_hash: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    scope: Literal['provided_candidate_chunks'] = 'provided_candidate_chunks'
    notice: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    status: Literal['completed', 'partial', 'failed', 'unknown']
    repositories: list[RepositoryReview]
    summary: ReportSummary
    warnings: list[ReportWarning] = Field(default_factory=list)
    ranking: RankingResults | None = None
    cost_status: Literal['not_calculated', 'calculated'] = 'not_calculated'
    estimated_cost: float | None = Field(default=None, ge=0)
    currency: str | None = None
    pricing_source: str | None = None

    @model_validator(mode='after')
    def cost_consistency(self):
        if self.cost_status == 'not_calculated':
            if self.estimated_cost is not None or self.currency is not None or self.pricing_source is not None:
                raise ValueError('uncalculated cost must not carry pricing values')
        elif (self.estimated_cost is None or not self.currency or not self.pricing_source):
            raise ValueError('calculated cost requires amount, currency and pricing source')
        return self

    def model_dump(self, *args, **kwargs):
        kwargs.setdefault('by_alias', True)
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args, **kwargs):
        kwargs.setdefault('by_alias', True)
        return super().model_dump_json(*args, **kwargs)
