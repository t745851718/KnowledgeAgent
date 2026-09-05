"""Construction and lifecycle of server dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .core import Settings
from .providers.bailian import BailianProvider
from .providers.mineru import MinerUProvider
from .providers.storage import LocalFileStorage
from .providers.zilliz import ZillizProvider
from .repositories import MemoryRepository, MongoRepository, Repository
from .services import HealthService, IngestionService, RagService, TaskSupervisor


@dataclass(slots=True)
class Container:
    settings: Settings
    repository: Repository
    storage: Any
    mineru: Any
    bailian: Any
    zilliz: Any
    supervisor: TaskSupervisor
    ingestion: IngestionService
    rag: RagService
    health: HealthService

    async def initialize(self) -> None:
        self.settings.upload_dir.mkdir(parents=True, exist_ok=True)
        self.settings.processing_dir.mkdir(parents=True, exist_ok=True)
        await self.repository.initialize()

    async def close(self) -> None:
        await self.supervisor.shutdown()
        await self.repository.close()
        close = getattr(self.zilliz, "close", None)
        if callable(close):
            await asyncio.to_thread(close)


def build_container(settings: Settings) -> Container:
    settings.validate()
    if settings.mongodb_uri:
        repository: Repository = MongoRepository(
            settings.mongodb_uri, settings.mongodb_database
        )
        using_memory_repository = False
    else:
        repository = MemoryRepository()
        using_memory_repository = True

    storage = LocalFileStorage(
        root=settings.upload_dir,
        volume_cloud_endpoint=settings.zilliz_cloud_endpoint,
        volume_api_key=settings.zilliz_api_key,
        volume_name=settings.zilliz_volume_name,
    )
    mineru = MinerUProvider(
        api_key=settings.mineru_api_key,
        api_base_url=settings.mineru_api_base,
        output_root=settings.processing_dir,
        model_version=settings.mineru_model,
        poll_interval=settings.mineru_poll_interval,
        max_wait=settings.mineru_max_wait,
    )
    bailian = BailianProvider(
        api_key=settings.bailian_api_key,
        base_url=settings.bailian_base_url,
        text_embedding_model=settings.sparse_embedding_model,
        vl_embedding_model=settings.embedding_model,
        rerank_model=settings.rerank_model,
        chat_model=settings.chat_model,
        dimension=settings.embedding_dimension,
    )
    zilliz = ZillizProvider(
        uri=settings.zilliz_uri,
        token=settings.zilliz_token,
        collection_name=settings.zilliz_collection,
        dimension=settings.embedding_dimension,
        enable_sparse=True,
    )
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
        zilliz=zilliz,
        settings=settings,
        using_memory_repository=using_memory_repository,
    )
    return Container(
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


__all__ = ["Container", "build_container"]
