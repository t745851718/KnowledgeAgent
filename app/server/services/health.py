"""Readiness probes (not a business workflow graph)."""

import asyncio
from typing import Any

from ..core import Settings
from ..repositories import Repository


class HealthService:
    def __init__(
        self,
        *,
        repository: Repository,
        storage: Any,
        zilliz: Any,
        settings: Settings,
        using_memory_repository: bool,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.zilliz = zilliz
        self.settings = settings
        self.using_memory_repository = using_memory_repository

    async def check(self) -> tuple[int, dict[str, Any]]:
        dependencies = {
            "mongodb": "down" if self.using_memory_repository else "up",
            "minio": "up",
            "zilliz": "up",
            "bailian": "not_checked",
            "mineru": "not_checked",
        }
        if not self.using_memory_repository:
            try:
                if not await self.repository.ping():
                    dependencies["mongodb"] = "down"
            except Exception:
                dependencies["mongodb"] = "down"
        try:
            if not await asyncio.to_thread(self.storage.ping):
                dependencies["minio"] = "down"
        except Exception:
            dependencies["minio"] = "down"
        try:
            if not await asyncio.to_thread(self.zilliz.ping):
                dependencies["zilliz"] = "down"
        except Exception:
            dependencies["zilliz"] = "down"
        ready = all(
            dependencies[name] == "up" for name in ("mongodb", "minio", "zilliz")
        )
        return (
            200 if ready else 503,
            {"status": "ready" if ready else "not_ready", "dependencies": dependencies},
        )
