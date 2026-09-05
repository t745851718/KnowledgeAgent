"""KnowledgeAgent FastAPI application entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .core import Settings
from .container import Container, build_container
from .api import install_exception_handlers, router
from .api.security import request_id_from_header


def create_app(
    *, settings: Settings | None = None, container: Container | None = None
) -> FastAPI:
    configured_settings = settings or Settings.from_env()
    dependencies = container or build_container(configured_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        await dependencies.initialize()
        application.state.container = dependencies
        try:
            yield
        finally:
            await dependencies.close()

    application = FastAPI(
        title=configured_settings.app_name,
        version=configured_settings.app_version,
        description="KnowledgeAgent 文档入库和 RAG 内部服务",
        lifespan=lifespan,
    )
    application.state.settings = configured_settings
    application.state.container = dependencies
    application.state.logger = logging.getLogger("knowledgeagent.server")

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request_id_from_header(request.headers.get("X-Request-Id"))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    install_exception_handlers(application)
    application.include_router(router)
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.server.main:app", host="127.0.0.1", port=8000, reload=True)
