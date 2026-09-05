"""Application services for ingestion, health checks and RAG completions."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from ..api.errors import AppError
from ..core.config import Settings
from ..core.ids import new_document_id, new_message_id, new_request_id, to_iso8601
from ..domain import Citation, Message, RagCompletionRequest, Usage
from ..providers import ProviderError
from ..repositories import Repository
from ..utils.sse import encode_sse
from ..utils.text_splitter import split_markdown, split_markdown_pages


ALLOWED_CONTENT_TYPES = {
    ".pdf": {"application/pdf", "application/octet-stream"},
    ".doc": {"application/msword", "application/octet-stream"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    },
    ".md": {"text/markdown", "text/plain", "application/octet-stream"},
    ".markdown": {"text/markdown", "text/plain", "application/octet-stream"},
}


def _provider_app_error(error: ProviderError) -> AppError:
    if error.code == "FILE_TOO_LARGE":
        return AppError(413, "FILE_TOO_LARGE", "文件超过服务端配置的大小限制")
    status_code = (
        503 if error.provider in {"zilliz", "zilliz-volume", "storage"} else 502
    )
    return AppError(
        status_code,
        "UPSTREAM_SERVICE_ERROR",
        error.message,
        details={"provider": error.provider, "provider_code": error.code},
        retryable=error.retryable,
    )


def _looks_like_supported_file(extension: str, header: bytes) -> bool:
    if extension == ".pdf":
        return header.startswith(b"%PDF-")
    if extension == ".doc":
        return header.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    if extension == ".docx":
        return header.startswith(b"PK\x03\x04")
    if extension in {".md", ".markdown"}:
        if b"\x00" in header:
            return False
        try:
            header.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True
    return False


def _mineru_pages(content_list_path: Path) -> list[tuple[int, str]]:
    """Convert MinerU content-list items to page-scoped Markdown text."""
    items = json.loads(content_list_path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        return []
    pages: dict[int, list[str]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("page_idx"), int):
            continue
        item_type = item.get("type")
        text = ""
        if isinstance(item.get("text"), str) and item_type not in {
            "page_number",
            "footer",
        }:
            text = item["text"].strip()
            level = item.get("text_level")
            if text and isinstance(level, int) and 1 <= level <= 6:
                text = f"{'#' * level} {text}"
        elif item_type == "table" and isinstance(item.get("table_body"), str):
            table_parts = []
            for key in ("table_caption", "table_body", "table_footnote"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    table_parts.append(value.strip())
                elif isinstance(value, list):
                    table_parts.extend(str(part).strip() for part in value if str(part).strip())
            text = "\n\n".join(table_parts)
        elif item_type in {"list", "index"} and isinstance(item.get("list_items"), list):
            text = "\n".join(
                f"- {str(value).strip()}"
                for value in item["list_items"]
                if str(value).strip()
            )
        elif item_type in {"image", "chart"}:
            captions = item.get("image_caption") or item.get("chart_caption") or []
            if isinstance(captions, str):
                text = captions.strip()
            elif isinstance(captions, list):
                text = "\n".join(str(value).strip() for value in captions if str(value).strip())
            if not text and isinstance(item.get("content"), str):
                text = item["content"].strip()
        if text:
            pages.setdefault(item["page_idx"] + 1, []).append(text)
    return [
        (page, "\n\n".join(parts))
        for page, parts in sorted(pages.items())
        if parts
    ]


class TaskSupervisor:
    """Own ingestion tasks so exceptions are observed and shutdown is clean."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def start(self, key: str, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks[key] = task

        def finished(completed: asyncio.Task[Any]) -> None:
            self._tasks.pop(key, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)

    async def cancel(self, key: str) -> None:
        task = self._tasks.get(key)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class IngestionService:
    def __init__(
        self,
        *,
        repository: Repository,
        storage: Any,
        mineru: Any,
        bailian: Any,
        zilliz: Any,
        settings: Settings,
        supervisor: TaskSupervisor,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.mineru = mineru
        self.bailian = bailian
        self.zilliz = zilliz
        self.settings = settings
        self.supervisor = supervisor

    async def accept(
        self,
        upload: UploadFile,
        *,
        owner_id: str,
        metadata: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        if idempotency_key:
            existing = await self.repository.get_document_by_idempotency_key(
                owner_id, idempotency_key
            )
            if existing is not None:
                created_at = datetime.fromisoformat(
                    str(existing["created_at"]).replace("Z", "+00:00")
                ).astimezone(UTC)
                if datetime.now(UTC) - created_at <= timedelta(hours=24):
                    return {
                        "document_id": str(existing["id"]),
                        "status": str(existing["status"]),
                    }
                await self.repository.update_document(
                    str(existing["id"]), {"idempotency_key": None}
                )
        filename = Path(upload.filename or "").name
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_CONTENT_TYPES:
            raise AppError(
                415,
                "UNSUPPORTED_FILE_TYPE",
                "仅支持 PDF、Word 和 Markdown 文件",
                details={"filename": filename},
            )
        content_type = (upload.content_type or "application/octet-stream").lower()
        if content_type not in ALLOWED_CONTENT_TYPES[extension]:
            raise AppError(
                415,
                "UNSUPPORTED_FILE_TYPE",
                "文件 MIME 类型与扩展名不匹配",
                details={"filename": filename, "content_type": content_type},
            )
        if upload.size is not None and upload.size > self.settings.max_upload_size:
            raise AppError(413, "FILE_TOO_LARGE", "文件超过服务端配置的大小限制")

        header = await upload.read(8192)
        await upload.seek(0)
        if not _looks_like_supported_file(extension, header):
            raise AppError(
                415,
                "UNSUPPORTED_FILE_TYPE",
                "文件内容与扩展名不匹配或编码不受支持",
                details={"filename": filename},
            )

        document_id = new_document_id()
        try:
            stored_path, size, sha256 = await self.storage.save_upload(
                upload,
                document_id=document_id,
                extension=extension,
                max_size=self.settings.max_upload_size,
            )
        except ProviderError as exc:
            raise _provider_app_error(exc) from exc

        now = to_iso8601()
        try:
            document = await self.repository.create_document(
                {
                    "id": document_id,
                    "owner_id": owner_id,
                    "name": filename,
                    "content_type": content_type,
                    "size": size,
                    "status": "queued",
                    "stage": "uploading",
                    "progress": 0,
                    "chunk_count": None,
                    "error": None,
                    "metadata": metadata,
                    "storage_path": str(stored_path),
                    "volume_path": None,
                    "sha256": sha256,
                    "idempotency_key": idempotency_key,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        except Exception:
            await asyncio.to_thread(self.storage.delete, stored_path)
            if idempotency_key:
                existing = await self.repository.get_document_by_idempotency_key(
                    owner_id, idempotency_key
                )
                if existing is not None:
                    return {
                        "document_id": str(existing["id"]),
                        "status": str(existing["status"]),
                    }
            raise
        self.supervisor.start(document_id, self._process(document))
        return {"document_id": document_id, "status": "queued"}

    async def _update(self, document_id: str, **updates: Any) -> None:
        await self.repository.update_document(document_id, updates)

    async def _process(self, document: Mapping[str, Any]) -> None:
        document_id = str(document["id"])
        owner_id = str(document["owner_id"])
        source = Path(str(document["storage_path"]))
        extension = source.suffix.lower()
        stage = "parsing"
        inserted = 0
        pages: list[tuple[int, str]] = []
        try:
            volume_path = await asyncio.to_thread(
                self.storage.upload_volume,
                source,
                owner_id=owner_id,
                document_id=document_id,
            )
            if volume_path:
                await self._update(document_id, volume_path=volume_path)
            await self._update(
                document_id, status="processing", stage=stage, progress=10
            )
            if extension in {".md", ".markdown"}:
                markdown = await asyncio.to_thread(source.read_text, encoding="utf-8")
            else:
                parsed = await asyncio.to_thread(
                    self.mineru.parse_document, source, document_id=document_id
                )
                if parsed.content_list_path:
                    pages = await asyncio.to_thread(
                        _mineru_pages, parsed.content_list_path
                    )
                else:
                    pages = []
                markdown = (
                    ""
                    if pages
                    else await asyncio.to_thread(
                        parsed.markdown_path.read_text, encoding="utf-8"
                    )
                )

            stage = "splitting"
            await self._update(document_id, stage=stage, progress=35)
            chunks = (
                split_markdown_pages(
                    pages,
                    chunk_size=self.settings.chunk_size,
                    overlap=self.settings.chunk_overlap,
                )
                if extension not in {".md", ".markdown"} and pages
                else split_markdown(
                    markdown,
                    chunk_size=self.settings.chunk_size,
                    overlap=self.settings.chunk_overlap,
                )
            )
            if not chunks:
                raise ProviderError("parser", "文档解析后没有可入库文本")

            stage = "embedding"
            await self._update(document_id, stage=stage, progress=50)
            await asyncio.to_thread(self.zilliz.ensure_collection)
            for start in range(0, len(chunks), 20):
                chunk_batch = chunks[start : start + 20]
                texts = [chunk.content for chunk in chunk_batch]
                dense_vectors, sparse_vectors = await asyncio.gather(
                    asyncio.to_thread(
                        self.bailian.embed_multimodal,
                        [{"text": text} for text in texts],
                    ),
                    asyncio.to_thread(
                        self.bailian.embed_sparse_texts,
                        texts,
                        text_type="document",
                    ),
                )
                embeddings = [
                    type(
                        "HybridEmbedding",
                        (),
                        {"dense": dense, "sparse": sparse},
                    )
                    for dense, sparse in zip(
                        dense_vectors, sparse_vectors, strict=True
                    )
                ]
                stage = "indexing"
                rows = [
                    {
                        "chunk_id": f"{document_id}_{chunk.chunk_index}",
                        "document_id": document_id,
                        "owner_id": owner_id,
                        "content": chunk.content,
                        "page": chunk.page,
                        "section": chunk.section,
                        "chunk_index": chunk.chunk_index,
                        "dense_vector": embedding.dense,
                        "sparse_vector": embedding.sparse,
                    }
                    for chunk, embedding in zip(
                        chunk_batch, embeddings, strict=True
                    )
                ]
                batch_inserted = await asyncio.to_thread(
                    self.zilliz.insert_chunks, rows
                )
                if batch_inserted != len(rows):
                    raise ProviderError(
                        "zilliz",
                        f"向量写入数量异常：期望 {len(rows)}，实际 {batch_inserted}",
                    )
                inserted += batch_inserted
                progress = 50 + int(45 * min(inserted, len(chunks)) / len(chunks))
                await self._update(document_id, stage=stage, progress=progress)
            await self._update(
                document_id,
                status="indexed",
                stage="completed",
                progress=100,
                chunk_count=inserted,
                error=None,
            )
        except asyncio.CancelledError:
            current = await self.repository.get_document(document_id)
            if current is not None and current.get("status") != "deleting":
                await self._update(
                    document_id,
                    status="failed",
                    stage=stage,
                    error={
                        "code": "INGESTION_CANCELLED",
                        "message": "服务关闭，文档处理被取消",
                        "retryable": True,
                    },
                )
            raise
        except Exception as exc:
            if inserted:
                try:
                    await asyncio.to_thread(
                        self.zilliz.delete_document,
                        owner_id=owner_id,
                        document_id=document_id,
                    )
                except Exception:
                    pass
            retryable = isinstance(exc, ProviderError) and exc.retryable
            message = exc.message if isinstance(exc, ProviderError) else str(exc)
            code = (
                "DOCUMENT_PARSE_FAILED"
                if stage in {"parsing", "splitting"}
                else "DOCUMENT_INDEX_FAILED"
            )
            await self._update(
                document_id,
                status="failed",
                stage=stage,
                error={"code": code, "message": message, "retryable": retryable},
            )

    async def get(
        self, document_id: str, *, owner_id: str | None = None
    ) -> dict[str, Any]:
        document = await self.repository.get_document(document_id, owner_id=owner_id)
        if document is None:
            raise AppError(404, "DOCUMENT_NOT_FOUND", "文档不存在")
        return document

    async def delete(
        self, document_id: str, *, owner_id: str | None = None
    ) -> None:
        document = await self.get(document_id, owner_id=owner_id)
        owner_id = str(document["owner_id"])
        await self._update(document_id, status="deleting")
        await self.supervisor.cancel(document_id)
        try:
            await asyncio.to_thread(
                self.zilliz.delete_document,
                owner_id=owner_id,
                document_id=document_id,
            )
            storage_path = document.get("storage_path")
            if storage_path:
                await asyncio.to_thread(
                    self.storage.delete,
                    storage_path,
                    document.get("volume_path"),
                )
            processing_dir = self.settings.processing_dir / document_id
            if processing_dir.is_dir():
                await asyncio.to_thread(shutil.rmtree, processing_dir)
            await self.repository.delete_document(document_id)
        except Exception as exc:
            message = exc.message if isinstance(exc, ProviderError) else str(exc)
            retryable = isinstance(exc, ProviderError) and exc.retryable
            await self._update(
                document_id,
                status="failed",
                error={
                    "code": "DOCUMENT_DELETE_FAILED",
                    "message": message,
                    "retryable": retryable,
                },
            )
            if isinstance(exc, ProviderError):
                raise _provider_app_error(exc) from exc
            raise


@dataclass(slots=True)
class PreparedRag:
    request: RagCompletionRequest
    message_id: str
    model_messages: list[dict[str, str]]
    citations: list[Citation]
    cached_message: Message | None = None
    cached_usage: Usage | None = None


class RagService:
    def __init__(
        self,
        *,
        repository: Repository,
        bailian: Any,
        zilliz: Any,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.bailian = bailian
        self.zilliz = zilliz
        self.settings = settings
        self._request_locks: dict[str, tuple[asyncio.Lock, int]] = {}

    async def acquire_request(self, request_id: str) -> asyncio.Lock:
        lock, users = self._request_locks.get(
            request_id, (asyncio.Lock(), 0)
        )
        self._request_locks[request_id] = (lock, users + 1)
        try:
            await lock.acquire()
        except BaseException:
            current_lock, current_users = self._request_locks.get(
                request_id, (lock, 1)
            )
            if current_lock is lock and current_users <= 1:
                self._request_locks.pop(request_id, None)
            elif current_lock is lock:
                self._request_locks[request_id] = (lock, current_users - 1)
            raise
        return lock

    def release_request(self, request_id: str, lock: asyncio.Lock) -> None:
        lock.release()
        current_lock, users = self._request_locks.get(request_id, (lock, 1))
        if current_lock is not lock:
            return
        if users <= 1:
            self._request_locks.pop(request_id, None)
        else:
            self._request_locks[request_id] = (lock, users - 1)

    async def prepare(self, request: RagCompletionRequest) -> PreparedRag:
        if request.request_id is None:
            request = request.model_copy(update={"request_id": new_request_id()})
        conversation = await self.repository.get_conversation(
            request.conversation_id, owner_id=request.owner_id
        )
        if conversation is None:
            raise AppError(404, "CONVERSATION_NOT_FOUND", "会话不存在")

        existing = await self.repository.get_message_by_request_id(request.request_id)
        if existing is not None:
            if (
                existing.get("conversation_id") != request.conversation_id
                or existing.get("input_message") != request.message
                or list(existing.get("document_ids") or [])
                != list(request.document_ids or [])
            ):
                raise AppError(
                    409,
                    "REQUEST_ID_CONFLICT",
                    "request_id 已被其他会话使用",
                )
            return PreparedRag(
                request=request,
                message_id=str(existing["id"]),
                model_messages=[],
                citations=[
                    Citation.model_validate(item)
                    for item in existing.get("citations", [])
                ],
                cached_message=Message.model_validate(existing),
                cached_usage=Usage.model_validate(existing.get("usage") or {}),
            )

        if request.document_ids:
            documents = []
            for document_id in request.document_ids:
                document = await self.repository.get_document(
                    document_id, owner_id=request.owner_id
                )
                if document is None:
                    raise AppError(404, "DOCUMENT_NOT_FOUND", "文档不存在")
                if document.get("status") != "indexed":
                    raise AppError(
                        409,
                        "DOCUMENT_NOT_READY",
                        "指定文档尚未完成入库",
                        details={
                            "document_id": document_id,
                            "status": document.get("status"),
                        },
                    )
                documents.append(document)
        else:
            documents = await self.repository.list_documents(
                request.owner_id, status="indexed"
            )
        if not documents:
            raise AppError(400, "INVALID_ARGUMENT", "当前没有可用于问答的已入库文档")

        await self.repository.create_message(
            {
                "conversation_id": request.conversation_id,
                "role": "user",
                "content": request.message,
                "citations": [],
                "created_at": to_iso8601(),
            }
        )

        try:
            dense_vectors, sparse_vectors = await asyncio.gather(
                asyncio.to_thread(
                    self.bailian.embed_multimodal,
                    [{"text": request.message}],
                ),
                asyncio.to_thread(
                    self.bailian.embed_sparse_texts,
                    [request.message],
                    text_type="query",
                ),
            )
            query_embedding = type(
                "HybridEmbedding",
                (),
                {"dense": dense_vectors[0], "sparse": sparse_vectors[0]},
            )
            hits = await asyncio.to_thread(
                self.zilliz.search,
                query_embedding.dense,
                owner_id=request.owner_id,
                document_ids=[str(document["id"]) for document in documents],
                sparse_vector=query_embedding.sparse,
                limit=self.settings.retrieval_limit,
                candidate_limit=max(self.settings.retrieval_limit, 30),
            )
            ranked = []
            if hits:
                rankings = await asyncio.to_thread(
                    self.bailian.rerank,
                    request.message,
                    [hit.content for hit in hits],
                    top_n=self.settings.rerank_limit,
                )
                ranked = [(hits[item.index], item.score) for item in rankings]
        except ProviderError as exc:
            raise _provider_app_error(exc) from exc

        names = {str(document["id"]): str(document["name"]) for document in documents}
        citations = [
            Citation(
                document_id=hit.document_id,
                document_name=names.get(hit.document_id, "未知文档"),
                chunk_id=hit.chunk_id,
                page=hit.page,
                score=score,
                text=hit.content,
            )
            for hit, score in ranked
        ]
        context = "\n\n".join(
            f"[来源 {index}: {citation.document_name}]\n{citation.text}"
            for index, citation in enumerate(citations, 1)
        )
        history = await self.repository.list_messages(request.conversation_id, limit=20)  # type: ignore[attr-defined]
        model_messages = [
            {
                "role": "system",
                "content": (
                    "你是知识库问答助手。仅根据给出的参考资料回答；资料不足时明确说明。"
                    "不要编造来源，回答使用用户的语言。\n\n参考资料：\n"
                    + (context or "（没有检索到相关资料）")
                ),
            }
        ]
        model_messages.extend(
            {"role": str(item["role"]), "content": str(item["content"])}
            for item in history
            if item.get("role") in {"user", "assistant"}
        )
        return PreparedRag(
            request=request,
            message_id=new_message_id(),
            model_messages=model_messages,
            citations=citations,
        )

    async def complete(self, prepared: PreparedRag) -> tuple[Message, Usage]:
        if prepared.cached_message is not None:
            return prepared.cached_message, prepared.cached_usage or Usage()
        try:
            result = await asyncio.to_thread(
                self.bailian.chat, prepared.model_messages
            )
        except ProviderError as exc:
            raise _provider_app_error(exc) from exc
        return await self._save_answer(
            prepared, content=result.content, usage=Usage.model_validate(result.usage)
        )

    async def _save_answer(
        self, prepared: PreparedRag, *, content: str, usage: Usage
    ) -> tuple[Message, Usage]:
        message = await self.repository.create_message(
            {
                "id": prepared.message_id,
                "conversation_id": prepared.request.conversation_id,
                "role": "assistant",
                "content": content,
                "citations": [item.model_dump() for item in prepared.citations],
                "request_id": prepared.request.request_id,
                "input_message": prepared.request.message,
                "document_ids": prepared.request.document_ids or [],
                "usage": usage.model_dump(),
                "created_at": to_iso8601(),
            }
        )
        return Message.model_validate(message), usage

    async def stream(self, prepared: PreparedRag) -> AsyncIterator[str]:
        yield encode_sse(
            "start",
            {
                "request_id": prepared.request.request_id,
                "message_id": prepared.message_id,
            },
        )
        if prepared.cached_message is not None:
            if prepared.cached_message.content:
                yield encode_sse("delta", {"content": prepared.cached_message.content})
            yield encode_sse(
                "citations",
                {"items": [item.model_dump() for item in prepared.citations]},
            )
            yield encode_sse(
                "done",
                {
                    "finish_reason": "stop",
                    "usage": (prepared.cached_usage or Usage()).model_dump(),
                },
            )
            return

        chunks: list[str] = []
        try:
            iterator: Iterator[Any] = self.bailian.stream_chat(
                prepared.model_messages
            )

            sentinel = object()

            def next_chunk() -> Any:
                try:
                    return next(iterator)
                except StopIteration:
                    return sentinel

            usage = Usage()
            while True:
                chunk = await asyncio.to_thread(next_chunk)
                if chunk is sentinel:
                    break
                content = chunk if isinstance(chunk, str) else chunk.content
                chunk_usage = None if isinstance(chunk, str) else chunk.usage
                if chunk_usage:
                    usage = Usage.model_validate(chunk_usage)
                if content:
                    chunks.append(content)
                    yield encode_sse("delta", {"content": content})
            yield encode_sse(
                "citations",
                {"items": [item.model_dump() for item in prepared.citations]},
            )
            await self._save_answer(prepared, content="".join(chunks), usage=usage)
            yield encode_sse(
                "done", {"finish_reason": "stop", "usage": usage.model_dump()}
            )
        except Exception as exc:
            if isinstance(exc, ProviderError):
                message = exc.message
                retryable = exc.retryable
            else:
                message = "模型服务暂时不可用"
                retryable = True
            yield encode_sse(
                "error",
                {
                    "code": "UPSTREAM_SERVICE_ERROR",
                    "message": message,
                    "request_id": prepared.request.request_id,
                    "retryable": retryable,
                },
            )


class HealthService:
    def __init__(
        self,
        *,
        repository: Repository,
        zilliz: Any,
        settings: Settings,
        using_memory_repository: bool,
    ) -> None:
        self.repository = repository
        self.zilliz = zilliz
        self.settings = settings
        self.using_memory_repository = using_memory_repository

    async def check(self) -> tuple[int, dict[str, Any]]:
        dependencies = {
            "mongodb": "down" if self.using_memory_repository else "up",
            "zilliz": "up",
            "bailian": "not_checked",
            "mineru": "not_checked",
        }
        if not self.using_memory_repository:
            try:
                if not await self.repository.ping():
                    dependencies["mongodb"] = "down"
            except Exception:
                dependencies["mongodb"] = "down"
        try:
            if not await asyncio.to_thread(self.zilliz.ping):
                dependencies["zilliz"] = "down"
        except Exception:
            dependencies["zilliz"] = "down"
        ready = dependencies["mongodb"] == "up" and dependencies["zilliz"] == "up"
        return (
            200 if ready else 503,
            {"status": "ready" if ready else "not_ready", "dependencies": dependencies},
        )


__all__ = [
    "HealthService",
    "IngestionService",
    "PreparedRag",
    "RagService",
    "TaskSupervisor",
]
