"""Synchronous adapters for Bailian embedding, rerank and chat services.

All public calls are deliberately synchronous because the installed DashScope and
LangChain clients expose reliable synchronous APIs. FastAPI handlers should run
them with ``await asyncio.to_thread(...)``.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import dashscope
from dashscope import MultiModalEmbedding, TextEmbedding, TextReRank
from langchain_openai import ChatOpenAI

from . import ProviderError


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    dense: list[float]
    sparse: dict[int, float] | None = None


@dataclass(frozen=True, slots=True)
class RerankResult:
    index: int
    score: float


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    usage: dict[str, int]
    web_citations: tuple["WebCitation", ...] = ()


@dataclass(frozen=True, slots=True)
class ChatStreamChunk:
    content: str
    usage: dict[str, int] | None = None
    web_citations: tuple["WebCitation", ...] = ()


@dataclass(frozen=True, slots=True)
class WebCitation:
    title: str
    url: str


def _native_url(url: str | None) -> str | None:
    if not url:
        return None
    value = url.rstrip("/")
    suffix = "/compatible-mode/v1"
    return value[: -len(suffix)] + "/api/v1" if value.endswith(suffix) else value


def _compatible_url(url: str | None) -> str | None:
    if not url:
        return None
    value = url.rstrip("/")
    suffix = "/api/v1"
    return value[: -len(suffix)] + "/compatible-mode/v1" if value.endswith(suffix) else value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content or "")


def _safe_web_url(value: Any) -> str | None:
    url = str(value or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return url


def _web_citations(content: Any) -> tuple[WebCitation, ...]:
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return ()
    values: list[WebCitation] = []
    seen: set[str] = set()

    def add(url_value: Any, title_value: Any = None) -> None:
        url = _safe_web_url(url_value)
        if not url or url in seen:
            return
        seen.add(url)
        hostname = urlparse(url).hostname or url
        title = str(title_value or hostname).strip() or hostname
        values.append(WebCitation(title=title[:200], url=url))

    for block in content:
        if not isinstance(block, Mapping):
            continue
        for annotation in block.get("annotations") or []:
            if isinstance(annotation, Mapping) and annotation.get("type") == "url_citation":
                add(annotation.get("url"), annotation.get("title"))
        if block.get("type") == "web_search_call":
            action = block.get("action")
            if not isinstance(action, Mapping):
                continue
            for source in action.get("sources") or []:
                if isinstance(source, Mapping):
                    add(source.get("url"), source.get("title"))
                else:
                    add(source)
    return tuple(values)


class BailianProvider:
    """Bailian model facade configured exclusively by arguments/environment."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        native_base_url: str | None = None,
        text_embedding_model: str = "qwen3.7-text-embedding",
        vl_embedding_model: str = "qwen3-vl-embedding",
        rerank_model: str = "qwen3.7-text-rerank",
        chat_model: str = "deepseek-v4-flash",
        dimension: int = 1024,
        timeout: float = 60,
    ) -> None:
        configured_base = base_url or os.getenv("BAILIAN_BASE_URL")
        self.api_key = api_key or os.getenv("BAILIAN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        self.native_base_url = native_base_url or os.getenv("DASHSCOPE_API_HOST") or _native_url(configured_base)
        self.compatible_base_url = _compatible_url(configured_base or self.native_base_url)
        self.text_embedding_model = text_embedding_model
        self.vl_embedding_model = vl_embedding_model
        self.rerank_model = rerank_model
        self.chat_model = chat_model
        self.dimension = dimension
        self.timeout = timeout
        self._llm: ChatOpenAI | None = None

    def _require_api_key(self) -> str:
        if not self.api_key:
            raise ProviderError("bailian", "缺少 BAILIAN_API_KEY 或 DASHSCOPE_API_KEY")
        return self.api_key

    def _native_kwargs(self) -> dict[str, Any]:
        return {"base_address": self.native_base_url} if self.native_base_url else {}

    @staticmethod
    def _check_response(response: Any, operation: str) -> Mapping[str, Any]:
        status = getattr(response, "status_code", None)
        if status is not None and int(status) != HTTPStatus.OK:
            code = str(getattr(response, "code", "") or "")
            message = str(getattr(response, "message", "") or f"{operation} 请求失败")
            raise ProviderError(
                "bailian",
                message,
                retryable=int(status) in {429, 500, 502, 503, 504},
                code=code or None,
            )
        return _mapping(getattr(response, "output", None))

    def embed_texts(
        self,
        texts: Sequence[str],
        *,
        text_type: str = "document",
        include_sparse: bool = True,
        instruct: str | None = None,
    ) -> list[EmbeddingResult]:
        values = [text.strip() for text in texts]
        if not values or any(not text for text in values):
            raise ValueError("texts 不能为空，且不能包含空文本")
        try:
            response = TextEmbedding.call(
                model=self.text_embedding_model,
                input=values,
                api_key=self._require_api_key(),
                text_type=text_type,
                dimension=self.dimension,
                output_type="dense&sparse" if include_sparse else "dense",
                instruct=instruct,
                request_timeout=self.timeout,
                **self._native_kwargs(),
            )
            output = self._check_response(response, "文本向量化")
            items = output.get("embeddings") or _mapping(response).get("data") or []
            results: list[EmbeddingResult] = []
            for item in items:
                item = _mapping(item)
                dense = [float(value) for value in item.get("embedding") or []]
                if len(dense) != self.dimension:
                    raise ProviderError("bailian", f"向量维度异常：期望 {self.dimension}，实际 {len(dense)}")
                sparse_items = item.get("sparse_embedding") or []
                sparse = {
                    int(entry["index"]): float(entry["value"])
                    for entry in sparse_items
                    if isinstance(entry, Mapping) and "index" in entry and "value" in entry
                }
                results.append(EmbeddingResult(dense=dense, sparse=sparse if include_sparse else None))
            if len(results) != len(values):
                raise ProviderError("bailian", f"向量数量异常：期望 {len(values)}，实际 {len(results)}")
            return results
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("bailian", f"文本向量化失败: {exc}", retryable=True) from exc

    def embed_multimodal(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        enable_fusion: bool = False,
        instruct: str | None = None,
    ) -> list[list[float]]:
        if not items:
            raise ValueError("items 不能为空")
        try:
            normalized: list[dict[str, Any]] = []
            for item in items:
                value = dict(item)
                image = value.get("image")
                if isinstance(image, Path):
                    mime_type = mimetypes.guess_type(image.name)[0]
                    if not mime_type or not mime_type.startswith("image/"):
                        raise ProviderError("bailian", "无法识别图片文件类型")
                    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
                    value["image"] = f"data:{mime_type};base64,{encoded}"
                normalized.append(value)
            response = MultiModalEmbedding.call(
                model=self.vl_embedding_model,
                input=normalized,
                api_key=self._require_api_key(),
                dimension=self.dimension,
                output_type="dense",
                enable_fusion=enable_fusion,
                instruct=instruct,
                request_timeout=self.timeout,
                **self._native_kwargs(),
            )
            output = self._check_response(response, "多模态向量化")
            embeddings = output.get("embeddings") or []
            vectors = [[float(value) for value in _mapping(item).get("embedding") or []] for item in embeddings]
            if any(len(vector) != self.dimension for vector in vectors):
                raise ProviderError("bailian", f"多模态向量维度必须为 {self.dimension}")
            expected = 1 if enable_fusion else len(normalized)
            if len(vectors) != expected:
                raise ProviderError("bailian", f"多模态向量数量异常：期望 {expected}，实际 {len(vectors)}")
            return vectors
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("bailian", f"多模态向量化失败: {exc}", retryable=True) from exc

    def embed_sparse_texts(
        self,
        texts: Sequence[str],
        *,
        text_type: str = "document",
        instruct: str | None = None,
    ) -> list[dict[int, float]]:
        values = [text.strip() for text in texts]
        if not values or any(not text for text in values):
            raise ValueError("texts 不能为空，且不能包含空文本")
        try:
            response = TextEmbedding.call(
                model=self.text_embedding_model,
                input=values,
                api_key=self._require_api_key(),
                text_type=text_type,
                dimension=self.dimension,
                output_type="sparse",
                instruct=instruct,
                request_timeout=self.timeout,
                **self._native_kwargs(),
            )
            output = self._check_response(response, "稀疏向量化")
            items = output.get("embeddings") or _mapping(response).get("data") or []
            results = [
                {
                    int(entry["index"]): float(entry["value"])
                    for entry in (_mapping(item).get("sparse_embedding") or [])
                    if isinstance(entry, Mapping)
                    and "index" in entry
                    and "value" in entry
                }
                for item in items
            ]
            if len(results) != len(values) or any(not item for item in results):
                raise ProviderError(
                    "bailian",
                    f"稀疏向量数量异常：期望 {len(values)}，实际 {len(results)}",
                )
            return results
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                "bailian", f"稀疏向量化失败: {exc}", retryable=True
            ) from exc

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int | None = None,
        instruct: str = "Given a query, retrieve relevant passages that answer the query.",
    ) -> list[RerankResult]:
        values = list(documents)
        if not query.strip() or not values:
            raise ValueError("query 和 documents 不能为空")
        try:
            response = TextReRank.call(
                model=self.rerank_model,
                query=query,
                documents=values,
                top_n=min(top_n or len(values), len(values)),
                return_documents=False,
                instruct=instruct,
                api_key=self._require_api_key(),
                request_timeout=self.timeout,
                **self._native_kwargs(),
            )
            output = self._check_response(response, "文本重排")
            return [
                RerankResult(index=int(item["index"]), score=float(item["relevance_score"]))
                for item in output.get("results") or []
            ]
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("bailian", f"文本重排失败: {exc}", retryable=True) from exc

    def _chat_client(self) -> ChatOpenAI:
        if self._llm is None:
            self._llm = ChatOpenAI(
                model=self.chat_model,
                base_url=self.compatible_base_url,
                api_key=self._require_api_key(),
                timeout=self.timeout,
                max_retries=2,
                stream_usage=True,
                use_responses_api=True,
                output_version="responses/v1",
                store=False,
            )
        return self._llm

    def _chat_runnable(self, *, web_search_enabled: bool) -> Any:
        client = self._chat_client()
        return client.bind_tools([{"type": "web_search"}]) if web_search_enabled else client

    def chat(self, messages: Any, *, web_search_enabled: bool = False) -> ChatResult:
        operation = "Responses API 联网生成" if web_search_enabled else "模型生成"
        try:
            response = self._chat_runnable(
                web_search_enabled=web_search_enabled
            ).invoke(messages)
            usage_raw = response.usage_metadata or response.response_metadata.get("token_usage") or {}
            usage = {
                "prompt_tokens": int(usage_raw.get("input_tokens", usage_raw.get("prompt_tokens", 0))),
                "completion_tokens": int(usage_raw.get("output_tokens", usage_raw.get("completion_tokens", 0))),
                "total_tokens": int(usage_raw.get("total_tokens", 0)),
            }
            return ChatResult(
                content=_content_text(response.content),
                usage=usage,
                web_citations=_web_citations(response.content),
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("bailian", f"{operation}失败: {exc}", retryable=True) from exc

    def stream_chat(
        self, messages: Any, *, web_search_enabled: bool = False
    ) -> Iterator[ChatStreamChunk]:
        operation = "Responses API 联网流式生成" if web_search_enabled else "模型流式生成"
        try:
            seen_citations: set[str] = set()
            for chunk in self._chat_runnable(
                web_search_enabled=web_search_enabled
            ).stream(messages):
                content = _content_text(chunk.content)
                citations = tuple(
                    citation
                    for citation in _web_citations(chunk.content)
                    if citation.url not in seen_citations
                )
                seen_citations.update(item.url for item in citations)
                usage_raw = chunk.usage_metadata or {}
                usage = None
                if usage_raw:
                    usage = {
                        "prompt_tokens": int(usage_raw.get("input_tokens", 0)),
                        "completion_tokens": int(usage_raw.get("output_tokens", 0)),
                        "total_tokens": int(usage_raw.get("total_tokens", 0)),
                    }
                if content or usage or citations:
                    yield ChatStreamChunk(
                        content=content,
                        usage=usage,
                        web_citations=citations,
                    )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("bailian", f"{operation}失败: {exc}", retryable=True) from exc


__all__ = [
    "BailianProvider",
    "ChatResult",
    "ChatStreamChunk",
    "EmbeddingResult",
    "RerankResult",
    "WebCitation",
]
