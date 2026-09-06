"""Bounded in-process telemetry and validated runtime configuration."""

from __future__ import annotations

import threading
import time
from collections import deque
from copy import deepcopy
from typing import Any

from app.server.api.errors import AppError
from app.server.core.ids import to_iso8601


class AdminService:
    def __init__(self, *, settings: Any, bailian: Any, capacity: int = 200) -> None:
        self.settings = settings
        self.bailian = bailian
        self._lock = threading.Lock()
        self._active_ingestions: dict[str, dict[str, Any]] = {}
        self._ingestions: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._rag_runs: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._config = {
            "retrieval_limit": settings.retrieval_limit,
            "rerank_limit": settings.rerank_limit,
            "chat_model": settings.chat_model,
        }

    def config(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._config)

    def value(self, name: str, fallback: Any) -> Any:
        with self._lock:
            return self._config.get(name, fallback)

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        allowed = {"retrieval_limit", "rerank_limit", "chat_model"}
        unknown = set(updates) - allowed - {"owner_id"}
        if unknown:
            raise AppError(400, "INVALID_ARGUMENT", f"不支持的配置项: {', '.join(sorted(unknown))}")
        cleaned: dict[str, Any] = {}
        for name in ("retrieval_limit", "rerank_limit"):
            if name in updates:
                value = updates[name]
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
                    raise AppError(400, "INVALID_ARGUMENT", f"{name} 必须是 1–200 的整数")
                cleaned[name] = value
        if "chat_model" in updates:
            model = str(updates["chat_model"]).strip()
            if not model or len(model) > 200:
                raise AppError(400, "INVALID_ARGUMENT", "chat_model 长度必须为 1–200")
            cleaned["chat_model"] = model
        with self._lock:
            self._config.update(cleaned)
            current = deepcopy(self._config)
        if "chat_model" in cleaned:
            self.bailian.chat_model = cleaned["chat_model"]
            # BailianProvider lazily caches ChatOpenAI for the selected model.
            if hasattr(self.bailian, "_llm"):
                self.bailian._llm = None
        return current

    def start_ingestion(self, document_id: str, *, owner_id: str, name: str) -> None:
        now = time.perf_counter()
        with self._lock:
            self._active_ingestions[document_id] = {
                "document_id": document_id, "owner_id": owner_id, "name": name,
                "started_at": to_iso8601(), "started_clock": now,
                "stage_started": now, "stage": "uploading", "stages_ms": {},
            }

    def stage_ingestion(self, document_id: str, stage: str) -> None:
        now = time.perf_counter()
        with self._lock:
            record = self._active_ingestions.get(document_id)
            if not record or record["stage"] == stage:
                return
            record["stages_ms"][record["stage"]] = round((now - record["stage_started"]) * 1000, 2)
            record["stage"], record["stage_started"] = stage, now

    def finish_ingestion(self, document_id: str, *, status: str, error: str | None = None) -> None:
        now = time.perf_counter()
        with self._lock:
            record = self._active_ingestions.pop(document_id, None)
            if not record:
                return
            record["stages_ms"][record["stage"]] = round((now - record["stage_started"]) * 1000, 2)
            record.update(
                status=status, error=error, finished_at=to_iso8601(),
                total_ms=round((now - record.pop("started_clock")) * 1000, 2),
            )
            record.pop("stage_started", None)
            self._ingestions.appendleft(record)

    def record_rag(
        self, *, request_id: str, model: str, prompt: list[dict[str, str]],
        output: str, usage: dict[str, int], duration_ms: float,
        web_search_enabled: bool,
    ) -> None:
        completion_tokens = int(usage.get("completion_tokens", 0))
        speed = completion_tokens / (duration_ms / 1000) if duration_ms > 0 else 0.0
        record = {
            "request_id": request_id, "created_at": to_iso8601(), "model": model,
            "duration_ms": round(duration_ms, 2), "tokens_per_second": round(speed, 2),
            "usage": deepcopy(usage), "prompt": deepcopy(prompt), "output": output,
            "web_search_enabled": web_search_enabled,
        }
        with self._lock:
            self._rag_runs.appendleft(record)

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            ingestions, rag_runs = deepcopy(list(self._ingestions)), deepcopy(list(self._rag_runs))
            active = len(self._active_ingestions)
        stage_totals: dict[str, list[float]] = {}
        for record in ingestions:
            for stage, duration in record["stages_ms"].items():
                stage_totals.setdefault(stage, []).append(duration)
        return {
            "summary": {
                "active_ingestions": active,
                "completed_ingestions": len(ingestions),
                "rag_requests": len(rag_runs),
                "total_generated_tokens": sum(item["usage"].get("completion_tokens", 0) for item in rag_runs),
                "average_tokens_per_second": round(
                    sum(item["tokens_per_second"] for item in rag_runs) / len(rag_runs), 2,
                ) if rag_runs else 0.0,
                "average_stage_ms": {
                    stage: round(sum(values) / len(values), 2)
                    for stage, values in stage_totals.items()
                },
            },
            "ingestions": ingestions,
            "rag_runs": rag_runs,
        }
