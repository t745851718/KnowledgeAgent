"""Uploaded Markdown: associate actual image inputs without server-side URL fetching."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from dataclasses import replace
from html.parser import HTMLParser
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from ..providers import ProviderError
from .text_splitter import TextChunk, split_markdown


MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024


class _HtmlImages(HTMLParser):
    """Extract img attributes as data; never render or execute uploaded HTML."""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.tokens: list[Token] = []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            attributes = dict(attrs)
            self.tokens.append(Token("image", "img", 0, attrs={
                "src": attributes.get("src") or "",
                "title": attributes.get("title") or "",
            }, content=attributes.get("alt") or ""))

    def handle_data(self, data):
        self.tokens.append(Token("text", "", 0, content=data))


def _image_source(source: str) -> str:
    # Never resolve uploaded paths against the host filesystem or MinIO namespace.
    if source.startswith("data:"):
        match = re.fullmatch(r"data:image/(png|jpeg|webp|bmp);base64,([A-Za-z0-9+/=]+)", source)
        if not match or len(match[2]) > 4 * ((MAX_INLINE_IMAGE_BYTES + 2) // 3):
            raise ProviderError("parser", "内嵌图片须为 PNG/JPEG/WEBP/BMP Base64，且不超过 5 MiB")
        try:
            payload = base64.b64decode(match[2], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ProviderError("parser", "内嵌图片 Base64 无效") from exc
        if not payload or len(payload) > MAX_INLINE_IMAGE_BYTES:
            raise ProviderError("parser", "内嵌图片为空或超过 5 MiB")
        return source
    try:
        url = urlsplit(source)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError
        if url.username or url.password or url.port not in {None, 80, 443}:
            raise ValueError
        host = url.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or re.fullmatch(r"[0-9.]+", host):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
    except ValueError as exc:
        raise ProviderError(
            "parser", "Markdown 图片须使用公开 HTTP(S) URL 或内嵌 Base64；"
            "单独上传 Markdown 不包含相对路径图片，请先将图片内嵌。",
        ) from exc
    # The model provider fetches the URL; this service never follows redirects,
    # resolves local paths, or forwards application credentials to image hosts.
    return source


def uploaded_markdown_chunks(markdown: str, *, chunk_size: int, overlap: int) -> list[TextChunk]:
    parser = MarkdownIt("commonmark", {"html": True})
    # Parse unsafe schemes too, so our validation rejects them instead of silently
    # indexing image syntax as text. Tokens are never rendered as HTML.
    parser.validateLink = lambda value: True
    tokens = parser.parse(markdown)
    for token in tokens:
        if token.type not in {"inline", "html_block"}:
            continue
        children: list[Token] = []
        for child in (token.children or []) if token.type == "inline" else [token]:
            children.extend(_HtmlImages(child.content).tokens if child.type in {
                "html_inline", "html_block",
            } else [child])
        token.children = children
    if not any(child.type == "image" for token in tokens for child in token.children or []):
        # Preserve existing text-only Markdown behavior, including list/table markup.
        return split_markdown(markdown, chunk_size=chunk_size, overlap=overlap)
    chunks: list[TextChunk] = []
    headings: list[str] = []
    pending: list[str] = []

    def append(text: str, image: str | None = None) -> None:
        prefix = "\n".join(f"{'#' * (i + 1)} {title}" for i, title in enumerate(headings))
        for chunk in split_markdown(f"{prefix}\n\n{text}", chunk_size=chunk_size, overlap=overlap):
            chunks.append(replace(chunk, chunk_index=len(chunks), image_paths=(image,) if image else ()))

    def flush() -> None:
        if pending:
            append("\n\n".join(pending))
            pending.clear()

    for index, token in enumerate(tokens):
        if token.type in {"inline", "html_block"}:
            children = token.children or []
            if index and tokens[index - 1].type == "heading_open":
                flush()
                level = int(tokens[index - 1].tag[1:])
                title = "".join(child.content for child in children if child.type in {"text", "code_inline", "image"})
                headings[level - 1:] = [title]
                if not any(child.type == "image" for child in children):
                    continue
            if not any(child.type == "image" for child in children):
                pending.append(token.content)
                continue
            text: list[str] = []
            for child in children:
                if child.type == "image":
                    if text:
                        pending.append("".join(text))
                        text.clear()
                    flush()
                    caption = child.content.strip() or child.attrGet("title") or "图片（无文字描述）"
                    append(caption, _image_source(child.attrGet("src") or ""))
                elif child.type in {"softbreak", "hardbreak"}:
                    text.append("\n")
                else:
                    text.append(child.content)
            if text:
                pending.append("".join(text))
        elif token.type in {"fence", "code_block"}:
            pending.append(f"```{token.info}\n{token.content}```")
    flush()
    return chunks
