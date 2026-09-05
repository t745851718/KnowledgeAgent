"""Request identifiers and optional internal API authentication."""

from __future__ import annotations

import hmac

from fastapi import Request

from .errors import AppError
from ..core.ids import new_request_id


def request_id_from_header(value: str | None) -> str:
    if value:
        candidate = value.strip()
        if candidate and len(candidate) <= 128 and all(
            character.isalnum() or character in "-_.:" for character in candidate
        ):
            return candidate
    return new_request_id()


async def require_internal_token(request: Request) -> None:
    expected = request.app.state.settings.internal_api_token
    if not expected:
        return
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(token, expected)
    ):
        raise AppError(401, "UNAUTHORIZED", "内部服务令牌缺失或无效")


__all__ = ["request_id_from_header", "require_internal_token"]
