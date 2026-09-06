"""Internal administration API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.server.api.security import require_internal_token


router = APIRouter(
    prefix="/internal/v1/admin", tags=["admin"],
    dependencies=[Depends(require_internal_token)],
)


@router.get("/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    return request.app.state.container.admin.metrics()


@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    return request.app.state.container.admin.config()


@router.put("/config")
async def update_config(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    return request.app.state.container.admin.update_config(payload)


__all__ = ["router"]
