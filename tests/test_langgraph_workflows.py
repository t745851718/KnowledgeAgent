"""Behavioral regressions for parallel execution, streaming, and graph failure cleanup."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.server.core import Settings
from app.server.domain import RagCompletionRequest
from app.server.providers import ProviderError
from app.server.providers.bailian import ChatResult
from app.server.providers.zilliz import SearchHit
from app.server.repositories import MemoryRepository
from app.server.services import IngestionService, RagService, TaskSupervisor
from app.server.services.ingestion import _embed_dense_chunks
from app.server.utils.text_splitter import TextChunk


class Models:
    def __init__(self):
        self.calls = []

    def embed_multimodal(self, items, **kwargs):
        self.calls.append("dense")
        return [[1.0, 0.0] for _ in items]

    def embed_sparse_texts(self, texts, **kwargs):
        self.calls.append("sparse")
        return [{1: 1.0} for _ in texts]

    def rerank(self, *args, **kwargs):
        raise AssertionError("No hits must skip reranking")

    def chat(self, messages):
        self.calls.append("chat")
        assert messages[-1]["content"] == "问题"
        return ChatResult("答案", {"total_tokens": 2})


def test_fusion_concurrency_is_bounded_and_output_order_is_preserved():
    barrier = threading.Barrier(4, timeout=2)
    lock = threading.Lock()
    active = peak = 0

    def embed(items, *, enable_fusion):
        nonlocal active, peak
        assert enable_fusion
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        with lock:
            active -= 1
        return [[float(items[0]["text"])]]

    chunks = [TextChunk(str(i), i, image_paths=(Path(f"{i}.png"),)) for i in range(8)]
    vectors = _embed_dense_chunks(SimpleNamespace(embed_multimodal=embed), chunks)
    assert peak == 4
    assert vectors == [[float(i)] for i in range(8)]


async def rag_fixture(models=None, repository=None):
    repository = repository or MemoryRepository()
    await repository.create_conversation({"id": "conv_graph", "owner_id": "owner"})
    await repository.create_document({"id": "doc_graph", "owner_id": "owner", "name": "说明", "status": "indexed"})
    models = models or Models()
    service = RagService(repository=repository, bailian=models,
                         zilliz=SimpleNamespace(search=lambda *a, **kw: []), settings=Settings())
    request = RagCompletionRequest(owner_id="owner", conversation_id="conv_graph",
                                   message="问题", request_id="req_graph")
    return service, repository, models, request


def test_query_embeddings_and_history_run_in_parallel():
    history_started = threading.Event()
    embedding_barrier = threading.Barrier(2, timeout=2)

    class Repository(MemoryRepository):
        async def list_messages(self, *args, **kwargs):
            history_started.set()
            return await super().list_messages(*args, **kwargs)

    class ParallelModels(Models):
        def embed_multimodal(self, *args, **kwargs):
            assert history_started.wait(2), "history was not scheduled concurrently"
            embedding_barrier.wait()
            return super().embed_multimodal(*args, **kwargs)

        def embed_sparse_texts(self, *args, **kwargs):
            embedding_barrier.wait()
            return super().embed_sparse_texts(*args, **kwargs)

    async def run():
        service, _, _, request = await rag_fixture(ParallelModels(), Repository())
        prepared = await service.prepare(request)
        assert prepared.model_messages[-1]["content"] == "问题"
        assert prepared.citations == []

    asyncio.run(run())


def test_long_documents_use_a_larger_hybrid_candidate_pool_and_preserve_image_context():
    class SearchRecorder:
        def __init__(self):
            self.kwargs = None

        def search(self, *_args, **kwargs):
            self.kwargs = kwargs
            return []

    async def run():
        service, _, _, request = await rag_fixture()
        recorder = SearchRecorder()
        service.zilliz = recorder
        await service.prepare(request)
        assert recorder.kwargs["limit"] == service.settings.retrieval_limit
        assert recorder.kwargs["candidate_limit"] == 60

        prepared = await service._prompt({
            "request": request,
            "documents": [{"id": "doc_graph", "name": "说明"}],
            "ranked": [(
                SearchHit(
                    chunk_id="doc_graph_0", document_id="doc_graph", owner_id="owner",
                    content="第 1 页图片（无文字描述）", score=0.9, page=1,
                ),
                0.9,
            )],
            "history": [],
        })
        prompt = prepared["prepared"].model_messages[0]["content"]
        assert "不得据此声称原文没有图片" in prompt
        assert "第 1 页图片（无文字描述）" in prompt

    asyncio.run(run())


def test_cached_answer_skips_models_and_replays_sse_without_duplicate_messages():
    async def run():
        service, repository, models, request = await rag_fixture()
        message, usage = await service.complete(await service.prepare(request))
        calls = list(models.calls)
        cached = await service.prepare(request)
        events = "".join([event async for event in service.stream(cached)])
        assert all(f"event: {name}" in events for name in ("start", "delta", "citations", "done"))
        assert message.id == cached.message_id
        assert cached.cached_usage == usage
        assert models.calls == calls
        messages = await repository.list_messages(request.conversation_id)
        assert [item["role"] for item in messages] == ["user", "assistant"]

    asyncio.run(run())


def test_stream_failure_emits_error_closes_iterator_and_does_not_save_answer():
    closed = threading.Event()

    class FailedStream(Models):
        def stream_chat(self, messages):
            try:
                yield "部分回答"
                raise ProviderError("bailian", "模拟上游失败", retryable=True)
            finally:
                closed.set()

    async def run():
        service, repository, _, request = await rag_fixture(FailedStream())
        events = "".join([event async for event in service.stream(await service.prepare(request))])
        assert "event: delta" in events and "event: error" in events
        assert "event: done" not in events
        assert closed.is_set()
        assert len(await repository.list_messages(request.conversation_id)) == 1

    asyncio.run(run())


def test_stream_delta_is_immediate_and_disconnect_cancels_before_persistence():
    release = threading.Event()
    waiting = threading.Event()
    closed = threading.Event()

    class SlowStream(Models):
        def stream_chat(self, messages):
            try:
                yield "第一段"
                waiting.set()
                assert release.wait(3)
                yield "第二段"
            finally:
                closed.set()

    async def run():
        service, repository, _, request = await rag_fixture(SlowStream())
        stream = service.stream(await service.prepare(request))
        assert "event: start" in await anext(stream)
        assert "event: delta" in await asyncio.wait_for(anext(stream), timeout=2)
        assert not release.is_set()
        assert await asyncio.to_thread(waiting.wait, 2)
        close = asyncio.create_task(stream.aclose())
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(close, timeout=2)
        assert closed.is_set()
        assert len(await repository.list_messages(request.conversation_id)) == 1

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_parallel_failure_waits_for_sdk_before_removing_source_files():
    started, failed, release = threading.Event(), threading.Event(), threading.Event()
    sources = []

    def download(key, source):
        sources.append(source)
        source.write_text("正文", encoding="utf-8")

    class FailingModels(Models):
        def embed_multimodal(self, *args, **kwargs):
            started.set()
            assert release.wait(3)
            assert sources[0].exists(), "graph cleanup ran before the SDK call finished"
            return super().embed_multimodal(*args, **kwargs)

        def embed_sparse_texts(self, *args, **kwargs):
            assert started.wait(2)
            failed.set()
            raise ProviderError("bailian", "稀疏模型失败")

    async def run():
        repository = MemoryRepository()
        document = await repository.create_document({
            "id": "doc_failure", "owner_id": "owner", "status": "queued",
            "storage_path": "documents/owner/doc_failure/source.md",
        })
        service = IngestionService(
            repository=repository, storage=SimpleNamespace(download=download), mineru=None,
            bailian=FailingModels(), settings=Settings(), supervisor=TaskSupervisor(),
            zilliz=SimpleNamespace(ensure_collection=lambda: None),
        )
        process = asyncio.create_task(service._process(document))
        assert await asyncio.to_thread(failed.wait, 2)
        await asyncio.sleep(0.05)
        assert not process.done()
        release.set()
        await asyncio.wait_for(process, timeout=2)
        result = await repository.get_document(document["id"])
        assert result["status"] == "failed"
        assert result["error"]["message"] == "稀疏模型失败"
        assert not sources[0].parent.exists()

    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize("partial_write", [False, True])
def test_many_batches_and_partial_write_cleanup(monkeypatch, partial_write):
    sizes, deleted, sources = [], [], []
    barrier = threading.Barrier(2, timeout=2)

    class ParallelModels(Models):
        def embed_multimodal(self, *args, **kwargs):
            barrier.wait()
            return super().embed_multimodal(*args, **kwargs)

        def embed_sparse_texts(self, *args, **kwargs):
            barrier.wait()
            return super().embed_sparse_texts(*args, **kwargs)

    def download(key, source):
        sources.append(source)
        source.write_text("正文", encoding="utf-8")

    def insert(rows):
        sizes.append(len(rows))
        return 1 if partial_write else len(rows)

    monkeypatch.setattr("app.server.services.ingestion.uploaded_markdown_chunks", lambda *a, **kw: [
        TextChunk("正文", index) for index in range(521)
    ])

    async def run():
        repository = MemoryRepository()
        document = await repository.create_document({
            "id": "doc_batch", "owner_id": "owner", "status": "queued",
            "storage_path": "documents/owner/doc_batch/source.md",
        })
        service = IngestionService(
            repository=repository, storage=SimpleNamespace(download=download), mineru=None,
            bailian=ParallelModels(), settings=Settings(), supervisor=TaskSupervisor(),
            zilliz=SimpleNamespace(ensure_collection=lambda: None, insert_chunks=insert,
                                   delete_document=lambda **kwargs: deleted.append(kwargs)),
        )
        await service._process(document)
        result = await repository.get_document(document["id"])
        if partial_write:
            assert result["status"] == "failed"
            assert result["error"]["code"] == "DOCUMENT_INDEX_FAILED"
            assert deleted == [{"owner_id": "owner", "document_id": "doc_batch"}]
        else:
            assert result["status"] == "indexed"
            assert result["chunk_count"] == 521
            assert sizes == [20] * 26 + [1]
        assert all(not path.parent.exists() for path in sources)

    asyncio.run(run())
