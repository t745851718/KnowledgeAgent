"""Dependency-free, heading-aware Markdown text splitting."""

from __future__ import annotations

import re
from dataclasses import dataclass


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class TextChunk:
    content: str
    chunk_index: int
    section: str | None = None
    page: int | None = None


def _split_windows(text: str, chunk_size: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(start + chunk_size, len(text))
        end = hard_end
        if hard_end < len(text):
            minimum = start + max(chunk_size // 2, overlap + 1)
            candidates = [text.rfind(separator, minimum, hard_end) for separator in ("\n\n", "\n", "。", ". ", " ")]
            split_at = max(candidates)
            if split_at >= minimum:
                end = split_at + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end and text[next_start].isspace():
            next_start += 1
        start = next_start
    return pieces


def split_markdown(markdown: str, *, chunk_size: int = 1200, overlap: int = 200) -> list[TextChunk]:
    """Split Markdown while retaining the nearest heading as chunk metadata."""

    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap 必须大于等于 0 且小于 chunk_size")

    sections: list[tuple[str | None, str]] = []
    heading_stack: list[str] = []
    current_lines: list[str] = []
    current_section: str | None = None

    def flush() -> None:
        value = "\n".join(current_lines).strip()
        if value:
            sections.append((current_section, value))
        current_lines.clear()

    for line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            heading_stack[level - 1 :] = [match.group(2).strip()]
            current_section = " / ".join(heading_stack)
        else:
            current_lines.append(line)
    flush()

    chunks: list[TextChunk] = []
    for section, body in sections:
        prefix = f"{section}\n\n" if section else ""
        available = max(1, chunk_size - len(prefix))
        effective_overlap = min(overlap, available - 1) if available > 1 else 0
        for piece in _split_windows(body, available, effective_overlap):
            content = (prefix + piece).strip()
            chunks.append(TextChunk(content=content, chunk_index=len(chunks), section=section))
    return chunks


def split_markdown_pages(
    pages: list[tuple[int, str]], *, chunk_size: int = 1200, overlap: int = 200
) -> list[TextChunk]:
    """Split page-scoped Markdown and retain one-based source page numbers."""
    chunks: list[TextChunk] = []
    for page, markdown in pages:
        for chunk in split_markdown(
            markdown, chunk_size=chunk_size, overlap=overlap
        ):
            chunks.append(
                TextChunk(
                    content=chunk.content,
                    chunk_index=len(chunks),
                    section=chunk.section,
                    page=page,
                )
            )
    return chunks


__all__ = ["TextChunk", "split_markdown", "split_markdown_pages"]
