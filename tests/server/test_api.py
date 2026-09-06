from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.server.core import Settings
from app.server.core.config import PROJECT_ROOT
from app.server.container import Container
from app.server.core.ids import new_conversation_id, to_iso8601
from app.server.main import create_app
from app.server.providers.bailian import (
    ChatResult,
    ChatStreamChunk,
    EmbeddingResult,
    RerankResult,
)
from app.server.providers.zilliz import SearchHit
from app.server.providers.storage import MinioStorage
from app.server.repositories import MemoryRepository
from app.server.services import HealthService, IngestionService, RagService, TaskSupervisor
from tests.support import integration_settings
from app.admin import AdminService


class FakeBailian:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_texts(self, texts, **kwargs):
        del kwargs
        return [
            EmbeddingResult(
                dense=[1.0] + [0.0] * (self.dimension - 1),
                sparse={1: 1.0},
            )
            for _ in texts
        ]

    def embed_multimodal(self, items, **kwargs):
        count = 1 if kwargs.get("enable_fusion") else len(items)
        return [[1.0] + [0.0] * (self.dimension - 1) for _ in range(count)]

    def embed_sparse_texts(self, texts, **kwargs):
        del kwargs
        return [{1: 1.0} for _ in texts]

    def rerank(self, query, documents, *, top_n=None, **kwargs):
        del query, kwargs
        limit = min(top_n or len(documents), len(documents))
        return [RerankResult(index=index, score=0.9 - index * 0.01) for index in range(limit)]

    def chat(self, messages):
        assert messages[-1]["role"] == "user"
        return ChatResult(
            content="这是根据知识库生成的回答。",
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        )

    def stream_chat(self, messages):
        assert messages[-1]["role"] == "user"
        yield ChatStreamChunk(content="这是根据")
        yield ChatStreamChunk(
            content="知识库生成的回答。",
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        )


def test_project_root_points_to_repository_root() -> None:
    assert (PROJECT_ROOT / "pyproject.toml").is_file()
    assert PROJECT_ROOT.name == "KnowledgeAgent"


class FakeMinerU:
    def parse_document(self, path: Path, *, document_id: str, output_root=None):
        del output_root
        raise AssertionError(f"Markdown 测试不应调用 MinerU: {path}, {document_id}")


class FakeZilliz:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def ensure_collection(self) -> None:
        return None

    def insert_chunks(self, chunks) -> int:
        self.rows.extend(dict(chunk) for chunk in chunks)
        return len(chunks)

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
                score=0.8,
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
            if not (
                row["owner_id"] == owner_id and row["document_id"] == document_id
            )
        ]
        return before - len(self.rows)

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


class MemoryObjectStorage:
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
            raise AssertionError("test upload exceeds max_size")
        key = f"documents/{owner_id}/{document_id}/source.{extension.lstrip('.')}"
        self.objects[key] = content
        return key, len(content), hashlib.sha256(content).hexdigest()

    def download(self, object_name, destination):
        target = Path(destination)
        target.write_bytes(self.objects[object_name])
        return target

    async def save_asset(self, upload, *, owner_id, document_id, relative_path, max_size):
        await upload.seek(0)
        content = await upload.read()
        if len(content) > max_size:
            raise AssertionError("test asset exceeds max_size")
        key = f"documents/{owner_id}/{document_id}/assets/{relative_path}"
        self.objects[key] = content
        return key, len(content)

    def save_image(self, source, *, owner_id, document_id, chunk_index, image_index):
        path = Path(source)
        key = f"documents/{owner_id}/{document_id}/images/{chunk_index}_{image_index}{path.suffix}"
        self.objects[key] = path.read_bytes()
        return key

    def read_image(self, object_name):
        return self.objects[object_name], "image/png"

    def delete(self, object_name):
        return self.objects.pop(object_name, None) is not None

    def delete_document_assets(self, *, owner_id, document_id):
        prefix = f"documents/{owner_id}/{document_id}/"
        for key in [key for key in self.objects if key.startswith(prefix)]:
            self.objects.pop(key)


