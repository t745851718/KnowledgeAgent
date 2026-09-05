"""Service lifecycle and error translation shared by graph-backed workflows."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from ..api.errors import AppError
from ..providers import ProviderError


def _provider_app_error(error: ProviderError) -> AppError:
    if error.code == "FILE_TOO_LARGE":
        return AppError(413, "FILE_TOO_LARGE", "文件超过服务端配置的大小限制")
    status_code = (
        503 if error.provider in {"zilliz", "storage"} else 502
    )
    return AppError(
        status_code,
        "UPSTREAM_SERVICE_ERROR",
        error.message,
        details={"provider": error.provider, "provider_code": error.code},
        retryable=error.retryable,
    )


class TaskSupervisor:
    """Own ingestion tasks so exceptions are observed and shutdown is clean."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def start(self, key: str, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks[key] = task

        def finished(completed: asyncio.Task[Any]) -> None:
            self._tasks.pop(key, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)

    async def cancel(self, key: str) -> None:
        task = self._tasks.get(key)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)



_inflight: ContextVar[set[asyncio.Task] | None] = ContextVar("workflow_sdk_calls", default=None)


@asynccontextmanager
async def sync_scope():
    """A graph can stop awaiting a cancelled sibling before its SDK thread exits."""
    tasks: set[asyncio.Task] = set()
    token = _inflight.set(tasks)
    try:
        yield
    finally:
        try:
            while tasks:
                await asyncio.shield(asyncio.gather(*tuple(tasks), return_exceptions=True))
        finally:
            _inflight.reset(token)


async def run_sync(function, *args, **kwargs):
    """Wait for in-flight SDK work before cancellation releases files or locks."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    tasks = _inflight.get()
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except Exception:
            pass
        raise
