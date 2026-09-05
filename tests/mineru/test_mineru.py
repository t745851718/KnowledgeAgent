"""调用 MinerU 精准解析 API 解析本地文档。

默认解析同目录下的 ``data/AttentionIsAllYouNeed.pdf``，并把 MinerU 返回的
ZIP 下载、解压到 ``tests/mineru/output/<文件名>/``。

运行：
    uv run python tests/mineru/test_mineru.py

也可以指定其他文件：
    uv run python tests/mineru/test_mineru.py path/to/document.pdf
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


API_BASE_URL = "https://mineru.net/api/v4"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOCUMENT = PROJECT_ROOT / "tests/data/AttentionIsAllYouNeed.pdf"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "output"
TERMINAL_STATES = {"done", "failed"}


class MinerUError(RuntimeError):
    """MinerU 请求或解析任务失败。"""


class UploadRequest(urllib.request.Request):
    """禁止 urllib 为上传请求自动添加 Content-Type。

    MinerU 返回的 OSS 预签名 URL 没有将 Content-Type 纳入签名；
    urllib 却会为任何带 data 的请求默认加上
    application/x-www-form-urlencoded，从而导致 SignatureDoesNotMatch。
    """

    def has_header(self, header_name: str) -> bool:
        if header_name.lower() == "content-type":
            return True
        return super().has_header(header_name)


def load_api_key() -> str:
    """从环境变量或项目根目录 .env 读取 MINERU_API_KEY。

    同时兼容标准的 ``KEY=value`` 和当前项目使用的 ``KEY: value`` 写法。
    """
    value = os.getenv("MINERU_API_KEY")
    if value:
        return value.strip()

    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        raise MinerUError(f"未找到环境变量 MINERU_API_KEY 或配置文件 {env_path}")

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()

        for separator in ("=", ":"):
            if separator not in line:
                continue
            key, candidate = line.split(separator, 1)
            if key.strip() == "MINERU_API_KEY":
                value = candidate.strip().strip("'\"")
                if value:
                    return value

    raise MinerUError(f"{env_path} 中缺少 MINERU_API_KEY")


def request_json(
    method: str,
    url: str,
    *,
    api_key: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    """发送 MinerU JSON 请求并校验 HTTP 状态与业务状态码。"""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise MinerUError(f"MinerU HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MinerUError(f"请求 MinerU 失败: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MinerUError("MinerU 返回了无法解析的 JSON") from exc

    if result.get("code") != 0:
        raise MinerUError(
            f"MinerU API 错误 code={result.get('code')}: {result.get('msg', '未知错误')}"
        )
    return result


def create_upload_task(
    document: Path,
    *,
    api_key: str,
    model_version: str,
) -> tuple[str, str]:
    """申请本地文件上传地址，返回 batch_id 和签名上传 URL。"""
    payload = {
        "files": [{"name": document.name, "data_id": document.stem[:128]}],
        "model_version": model_version,
        "enable_formula": True,
        "enable_table": True,
        "language": "en",
    }
    result = request_json(
        "POST",
        f"{API_BASE_URL}/file-urls/batch",
        api_key=api_key,
        payload=payload,
    )
    data = result.get("data") or {}
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if not batch_id or len(file_urls) != 1:
        raise MinerUError("MinerU 未返回有效的 batch_id 或文件上传地址")
    return str(batch_id), str(file_urls[0])


def upload_document(document: Path, upload_url: str, *, timeout: float = 300) -> None:
    """将文件上传至 MinerU 签名地址；官方要求不设置 Content-Type。"""
    try:
        with document.open("rb") as source:
            request = UploadRequest(
                upload_url,
                data=source,
                headers={"Content-Length": str(document.stat().st_size)},
                method="PUT",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status not in {200, 201, 204}:
                    raise MinerUError(f"上传文件失败，HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise MinerUError(f"上传文件失败，HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MinerUError(f"上传文件失败: {exc}") from exc


def wait_for_result(
    batch_id: str,
    *,
    api_key: str,
    poll_interval: float,
    max_wait: float,
) -> str:
    """轮询解析状态，成功时返回结果 ZIP 地址。"""
    deadline = time.monotonic() + max_wait
    previous_status = ""

    while time.monotonic() < deadline:
        result = request_json(
            "GET",
            f"{API_BASE_URL}/extract-results/batch/{batch_id}",
            api_key=api_key,
        )
        items = (result.get("data") or {}).get("extract_result") or []
        if not items:
            status = "waiting-file"
            item: dict[str, Any] = {}
        else:
            item = items[0]
            status = str(item.get("state", "unknown"))

        progress = item.get("extract_progress") or {}
        progress_text = ""
        if progress.get("total_pages"):
            progress_text = (
                f" ({progress.get('extracted_pages', 0)}/{progress['total_pages']} 页)"
            )
        display_status = f"{status}{progress_text}"
        if display_status != previous_status:
            print(f"解析状态: {display_status}")
            previous_status = display_status

        if status == "done":
            zip_url = item.get("full_zip_url")
            if not zip_url:
                raise MinerUError("任务已完成，但响应中没有 full_zip_url")
            return str(zip_url)
        if status == "failed":
            raise MinerUError(f"解析失败: {item.get('err_msg') or '未知原因'}")
        if status not in TERMINAL_STATES:
            time.sleep(poll_interval)

    raise MinerUError(f"等待解析超时（{max_wait:g} 秒），batch_id={batch_id}")


def download_and_extract(zip_url: str, output_dir: Path, *, timeout: float = 300) -> Path:
    """下载结果 ZIP，并安全解压到输出目录。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / "mineru-result.zip"

    try:
        with (
            urllib.request.urlopen(zip_url, timeout=timeout) as response,
            archive_path.open("wb") as target,
        ):
            shutil.copyfileobj(response, target)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise MinerUError(f"下载解析结果失败: {exc}") from exc

    resolved_output = output_dir.resolve()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                target = (output_dir / member.filename).resolve()
                if not target.is_relative_to(resolved_output):
                    raise MinerUError(f"结果 ZIP 包含不安全路径: {member.filename}")
            archive.extractall(output_dir)
    except zipfile.BadZipFile as exc:
        raise MinerUError(f"下载结果不是有效的 ZIP: {archive_path}") from exc

    return archive_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="上传本地文档并调用 MinerU 解析")
    parser.add_argument("document", nargs="?", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--model", choices=("pipeline", "vlm"), default="vlm")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-wait", type=float, default=900.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    document = args.document.resolve()
    if not document.is_file():
        raise MinerUError(f"文档不存在: {document}")
    if document.stat().st_size > 200 * 1024 * 1024:
        raise MinerUError("文档超过 MinerU 200 MB 限制")
    if args.poll_interval <= 0 or args.max_wait <= 0:
        raise MinerUError("--poll-interval 和 --max-wait 必须大于 0")

    api_key = load_api_key()
    print(f"提交文档: {document}")
    batch_id, upload_url = create_upload_task(
        document,
        api_key=api_key,
        model_version=args.model,
    )
    print(f"已创建解析批次: {batch_id}")

    upload_document(document, upload_url)
    print("文件上传完成，等待 MinerU 解析")

    zip_url = wait_for_result(
        batch_id,
        api_key=api_key,
        poll_interval=args.poll_interval,
        max_wait=args.max_wait,
    )
    output_dir = args.output_dir.resolve() / document.stem
    archive_path = download_and_extract(zip_url, output_dir)
    print(f"解析完成，结果已保存到: {output_dir}")
    print(f"原始结果包: {archive_path}")


if __name__ == "__main__":
    try:
        main()
    except MinerUError as exc:
        raise SystemExit(f"错误: {exc}") from exc
