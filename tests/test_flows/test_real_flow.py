from __future__ import annotations

import os
import json
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from minio.error import S3Error

from app.server.core import Settings
from app.server.core.config import PROJECT_ROOT
from app.server.main import create_app
from app.server.container import build_container
from tests.support import integration_settings


pytestmark = [
    pytest.mark.full_flow,
    pytest.mark.skipif(
        os.getenv("RUN_FULL_FLOW") != "1",
        reason="真实全流程会访问计费服务，需要设置 RUN_FULL_FLOW=1",
    ),
]


def _headers(settings: Settings, owner_id: str) -> dict[str, str]:
    headers = {"X-Owner-Id": owner_id}
    if settings.internal_api_token:
        headers["Authorization"] = f"Bearer {settings.internal_api_token}"
    return headers


def _mime_type(path: Path) -> str:
    return {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
    }[path.suffix.lower()]


def _wait_for_terminal_state(
    client: TestClient,
    document_id: str,
    headers: dict[str, str],
    timeout: float,
) -> dict:
    deadline = time.monotonic() + timeout
    last_document: dict | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/internal/v1/documents/{document_id}", headers=headers)
        assert response.status_code == 200, response.text
        last_document = response.json()
        if last_document["status"] in {"indexed", "failed"}:
            return last_document
        time.sleep(0.5)
    raise AssertionError(f"入库任务超时，最后状态: {last_document}")


def _wait_for_temp_cleanup(document_id: str, timeout: float = 5) -> None:
    root = Path(tempfile.gettempdir())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not list(root.glob(f"knowledgeagent-{document_id}-*")):
            return
        time.sleep(0.05)
    raise AssertionError(f"文档 {document_id} 的系统临时目录未清理")


