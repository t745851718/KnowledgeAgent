"""Synchronous MinerU document parsing client."""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ProviderError


class _UploadRequest(urllib.request.Request):
    """Prevent urllib from adding a Content-Type not covered by the OSS signature."""

    def has_header(self, header_name: str) -> bool:
        if header_name.lower() == "content-type":
            return True
        return super().has_header(header_name)


@dataclass(frozen=True, slots=True)
class MinerUResult:
    batch_id: str
    work_dir: Path
    markdown_path: Path
    content_list_path: Path | None


class MinerUProvider:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base_url: str | None = None,
        output_root: str | Path | None = None,
        model_version: str = "vlm",
        language: str | None = None,
        poll_interval: float = 5,
        max_wait: float = 900,
        request_timeout: float = 30,
        transfer_timeout: float = 300,
    ) -> None:
        self.api_key = api_key or os.getenv("MINERU_API_KEY")
        self.api_base_url = (api_base_url or os.getenv("MINERU_API_BASE_URL") or "https://mineru.net/api/v4").rstrip("/")
        self.output_root = Path(output_root or os.getenv("MINERU_OUTPUT_ROOT") or ".data/mineru")
        self.model_version = model_version
        self.language = language or os.getenv("MINERU_LANGUAGE") or "ch"
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self.request_timeout = request_timeout
        self.transfer_timeout = transfer_timeout

    def _key(self) -> str:
        if not self.api_key:
            raise ProviderError("mineru", "缺少 MINERU_API_KEY")
        return self.api_key

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._key()}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.api_base_url}/{path.lstrip('/')}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderError("mineru", f"HTTP {exc.code}: {detail}", retryable=exc.code in {429, 500, 502, 503, 504}) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError("mineru", f"网络请求失败: {exc}", retryable=True) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError("mineru", "返回内容不是有效 JSON", retryable=True) from exc
        if result.get("code") != 0:
            raise ProviderError("mineru", str(result.get("msg") or "API 请求失败"), code=str(result.get("code")))
        return result

    def _create_task(self, document: Path, data_id: str) -> tuple[str, str]:
        result = self._request_json(
            "POST",
            "file-urls/batch",
            {
                "files": [{"name": document.name, "data_id": data_id[:128]}],
                "model_version": self.model_version,
                "enable_formula": True,
                "enable_table": True,
                "language": self.language,
            },
        )
        data = result.get("data") or {}
        urls = data.get("file_urls") or []
        if not data.get("batch_id") or len(urls) != 1:
            raise ProviderError("mineru", "未返回有效 batch_id 或上传地址")
        return str(data["batch_id"]), str(urls[0])

    def _upload(self, document: Path, upload_url: str) -> None:
        try:
            with document.open("rb") as source:
                request = _UploadRequest(
                    upload_url,
                    data=source,
                    headers={"Content-Length": str(document.stat().st_size)},
                    method="PUT",
                )
                with urllib.request.urlopen(request, timeout=self.transfer_timeout) as response:
                    if response.status not in {200, 201, 204}:
                        raise ProviderError("mineru", f"文件上传失败，HTTP {response.status}")
        except ProviderError:
            raise
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderError("mineru", f"文件上传失败，HTTP {exc.code}: {detail}", retryable=exc.code >= 500) from exc
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError("mineru", f"文件上传失败: {exc}", retryable=True) from exc

    def _wait(self, batch_id: str) -> str:
        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            result = self._request_json("GET", f"extract-results/batch/{batch_id}")
            items = (result.get("data") or {}).get("extract_result") or []
            item = items[0] if items else {}
            state = str(item.get("state") or "waiting-file")
            if state == "done":
                if not item.get("full_zip_url"):
                    raise ProviderError("mineru", "任务完成但缺少 full_zip_url")
                return str(item["full_zip_url"])
            if state == "failed":
                raise ProviderError("mineru", f"解析失败: {item.get('err_msg') or '未知原因'}")
            time.sleep(self.poll_interval)
        raise ProviderError("mineru", f"解析超时，batch_id={batch_id}", retryable=True)

    def _download_extract(self, url: str, work_dir: Path) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)
        archive_path = work_dir / "mineru-result.zip"
        try:
            with urllib.request.urlopen(url, timeout=self.transfer_timeout) as response, archive_path.open("wb") as target:
                shutil.copyfileobj(response, target)
            resolved_root = work_dir.resolve()
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    if not (work_dir / member.filename).resolve().is_relative_to(resolved_root):
                        raise ProviderError("mineru", f"结果包包含不安全路径: {member.filename}")
                archive.extractall(work_dir)
        except ProviderError:
            raise
        except (OSError, urllib.error.URLError, TimeoutError, zipfile.BadZipFile) as exc:
            raise ProviderError("mineru", f"下载或解压结果失败: {exc}", retryable=True) from exc

    def parse_document(self, document: str | Path, *, document_id: str) -> MinerUResult:
        source = Path(document).resolve()
        if not source.is_file():
            raise ProviderError("mineru", f"文档不存在: {source}")
        if source.stat().st_size > 200 * 1024 * 1024:
            raise ProviderError("mineru", "文档超过 200 MB")
        if self.poll_interval <= 0 or self.max_wait <= 0:
            raise ProviderError("mineru", "poll_interval 和 max_wait 必须大于 0")
        try:
            batch_id, upload_url = self._create_task(source, document_id)
            self._upload(source, upload_url)
            result_url = self._wait(batch_id)
            work_dir = (self.output_root / document_id).resolve()
            self._download_extract(result_url, work_dir)
            markdown_paths = sorted(work_dir.rglob("full.md"))
            if not markdown_paths:
                markdown_paths = sorted(work_dir.rglob("*.md"))
            if not markdown_paths:
                raise ProviderError("mineru", "解析结果中没有 Markdown 文件")
            content_lists = sorted(work_dir.rglob("*_content_list.json"))
            return MinerUResult(
                batch_id=batch_id,
                work_dir=work_dir,
                markdown_path=markdown_paths[0],
                content_list_path=content_lists[0] if content_lists else None,
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("mineru", f"解析文档失败: {exc}", retryable=True) from exc

    parse = parse_document


__all__ = ["MinerUProvider", "MinerUResult"]
