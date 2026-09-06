"""Uploaded Markdown image inputs, without external network or model calls."""

import asyncio
import base64
from types import SimpleNamespace

import pytest

from app.server.core import Settings
from app.server.providers import ProviderError
from app.server.repositories import MemoryRepository
from app.server.services.ingestion import IngestionService, TaskSupervisor
from app.server.utils.markdown_chunks import uploaded_markdown_chunks
from app.server.utils.text_splitter import split_markdown
from tests.test_multimodal_ingestion import PNG


INLINE = "data:image/png;base64," + base64.b64encode(PNG).decode()


def split(text, size=1200):
    return uploaded_markdown_chunks(text, chunk_size=size, overlap=10)


def test_inline_reference_images_and_description_alignment():
    chunks = split(f'# 章节\n\n正文\n\n![第一图](https://example.com/a_(1).png "标题") 后文\n\n'
                   f'![第二图][figure]\n\n[figure]: {INLINE}\n')
    visual = [chunk for chunk in chunks if chunk.image_paths]
    assert [chunk.image_paths for chunk in visual] == [("https://example.com/a_(1).png",), (INLINE,)]
    assert [chunk.content for chunk in visual] == ["章节\n\n第一图", "章节\n\n第二图"]
    assert all(chunk.page is None and chunk.section == "章节" for chunk in chunks)
    assert all("base64" not in chunk.content for chunk in chunks)
    assert any("后文" in chunk.content for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))


def test_code_and_escaped_images_are_not_model_inputs():
    chunks = split('```md\n![示例](file:///etc/example.png)\n```\n\n'
                   '`![代码](./missing.png)`\n\n\\![转义](./missing.png)')
    assert chunks and all(not chunk.image_paths for chunk in chunks)


def test_text_only_markdown_keeps_existing_split_behavior():
    text = "# 章节\n\n- 列表项\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
    assert split(text) == split_markdown(text, chunk_size=1200, overlap=10)


@pytest.mark.parametrize("source", ["/tmp/a.png", "file:///tmp/a.png",
    "https://localhost/a", "http://127.0.0.1/a", "http://169.254.169.254/a",
    "http://[::1]/a", "https://user:pass@example.com/a", "javascript:alert(1)",
    "data:image/png;base64,xxx", "data:text/html;base64,YQ=="])
def test_invalid_or_unavailable_images_fail_explicitly(source):
    with pytest.raises(ProviderError):
        split(f"![描述](<{source}>)")


def test_missing_relative_image_is_counted_and_caption_remains_searchable():
    stats = {}
    chunks = uploaded_markdown_chunks(
        "![缺失图](images/a.png)", chunk_size=1200, overlap=10, stats=stats,
    )
    assert stats == {"total_images": 1, "missing_images": 1}
    assert chunks[0].content == "缺失图"
    assert chunks[0].image_paths == ()


def test_folder_relative_image_resolves_inside_package(tmp_path):
    package = tmp_path / "package"
    document_dir = package / "notes"
    image = document_dir / "assets" / "diagram.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG)
    stats = {}
    chunks = uploaded_markdown_chunks(
        "![流程图](assets/diagram.png)", chunk_size=1200, overlap=10,
        asset_root=document_dir, asset_boundary=package, stats=stats,
    )
    assert stats == {"total_images": 1, "missing_images": 0}
    assert chunks[0].image_paths == (image.resolve(),)
    nested = document_dir / "chapters"
    chunks = uploaded_markdown_chunks(
        "![流程图](../assets/diagram.png)", chunk_size=1200, overlap=10,
        asset_root=nested, asset_boundary=package,
    )
    assert chunks[0].image_paths == (image.resolve(),)
    with pytest.raises(ProviderError, match="越出"):
        uploaded_markdown_chunks(
            "![越界](../../../outside.png)", chunk_size=1200, overlap=10,
            asset_root=nested, asset_boundary=package,
        )


def test_missing_caption_and_long_caption():
    assert "无文字描述" in split(f"![]({INLINE})")[0].content
    assert split(f'![]({INLINE} "图片标题")')[0].content == "图片标题"
    chunks = split(f"![{'长描述' * 60}]({INLINE})", size=50)
    assert len(chunks) > 1 and all(chunk.image_paths == (INLINE,) for chunk in chunks)


def test_html_images_are_extracted_without_executing_html():
    chunks = split(f'# 标题\n\n<img src="{INLINE}" alt="HTML图片" style="zoom:50%" />\n\n'
                   '文字 <img src="https://example.com/a.png" alt="行内图"> 后文')
    visual = [chunk for chunk in chunks if chunk.image_paths]
    assert [chunk.content for chunk in visual] == ["标题\n\nHTML图片", "标题\n\n行内图"]
    assert visual[0].image_paths == (INLINE,)
    assert split('<img src="images/missing.png">')[0].image_paths == ()


def test_oversized_inline_image_rejected(monkeypatch):
    monkeypatch.setattr("app.server.utils.markdown_chunks.MAX_INLINE_IMAGE_BYTES", 1)
    with pytest.raises(ProviderError, match="5 MiB"):
        split(f"![图片]({INLINE})")


def test_uploaded_markdown_runs_graph_and_fuses_images():
    dense_calls, sparse_calls, rows = [], [], []

    def dense(items, *, enable_fusion=False):
        dense_calls.append((items, enable_fusion))
        return [[1.0, 0.0]] * (1 if enable_fusion else len(items))

    def sparse(texts, **kwargs):
        sparse_calls.extend(texts)
        return [{0: 1.0} for _ in texts]

    def insert(batch):
        rows.extend(batch)
        return len(batch)

    async def run():
        repository = MemoryRepository()
        document = await repository.create_document({
            "id": "doc_markdown", "owner_id": "owner", "status": "queued",
            "storage_path": "documents/owner/doc_markdown/source.md",
        })
        service = IngestionService(
            repository=repository,
            storage=SimpleNamespace(download=lambda key, target: target.write_text(
                f"# 章节\n\n正文\n\n![真实图片描述]({INLINE})", encoding="utf-8")),
            mineru=SimpleNamespace(),  # Any attempted MinerU call must fail.
            bailian=SimpleNamespace(embed_multimodal=dense, embed_sparse_texts=sparse),
            zilliz=SimpleNamespace(ensure_collection=lambda: None, insert_chunks=insert),
            settings=Settings(embedding_dimension=2), supervisor=TaskSupervisor(),
        )
        await service._process(document)
        assert (await repository.get_document(document["id"]))["status"] == "indexed"

    asyncio.run(run())
    fused = [items for items, fusion in dense_calls if fusion]
    assert fused == [[{"text": "章节\n\n真实图片描述"}, {"image": INLINE}]]
    assert sparse_calls == [row["content"] for row in rows]
    assert all("base64" not in text for text in sparse_calls)
    assert all("image_paths" not in row for row in rows)