def test_real_upload_parse_rag_and_cleanup_flow() -> None:
    settings = integration_settings()
    configured_path = os.getenv("FULL_FLOW_DOCUMENT")
    document_path = Path(configured_path) if configured_path else (
        PROJECT_ROOT / "tests/data/AttentionIsAllYouNeed.pdf"
    )
    if not document_path.is_absolute():
        document_path = PROJECT_ROOT / document_path
    document_path = document_path.resolve()
    assert document_path.is_file(), f"测试文档不存在: {document_path}"

    timeout = float(os.getenv("FULL_FLOW_TIMEOUT", "900"))
    run_id = uuid.uuid4().hex[:12]
    owner_id = f"flow-{run_id}"
    headers = _headers(settings, owner_id)
    document_id: str | None = None
    conversation_id: str | None = None
    object_key: str | None = None
    local_upload_root = PROJECT_ROOT / "data/uploads"
    local_files_before = set(local_upload_root.rglob("*")) if local_upload_root.exists() else set()

    container = build_container(settings)
    original_embed = container.bailian.embed_multimodal
    fused_chunks = 0
    fusion_count_lock = threading.Lock()

    def observed_embed(items, **kwargs):
        nonlocal fused_chunks
        result = original_embed(items, **kwargs)
        if kwargs.get("enable_fusion"):
            assert any("text" in item for item in items)
            assert any("image" in item for item in items)
            assert len(result) == 1
            with fusion_count_lock:
                fused_chunks += 1
        return result

    container.bailian.embed_multimodal = observed_embed
    print("TEST_RESOURCES " + json.dumps({
        "mongodb": settings.mongodb_database,
        "zilliz_collection": settings.zilliz_collection,
        "minio_bucket": settings.minio_bucket_name,
    }))
    with TestClient(create_app(settings=settings, container=container)) as client:
        container = client.app.state.container
        try:
            container.zilliz.ensure_collection()
            ready = client.get("/internal/v1/health/ready", headers=headers)
            assert ready.status_code == 200, ready.text
            dependencies = ready.json()["dependencies"]
            for dependency in ("mongodb", "minio", "zilliz"):
                assert dependencies[dependency] == "up", dependencies

            with document_path.open("rb") as source:
                uploaded = client.post(
                    "/internal/v1/documents/ingestions",
                    headers={**headers, "Idempotency-Key": f"flow-upload-{run_id}"},
                    data={"metadata": f'{{"source":"full-flow","run_id":"{run_id}"}}'},
                    files={
                        "file": (
                            document_path.name,
                            source,
                            _mime_type(document_path),
                        )
                    },
                )
            assert uploaded.status_code == 202, uploaded.text
            document_id = uploaded.json()["document_id"]
            object_key = container.storage.object_name(
                owner_id=owner_id,
                document_id=document_id,
                extension=document_path.suffix,
            )
            stat = container.storage.client.stat_object(
                container.storage.bucket_name, object_key
            )
            assert stat.size == document_path.stat().st_size

            document = _wait_for_terminal_state(
                client, document_id, headers, timeout
            )
            assert document["status"] == "indexed", document
            assert document["stage"] == "completed"
            assert document["chunk_count"] > 0
            if document_path.name == "AttentionIsAllYouNeed.pdf":
                assert fused_chunks > 0, "默认含图 PDF 必须经过真实图文融合调用"
            expression = container.zilliz._filter(owner_id, [document_id])
            vector_rows = container.zilliz.client.query(
                collection_name=settings.zilliz_collection,
                filter=expression, output_fields=["chunk_id"],
                consistency_level="Strong", limit=16384,
            )
            assert len(vector_rows) == document["chunk_count"]
            print(f"INDEXED chunks={document['chunk_count']} fused_chunks={fused_chunks}")
            _wait_for_temp_cleanup(document_id)

            conversation = client.post(
                "/internal/v1/conversations",
                headers=headers,
                json={"title": f"Full flow {run_id}"},
            )
            assert conversation.status_code == 201, conversation.text
            conversation_id = conversation.json()["id"]

            question = "这篇文档的核心内容是什么？"
            request_body = {
                "owner_id": owner_id,
                "conversation_id": conversation_id,
                "message": question,
                "document_ids": [document_id],
                "request_id": f"flow-chat-{run_id}",
                "stream": False,
            }
            completion = client.post(
                "/internal/v1/rag/completions", headers=headers, json=request_body
            )
            assert completion.status_code == 200, completion.text
            answer = completion.json()
            assert answer["message"]["content"]
            assert answer["message"]["citations"]
            assert all(
                item["document_id"] == document_id
                for item in answer["message"]["citations"]
            )

            streamed = client.post(
                "/internal/v1/rag/completions",
                headers=headers,
                json={
                    **request_body,
                    "request_id": f"flow-sse-{run_id}",
                    "stream": True,
                },
            )
            assert streamed.status_code == 200, streamed.text
            for event in ("start", "delta", "citations", "done"):
                assert f"event: {event}" in streamed.text
            assert "event: error" not in streamed.text
            messages = container.repository.messages.find({"conversation_id": conversation_id})
            persisted = list(messages)
            assert len(persisted) == 4
            assert sum(item["role"] == "assistant" for item in persisted) == 2
            print(f"RAG_OK citations={len(answer['message']['citations'])} messages={len(persisted)}")

            deleted = client.delete(
                f"/internal/v1/documents/{document_id}", headers=headers
            )
            assert deleted.status_code == 204, deleted.text
            with pytest.raises(S3Error) as missing_object:
                container.storage.client.stat_object(
                    container.storage.bucket_name, object_key
                )
            assert missing_object.value.code in {"NoSuchKey", "NoSuchObject"}
            missing_document = client.get(
                f"/internal/v1/documents/{document_id}", headers=headers
            )
            assert missing_document.status_code == 404
            assert container.repository.documents.count_documents({"_id": document_id}) == 0
            assert not container.zilliz.client.query(
                collection_name=settings.zilliz_collection,
                filter=expression, output_fields=["chunk_id"],
                consistency_level="Strong", limit=1,
            )
            _wait_for_temp_cleanup(document_id)
            document_id = None

            deleted_conversation = client.delete(
                f"/internal/v1/conversations/{conversation_id}", headers=headers
            )
            assert deleted_conversation.status_code == 204
            assert container.repository.messages.count_documents({"conversation_id": conversation_id}) == 0
            conversation_id = None
        finally:
            if document_id:
                client.delete(f"/internal/v1/documents/{document_id}", headers=headers)
            if conversation_id:
                client.delete(
                    f"/internal/v1/conversations/{conversation_id}", headers=headers
                )
            if object_key:
                try:
                    container.storage.delete(object_key)
                except Exception:
                    pass

    local_files_after = set(local_upload_root.rglob("*")) if local_upload_root.exists() else set()
    assert local_files_after == local_files_before, "全流程测试不应生成本地上传文件"
    print("REAL_FULL_FLOW_PASS cleanup=verified resources=retained")
