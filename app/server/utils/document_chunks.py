"""MinerU text/image association and heading-aware chunk construction."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..providers import ProviderError
from .text_splitter import TextChunk, split_markdown


def _mineru_blocks(content_list_path: Path) -> list[tuple[int, str, dict[str, Any]]]:
    """Retain source items alongside their readable text, including image references."""
    items = json.loads(content_list_path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        return []
    blocks = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("page_idx"), int):
            continue
        item_type = item.get("type")
        text = ""
        if isinstance(item.get("text"), str) and item_type not in {
            "page_number",
            "footer",
        }:
            text = item["text"].strip()
            level = item.get("text_level")
            if text and isinstance(level, int) and 1 <= level <= 6:
                text = f"{'#' * level} {text}"
        elif item_type == "table" and isinstance(item.get("table_body"), str):
            table_parts = []
            for key in ("table_caption", "table_body", "table_footnote"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    table_parts.append(value.strip())
                elif isinstance(value, list):
                    table_parts.extend(str(part).strip() for part in value if str(part).strip())
            text = "\n\n".join(table_parts)
        elif item_type in {"list", "index"} and isinstance(item.get("list_items"), list):
            text = "\n".join(
                f"- {str(value).strip()}"
                for value in item["list_items"]
                if str(value).strip()
            )
        elif item_type in {"image", "chart"}:
            captions = item.get("image_caption") or item.get("chart_caption") or []
            if isinstance(captions, str):
                text = captions.strip()
            elif isinstance(captions, list):
                text = "\n".join(str(value).strip() for value in captions if str(value).strip())
            if not text and isinstance(item.get("content"), str):
                text = item["content"].strip()
        if text or item_type in {"image", "chart"} or item.get("img_path"):
            blocks.append((item["page_idx"] + 1, text, item))
    return blocks


def _mineru_pages(content_list_path: Path) -> list[tuple[int, str]]:
    """Text-only view retained for callers that only need page Markdown."""
    pages: dict[int, list[str]] = {}
    for page, text, _ in _mineru_blocks(content_list_path):
        if text:
            pages.setdefault(page, []).append(text)
    return [
        (page, "\n\n".join(parts))
        for page, parts in sorted(pages.items())
        if parts
    ]


def _mineru_chunks(
    content_list_path: Path, *, chunk_size: int, overlap: int
) -> list[TextChunk]:
    return _split_mineru_blocks(
        _mineru_blocks(content_list_path), content_list_path.parent,
        chunk_size=chunk_size, overlap=overlap,
    )


def _mineru_markdown_chunks(
    markdown: str, root: Path, *, chunk_size: int, overlap: int
) -> list[TextChunk]:
    """Retain local inline images when MinerU returns only Markdown (no page metadata)."""
    blocks: list[tuple[int | None, str, dict[str, Any]]] = []

    def add_text(text: str) -> None:
        for line in text.splitlines():
            heading = re.match(r"^(#{1,6})\s+(.+)$", line)
            item: dict[str, Any] = {"type": "text", "text": line}
            if heading:
                item.update(text_level=len(heading[1]), text=heading[2])
            blocks.append((None, line, item))

    position = 0
    for match in re.finditer(r"!\[([^\]]*)\]\(([^\s)]+)\)", markdown):
        add_text(markdown[position:match.start()])
        caption = match[1].strip()
        if not caption:
            # MinerU commonly writes the figure caption in the next paragraph.
            following = markdown[match.end():].strip().split("\n\n", 1)[0]
            if following and not following.startswith(("#", "![")):
                caption = following
        blocks.append((None, caption, {"type": "image", "img_path": match[2]}))
        position = match.end()
    add_text(markdown[position:])
    return _split_mineru_blocks(blocks, root, chunk_size=chunk_size, overlap=overlap)


def _split_mineru_blocks(
    blocks: list[tuple[int | None, str, dict[str, Any]]], root: Path,
    *, chunk_size: int, overlap: int,
) -> list[TextChunk]:
    """Split ordered text runs and bind each visual block to its own caption chunks."""
    root = root.resolve()
    chunks: list[TextChunk] = []
    pending: list[str] = []
    headings: list[str] = []
    active_page: int | None = None

    def append_chunks(text: str, page: int | None, images: tuple[Path, ...] = ()) -> None:
        prefix = "\n".join(f"{'#' * (i + 1)} {title}" for i, title in enumerate(headings))
        for chunk in split_markdown(
            f"{prefix}\n\n{text}", chunk_size=chunk_size, overlap=overlap
        ):
            chunks.append(replace(chunk, chunk_index=len(chunks), page=page, image_paths=images))

    def flush() -> None:
        if pending:
            append_chunks("\n\n".join(pending), active_page)
            pending.clear()

    for page, text, item in blocks:
        if active_page != page:
            flush()
            active_page = page
        level = item.get("text_level")
        if item.get("type") == "text" and isinstance(level, int) and 1 <= level <= 6:
            flush()
            headings[level - 1:] = [str(item["text"]).strip()]
            continue
        if item.get("type") in {"image", "chart"} or item.get("img_path"):
            flush()
            location = f"第 {page} 页图片" if page is not None else "图片"
            reference = item.get("img_path")
            if not isinstance(reference, str) or not reference.strip():
                raise ProviderError("parser", f"{location}缺少 img_path")
            relative = Path(reference)
            image_path = (root / relative).resolve()
            if relative.is_absolute() or not image_path.is_relative_to(root):
                raise ProviderError("parser", f"{location}路径超出解析目录")
            if not image_path.is_file():
                raise ProviderError("parser", f"{location}文件不存在")
            # No fabricated visual description: a missing caption is an explicit label.
            caption = text or f"{location}（无文字描述）"
            append_chunks(caption, page, (image_path,))
        elif text:
            pending.append(text)
    flush()
    return chunks
