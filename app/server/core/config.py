"""Server configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def _integer(name: str, default: int) -> int:
    value = _optional(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc


def _float(name: str, default: float) -> float:
    value = _optional(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc


def _boolean(name: str, default: bool) -> bool:
    value = _optional(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "KnowledgeAgent Server"
    app_version: str = "0.1.0"
    internal_api_token: str | None = None
    mongodb_uri: str | None = None
    mongodb_database: str = "knowledge_agent"
    bailian_base_url: str | None = None
    bailian_api_key: str | None = None
    chat_model: str = "deepseek-v4-flash"
    embedding_model: str = "qwen3-vl-embedding"
    sparse_embedding_model: str = "qwen3.7-text-embedding"
    embedding_dimension: int = 1024
    rerank_model: str = "qwen3.7-text-rerank"
    mineru_api_key: str | None = None
    mineru_api_base: str = "https://mineru.net/api/v4"
    mineru_model: str = "vlm"
    mineru_poll_interval: float = 5.0
    mineru_max_wait: float = 900.0
    zilliz_uri: str | None = None
    zilliz_token: str | None = None
    zilliz_collection: str = "knowledge_agent_chunks_vl_v1"
    zilliz_api_key: str | None = None
    zilliz_volume_name: str | None = None
    zilliz_cloud_endpoint: str = "https://api.cloud.zilliz.com"
    upload_dir: Path = PROJECT_ROOT / "data/uploads"
    processing_dir: Path = PROJECT_ROOT / "data/processing"
    max_upload_size: int = 200 * 1024 * 1024
    chunk_size: int = 1200
    chunk_overlap: int = 200
    retrieval_limit: int = 20
    rerank_limit: int = 6
    probe_paid_dependencies: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        return cls(
            internal_api_token=_optional("INTERNAL_API_TOKEN"),
            mongodb_uri=_optional("MONGODB_URI"),
            mongodb_database=_optional("MONGODB_DATABASE") or "knowledge_agent",
            bailian_base_url=_optional("BAILIAN_BASE_URL"),
            bailian_api_key=_optional("BAILIAN_API_KEY")
            or _optional("DASHSCOPE_API_KEY"),
            chat_model=_optional("CHAT_MODEL") or "deepseek-v4-flash",
            embedding_model=_optional("EMBEDDING_MODEL")
            or "qwen3-vl-embedding",
            sparse_embedding_model=_optional("SPARSE_EMBEDDING_MODEL")
            or "qwen3.7-text-embedding",
            embedding_dimension=_integer("EMBEDDING_DIMENSION", 1024),
            rerank_model=_optional("RERANK_MODEL") or "qwen3.7-text-rerank",
            mineru_api_key=_optional("MINERU_API_KEY"),
            mineru_api_base=_optional("MINERU_API_BASE")
            or "https://mineru.net/api/v4",
            mineru_model=_optional("MINERU_MODEL") or "vlm",
            mineru_poll_interval=_float("MINERU_POLL_INTERVAL", 5.0),
            mineru_max_wait=_float("MINERU_MAX_WAIT", 900.0),
            zilliz_uri=_optional("ZILLIZ_URI"),
            zilliz_token=_optional("ZILLIZ_TOKEN"),
            zilliz_collection=_optional("ZILLIZ_COLLECTION")
            or "knowledge_agent_chunks_vl_v1",
            zilliz_api_key=_optional("ZILLIZ_API_KEY"),
            zilliz_volume_name=_optional("ZILLIZ_VOLUME_NAME"),
            zilliz_cloud_endpoint=_optional("ZILLIZ_CLOUD_ENDPOINT")
            or "https://api.cloud.zilliz.com",
            upload_dir=Path(
                _optional("UPLOAD_DIR") or PROJECT_ROOT / "data/uploads"
            ).resolve(),
            processing_dir=Path(
                _optional("PROCESSING_DIR") or PROJECT_ROOT / "data/processing"
            ).resolve(),
            max_upload_size=_integer("MAX_UPLOAD_SIZE", 200 * 1024 * 1024),
            chunk_size=_integer("CHUNK_SIZE", 1200),
            chunk_overlap=_integer("CHUNK_OVERLAP", 200),
            retrieval_limit=_integer("RETRIEVAL_LIMIT", 20),
            rerank_limit=_integer("RERANK_LIMIT", 6),
            probe_paid_dependencies=_boolean("PROBE_PAID_DEPENDENCIES", False),
        )

    def validate(self) -> None:
        if self.max_upload_size <= 0:
            raise ValueError("MAX_UPLOAD_SIZE 必须大于 0")
        if self.embedding_dimension <= 0:
            raise ValueError("EMBEDDING_DIMENSION 必须大于 0")
        if self.chunk_size <= 0:
            raise ValueError("CHUNK_SIZE 必须大于 0")
        if self.chunk_size > 60_000:
            raise ValueError("CHUNK_SIZE 不能超过 60000")
        if self.chunk_overlap < 0 or self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP 必须大于等于 0 且小于 CHUNK_SIZE")
