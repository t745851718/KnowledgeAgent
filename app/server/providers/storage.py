"""Durable local source-file storage with optional Zilliz Volume mirroring."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any, BinaryIO

from pymilvus.bulk_writer.volume_file_manager import VolumeFileManager

from . import ProviderError


def _safe_segment(value: str, name: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{name} 不是安全的路径片段")
    return value


class LocalFileStorage:
    """Store uploads under ``<root>/<document_id>/source.<extension>``."""

    _COPY_CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        volume_cloud_endpoint: str | None = None,
        volume_api_key: str | None = None,
        volume_name: str | None = None,
    ) -> None:
        self.root = Path(root or os.getenv("FILE_STORAGE_ROOT") or ".data/files").resolve()
        self.volume_cloud_endpoint = volume_cloud_endpoint or os.getenv("ZILLIZ_CLOUD_ENDPOINT")
        self.volume_api_key = volume_api_key or os.getenv("ZILLIZ_API_KEY")
        self.volume_name = volume_name or os.getenv("ZILLIZ_VOLUME_NAME")

    @property
    def volume_enabled(self) -> bool:
        return bool(self.volume_cloud_endpoint and self.volume_api_key and self.volume_name)

    def _destination(self, document_id: str, extension: str) -> Path:
        document = _safe_segment(document_id, "document_id")
        normalized_extension = extension.lower().lstrip(".")
        if not normalized_extension or not normalized_extension.isalnum():
            raise ValueError("文件扩展名不合法")
        target = (self.root / document / f"source.{normalized_extension}").resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("文件路径超出存储根目录")
        return target

    def _checked_path(self, path: str | Path) -> Path:
        target = Path(path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("文件路径超出存储根目录")
        return target

    def save_file(
        self,
        source: BinaryIO | str | Path,
        *,
        document_id: str,
        extension: str,
        max_size: int | None = None,
    ) -> tuple[Path, int, str]:
        """Synchronously copy a file, returning ``(path, size, sha256)``."""

        destination = self._destination(document_id, extension)
        size = 0
        digest = hashlib.sha256()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            should_close = isinstance(source, (str, Path))
            input_file: BinaryIO = Path(source).open("rb") if should_close else source
            try:
                try:
                    input_file.seek(0)
                except (AttributeError, OSError):
                    pass
                with destination.open("wb") as output_file:
                    while data := input_file.read(self._COPY_CHUNK_SIZE):
                        size += len(data)
                        if max_size is not None and size > max_size:
                            raise ProviderError("storage", f"文件超过大小限制 {max_size} 字节", code="FILE_TOO_LARGE")
                        digest.update(data)
                        output_file.write(data)
            finally:
                if should_close:
                    input_file.close()
            return destination, size, digest.hexdigest()
        except Exception as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("storage", f"保存原文件失败: {exc}") from exc

    async def save_upload(
        self,
        upload: Any,
        *,
        document_id: str,
        extension: str,
        max_size: int,
    ) -> tuple[Path, int, str]:
        """Stream a FastAPI ``UploadFile``-like object to local storage."""

        destination = self._destination(document_id, extension)
        size = 0
        digest = hashlib.sha256()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            seek = getattr(upload, "seek", None)
            if callable(seek):
                result = seek(0)
                if hasattr(result, "__await__"):
                    await result
            with destination.open("wb") as output_file:
                while True:
                    data = upload.read(self._COPY_CHUNK_SIZE)
                    if hasattr(data, "__await__"):
                        data = await data
                    if not data:
                        break
                    size += len(data)
                    if size > max_size:
                        raise ProviderError("storage", f"文件超过大小限制 {max_size} 字节", code="FILE_TOO_LARGE")
                    digest.update(data)
                    output_file.write(data)
            return destination, size, digest.hexdigest()
        except Exception as exc:
            destination.unlink(missing_ok=True)
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("storage", f"保存上传文件失败: {exc}") from exc

    def upload_volume(self, path: str | Path, *, owner_id: str, document_id: str) -> str | None:
        """Mirror a local file when all Zilliz Volume settings are present."""

        if not self.volume_enabled:
            return None
        source = self._checked_path(path)
        owner = _safe_segment(owner_id, "owner_id")
        document = _safe_segment(document_id, "document_id")
        volume_directory = f"documents/{owner}/{document}/"
        try:
            manager = VolumeFileManager(
                cloud_endpoint=self.volume_cloud_endpoint or "",
                api_key=self.volume_api_key or "",
                volume_name=self.volume_name or "",
            )
            manager.upload_file_to_volume(
                source_file_path=str(source),
                target_volume_path=volume_directory,
            )
            return f"{volume_directory}{source.name}"
        except Exception as exc:
            raise ProviderError("zilliz-volume", f"上传原文件失败: {exc}", retryable=True) from exc

    def delete(self, path: str | Path, volume_path: str | None = None) -> bool:
        """Delete the local file; per-file Managed Volume deletion has no SDK API."""

        del volume_path
        target = self._checked_path(path)
        if not target.exists():
            return False
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            parent = target.parent
            while parent != self.root and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
            return True
        except OSError as exc:
            raise ProviderError("storage", f"删除本地原文件失败: {exc}") from exc


LocalStorage = LocalFileStorage

__all__ = ["LocalFileStorage", "LocalStorage"]
