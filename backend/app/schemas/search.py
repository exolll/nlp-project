from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.requirements import RequirementRevision, UUIDString
from app.services.search_contracts import QUERY_HASH_ALGORITHM, canonicalize_query, query_hash, validate_source_requirement_pointers


class SearchIntegrityError(ValueError):
    pass


class SearchStateError(ValueError):
    pass


class SearchContract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class SearchQueryDocument(SearchContract):
    query_id: UUIDString
    query_text: Annotated[str, Field(min_length=1, max_length=30000)]
    query_type: Literal['primary', 'expansion']
    language_filter: Annotated[str, Field(min_length=1, max_length=64)] | None
    qualifiers: dict[str, list[str]]
    source_requirement_pointers: list[str]
    query_hash: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    created_at: datetime

    @field_validator('created_at')
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Search timestamps must be timezone-aware')
        return value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def validate_query(self):
        fields = canonicalize_query(self.query_text, language_filter=self.language_filter, qualifiers=self.qualifiers)
        if len(self.query_text.encode('utf-8')) > 65535:
            raise ValueError('Query exceeds the SQL TEXT byte limit')
        if fields['qualifiers'] != self.qualifiers:
            raise ValueError('Query qualifiers must already be canonical')
        if self.query_hash != query_hash(**fields):
            raise ValueError('Query hash does not match its fields')
        if not self.source_requirement_pointers or len(set(self.source_requirement_pointers)) != len(self.source_requirement_pointers):
            raise ValueError('Query sources must be nonempty and unique')
        return self


class SearchGroupDocument(SearchContract):
    group_id: UUIDString
    group_name: Literal['A', 'B', 'C']
    group_purpose: Annotated[str, Field(min_length=1, max_length=255)]
    source_requirement_pointers: list[str]
    group_status: Literal['ready', 'skipped']
    skip_reason: Annotated[str, Field(min_length=1, max_length=255)] | None
    created_at: datetime
    queries: list[SearchQueryDocument]

    @field_validator('created_at')
    @classmethod
    def aware_time(cls, value):
        return SearchQueryDocument.aware_time(value)

    @model_validator(mode='after')
    def validate_group(self):
        if not self.group_purpose.strip():
            raise ValueError('Group purpose cannot be blank')
        if len(set(self.source_requirement_pointers)) != len(self.source_requirement_pointers):
            raise ValueError('Group sources must be unique')
        if self.group_status == 'ready':
            if self.skip_reason is not None or not self.queries or not self.source_requirement_pointers:
                raise ValueError('Ready groups require sources and queries, without a skip reason')
        elif self.queries or self.skip_reason is None or not self.skip_reason.strip():
            raise ValueError('Skipped groups require a reason and cannot contain queries')
        if len({q.query_hash for q in self.queries}) != len(self.queries):
            raise ValueError('Queries within a group must be unique')
        for query in self.queries:
            if not set(query.source_requirement_pointers).issubset(self.source_requirement_pointers):
                raise ValueError('Query sources must belong to the group')
        return self


class SearchPlanDocument(SearchContract):
    plan_id: UUIDString
    run_id: UUIDString
    confirmed_revision_id: UUIDString
    planner_version: Annotated[str, Field(min_length=1, max_length=64)]
    model_version: Annotated[str, Field(min_length=1, max_length=128)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=64)]
    query_hash_algorithm: Literal['projectpilot-query-v1'] = QUERY_HASH_ALGORITHM
    created_at: datetime
    groups: list[SearchGroupDocument]

    @field_validator('created_at')
    @classmethod
    def aware_time(cls, value):
        return SearchQueryDocument.aware_time(value)

    @model_validator(mode='after')
    def validate_plan(self):
        if any(not value.strip() for value in (self.planner_version, self.model_version, self.prompt_version)):
            raise ValueError('Versions cannot be blank')
        if len(self.groups) != 3 or {g.group_name for g in self.groups} != {'A', 'B', 'C'}:
            raise ValueError('A plan must contain exactly groups A, B and C')
        if len({g.group_id for g in self.groups}) != 3:
            raise ValueError('Group IDs must be unique')
        queries = [q for group in self.groups for q in group.queries]
        if len({q.query_id for q in queries}) != len(queries):
            raise ValueError('Query IDs must be unique within a plan')
        return self

    def validate_revision(self, revision: RequirementRevision, task_id: str):
        for group in self.groups:
            validate_source_requirement_pointers(
                revision, group.source_requirement_pointers, task_id=task_id,
                confirmed_revision_id=self.confirmed_revision_id, group_status=group.group_status,
            )
            for query in group.queries:
                validate_source_requirement_pointers(
                    revision, query.source_requirement_pointers, task_id=task_id,
                    confirmed_revision_id=self.confirmed_revision_id,
                    group_pointers=group.source_requirement_pointers,
                )
