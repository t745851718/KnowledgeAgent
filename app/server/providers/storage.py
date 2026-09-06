"""MinIO-backed source document storage."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from minio import Minio
from minio.deleteobjects import DeleteObject
from minio.error import S3Error

from . import ProviderError


def _safe_segment(value: str, name: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{name} 不是安全的路径片段")
    return value


def _endpoint(value: str, secure: bool | None) -> tuple[str, bool]:
    endpoint = value.strip().rstrip("/")
    if "://" not in endpoint:
        return endpoint, False if secure is None else secure
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MINIO_ENDPOINT 必须是 host:port 或有效的 HTTP(S) URL")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("MINIO_ENDPOINT 不能包含路径、查询参数或 fragment")
    return parsed.netloc, parsed.scheme == "https"


class MinioStorage:
    """Store each source as ``documents/<owner>/<document>/source.<ext>``."""

    _CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        *,
        endpoint: str | None,
        access_key: str | None,
        secret_key: str | None,
        bucket_name: str | None,
        secure: bool | None = None,
        client: Any | None = None,
    ) -> None:
        self.bucket_name = (bucket_name or "").strip().lower()
        if self.bucket_name and not re.fullmatch(
            r"(?=.{3,63}$)[a-z0-9][a-z0-9.-]*[a-z0-9]", self.bucket_name
        ):
            raise ProviderError("storage", "BUCKET_NAME 不是有效的 S3 Bucket 名称")
        self._configuration_error: str | None = None
        if client is not None:
            self.client = client
            return
        if not endpoint or not access_key or not secret_key or not self.bucket_name:
            self.client = None
            self._configuration_error = (
                "缺少 MINIO_ENDPOINT、MINIO_ACCESS_KEY、MINIO_SECRET_KEY 或 BUCKET_NAME"
            )
            return
        try:
            normalized_endpoint, use_tls = _endpoint(endpoint, secure)
            self.client = Minio(
                normalized_endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=use_tls,
            )
        except Exception as exc:
            raise ProviderError("storage", f"MinIO 配置无效: {exc}") from exc

    def _require_client(self) -> Any:
        if self.client is None:
            raise ProviderError("storage", self._configuration_error or "MinIO 未配置")
        return self.client

    def initialize(self) -> None:
        client = self._require_client()
        try:
            if not client.bucket_exists(self.bucket_name):
                client.make_bucket(self.bucket_name)
        except Exception as exc:
            raise ProviderError(
                "storage", f"初始化 MinIO Bucket 失败: {exc}", retryable=True
            ) from exc

    def ping(self) -> bool:
        try:
            return bool(self._require_client().bucket_exists(self.bucket_name))
        except Exception:
            return False

    @staticmethod
    def object_name(*, owner_id: str, document_id: str, extension: str) -> str:
        owner = _safe_segment(owner_id, "owner_id")
        document = _safe_segment(document_id, "document_id")
        normalized_extension = extension.lower().lstrip(".")
        if not normalized_extension or not normalized_extension.isalnum():
            raise ValueError("文件扩展名不合法")
        return f"documents/{owner}/{document}/source.{normalized_extension}"

    @staticmethod
    def _validate_object_name(object_name: str) -> str:
        path = Path(object_name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not object_name.startswith("documents/")
        ):
            raise ValueError("MinIO 对象键不合法")
        return object_name

    async def save_upload(
        self,
        upload: Any,
        *,
        owner_id: str,
        document_id: str,
        extension: str,
        content_type: str,
        max_size: int,
    ) -> tuple[str, int, str]:
        """Hash the request stream, then upload it without a persistent local copy."""

        object_name = self.object_name(
            owner_id=owner_id, document_id=document_id, extension=extension
        )
        size = 0
        digest = hashlib.sha256()
        try:
            await upload.seek(0)
            while True:
                data = await upload.read(self._CHUNK_SIZE)
                if not data:
                    break
                size += len(data)
                if size > max_size:
                    raise ProviderError(
                        "storage",
                        f"文件超过大小限制 {max_size} 字节",
                        code="FILE_TOO_LARGE",
                    )
                digest.update(data)
            await upload.seek(0)
            await asyncio.to_thread(
                self._require_client().put_object,
                self.bucket_name,
                object_name,
                upload.file,
                size,
                content_type=content_type,
            )
            return object_name, size, digest.hexdigest()
        except Exception as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(
                "storage", f"上传原文件到 MinIO 失败: {exc}", retryable=True
            ) from exc

    @staticmethod
    def asset_object_name(*, owner_id: str, document_id: str, relative_path: str) -> str:
        path = PurePosixPath(relative_path.replace("\\", "/"))
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("资源相对路径不合法")
        owner = _safe_segment(owner_id, "owner_id")
        document = _safe_segment(document_id, "document_id")
        return f"documents/{owner}/{document}/assets/{path.as_posix()}"

    async def save_asset(
        self, upload: Any, *, owner_id: str, document_id: str,
        relative_path: str, max_size: int,
    ) -> tuple[str, int]:
        key = self.asset_object_name(
            owner_id=owner_id, document_id=document_id, relative_path=relative_path,
        )
        size = 0
        try:
            await upload.seek(0)
            while data := await upload.read(self._CHUNK_SIZE):
                size += len(data)
                if size > max_size:
                    raise ProviderError("storage", f"资源文件超过大小限制 {max_size} 字节", code="FILE_TOO_LARGE")
            await upload.seek(0)
            await asyncio.to_thread(
                self._require_client().put_object, self.bucket_name, key, upload.file, size,
                content_type=upload.content_type or mimetypes.guess_type(relative_path)[0] or "application/octet-stream",
            )
            return key, size
        except Exception as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("storage", f"上传 Markdown 资源失败: {exc}", retryable=True) from exc

    def download(self, object_name: str, destination: str | Path) -> Path:
        """Download an object to a caller-owned temporary path for processing."""

        key = self._validate_object_name(object_name)
        target = Path(destination)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._require_client().fget_object(self.bucket_name, key, str(target))
            return target
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise ProviderError(
                "storage", f"从 MinIO 下载原文件失败: {exc}", retryable=True
            ) from exc

    @staticmethod
    def image_object_name(*, owner_id: str, document_id: str, chunk_index: int, image_index: int, extension: str) -> str:
        if chunk_index < 0 or image_index < 0:
            raise ValueError("图片索引不能为负数")
        owner = _safe_segment(owner_id, "owner_id")
        document = _safe_segment(document_id, "document_id")
        suffix = extension.lower().lstrip(".")
        if not suffix.isalnum():
            raise ValueError("图片扩展名不受支持")
        return f"documents/{owner}/{document}/images/{chunk_index}_{image_index}.{suffix}"

    def save_image(self, source: str | Path, *, owner_id: str, document_id: str, chunk_index: int, image_index: int) -> str:
        path = Path(source)
        key = self.image_object_name(owner_id=owner_id, document_id=document_id, chunk_index=chunk_index, image_index=image_index, extension=path.suffix)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            with path.open("rb") as image:
                self._require_client().put_object(self.bucket_name, key, image, path.stat().st_size, content_type=content_type)
            return key
        except Exception as exc:
            raise ProviderError("storage", f"保存解析图片失败: {exc}", retryable=True) from exc

    def read_image(self, object_name: str) -> tuple[bytes, str]:
        key = self._validate_object_name(object_name)
        response = None
        try:
            response = self._require_client().get_object(self.bucket_name, key)
            return response.read(), mimetypes.guess_type(key)[0] or "application/octet-stream"
        except Exception as exc:
            raise ProviderError("storage", f"读取解析图片失败: {exc}", retryable=True) from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def delete_document_assets(self, *, owner_id: str, document_id: str) -> None:
        prefix = f"documents/{_safe_segment(owner_id, 'owner_id')}/{_safe_segment(document_id, 'document_id')}/"
        try:
            client = self._require_client()
            objects = list(client.list_objects(
                self.bucket_name,
                prefix=prefix,
                recursive=True,
                include_version=True,
            ))
            errors = list(client.remove_objects(
                self.bucket_name,
                (
                    DeleteObject(item.object_name, getattr(item, "version_id", None))
                    for item in objects
                ),
            ))
            if errors:
                raise ProviderError(
                    "storage",
                    f"删除文档资源失败，MinIO 返回 {len(errors)} 个错误",
                    retryable=True,
                )
            remaining = list(client.list_objects(
                self.bucket_name,
                prefix=prefix,
                recursive=True,
                include_version=True,
            ))
            if remaining:
                raise ProviderError(
                    "storage",
                    f"删除文档资源后仍残留 {len(remaining)} 个对象",
                    retryable=True,
                )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("storage", f"删除文档资源失败: {exc}", retryable=True) from exc

    def delete(self, object_name: str) -> bool:
        key = self._validate_object_name(object_name)
        try:
            self._require_client().remove_object(self.bucket_name, key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise ProviderError(
                "storage", f"删除 MinIO 原文件失败: {exc}", retryable=True
            ) from exc
        except Exception as exc:
            raise ProviderError(
                "storage", f"删除 MinIO 原文件失败: {exc}", retryable=True
            ) from exc


__all__ = ["MinioStorage"]
