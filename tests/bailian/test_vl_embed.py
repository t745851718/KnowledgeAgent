"""测试 qwen3-vl-embedding 的文本、图片和图文融合向量。

默认使用仓库中的一张图片，分别请求：
1. 独立向量：文本和图片各返回一个向量；
2. 融合向量：文本和图片共同返回一个向量。

运行：
    uv run python tests/bailian/test_vl_embed.py

指定图片或只测试一种模式：
    uv run python tests/bailian/test_vl_embed.py path/to/image.png
    uv run python tests/bailian/test_vl_embed.py --mode fusion
"""

from __future__ import annotations

import argparse
import math
import os
from http import HTTPStatus
from pathlib import Path
from typing import Any

import dashscope
from dotenv import load_dotenv


MODEL = "qwen3-vl-embedding"
DIMENSION = 1024
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = PROJECT_ROOT / "tests/data/images/image-20260207233638519.png"


def configure_dashscope() -> str:
    """加载密钥，并将 OpenAI 兼容地址转换为 DashScope 原生地址。"""
    load_dotenv(PROJECT_ROOT / ".env")

    api_key = os.getenv("BAILIAN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 BAILIAN_API_KEY 或 DASHSCOPE_API_KEY")

    api_host = os.getenv("DASHSCOPE_API_HOST")
    if not api_host:
        api_host = os.getenv("BAILIAN_BASE_URL")
        if api_host and api_host.rstrip("/").endswith("/compatible-mode/v1"):
            api_host = (
                api_host.rstrip("/")[: -len("/compatible-mode/v1")] + "/api/v1"
            )

    if not api_host:
        raise RuntimeError(
            "缺少 DASHSCOPE_API_HOST；也无法从 BAILIAN_BASE_URL 推导原生 API 地址"
        )
    if not api_host.rstrip("/").endswith("/api/v1"):
        raise RuntimeError(
            "qwen3-vl-embedding 需要 DashScope 原生 API 地址，地址应以 /api/v1 结尾"
        )

    dashscope.base_http_api_url = api_host.rstrip("/")
    return api_key


def request_embeddings(
    *, api_key: str, text: str, image: Path, enable_fusion: bool
) -> list[dict[str, Any]]:
    """调用模型并返回 embedding 条目。DashScope SDK 会自动上传本地图片。"""
    response = dashscope.MultiModalEmbedding.call(
        api_key=api_key,
        model=MODEL,
        input=[{"text": text}, {"image": image.resolve().as_uri()}],
        dimension=DIMENSION,
        output_type="dense",
        enable_fusion=enable_fusion,
    )

    if response.status_code != HTTPStatus.OK:
        raise RuntimeError(
            "模型调用失败："
            f"status={response.status_code}, "
            f"code={response.code}, "
            f"message={response.message}, "
            f"request_id={response.request_id}"
        )

    embeddings = (response.output or {}).get("embeddings") or []
    expected_count = 1 if enable_fusion else 2
    if len(embeddings) != expected_count:
        raise RuntimeError(
            f"向量数量不符合预期：期望 {expected_count}，实际 {len(embeddings)}"
        )

    for item in embeddings:
        vector = item.get("embedding") or []
        if len(vector) != DIMENSION:
            raise RuntimeError(
                f"向量维度不符合预期：期望 {DIMENSION}，实际 {len(vector)}"
            )
    return embeddings


def print_summary(title: str, embeddings: list[dict[str, Any]]) -> None:
    """只输出摘要，避免在终端打印完整的 1024 维向量。"""
    print(f"\n{title}")
    for item in embeddings:
        vector = item["embedding"]
        norm = math.sqrt(sum(value * value for value in vector))
        preview = ", ".join(f"{value:.6f}" for value in vector[:5])
        print(
            f"- index={item.get('index')}, type={item.get('type')}, "
            f"dimension={len(vector)}, l2_norm={norm:.6f}"
        )
        print(f"  preview=[{preview}, ...]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument(
        "--text",
        default="这是一张知识库文档截图，包含技术说明和示例。",
        help="与图片一起向量化的文本",
    )
    parser.add_argument(
        "--mode",
        choices=("independent", "fusion", "both"),
        default="both",
        help="测试独立向量、融合向量或两者",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    image = args.image.resolve()
    if not image.is_file():
        raise FileNotFoundError(f"测试图片不存在：{image}")
    if image.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("qwen3-vl-embedding 要求单张图片不超过 10 MB")

    api_key = configure_dashscope()

    if args.mode in {"independent", "both"}:
        embeddings = request_embeddings(
            api_key=api_key,
            text=args.text,
            image=image,
            enable_fusion=False,
        )
        print_summary("独立向量测试通过", embeddings)

    if args.mode in {"fusion", "both"}:
        embeddings = request_embeddings(
            api_key=api_key,
            text=args.text,
            image=image,
            enable_fusion=True,
        )
        print_summary("融合向量测试通过", embeddings)


if __name__ == "__main__":
    main()
