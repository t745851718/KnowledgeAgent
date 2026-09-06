"""Offline regressions for image/text alignment through ingestion and the SDK boundary."""

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.server.core import Settings
from app.server.providers import ProviderError
from app.server.providers.bailian import BailianProvider, MultiModalEmbedding
from app.server.repositories import MemoryRepository
from app.server.services.workflows import (
    IngestionService,
    TaskSupervisor,
    _embed_dense_chunks,
    _mineru_chunks,
    _mineru_markdown_chunks,
)
from app.server.utils.text_splitter import TextChunk


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB9sAAAAASUVORK5CYII="
)


def write_parse_result(root, items):
    root.mkdir(parents=True, exist_ok=True)
    images = root / "images"
    images.mkdir(exist_ok=True)
    for name in ("first.png", "second.png"):
        (images / name).write_bytes(PNG)
    content_list = root / "doc_content_list.json"
    content_list.write_text(json.dumps(items), encoding="utf-8")
    return content_list


def mixed_items():
    return [
        {"type": "text", "text": "实验", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "实验正文。", "page_idx": 0},
        {"type": "image", "image_caption": ["第一张图片描述。"],
         "img_path": "images/first.png", "page_idx": 0},
        {"type": "text", "text": "另一段正文。", "page_idx": 0},
        {"type": "chart", "chart_caption": ["第二张图表描述。"],
         "img_path": "images/second.png", "page_idx": 1},
    ]


def test_chunks_keep_each_image_with_its_description_and_page(tmp_path):
    path = write_parse_result(tmp_path, mixed_items())
    chunks = _mineru_chunks(path, chunk_size=120, overlap=10)
    assert [c.chunk_index for c in chunks] == list(range(4))
    assert [c.page for c in chunks] == [1, 1, 1, 2]
    assert [c.section for c in chunks] == ["实验"] * 4
    assert [c.image_paths for c in chunks] == [
        (), ((tmp_path / "images/first.png").resolve(),), (),
        ((tmp_path / "images/second.png").resolve(),),
    ]
    assert "第一张" in chunks[1].content and "第二张" not in chunks[1].content
    assert "第二张" in chunks[3].content and "第一张" not in chunks[3].content


def test_long_caption_keeps_image_on_every_split(tmp_path):
    path = write_parse_result(tmp_path, [
        {"type": "image", "image_caption": ["图片描述。" * 80],
         "img_path": "images/first.png", "page_idx": 3},
    ])
    chunks = _mineru_chunks(path, chunk_size=40, overlap=5)
    assert len(chunks) > 1
    assert all(c.page == 4 and c.image_paths[0].name == "first.png" for c in chunks)


def test_image_without_caption_is_not_dropped(tmp_path):
    path = write_parse_result(tmp_path, [
        {"type": "image", "img_path": "images/first.png", "page_idx": 0},
    ])
    chunks = _mineru_chunks(path, chunk_size=80, overlap=5)
    assert len(chunks) == 1
    assert chunks[0].image_paths
    assert "无文字描述" in chunks[0].content


def test_mineru_markdown_fallback_retains_images_and_adjacent_caption(tmp_path):
    write_parse_result(tmp_path, [])
    chunks = _mineru_markdown_chunks(
        "# 实验\n\n正文。\n\n![](images/first.png)\n\n图 1 实验结果。",
        tmp_path, chunk_size=80, overlap=5,
    )
    visual = [chunk for chunk in chunks if chunk.image_paths]
    assert len(visual) == 1
    assert visual[0].image_paths[0].name == "first.png"
    assert visual[0].section == "实验"
    assert visual[0].page is None
    assert "图 1 实验结果" in visual[0].content


@pytest.mark.parametrize("reference", [None, "../outside.png", "/tmp/outside.png", "images/missing.png"])
def test_invalid_image_reference_fails_instead_of_using_caption_only(tmp_path, reference):
    path = write_parse_result(tmp_path, [
        {"type": "image", "image_caption": ["描述"], "img_path": reference, "page_idx": 0},
    ])
    with pytest.raises(ProviderError):
        _mineru_chunks(path, chunk_size=80, overlap=5)


