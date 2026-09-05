"""Server-Sent Events encoding helpers."""

from __future__ import annotations

import json
from typing import Any


def encode_sse(event: str, data: dict[str, Any]) -> str:
    """Encode one SSE event with compact, single-line UTF-8 JSON data."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


__all__ = ["encode_sse"]
