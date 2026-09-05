"""Shared types for integrations with external services."""

from __future__ import annotations


class ProviderError(RuntimeError):
    """A sanitized error raised by an external-service adapter."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retryable: bool = False,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.retryable = retryable
        self.code = code

    def __str__(self) -> str:
        return f"{self.provider}: {self.message}"


__all__ = ["ProviderError"]
