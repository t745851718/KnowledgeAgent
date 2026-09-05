"""FastAPI routes described by README/api.md."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse

from .errors import AppError
from ..core.ids import new_conversation_id, to_iso8601
from ..domain import (
    Conversation,
    ConversationCreate,
    Document,
    IngestionAccepted,
    Message,
    RagCompletionRequest,
)
from .security import require_internal_token


router = APIRouter(
    prefix="/internal/v1", dependencies=[Depends(require_internal_token)]
)


def _container(request: Request) -> Any:
    return request.app.state.container


def _owner_id(header_value: str | None, supplied_value: str | None = None) -> str:
    header = header_value.strip() if header_value else None
    supplied = supplied_value.strip() if supplied_value else None
    if header and supplied and header != supplied:
        raise AppError(403, "FORBIDDEN", "owner_id 与可信请求头不一致")
    owner_id = header or supplied
    if not owner_id:
        raise AppError(400, "INVALID_ARGUMENT", "缺少 owner_id")
    if len(owner_id.encode("utf-8")) > 128:
        raise AppError(400, "INVALID_ARGUMENT", "owner_id 的 UTF-8 编码不能超过 128 字节")
    return owner_id


def _page(items: list[dict[str, Any]], cursor: str | None, limit: int) -> dict[str, Any]:
    start = 0
    if cursor:
        try:
            start = next(
                index + 1
                for index, item in enumerate(items)
                if item.get("id") == cursor
            )
        except StopIteration as exc:
            raise AppError(400, "INVALID_ARGUMENT", "分页 cursor 无效") from exc
    selected = items[start : start + limit]
    has_more = start + limit < len(items)
    return {
        "items": selected,
        "next_cursor": selected[-1]["id"] if selected and has_more else None,
        "has_more": has_more,
    }


@router.get("/health/ready", tags=["health"])
async def ready(request: Request) -> JSONResponse:
    status_code, body = await _container(request).health.check()
    return JSONResponse(status_code=status_code, content=body)


@router.post(
    "/documents/ingestions",
    response_model=IngestionAccepted,
    status_code=202,
    tags=["documents"],
)
async def create_ingestion(
    request: Request,
    file: Annotated[UploadFile, File(description="PDF、Word 或 Markdown 文件")],
    owner_id: Annotated[str | None, Form(max_length=128)] = None,
    owner_header: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=200)
    ] = None,
    metadata: Annotated[str | None, Form()] = None,
) -> dict[str, str]:
    resolved_owner_id = _owner_id(owner_header, owner_id)
    try:
        parsed_metadata = json.loads(metadata) if metadata else {}
    except json.JSONDecodeError as exc:
        raise AppError(
            400,
            "INVALID_ARGUMENT",
            "metadata 必须是有效的 JSON 对象",
            details={"position": exc.pos},
        ) from exc
    if not isinstance(parsed_metadata, dict):
        raise AppError(400, "INVALID_ARGUMENT", "metadata 必须是 JSON 对象")
    return await _container(request).ingestion.accept(
        file,
        owner_id=resolved_owner_id,
        metadata=parsed_metadata,
        idempotency_key=idempotency_key,
    )


@router.get("/documents", tags=["documents"])
async def list_documents(
    request: Request,
    owner_header: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
    owner_id: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    resolved_owner_id = _owner_id(owner_header, owner_id)
    allowed_statuses = {"queued", "processing", "indexed", "failed", "deleting"}
    if status is not None and status not in allowed_statuses:
        raise AppError(400, "INVALID_ARGUMENT", "文档 status 无效")
    items = await _container(request).repository.list_documents(
        resolved_owner_id, status=status
    )
    return _page(items, cursor, limit)


@router.get(
    "/documents/{document_id}", response_model=Document, tags=["documents"]
)
async def get_document(
    document_id: str,
    request: Request,
    owner_header: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    return await _container(request).ingestion.get(
        document_id, owner_id=_owner_id(owner_header, owner_id)
    )


@router.get("/documents/{document_id}/images/{chunk_index}/{image_index}", tags=["documents"])
async def get_document_image(
    document_id: str, chunk_index: int, image_index: int, request: Request,
    owner_header: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
) -> Response:
    document = await _container(request).ingestion.get(document_id, owner_id=_owner_id(owner_header))
    image_keys = document.get("image_keys") or {}
    keys = image_keys.get(str(chunk_index), image_keys.get(chunk_index, []))
    if image_index >= len(keys):
        raise AppError(404, "IMAGE_NOT_FOUND", "图片不存在")
    data, content_type = await asyncio.to_thread(_container(request).storage.read_image, keys[image_index])
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})


@router.delete(
    "/documents/{document_id}", status_code=204, tags=["documents"]
)
async def delete_document(
    document_id: str,
    request: Request,
    owner_header: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
    owner_id: str | None = None,
) -> Response:
    await _container(request).ingestion.delete(
        document_id, owner_id=_owner_id(owner_header, owner_id)
    )
    return Response(status_code=204)


@router.post(
    "/conversations",
    response_model=Conversation,
    status_code=201,
    tags=["conversations"],
)
async def create_conversation(
    payload: ConversationCreate,
    request: Request,
    owner_header: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
) -> dict[str, Any]:
    owner_id = _owner_id(owner_header, payload.owner_id)
    now = to_iso8601()
    return await _container(request).repository.create_conversation(
        {
            "id": new_conversation_id(),
            "owner_id": owner_id,
            "title": (payload.title or "新会话").strip() or "新会话",
            "created_at": now,
            "updated_at": now,
        }
    )


@router.get("/conversations", tags=["conversations"])
async def list_conversations(
    request: Request,
    owner_header: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
    owner_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    resolved_owner_id = _owner_id(owner_header, owner_id)
    items = await _container(request).repository.list_conversations(
        owner_id=resolved_owner_id, limit=10_000
    )
    return _page(items, cursor, limit)


@router.get("/conversations/{conversation_id}/messages", tags=["conversations"])
async def list_messages(
    conversation_id: str,
    request: Request,
    owner_header: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
    owner_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    resolved_owner_id = _owner_id(owner_header, owner_id)
    conversation = await _container(request).repository.get_conversation(
        conversation_id, owner_id=resolved_owner_id
    )
    if conversation is None:
        raise AppError(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    items = await _container(request).repository.list_messages(
        conversation_id, limit=10_000
    )
    return _page(items, cursor, limit)


@router.delete(
    "/conversations/{conversation_id}", status_code=204, tags=["conversations"]
)
async def delete_conversation(
    conversation_id: str,
    request: Request,
    owner_header: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
    owner_id: str | None = None,
) -> Response:
    deleted = await _container(request).repository.delete_conversation(
        conversation_id, owner_id=_owner_id(owner_header, owner_id)
    )
    if not deleted:
        raise AppError(404, "CONVERSATION_NOT_FOUND", "会话不存在")
    return Response(status_code=204)


@router.post("/rag/completions", response_model=None, tags=["rag"])
async def rag_completion(
    payload: RagCompletionRequest,
    request: Request,
    owner_header: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
) -> JSONResponse | StreamingResponse:
    if owner_header:
        _owner_id(owner_header, payload.owner_id)
    if payload.request_id is None:
        payload = payload.model_copy(update={"request_id": request.state.request_id})
    request_id = str(payload.request_id)
    rag = _container(request).rag
    lock = await rag.acquire_request(request_id)
    try:
        prepared = await rag.prepare(payload)
    except BaseException:
        rag.release_request(request_id, lock)
        raise
    if payload.stream:
        async def guarded_stream():
            try:
                async for event in rag.stream(prepared):
                    yield event
            finally:
                rag.release_request(request_id, lock)

        return StreamingResponse(
            guarded_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    try:
        message, usage = await rag.complete(prepared)
        return JSONResponse(
            content={"message": message.model_dump(), "usage": usage.model_dump()}
        )
    finally:
        rag.release_request(request_id, lock)


__all__ = ["router"]
