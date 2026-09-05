"""RAG preparation and answer graphs with a shared streaming/non-streaming path."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from ..api.errors import AppError
from ..core import Settings
from ..core.ids import new_message_id, new_request_id, to_iso8601
from ..domain import Citation, Message, RagCompletionRequest, RetrievedImage, Usage
from ..providers import ProviderError
from ..providers.zilliz import SearchHit
from ..repositories import Repository
from ..utils.sse import encode_sse
from .common import _provider_app_error, run_sync, sync_scope


@dataclass(slots=True)
class PreparedRag:
    request: RagCompletionRequest
    message_id: str
    model_messages: list[dict[str, str]]
    citations: list[Citation]
    cached_message: Message | None = None
    cached_usage: Usage | None = None


class RagState(TypedDict, total=False):
    request: RagCompletionRequest
    documents: list[dict[str, Any]]
    dense: list[float]
    sparse: dict[int, float]
    hits: list[SearchHit]
    ranked: list[tuple[SearchHit, float]]
    history: list[dict[str, Any]]
    prepared: PreparedRag


class AnswerState(TypedDict, total=False):
    prepared: PreparedRag
    stream: bool
    content: str
    usage: Usage
    message: Message


def _emit(state: AnswerState, event: str, data: dict) -> None:
    if state["stream"]:
        get_stream_writer()((event, data))


class RagService:
    def __init__(self, *, repository: Repository, bailian: Any,
                 zilliz: Any, settings: Settings) -> None:
        self.repository = repository
        self.bailian = bailian
        self.zilliz = zilliz
        self.settings = settings
        self._request_locks: dict[str, tuple[asyncio.Lock, int]] = {}

        graph = StateGraph(RagState)
        for name in ("validate", "documents", "save_user", "dense_query", "sparse_query",
                     "history", "search", "rerank", "prompt"):
            graph.add_node(name, getattr(self, f"_{name}"))
        graph.add_edge(START, "validate")
        graph.add_conditional_edges(
            "validate", lambda state: "cached" if "prepared" in state else "fresh",
            {"cached": END, "fresh": "documents"},
        )
        graph.add_edge("documents", "save_user")
        for name in ("dense_query", "sparse_query", "history"):
            graph.add_edge("save_user", name)
        graph.add_edge(["dense_query", "sparse_query"], "search")
        graph.add_edge("search", "rerank")
        graph.add_edge(["rerank", "history"], "prompt")
        graph.add_edge("prompt", END)
        self.prepare_graph = graph.compile()

        answer = StateGraph(AnswerState)
        answer.add_node("replay", self._replay)
        answer.add_node("generate", self._generate)
        answer.add_node("save", self._save)
        answer.add_conditional_edges(
            START, lambda state: "replay" if state["prepared"].cached_message is not None else "generate",
            {"replay": "replay", "generate": "generate"},
        )
        answer.add_edge("replay", END)
        answer.add_edge("generate", "save")
        answer.add_edge("save", END)
        self.answer_graph = answer.compile()

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
        try:
            async with sync_scope():
                state = await self.prepare_graph.ainvoke({"request": request})
            return state["prepared"]
        except ProviderError as exc:
            raise _provider_app_error(exc) from exc

    async def _validate(self, state: RagState) -> dict:
        request = state["request"]
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
            return {"request": request, "prepared": PreparedRag(
                request=request,
                message_id=str(existing["id"]),
                model_messages=[],
                citations=[
                    Citation.model_validate(item)
                    for item in existing.get("citations", [])
                ],
                cached_message=Message.model_validate(existing),
                cached_usage=Usage.model_validate(existing.get("usage") or {}),
            )}
        return {"request": request}

    async def _documents(self, state: RagState) -> dict:
        request = state["request"]
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

        return {"documents": documents}

    async def _save_user(self, state: RagState) -> dict:
        request = state["request"]
        await self.repository.create_message(
            {
                "conversation_id": request.conversation_id,
                "role": "user",
                "content": request.message,
                "citations": [],
                "created_at": to_iso8601(),
            }
        )

        return {}

    async def _dense_query(self, state: RagState) -> dict:
        vectors = await run_sync(self.bailian.embed_multimodal, [{"text": state["request"].message}])
        return {"dense": vectors[0]}

    async def _sparse_query(self, state: RagState) -> dict:
        vectors = await run_sync(self.bailian.embed_sparse_texts,
                                 [state["request"].message], text_type="query")
        return {"sparse": vectors[0]}

    async def _history(self, state: RagState) -> dict:
        return {"history": await self.repository.list_messages(state["request"].conversation_id, limit=20)}

    async def _search(self, state: RagState) -> dict:
        hits = await run_sync(
            self.zilliz.search, state["dense"], owner_id=state["request"].owner_id,
            document_ids=[str(document["id"]) for document in state["documents"]],
            sparse_vector=state["sparse"], limit=self.settings.retrieval_limit,
            # RRF needs enough candidates from both vector spaces before reranking.
            # The 30-result final pool gives the reranker coverage across long
            # courseware documents without expanding the final prompt beyond rerank_limit.
            candidate_limit=max(self.settings.retrieval_limit * 2, 60),
        )
        return {"hits": hits}

    async def _rerank(self, state: RagState) -> dict:
        hits = state["hits"]
        if not hits:
            return {"ranked": []}
        rankings = await run_sync(
            self.bailian.rerank, state["request"].message,
            [hit.content for hit in hits], top_n=self.settings.rerank_limit,
        )
        return {"ranked": [(hits[item.index], item.score) for item in rankings]}

    async def _prompt(self, state: RagState) -> dict:
        request, documents = state["request"], state["documents"]
        ranked, history = state["ranked"], state["history"]
        names = {str(document["id"]): str(document["name"]) for document in documents}
        image_keys = {str(document["id"]): document.get("image_keys") or {} for document in documents}
        citations = [
            Citation(
                document_id=hit.document_id,
                document_name=names.get(hit.document_id, "未知文档"),
                chunk_id=hit.chunk_id,
                page=hit.page,
                score=score,
                text=hit.content,
                images=[
                    RetrievedImage(document_id=hit.document_id, chunk_index=hit.chunk_index, image_index=index)
                    for index, _ in enumerate(image_keys.get(hit.document_id, {}).get(str(hit.chunk_index), image_keys.get(hit.document_id, {}).get(hit.chunk_index, [])))
                ],
            )
            for hit, score in ranked
        ]
        context = "\n\n".join(
            f"[来源 {index}: {citation.document_name}]\n{citation.text}"
            for index, citation in enumerate(citations, 1)
        )
        model_messages = [
            {
                "role": "system",
                "content": (
                    "你是知识库问答助手。仅根据给出的参考资料回答；资料不足时明确说明。"
                    "不要编造来源，回答使用用户的语言。"
                    "若资料出现‘第 N 页图片’或‘图片（无文字描述）’，表示原文确有图片；"
                    "不得据此声称原文没有图片。可依据相邻文字解释图片主题；"
                    "若没有图片文字描述，应明确只能还原文字上下文、不能还原图片细节。"
                    "\n\n参考资料：\n"
                    + (context or "（没有检索到相关资料）")
                ),
            }
        ]
        model_messages.extend(
            {"role": str(item["role"]), "content": str(item["content"])}
            for item in history
            if item.get("role") in {"user", "assistant"}
        )
        return {"prepared": PreparedRag(
            request=request,
            message_id=new_message_id(),
            model_messages=model_messages,
            citations=citations,
        )}

    async def complete(self, prepared: PreparedRag) -> tuple[Message, Usage]:
        try:
            async with sync_scope():
                state = await self.answer_graph.ainvoke({"prepared": prepared, "stream": False})
            return state["message"], state["usage"]
        except ProviderError as exc:
            raise _provider_app_error(exc) from exc

    async def _replay(self, state: AnswerState) -> dict:
        prepared = state["prepared"]
        message = prepared.cached_message
        assert message is not None
        usage = prepared.cached_usage or Usage()
        if message.content:
            _emit(state, "delta", {"content": message.content})
        _emit(state, "citations", {"items": [item.model_dump() for item in prepared.citations]})
        _emit(state, "done", {"finish_reason": "stop", "usage": usage.model_dump()})
        return {"message": message, "usage": usage}

    async def _generate(self, state: AnswerState) -> dict:
        prepared = state["prepared"]
        if not state["stream"]:
            result = await run_sync(self.bailian.chat, prepared.model_messages)
            return {"content": result.content, "usage": Usage.model_validate(result.usage)}

        iterator = self.bailian.stream_chat(prepared.model_messages)
        sentinel = object()
        chunks: list[str] = []
        usage = Usage()
        try:
            while True:
                chunk = await run_sync(next, iterator, sentinel)
                if chunk is sentinel:
                    break
                content = chunk if isinstance(chunk, str) else chunk.content
                chunk_usage = None if isinstance(chunk, str) else chunk.usage
                if chunk_usage:
                    usage = Usage.model_validate(chunk_usage)
                if content:
                    chunks.append(content)
                    _emit(state, "delta", {"content": content})
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                await run_sync(close)
        _emit(state, "citations", {"items": [item.model_dump() for item in prepared.citations]})
        return {"content": "".join(chunks), "usage": usage}

    async def _save(self, state: AnswerState) -> dict:
        message, usage = await self._save_answer(
            state["prepared"], content=state["content"], usage=state["usage"],
        )
        _emit(state, "done", {"finish_reason": "stop", "usage": usage.model_dump()})
        return {"message": message, "usage": usage}

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
        yield encode_sse("start", {"request_id": prepared.request.request_id,
                                   "message_id": prepared.message_id})
        try:
            async with aclosing(self.answer_graph.astream(
                {"prepared": prepared, "stream": True}, stream_mode="custom",
            )) as events:
                async for event, data in events:
                    yield encode_sse(event, data)
        except Exception as exc:
            yield encode_sse("error", {
                "code": "UPSTREAM_SERVICE_ERROR",
                "message": exc.message if isinstance(exc, ProviderError) else "模型服务暂时不可用",
                "request_id": prepared.request.request_id,
                "retryable": exc.retryable if isinstance(exc, ProviderError) else True,
            })
