from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.server.core import Settings
from app.server.domain import RagCompletionRequest
from app.server.providers.bailian import (
    ChatResult,
    ChatStreamChunk,
    WebCitation,
    _web_citations,
)
from app.server.providers.zilliz import SearchHit
from app.server.repositories import MemoryRepository
from app.server.services.rag import RagService, weighted_rrf


def hit(name: str) -> SearchHit:
    return SearchHit(
        chunk_id=name,
        document_id="doc",
        owner_id="owner",
        content=name,
        score=1.0,
    )


def test_weighted_rrf_deduplicates_and_honors_branch_weights():
    shared = hit("shared")
    ranked = weighted_rrf(
        [[shared, hit("dense")], [shared, hit("sparse")]],
        [1.0, 2.0],
        k=60,
        limit=3,
    )
    assert [item.chunk_id for item in ranked] == ["shared", "sparse", "dense"]
    assert len([item for item in ranked if item.chunk_id == "shared"]) == 1


def test_responses_web_sources_are_deduplicated_and_restricted_to_http():
    citations = _web_citations([
        {
            "type": "text",
            "text": "回答",
            "annotations": [
                {
                    "type": "url_citation",
                    "url": "https://example.test/report",
                    "title": "报告",
                },
                {"type": "url_citation", "url": "javascript:alert(1)"},
            ],
        },
        {
            "type": "web_search_call",
            "action": {
                "sources": [
                    {"url": "https://example.test/report"},
                    {"url": "http://news.example.test/a"},
                    {"url": "https://user:secret@example.test/private"},
                ]
            },
        },
    ])
    assert citations == (
        WebCitation(title="报告", url="https://example.test/report"),
        WebCitation(title="news.example.test", url="http://news.example.test/a"),
    )


class ResponseModels:
    def __init__(self):
        self.chat_calls: list[bool] = []
        self.stream_calls: list[bool] = []

    def embed_multimodal(self, items, **_kwargs):
        return [[1.0, 0.0] for _ in items]

    def embed_sparse_texts(self, texts, **_kwargs):
        return [{1: 1.0} for _ in texts]

    def rerank(self, *_args, **_kwargs):
        raise AssertionError("空召回不应调用 rerank")

    def chat(self, _messages, *, web_search_enabled=False):
        self.chat_calls.append(web_search_enabled)
        return ChatResult(
            content="联网回答" if web_search_enabled else "知识库回答",
            usage={"total_tokens": 2},
            web_citations=(
                WebCitation("网络结果", "https://example.test/a"),
            ) if web_search_enabled else (),
        )

    def stream_chat(self, _messages, *, web_search_enabled=False):
        self.stream_calls.append(web_search_enabled)
        yield ChatStreamChunk(content="流式联网回答")
        if web_search_enabled:
            yield ChatStreamChunk(
                content="",
                usage={"total_tokens": 3},
                web_citations=(WebCitation("实时来源", "https://example.test/live"),),
            )


async def _service_fixture() -> tuple[RagService, MemoryRepository, ResponseModels]:
    repository = MemoryRepository()
    await repository.create_conversation({
        "id": "conv", "owner_id": "owner", "title": "测试会话",
    })
    await repository.create_document({
        "id": "doc", "owner_id": "owner", "name": "文档",
        "status": "indexed",
    })
    models = ResponseModels()
    service = RagService(
        repository=repository,
        bailian=models,
        zilliz=SimpleNamespace(search=lambda *_args, **_kwargs: []),
        settings=Settings(),
    )
    return service, repository, models


def test_responses_web_tool_is_opt_in_and_citations_are_persisted():
    async def run():
        service, repository, models = await _service_fixture()
        ordinary = RagCompletionRequest(
            owner_id="owner", conversation_id="conv", message="普通问题",
            request_id="req-ordinary",
        )
        web = RagCompletionRequest(
            owner_id="owner", conversation_id="conv", message="最新问题",
            request_id="req-web", web_search_enabled=True,
        )

        ordinary_message, _ = await service.complete(await service.prepare(ordinary))
        web_message, _ = await service.complete(await service.prepare(web))

        assert models.chat_calls == [False, True]
        assert ordinary_message.citations == []
        assert web_message.citations[0].source_type == "web"
        assert web_message.citations[0].url == "https://example.test/a"
        persisted = await repository.get_message(web_message.id)
        assert persisted["citations"][0]["url"] == "https://example.test/a"

    asyncio.run(run())


def test_streaming_responses_web_sources_are_emitted_and_replayed():
    async def run():
        service, repository, models = await _service_fixture()
        request = RagCompletionRequest(
            owner_id="owner", conversation_id="conv", message="实时问题",
            request_id="req-stream", web_search_enabled=True,
        )
        first = "".join([event async for event in service.stream(await service.prepare(request))])
        assert models.stream_calls == [True]
        assert "event: delta" in first
        assert '\"source_type\":\"web\"' in first
        assert "https://example.test/live" in first

        cached = await service.prepare(request)
        replay = "".join([event async for event in service.stream(cached)])
        assert models.stream_calls == [True]
        assert "https://example.test/live" in replay
        assert len(await repository.list_messages("conv")) == 2

    asyncio.run(run())
