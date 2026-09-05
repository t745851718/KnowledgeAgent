"""Pydantic request and response models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    DELETING = "deleting"


class DocumentStage(StrEnum):
    UPLOADING = "uploading"
    PARSING = "parsing"
    SPLITTING = "splitting"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    COMPLETED = "completed"


class DocumentError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class Document(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    owner_id: str
    name: str
    content_type: str
    size: int = Field(ge=0)
    status: DocumentStatus
    stage: DocumentStage
    progress: int = Field(ge=0, le=100)
    chunk_count: int | None = Field(default=None, ge=0)
    error: DocumentError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    storage_path: str | None = None
    created_at: str
    updated_at: str


class IngestionAccepted(BaseModel):
    document_id: str
    status: DocumentStatus = DocumentStatus.QUEUED


class ConversationCreate(BaseModel):
    owner_id: str | None = Field(default=None, min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=200)


class Conversation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    owner_id: str
    title: str
    created_at: str
    updated_at: str


class Citation(BaseModel):
    document_id: str
    document_name: str
    chunk_id: str
    page: int | None = None
    score: float
    text: str


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    conversation_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    citations: list[Citation] = Field(default_factory=list)
    request_id: str | None = None
    created_at: str


class RagCompletionRequest(BaseModel):
    owner_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=32_000)
    document_ids: list[str] | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    stream: bool = True

    @field_validator("owner_id", "conversation_id", "message")
    @classmethod
    def strip_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value

    @field_validator("request_id")
    @classmethod
    def strip_optional_request_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("request_id 不能为空")
        return value

    @field_validator("document_ids")
    @classmethod
    def unique_document_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("document_ids 不能包含空字符串")
        return list(dict.fromkeys(cleaned))


class RagCompletionResponse(BaseModel):
    message: Message
    usage: Usage
