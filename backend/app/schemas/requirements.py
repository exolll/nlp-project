"""Frozen requirement wire contracts; no defaults on the twelve business fields."""
from datetime import datetime
from typing import Annotated, Any, Literal, get_args
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.schemas.documents import Contract, ErrorResponse

SourceKind = Literal['assignment_document', 'user_primary_text', 'user_supplement', 'user_revision']
Modality = Literal['required', 'preferred', 'prohibited', 'informational']
Operator = Literal['ALL', 'ANY', 'not_applicable']
RequirementField = Literal[
    'project_topic', 'project_description', 'project_type', 'domain',
    'required_features', 'preferred_features', 'programming_languages',
    'required_technologies', 'preferred_technologies',
]
BUSINESS_FIELDS = get_args(RequirementField)


def uuid_string(value: str) -> str:
    return str(UUID(value))


UUIDString = Annotated[str, AfterValidator(uuid_string)]
Nonempty = Annotated[str, Field(min_length=1, max_length=30000)]


class StrictContract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class UncertainItem(StrictContract):
    field: RequirementField
    requirement_item_ids: list[str]
    reason: Nonempty
    question: Nonempty
    evidence_ids: list[str]


class RequirementEvidence(StrictContract):
    evidence_id: Nonempty
    field: RequirementField
    original_text: Nonempty
    source_kind: SourceKind
    source_location: Nonempty
    requirement_item_id: str | None
    segment_id: str | None
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)


class RequirementJSON(StrictContract):
    project_topic: Nonempty | None
    project_description: Nonempty | None
    project_type: Nonempty | None
    domain: Nonempty | None
    required_features: list[Nonempty] | None
    preferred_features: list[Nonempty] | None
    programming_languages: list[Nonempty] | None
    required_technologies: list[Nonempty] | None
    preferred_technologies: list[Nonempty] | None
    missing_information: list[str]
    uncertain_information: list[UncertainItem] | None
    source_evidence: list[RequirementEvidence]


class ItemEdit(StrictContract):
    requirement_item_id: str | None
    field: RequirementField
    value_path: str
    text: Nonempty
    modality: Modality
    source_kind: SourceKind
    evidence_ids: list[str]
    logic_group_id: str | None
    operator: Operator
    supersedes_item_id: str | None


class RequirementItem(ItemEdit):
    requirement_item_id: UUIDString
    supersedes_item_id: UUIDString | None


class ModelExtraction(StrictContract):
    requirements: RequirementJSON
    requirement_items: list[ItemEdit]


class RevisionEdit(ModelExtraction):
    change_reason: Annotated[str, Field(min_length=1, max_length=2000)]


class RequirementChange(Contract):
    field: str
    old_value: Any
    new_value: Any
    reason: str
    original_text: str
    created_at: datetime
    requirement_item_ids: list[str]


class RequirementRevision(Contract):
    revision_id: UUID
    task_id: UUID
    revision_number: int
    parent_revision_id: UUID | None
    status: Literal['extracted', 'draft', 'confirmed']
    requirements: RequirementJSON
    requirement_items: list[RequirementItem]
    change_log: list[RequirementChange]
    created_at: datetime
    confirmed_at: datetime | None


class ExtractionJob(Contract):
    extraction_id: UUID
    status: Literal['queued', 'extracting', 'validating', 'completed', 'failed']
    error: ErrorResponse | None
    model: str
    prompt_version: str
    usage: dict[str, Any] | None
    created_at: datetime


class DocumentRequirements(Contract):
    extraction: ExtractionJob | None
    revisions: list[RequirementRevision]
    latest_revision_id: UUID | None
