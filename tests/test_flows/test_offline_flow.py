from __future__ import annotations

import hashlib
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.server.container import Container
from app.server.core import Settings
from app.server.main import create_app
from app.server.providers.bailian import ChatResult, ChatStreamChunk, RerankResult
from app.server.providers.zilliz import SearchHit
from app.server.repositories import MemoryRepository
from app.server.services import HealthService, IngestionService, RagService, TaskSupervisor


class MemoryMinio:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def initialize(self) -> None:
        return None

    def ping(self) -> bool:
        return True

    async def save_upload(
        self,
        upload,
        *,
        owner_id,
        document_id,
        extension,
        content_type,
        max_size,
    ):
        del content_type
        await upload.seek(0)
        content = await upload.read()
        if len(content) > max_size:
            raise ValueError("文件超过测试限制")
        key = f"documents/{owner_id}/{document_id}/source.{extension.lstrip('.')}"
        self.objects[key] = content
        return key, len(content), hashlib.sha256(content).hexdigest()

    def download(self, key: str, destination: str | Path) -> Path:
        target = Path(destination)
        target.write_bytes(self.objects[key])
        return target

    def delete(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None

    def delete_document_assets(self, *, owner_id: str, document_id: str) -> None:
        prefix = f"documents/{owner_id}/{document_id}/"
        for key in [key for key in self.objects if key.startswith(prefix)]:
            self.objects.pop(key)


@dataclass
class ParsedDocument:
    markdown_path: Path
    content_list_path: Path | None = None


class FakeMinerU:
    def parse_document(self, source, *, document_id, output_root=None):
        root = Path(output_root) / document_id
        root.mkdir(parents=True)
        markdown = root / "full.md"
        markdown.write_text("# 解析结果\n\n原文件统一保存在 MinIO。", encoding="utf-8")
        return ParsedDocument(markdown_path=markdown)


class FakeBailian:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_multimodal(self, items, **kwargs):
        del kwargs
        return [[1.0] + [0.0] * (self.dimension - 1) for _ in items]

    def embed_sparse_texts(self, texts, **kwargs):
        del kwargs
        return [{1: 1.0} for _ in texts]

    def rerank(self, query, documents, *, top_n=None, **kwargs):
        del query, kwargs
        return [
            RerankResult(index=index, score=0.9 - index * 0.01)
            for index in range(min(top_n or len(documents), len(documents)))
        ]

    def chat(self, messages):
        del messages
        return ChatResult(
            content="原文件保存在 MinIO。",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    def stream_chat(self, messages):
        del messages
        yield ChatStreamChunk(content="原文件保存在 ")
        yield ChatStreamChunk(
            content="MinIO。",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )


class FakeZilliz:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def ensure_collection(self) -> None:
        return None

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None

    def insert_chunks(self, rows) -> int:
        self.rows.extend(dict(row) for row in rows)
        return len(rows)

    def search(
        self,
        dense_vector,
        *,
        owner_id,
        document_ids=None,
        sparse_vector=None,
        limit=10,
        candidate_limit=30,
    ):
        del dense_vector, sparse_vector, candidate_limit
        allowed = set(document_ids or [])
        rows = [
            row
            for row in self.rows
            if row["owner_id"] == owner_id
            and (not allowed or row["document_id"] in allowed)
        ]
        return [
            SearchHit(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                owner_id=row["owner_id"],
                content=row["content"],
                score=0.9,
                page=row.get("page"),
                section=row.get("section"),
                chunk_index=row["chunk_index"],
            )
            for row in rows[:limit]
        ]

    def delete_document(self, *, owner_id: str, document_id: str) -> int:
        before = len(self.rows)
        self.rows = [
            row
            for row in self.rows
            if not (row["owner_id"] == owner_id and row["document_id"] == document_id)
        ]
        return before - len(self.rows)


def _client() -> tuple[TestClient, MemoryMinio, FakeZilliz]:
    settings = Settings(
        embedding_dimension=4,
        chunk_size=80,
        chunk_overlap=10,
        retrieval_limit=10,
        rerank_limit=3,
    )
    repository = MemoryRepository()
    storage = MemoryMinio()
    mineru = FakeMinerU()
    bailian = FakeBailian(settings.embedding_dimension)
    zilliz = FakeZilliz()
    supervisor = TaskSupervisor()
    ingestion = IngestionService(
        repository=repository,
        storage=storage,
        mineru=mineru,
        bailian=bailian,
        zilliz=zilliz,
        settings=settings,
        supervisor=supervisor,
    )
    rag = RagService(
        repository=repository,
        bailian=bailian,
        zilliz=zilliz,
        settings=settings,
    )
    health = HealthService(
        repository=repository,
        storage=storage,
        zilliz=zilliz,
        settings=settings,
        using_memory_repository=False,
    )
    container = Container(
        settings=settings,
        repository=repository,
        storage=storage,
        mineru=mineru,
        bailian=bailian,
        zilliz=zilliz,
        supervisor=supervisor,
        ingestion=ingestion,
        rag=rag,
        health=health,
    )
    return TestClient(create_app(settings=settings, container=container)), storage, zilliz


def _wait_for_index(client: TestClient, document_id: str, headers: dict[str, str]):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/internal/v1/documents/{document_id}", headers=headers)
        assert response.status_code == 200, response.text
        document = response.json()
        if document["status"] in {"indexed", "failed"}:
            return document
        time.sleep(0.01)
    raise AssertionError("离线入库任务超时")


def test_complete_offline_pdf_flow() -> None:
    client, storage, zilliz = _client()
    headers = {"X-Owner-Id": "flow-user"}
    temp_root = Path(tempfile.gettempdir())

    with client:
        ready = client.get("/internal/v1/health/ready")
        assert ready.status_code == 200
        assert ready.json()["dependencies"]["minio"] == "up"

        uploaded = client.post(
            "/internal/v1/documents/ingestions",
            headers={**headers, "Idempotency-Key": "offline-flow-upload"},
            files={"file": ("flow.pdf", b"%PDF-1.7\nflow test", "application/pdf")},
        )
        assert uploaded.status_code == 202, uploaded.text
        document_id = uploaded.json()["document_id"]
        object_key = f"documents/flow-user/{document_id}/source.pdf"
        assert object_key in storage.objects

        document = _wait_for_index(client, document_id, headers)
        assert document["status"] == "indexed", document
        assert zilliz.rows

        conversation = client.post(
            "/internal/v1/conversations", headers=headers, json={"title": "流程测试"}
        )
        assert conversation.status_code == 201, conversation.text
        conversation_id = conversation.json()["id"]

        body = {
            "owner_id": "flow-user",
            "conversation_id": conversation_id,
            "message": "原文件保存在哪里？",
            "document_ids": [document_id],
            "stream": False,
            "request_id": "offline-flow-chat",
        }
        answer = client.post("/internal/v1/rag/completions", json=body)
        assert answer.status_code == 200, answer.text
        assert answer.json()["message"]["citations"][0]["document_id"] == document_id

        streamed = client.post(
            "/internal/v1/rag/completions",
            json={**body, "request_id": "offline-flow-sse", "stream": True},
        )
        assert streamed.status_code == 200, streamed.text
        for event in ("start", "delta", "citations", "done"):
            assert f"event: {event}" in streamed.text

        deleted = client.delete(f"/internal/v1/documents/{document_id}", headers=headers)
        assert deleted.status_code == 204, deleted.text
        assert object_key not in storage.objects
        assert not any(row["document_id"] == document_id for row in zilliz.rows)
        assert not list(temp_root.glob(f"knowledgeagent-{document_id}-*"))

        deleted_conversation = client.delete(
            f"/internal/v1/conversations/{conversation_id}", headers=headers
        )
        assert deleted_conversation.status_code == 204

        missing = client.get(f"/internal/v1/documents/{document_id}", headers=headers)
        assert missing.status_code == 404