def test_image_symlink_cannot_escape_parse_directory(tmp_path):
    root = tmp_path / "parsed"
    path = write_parse_result(root, [
        {"type": "image", "img_path": "images/link.png", "page_idx": 0},
    ])
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    (root / "images/link.png").symlink_to(outside)
    with pytest.raises(ProviderError, match="超出解析目录"):
        _mineru_chunks(path, chunk_size=80, overlap=5)


def test_sdk_receives_actual_image_bytes_and_fusion_flag(tmp_path, monkeypatch):
    path = tmp_path / "figure.png"
    path.write_bytes(PNG)
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, output={"embeddings": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr(MultiModalEmbedding, "call", call)
    provider = BailianProvider(api_key="offline-key", dimension=2)
    assert provider.embed_multimodal([{"text": "图示"}, {"image": path}], enable_fusion=True) == [[0.1, 0.2]]
    assert calls[0]["enable_fusion"] is True
    assert calls[0]["input"][0] == {"text": "图示"}
    image = calls[0]["input"][1]["image"]
    assert image.startswith("data:image/png;base64,")
    assert base64.b64decode(image.split(",", 1)[1]) == PNG


class RecordingBailian:
    def __init__(self):
        self.dense_calls = []
        self.sparse_texts = []

    def embed_multimodal(self, items, *, enable_fusion=False):
        self.dense_calls.append((items, enable_fusion))
        if enable_fusion:
            assert len(items) == 2
            assert isinstance(items[1]["image"], Path)
            assert items[1]["image"].read_bytes() == PNG
            return [[2.0, 0.0]]
        return [[1.0, 0.0] for _ in items]

    def embed_sparse_texts(self, texts, *, text_type):
        assert text_type == "document"
        self.sparse_texts.extend(texts)
        return [{index: 1.0} for index, _ in enumerate(texts)]


def test_full_ingestion_preserves_dense_sparse_alignment_and_cleans_images():
    bailian = RecordingBailian()
    rows = []
    parsed_paths = []

    def parse(source, *, document_id, output_root):
        assert source.is_file()
        path = write_parse_result(Path(output_root) / document_id, mixed_items())
        parsed_paths.append(path.parent)
        return SimpleNamespace(content_list_path=path, markdown_path=path.parent / "full.md")

    def insert(batch):
        rows.extend(batch)
        return len(batch)

    async def run():
        repository = MemoryRepository()
        document = await repository.create_document({
            "id": "doc_multimodal", "owner_id": "owner", "status": "queued",
            "storage_path": "documents/owner/doc_multimodal/source.pdf",
        })
        service = IngestionService(
            repository=repository,
            storage=SimpleNamespace(
                download=lambda key, target: target.write_bytes(b"%PDF-test"),
                save_image=lambda source, **kwargs: f"stored/{kwargs['chunk_index']}_{kwargs['image_index']}{source.suffix}",
            ),
            mineru=SimpleNamespace(parse_document=parse),
            bailian=bailian,
            zilliz=SimpleNamespace(ensure_collection=lambda: None, insert_chunks=insert),
            settings=Settings(embedding_dimension=2, chunk_size=120, chunk_overlap=10),
            supervisor=TaskSupervisor(),
        )
        await service._process(document)
        result = await repository.get_document(document["id"])
        assert result["status"] == "indexed", result
        assert result["chunk_count"] == 4

    asyncio.run(run())
    assert [row["dense_vector"][0] for row in rows] == [1, 2, 1, 2]
    assert [row["content"] for row in rows] == bailian.sparse_texts
    assert [row["sparse_vector"] for row in rows] == [{i: 1.0} for i in range(4)]
    assert all("image_paths" not in row for row in rows)
    fused = [items for items, fusion in bailian.dense_calls if fusion]
    assert len(fused) == 2
    descriptions = {items[1]["image"].name: items[0]["text"] for items in fused}
    assert "第一张" in descriptions["first.png"]
    assert "第二张" in descriptions["second.png"]
    assert all(not path.exists() for path in parsed_paths)


def test_wrong_fusion_cardinality_is_rejected(tmp_path):
    provider = SimpleNamespace(embed_multimodal=lambda *args, **kwargs: [[1.0], [2.0]])
    with pytest.raises(ProviderError, match="一个 dense"):
        _embed_dense_chunks(provider, [TextChunk("描述", 0, image_paths=(tmp_path / "figure.png",))])
