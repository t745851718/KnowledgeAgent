"""Document ingestion graphs; admission and cleanup remain service boundaries."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

from fastapi import UploadFile
from langgraph.graph import END, START, StateGraph

from ..api.errors import AppError
from ..core import Settings
from ..core.ids import new_document_id, to_iso8601
from ..providers import ProviderError
from ..repositories import Repository
from ..utils.document_chunks import _mineru_chunks, _mineru_markdown_chunks
from ..utils.text_splitter import TextChunk
from ..utils.markdown_chunks import uploaded_markdown_chunks
from .common import TaskSupervisor, _provider_app_error, run_sync, sync_scope


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


def _embed_dense_chunks(bailian: Any, chunks: list[TextChunk]) -> list[list[float]]:
    """Batch text and bound image-fusion concurrency while restoring chunk order."""
    text_indices = [index for index, chunk in enumerate(chunks) if not chunk.image_paths]
    groups = ([text_indices] if text_indices else []) + [
        [index] for index, chunk in enumerate(chunks) if chunk.image_paths
    ]

    def embed(indices: list[int]) -> list[list[float]]:
        chunk = chunks[indices[0]]
        if chunk.image_paths:
            result = bailian.embed_multimodal(
                [{"text": chunk.content}, *({"image": path} for path in chunk.image_paths)],
                enable_fusion=True,
            )
            if len(result) != 1:
                raise ProviderError("bailian", "图文融合必须为每个 chunk 返回一个 dense 向量")
        else:
            result = bailian.embed_multimodal([{"text": chunks[index].content} for index in indices])
            if len(result) != len(indices):
                raise ProviderError("bailian", "文本 dense 向量数量与 chunk 数量不一致")
        return result

    vectors: dict[int, list[float]] = {}
    if len(groups) <= 1:
        results = [embed(indices) for indices in groups]
    else:
        # Context shutdown waits for SDK calls even when one fails; cleanup may then
        # safely remove their image files. Avoid one thread per image or all-document fanout.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="dense-fusion") as pool:
            results = list(pool.map(embed, groups))
    for indices, result in zip(groups, results, strict=True):
        vectors.update(zip(indices, result, strict=True))
    return [vectors[index] for index in range(len(chunks))]



@dataclass(slots=True)
class IngestionRun:
    """Per-invocation cleanup ledger, including writes made by a failing node."""
    document: Mapping[str, Any]
    root: Path
    stage: str = "parsing"
    inserted: int = 0
    index_started: bool = False
    image_keys: dict[int, list[str]] | None = None

    @property
    def document_id(self) -> str:
        return str(self.document["id"])

    @property
    def source(self) -> Path:
        return self.root / f"source{Path(str(self.document['storage_path'])).suffix.lower()}"


class IngestionState(TypedDict, total=False):
    run: IngestionRun
    parsed: Any
    markdown: str
    chunks: list[TextChunk]


class BatchState(TypedDict, total=False):
    run: IngestionRun
    chunks: list[TextChunk]
    dense: list[list[float]]
    sparse: list[dict[int, float]]


class IngestionService:
    def __init__(self, *, repository: Repository, storage: Any, mineru: Any,
                 bailian: Any, zilliz: Any, settings: Settings,
                 supervisor: TaskSupervisor) -> None:
        self.repository = repository
        self.storage = storage
        self.mineru = mineru
        self.bailian = bailian
        self.zilliz = zilliz
        self.settings = settings
        self.supervisor = supervisor

        batch = StateGraph(BatchState)
        batch.add_node("dense", self._dense)
        batch.add_node("sparse", self._sparse)
        batch.add_node("write", self._write)
        batch.add_edge(START, "dense")
        batch.add_edge(START, "sparse")
        batch.add_edge(["dense", "sparse"], "write")
        batch.add_edge("write", END)
        self.batch_graph = batch.compile()

        graph = StateGraph(IngestionState)
        steps = ("download", "parse", "split", "index_batches", "finish")
        previous = START
        for name in steps:
            graph.add_node(name, getattr(self, f"_{name}"))
            graph.add_edge(previous, name)
            previous = name
        graph.add_edge(previous, END)
        self.graph = graph.compile()

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
                owner_id=owner_id,
                document_id=document_id,
                extension=extension,
                content_type=content_type,
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

    async def _stage(self, run: IngestionRun, stage: str, **updates: Any) -> None:
        run.stage = stage
        await self._update(run.document_id, stage=stage, **updates)

    async def _download(self, state: IngestionState) -> dict:
        run = state["run"]
        await self._stage(run, "parsing", status="processing", progress=10)
        await run_sync(self.storage.download, run.document["storage_path"], run.source)
        return {}

    async def _parse(self, state: IngestionState) -> dict:
        run = state["run"]
        if run.source.suffix in {".md", ".markdown"}:
            return {"markdown": await run_sync(run.source.read_text, encoding="utf-8")}
        parsed = await run_sync(
            self.mineru.parse_document, run.source,
            document_id=run.document_id, output_root=run.root / "mineru",
        )
        return {"parsed": parsed}

    async def _split(self, state: IngestionState) -> dict:
        await self._stage(state["run"], "splitting", progress=35)
        options = {"chunk_size": self.settings.chunk_size, "overlap": self.settings.chunk_overlap}
        parsed = state.get("parsed")
        chunks = []
        if parsed is not None:
            if parsed.content_list_path:
                chunks = await run_sync(_mineru_chunks, parsed.content_list_path, **options)
            if not chunks:
                markdown = await run_sync(parsed.markdown_path.read_text, encoding="utf-8")
                chunks = await run_sync(
                    _mineru_markdown_chunks, markdown, parsed.markdown_path.parent, **options,
                )
        else:
            chunks = await run_sync(uploaded_markdown_chunks, state["markdown"], **options)
        if not chunks:
            raise ProviderError("parser", "文档解析后没有可入库文本")
        image_keys: dict[int, list[str]] = {}
        for chunk in chunks:
            keys = []
            for image_index, image in enumerate(chunk.image_paths):
                if isinstance(image, Path):
                    keys.append(await run_sync(
                        self.storage.save_image, image, owner_id=str(state["run"].document["owner_id"]),
                        document_id=state["run"].document_id, chunk_index=chunk.chunk_index,
                        image_index=image_index,
                    ))
            if keys:
                image_keys[chunk.chunk_index] = keys
        state["run"].image_keys = {str(index): keys for index, keys in image_keys.items()}
        if image_keys:
            await self._update(state["run"].document_id, image_keys=state["run"].image_keys)
        return {"chunks": chunks}

    async def _index_batches(self, state: IngestionState) -> dict:
        run, chunks = state["run"], state["chunks"]
        await self._stage(run, "embedding", progress=50)
        await run_sync(self.zilliz.ensure_collection)
        # Reuse a small graph per batch: bounded vector memory and no graph recursion limit
        # depending on document length. Each batch joins dense+sparse before insertion.
        for start in range(0, len(chunks), 20):
            await self._stage(run, "embedding")
            await self.batch_graph.ainvoke({"run": run, "chunks": chunks[start:start + 20]})
            await self._update(run.document_id, progress=50 + int(45 * run.inserted / len(chunks)))
        return {}

    async def _dense(self, state: BatchState) -> dict:
        return {"dense": await run_sync(_embed_dense_chunks, self.bailian, state["chunks"])}

    async def _sparse(self, state: BatchState) -> dict:
        return {"sparse": await run_sync(
            self.bailian.embed_sparse_texts,
            [chunk.content for chunk in state["chunks"]], text_type="document",
        )}

    async def _write(self, state: BatchState) -> dict:
        run = state["run"]
        await self._stage(run, "indexing")
        rows = [
            {
                "chunk_id": f"{run.document_id}_{chunk.chunk_index}",
                "document_id": run.document_id,
                "owner_id": str(run.document["owner_id"]),
                "content": chunk.content, "page": chunk.page, "section": chunk.section,
                "chunk_index": chunk.chunk_index,
                "dense_vector": dense, "sparse_vector": sparse,
            }
            for chunk, dense, sparse in zip(
                state["chunks"], state["dense"], state["sparse"], strict=True,
            )
        ]
        run.index_started = True
        inserted = await run_sync(self.zilliz.insert_chunks, rows)
        if inserted != len(rows):
            raise ProviderError("zilliz", f"向量写入数量异常：期望 {len(rows)}，实际 {inserted}")
        run.inserted += inserted
        return {}

    async def _finish(self, state: IngestionState) -> dict:
        run = state["run"]
        await self._stage(run, "completed", status="indexed", progress=100,
                          chunk_count=run.inserted, error=None)
        return {}

    async def _process(self, document: Mapping[str, Any]) -> None:
        run = IngestionRun(document, Path(tempfile.mkdtemp(prefix=f"knowledgeagent-{document['id']}-")))
        try:
            async with sync_scope():
                await self.graph.ainvoke({"run": run})
        except asyncio.CancelledError:
            current = await self.repository.get_document(run.document_id)
            if current is not None and current.get("status") != "deleting":
                await self._update(
                    run.document_id, status="failed", stage=run.stage,
                    error={"code": "INGESTION_CANCELLED", "message": "服务关闭，文档处理被取消", "retryable": True},
                )
            raise
        except Exception as exc:
            if run.index_started:
                try:
                    await run_sync(self.zilliz.delete_document,
                                   owner_id=str(document["owner_id"]), document_id=run.document_id)
                except Exception:
                    pass
            if run.image_keys:
                try:
                    await run_sync(self.storage.delete_document_assets,
                                   owner_id=str(document["owner_id"]), document_id=run.document_id)
                except Exception:
                    pass
            code = "DOCUMENT_PARSE_FAILED" if run.stage in {"parsing", "splitting"} else "DOCUMENT_INDEX_FAILED"
            await self._update(
                run.document_id, status="failed", stage=run.stage,
                error={"code": code, "message": exc.message if isinstance(exc, ProviderError) else str(exc),
                       "retryable": isinstance(exc, ProviderError) and exc.retryable},
            )
        finally:
            await run_sync(shutil.rmtree, run.root, True)

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
            await asyncio.to_thread(self.storage.delete_document_assets,
                                    owner_id=owner_id, document_id=document_id)
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