def make_client(tmp_path: Path, *, token: str | None = None):
    settings = Settings(
        internal_api_token=token,
        embedding_dimension=4,
        chunk_size=80,
        chunk_overlap=10,
        retrieval_limit=10,
        rerank_limit=3,
    )
    repository = MemoryRepository()
    storage = MemoryObjectStorage()
    bailian = FakeBailian(settings.embedding_dimension)
    admin = AdminService(settings=settings, bailian=bailian)
    zilliz = FakeZilliz()
    supervisor = TaskSupervisor()
    ingestion = IngestionService(
        repository=repository,
        storage=storage,
        mineru=FakeMinerU(),
        bailian=bailian,
        zilliz=zilliz,
        settings=settings,
        supervisor=supervisor,
        admin=admin,
    )
    rag = RagService(
        repository=repository,
        bailian=bailian,
        zilliz=zilliz,
        settings=settings,
        admin=admin,
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
        mineru=FakeMinerU(),
        bailian=bailian,
        zilliz=zilliz,
        supervisor=supervisor,
        ingestion=ingestion,
        rag=rag,
        health=health,
        admin=admin,
    )
    return TestClient(create_app(settings=settings, container=container)), repository


def upload_markdown(client: TestClient) -> str:
    response = client.post(
        "/internal/v1/documents/ingestions",
        data={"owner_id": "user_1", "metadata": '{"source":"test"}'},
        files={
            "file": (
                "guide.md",
                "# Transformer\n\n位置编码为序列中的 token 提供位置信息。",
                "text/markdown",
            )
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["document_id"]


def wait_until_indexed(client: TestClient, document_id: str) -> dict[str, Any]:
    for _ in range(100):
        response = client.get(
            f"/internal/v1/documents/{document_id}",
            headers={"X-Owner-Id": "user_1"},
        )
        assert response.status_code == 200
        document = response.json()
        if document["status"] in {"indexed", "failed"}:
            return document
        time.sleep(0.01)
    raise AssertionError("文档入库未在测试超时前完成")


def create_conversation(repository: MemoryRepository) -> str:
    conversation_id = new_conversation_id()
    asyncio.run(
        repository.create_conversation(
            {
                "id": conversation_id,
                "owner_id": "user_1",
                "title": "测试会话",
                "created_at": to_iso8601(),
                "updated_at": to_iso8601(),
            }
        )
    )
    return conversation_id


def test_ready_and_request_id(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        response = client.get(
            "/internal/v1/health/ready", headers={"X-Request-Id": "req-test-1"}
        )
    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "req-test-1"
    assert response.json()["status"] == "ready"


def test_optional_internal_authentication(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, token="internal-secret")
    with client:
        unauthorized = client.get("/internal/v1/health/ready")
        authorized = client.get(
            "/internal/v1/health/ready",
            headers={"Authorization": "Bearer internal-secret"},
        )
    assert unauthorized.status_code == 401
    assert unauthorized.json()["code"] == "UNAUTHORIZED"
    assert authorized.status_code == 200


def test_markdown_ingestion_status_and_delete(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        document_id = upload_markdown(client)
        document = wait_until_indexed(client, document_id)
        assert document["stage"] == "completed"
        assert document["progress"] == 100
        assert document["chunk_count"] >= 1
        assert document["metadata"] == {"source": "test"}

        deleted = client.delete(
            f"/internal/v1/documents/{document_id}",
            headers={"X-Owner-Id": "user_1"},
        )
        missing = client.get(
            f"/internal/v1/documents/{document_id}",
            headers={"X-Owner-Id": "user_1"},
        )
    assert deleted.status_code == 204
    assert missing.status_code == 404
    assert missing.json()["code"] == "DOCUMENT_NOT_FOUND"
    assert client.app.state.container.storage.objects == {}


@pytest.mark.skipif(
    os.getenv("RUN_MINIO_INTEGRATION") != "1",
    reason="需要显式启用本地 MinIO 集成测试",
)
def test_real_minio_ingestion_and_delete(tmp_path: Path) -> None:
    settings_from_env = integration_settings()
    storage = MinioStorage(
        endpoint=settings_from_env.minio_endpoint,
        access_key=settings_from_env.minio_access_key,
        secret_key=settings_from_env.minio_secret_key,
        bucket_name=settings_from_env.minio_bucket_name,
        secure=settings_from_env.minio_secure,
    )
    client, repository = make_client(tmp_path)
    container = client.app.state.container
    container.storage = storage
    container.ingestion.storage = storage

    with client:
        document_id = upload_markdown(client)
        document = wait_until_indexed(client, document_id)
        assert document["status"] == "indexed"
        stored = asyncio.run(repository.get_document(document_id, owner_id="user_1"))
        assert stored is not None
        object_name = str(stored["storage_path"])
        stat = storage.client.stat_object(storage.bucket_name, object_name)
        assert stat.size > 0
        assert not (tmp_path / "uploads").exists()

        deleted = client.delete(
            f"/internal/v1/documents/{document_id}",
            headers={"X-Owner-Id": "user_1"},
        )
        assert deleted.status_code == 204

    from minio.error import S3Error

    with pytest.raises(S3Error) as missing:
        storage.client.stat_object(storage.bucket_name, object_name)
    assert missing.value.code in {"NoSuchKey", "NoSuchObject"}


def test_upload_validation(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        bad_metadata = client.post(
            "/internal/v1/documents/ingestions",
            data={"owner_id": "user_1", "metadata": "not-json"},
            files={"file": ("guide.md", "# title", "text/markdown")},
        )
        bad_type = client.post(
            "/internal/v1/documents/ingestions",
            data={"owner_id": "user_1"},
            files={"file": ("notes.txt", "hello", "text/plain")},
        )
    assert bad_metadata.status_code == 400
    assert bad_metadata.json()["code"] == "INVALID_ARGUMENT"
    assert bad_type.status_code == 415
    assert bad_type.json()["code"] == "UNSUPPORTED_FILE_TYPE"


def test_markdown_folder_assets_and_missing_counts_are_exposed(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        packaged = client.post(
            "/internal/v1/documents/ingestions",
            headers={"X-Owner-Id": "user_1"},
            data={
                "markdown_path": "course/guide.md",
                "asset_paths": '["course/assets/diagram.png"]',
            },
            files=[
                ("file", ("guide.md", "# 指南\n\n![流程图](assets/diagram.png)", "text/markdown")),
                ("assets", ("diagram.png", b"\x89PNG\r\n\x1a\nimage", "image/png")),
            ],
        )
        assert packaged.status_code == 202, packaged.text
        complete = wait_until_indexed(client, packaged.json()["document_id"])
        assert complete["status"] == "indexed", complete
        assert complete["total_images"] == 1
        assert complete["missing_images"] == 0

        missing = client.post(
            "/internal/v1/documents/ingestions",
            headers={"X-Owner-Id": "user_1"},
            files={"file": ("missing.md", "![缺失图](assets/missing.png)", "text/markdown")},
        )
        assert missing.status_code == 202, missing.text
        complete = wait_until_indexed(client, missing.json()["document_id"])
        assert complete["status"] == "indexed"
        assert complete["total_images"] == complete["missing_images"] == 1


def test_rag_non_stream_and_sse(tmp_path: Path) -> None:
    client, repository = make_client(tmp_path)
    conversation_id = create_conversation(repository)
    with client:
        document_id = upload_markdown(client)
        assert wait_until_indexed(client, document_id)["status"] == "indexed"

        payload = {
            "owner_id": "user_1",
            "conversation_id": conversation_id,
            "message": "为什么需要位置编码？",
            "document_ids": [document_id],
            "request_id": "chat_non_stream",
            "stream": False,
        }
        response = client.post("/internal/v1/rag/completions", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["message"]["role"] == "assistant"
        assert body["message"]["citations"][0]["document_id"] == document_id
        assert body["usage"]["total_tokens"] == 28

        payload["request_id"] = "chat_stream"
        payload["stream"] = True
        streamed = client.post("/internal/v1/rag/completions", json=payload)
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert "event: start" in streamed.text
    assert "event: delta" in streamed.text
    assert "event: citations" in streamed.text
    assert "event: done" in streamed.text
    assert "这是根据" in streamed.text
    assert '"total_tokens":28' in streamed.text


def test_document_list_owner_isolation_and_upload_idempotency(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    upload = {
        "file": (
            "guide.md",
            "# 文档\n\n这是一段用于幂等测试的内容。",
            "text/markdown",
        )
    }
    headers = {"X-Owner-Id": "user_1", "Idempotency-Key": "upload-key-1"}
    with client:
        first = client.post(
            "/internal/v1/documents/ingestions", headers=headers, files=upload
        )
        second = client.post(
            "/internal/v1/documents/ingestions", headers=headers, files=upload
        )
        assert first.status_code == 202
        assert second.status_code == 202
        document_id = first.json()["document_id"]
        assert second.json()["document_id"] == document_id
        assert wait_until_indexed(client, document_id)["status"] == "indexed"

        listed = client.get(
            "/internal/v1/documents", headers={"X-Owner-Id": "user_1"}
        )
        forbidden_as_missing = client.get(
            f"/internal/v1/documents/{document_id}",
            headers={"X-Owner-Id": "user_2"},
        )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [document_id]
    assert forbidden_as_missing.status_code == 404


def test_conversation_crud(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    headers = {"X-Owner-Id": "user_1"}
    with client:
        created = client.post(
            "/internal/v1/conversations",
            headers=headers,
            json={"title": "Transformer 问答"},
        )
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]

        listed = client.get("/internal/v1/conversations", headers=headers)
        messages = client.get(
            f"/internal/v1/conversations/{conversation_id}/messages",
            headers=headers,
        )
        other_owner = client.get(
            f"/internal/v1/conversations/{conversation_id}/messages",
            headers={"X-Owner-Id": "user_2"},
        )
        deleted = client.delete(
            f"/internal/v1/conversations/{conversation_id}", headers=headers
        )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == conversation_id
    assert messages.status_code == 200
    assert messages.json()["items"] == []
    assert other_owner.status_code == 404
    assert deleted.status_code == 204


def test_rag_request_id_defaults_and_conflicts(tmp_path: Path) -> None:
    client, repository = make_client(tmp_path)
    conversation_id = create_conversation(repository)
    with client:
        document_id = upload_markdown(client)
        assert wait_until_indexed(client, document_id)["status"] == "indexed"
        payload = {
            "owner_id": "user_1",
            "conversation_id": conversation_id,
            "message": "位置编码是什么？",
            "document_ids": [document_id],
            "stream": False,
        }
        headers = {"X-Request-Id": "chat-from-header"}
        first = client.post(
            "/internal/v1/rag/completions", json=payload, headers=headers
        )
        repeated = client.post(
            "/internal/v1/rag/completions", json=payload, headers=headers
        )
        conflicting = client.post(
            "/internal/v1/rag/completions",
            json={**payload, "message": "另一个问题"},
            headers=headers,
        )
    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["message"]["id"] == first.json()["message"]["id"]
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "REQUEST_ID_CONFLICT"


def test_rejects_blank_request_id_and_oversized_utf8_owner(tmp_path: Path) -> None:
    client, repository = make_client(tmp_path)
    conversation_id = create_conversation(repository)
    with client:
        blank_request = client.post(
            "/internal/v1/rag/completions",
            json={
                "owner_id": "user_1",
                "conversation_id": conversation_id,
                "message": "测试",
                "request_id": "   ",
                "stream": False,
            },
        )
        oversized_owner = client.get(
            "/internal/v1/documents",
            params={"owner_id": "中" * 43},
        )

    assert blank_request.status_code == 422
    assert oversized_owner.status_code == 400
    assert oversized_owner.json()["code"] == "INVALID_ARGUMENT"


def test_admin_metrics_and_runtime_configuration(tmp_path: Path) -> None:
    client, repository = make_client(tmp_path)
    conversation_id = create_conversation(repository)
    with client:
        configured = client.put(
            "/internal/v1/admin/config",
            json={"retrieval_limit": 7, "rerank_limit": 2,
                  "chat_model": "test-chat-model"},
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["retrieval_limit"] == 7
        assert client.app.state.container.bailian.chat_model == "test-chat-model"

        document_id = upload_markdown(client)
        assert wait_until_indexed(client, document_id)["status"] == "indexed"
        response = client.post(
            "/internal/v1/rag/completions",
            json={
                "owner_id": "user_1", "conversation_id": conversation_id,
                "message": "位置编码是什么？", "document_ids": [document_id],
                "request_id": "admin-observed", "stream": False,
            },
        )
        assert response.status_code == 200, response.text
        metrics = client.get("/internal/v1/admin/metrics")
    assert metrics.status_code == 200
    body = metrics.json()
    assert body["summary"]["completed_ingestions"] == 1
    assert body["summary"]["rag_requests"] == 1
    assert body["ingestions"][0]["status"] == "indexed"
    assert body["rag_runs"][0]["prompt"]
    assert body["rag_runs"][0]["output"] == "这是根据知识库生成的回答。"
    assert body["rag_runs"][0]["usage"]["completion_tokens"] == 8
