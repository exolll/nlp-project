from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')

    @field_validator('*', mode='after')
    @classmethod
    def utc_dates(cls, value):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class ErrorResponse(Contract):
    error_code: str
    message: str
    retryable: bool = False
    details: dict | None = None
    request_id: str | None = None


class SourceSegment(Contract):
    segment_id: str
    source_type: Literal['file', 'user_text']
    source_kind: Literal['assignment_document', 'user_primary_text', 'user_supplement']
    content: str
    original_content: str
    source_location: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    location_detail: dict[str, Any]


class NormalizedDocument(Contract):
    document_id: UUID
    task_id: UUID
    input_mode: Literal['pdf', 'docx', 'text', 'pdf_text', 'docx_text']
    original_filename: str | None
    mime_type: str
    file_size: int = Field(ge=0)
    total_pages: int | None
    extracted_text: str
    supplemental_text: str | None
    normalized_text: str
    source_segments: list[SourceSegment]
    content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    extraction_status: Literal['completed'] = 'completed'
    warnings: list[str]
    created_at: datetime
    character_count: int = Field(gt=0)

    @model_validator(mode='after')
    def verify_locations(self):
        if not self.source_segments:
            raise ValueError('文档必须有来源片段。')
        for segment in self.source_segments:
            if self.normalized_text[segment.start_offset:segment.end_offset] != segment.content:
                raise ValueError('来源片段的字符范围不匹配。')
        return self


class TaskResponse(Contract):
    task_id: UUID
    created_at: datetime
    status: Literal['empty', 'processing', 'completed', 'failed']
    document_id: UUID | None


class TaskList(Contract):
    tasks: list[TaskResponse]


class InputStatus(Contract):
    task_id: UUID
    document_id: UUID | None
    status: Literal['empty', 'processing', 'completed', 'failed']
    error: ErrorResponse | None = None


class SessionResponse(Contract):
    access_token: str
    expires_at: datetime


class InputConfig(Contract):
    max_file_bytes: int
    max_pdf_pages: int
    max_primary_characters: int
    max_supplement_characters: int
    storage_backend: str
    environment: str
