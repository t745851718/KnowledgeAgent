"""Milvus/Zilliz vector-store adapter."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pymilvus import AnnSearchRequest, DataType, MilvusClient, RRFRanker

from . import ProviderError


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: str
    document_id: str
    owner_id: str
    content: str
    score: float
    page: int | None = None
    section: str | None = None
    chunk_index: int = 0
    source_url: str | None = None
    source_title: str | None = None
    source_type: str = "knowledge"


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class ZillizProvider:
    def __init__(
        self,
        *,
        uri: str | None = None,
        token: str | None = None,
        collection_name: str | None = None,
        dimension: int = 1024,
        enable_sparse: bool = True,
        client: MilvusClient | None = None,
    ) -> None:
        self.uri = uri or os.getenv("ZILLIZ_URI")
        self.token = token or os.getenv("ZILLIZ_TOKEN")
        self.collection_name = (
            collection_name
            or os.getenv("ZILLIZ_COLLECTION")
            or "knowledge_agent_chunks_vl_v1"
        )
        self.dimension = dimension
        self.enable_sparse = enable_sparse
        self._client = client
        self._collection_lock = threading.Lock()
        self._collection_ready = False
        self._document_locks_guard = threading.Lock()
        self._document_locks: dict[str, threading.Lock] = {}

    def _document_lock(self, document_id: str) -> threading.Lock:
        with self._document_locks_guard:
            return self._document_locks.setdefault(document_id, threading.Lock())

    @property
    def client(self) -> MilvusClient:
        if self._client is None:
            if not self.uri:
                raise ProviderError("zilliz", "缺少 ZILLIZ_URI")
            try:
                self._client = MilvusClient(uri=self.uri, token=self.token or "")
            except Exception as exc:
                raise ProviderError("zilliz", f"创建客户端失败: {exc}", retryable=True) from exc
        return self._client

    def ensure_collection(self) -> None:
        if self._collection_ready:
            return
        with self._collection_lock:
            if self._collection_ready:
                return
            self._ensure_collection_unlocked()
            self._collection_ready = True

    def _ensure_collection_unlocked(self) -> None:
        try:
            if self.client.has_collection(self.collection_name):
                description = self.client.describe_collection(self.collection_name)
                fields = {field["name"]: field for field in description.get("fields", [])}
                dense = fields.get("dense_vector", {})
                existing_dim = int((dense.get("params") or {}).get("dim", self.dimension))
                if existing_dim != self.dimension:
                    raise ProviderError("zilliz", f"Collection 向量维度为 {existing_dim}，配置为 {self.dimension}")
                if self.enable_sparse and "sparse_vector" not in fields:
                    raise ProviderError("zilliz", "Collection 缺少 sparse_vector 字段")
                return

            schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
            schema.add_field("document_id", DataType.VARCHAR, max_length=128)
            schema.add_field("owner_id", DataType.VARCHAR, max_length=128)
            schema.add_field("content", DataType.VARCHAR, max_length=65535)
            schema.add_field("page", DataType.INT64, nullable=True)
            schema.add_field("section", DataType.VARCHAR, max_length=1024, nullable=True)
            schema.add_field("chunk_index", DataType.INT64)
            schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=self.dimension)
            if self.enable_sparse:
                schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)

            indexes = MilvusClient.prepare_index_params()
            indexes.add_index("dense_vector", index_type="AUTOINDEX", metric_type="COSINE")
            if self.enable_sparse:
                indexes.add_index("sparse_vector", index_type="AUTOINDEX", metric_type="IP")
            self.client.create_collection(self.collection_name, schema=schema, index_params=indexes)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("zilliz", f"初始化 Collection 失败: {exc}", retryable=True) from exc

    def insert_chunks(self, chunks: Sequence[Mapping[str, Any]]) -> int:
        if not chunks:
            return 0
        rows: list[dict[str, Any]] = []
        for chunk in chunks:
            dense = list(chunk.get("dense_vector") or [])
            if len(dense) != self.dimension:
                raise ProviderError("zilliz", f"dense_vector 维度必须为 {self.dimension}")
            owner_id = str(chunk["owner_id"])
            content = str(chunk["content"])
            if _utf8_size(owner_id) > 128:
                raise ProviderError("zilliz", "owner_id 的 UTF-8 编码不能超过 128 字节")
            if _utf8_size(content) > 65535:
                raise ProviderError("zilliz", "chunk 内容的 UTF-8 编码不能超过 65535 字节")
            row = {
                "chunk_id": str(chunk["chunk_id"]),
                "document_id": str(chunk["document_id"]),
                "owner_id": owner_id,
                "content": content,
                "page": chunk.get("page"),
                "section": (
                    _truncate_utf8(str(chunk["section"]), 1024)
                    if chunk.get("section") is not None
                    else None
                ),
                "chunk_index": int(chunk.get("chunk_index", 0)),
                "dense_vector": dense,
            }
            if self.enable_sparse:
                sparse = chunk.get("sparse_vector")
                if not sparse:
                    raise ProviderError("zilliz", "启用稀疏检索时每个 chunk 都必须包含 sparse_vector")
                row["sparse_vector"] = {int(key): float(value) for key, value in dict(sparse).items()}
            rows.append(row)
        try:
            document_ids = {row["document_id"] for row in rows}
            if len(document_ids) != 1:
                raise ProviderError(
                    "zilliz", "一次 insert_chunks 只能写入一个文档"
                )
            with self._document_lock(next(iter(document_ids))):
                result = self.client.insert(self.collection_name, rows)
            return int(result.get("insert_count", len(rows)))
        except Exception as exc:
            raise ProviderError("zilliz", f"写入向量失败: {exc}", retryable=True) from exc

    @staticmethod
    def _filter(owner_id: str, document_ids: Sequence[str] | None) -> str:
        expression = f"owner_id == {_literal(owner_id)}"
        if document_ids:
            ids = ", ".join(_literal(value) for value in document_ids)
            expression += f" and document_id in [{ids}]"
        return expression

    @staticmethod
    def _hits(result: list[list[dict[str, Any]]]) -> list[SearchHit]:
        hits = result[0] if result else []
        normalized: list[SearchHit] = []
        for hit in hits:
            entity = hit.get("entity") or {}
            normalized.append(
                SearchHit(
                    chunk_id=str(entity.get("chunk_id") or hit.get("id") or ""),
                    document_id=str(entity.get("document_id") or ""),
                    owner_id=str(entity.get("owner_id") or ""),
                    content=str(entity.get("content") or ""),
                    score=float(hit.get("distance", hit.get("score", 0))),
                    page=entity.get("page"),
                    section=entity.get("section"),
                    chunk_index=int(entity.get("chunk_index", 0)),
                )
            )
        return normalized

    def search(
        self,
        dense_vector: Sequence[float],
        *,
        owner_id: str,
        document_ids: Sequence[str] | None = None,
        sparse_vector: Mapping[int, float] | None = None,
        limit: int = 10,
        candidate_limit: int = 30,
    ) -> list[SearchHit]:
        if len(dense_vector) != self.dimension:
            raise ProviderError("zilliz", f"查询向量维度必须为 {self.dimension}")
        expression = self._filter(owner_id, document_ids)
        output_fields = ["chunk_id", "document_id", "owner_id", "content", "page", "section", "chunk_index"]
        try:
            if self.enable_sparse and sparse_vector:
                requests = [
                    AnnSearchRequest(
                        data=[list(dense_vector)],
                        anns_field="dense_vector",
                        param={"metric_type": "COSINE"},
                        limit=candidate_limit,
                        filter=expression,
                    ),
                    AnnSearchRequest(
                        data=[{int(key): float(value) for key, value in sparse_vector.items()}],
                        anns_field="sparse_vector",
                        param={"metric_type": "IP"},
                        limit=candidate_limit,
                        filter=expression,
                    ),
                ]
                result = self.client.hybrid_search(
                    self.collection_name, requests, RRFRanker(), limit=limit, output_fields=output_fields
                )
            else:
                result = self.client.search(
                    self.collection_name,
                    data=[list(dense_vector)],
                    anns_field="dense_vector",
                    filter=expression,
                    limit=limit,
                    output_fields=output_fields,
                    search_params={"metric_type": "COSINE"},
                )
            return self._hits(result)
        except Exception as exc:
            raise ProviderError("zilliz", f"向量检索失败: {exc}", retryable=True) from exc

    def search_dense(self, dense_vector: Sequence[float], *, owner_id: str,
                     document_ids: Sequence[str] | None = None,
                     limit: int = 30) -> list[SearchHit]:
        if len(dense_vector) != self.dimension:
            raise ProviderError("zilliz", f"查询向量维度必须为 {self.dimension}")
        try:
            result = self.client.search(
                self.collection_name, data=[list(dense_vector)], anns_field="dense_vector",
                filter=self._filter(owner_id, document_ids), limit=limit,
                output_fields=["chunk_id", "document_id", "owner_id", "content", "page", "section", "chunk_index"],
                search_params={"metric_type": "COSINE"},
            )
            return self._hits(result)
        except Exception as exc:
            raise ProviderError("zilliz", f"dense 检索失败: {exc}", retryable=True) from exc

    def search_sparse(self, sparse_vector: Mapping[int, float], *, owner_id: str,
                      document_ids: Sequence[str] | None = None,
                      limit: int = 30) -> list[SearchHit]:
        try:
            result = self.client.search(
                self.collection_name,
                data=[{int(key): float(value) for key, value in sparse_vector.items()}],
                anns_field="sparse_vector", filter=self._filter(owner_id, document_ids),
                limit=limit, search_params={"metric_type": "IP"},
                output_fields=["chunk_id", "document_id", "owner_id", "content", "page", "section", "chunk_index"],
            )
            return self._hits(result)
        except Exception as exc:
            raise ProviderError("zilliz", f"sparse 检索失败: {exc}", retryable=True) from exc

    def delete_document(self, *, owner_id: str, document_id: str) -> int:
        expression = f"owner_id == {_literal(owner_id)} and document_id == {_literal(document_id)}"
        try:
            with self._document_lock(document_id):
                if not self.client.has_collection(self.collection_name):
                    return 0
                result = self.client.delete(self.collection_name, filter=expression)
            return int(result.get("delete_count", 0))
        except Exception as exc:
            raise ProviderError("zilliz", f"删除文档向量失败: {exc}", retryable=True) from exc

    def ping(self) -> bool:
        try:
            self.client.list_collections()
            return True
        except Exception as exc:
            raise ProviderError("zilliz", f"连接检查失败: {exc}", retryable=True) from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


__all__ = ["SearchHit", "ZillizProvider"]
