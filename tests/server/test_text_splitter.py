from app.server.providers.zilliz import _truncate_utf8
from app.server.services.workflows import _mineru_pages
from app.server.utils.text_splitter import split_markdown_pages


def test_mineru_page_metadata_is_preserved(tmp_path) -> None:
    content_list = tmp_path / "content_list.json"
    content_list.write_text(
        """[
          {"type":"text","text":"第一章","text_level":1,"page_idx":0},
          {"type":"text","text":"第一页正文。","page_idx":0},
          {"type":"page_number","text":"2","page_idx":1},
          {"type":"text","text":"第二页正文。","page_idx":1}
        ]""",
        encoding="utf-8",
    )

    pages = _mineru_pages(content_list)
    chunks = split_markdown_pages(pages, chunk_size=80, overlap=10)

    assert pages == [(1, "# 第一章\n\n第一页正文。"), (2, "第二页正文。")]
    assert [chunk.page for chunk in chunks] == [1, 2]
    assert chunks[0].section == "第一章"


def test_mineru_structured_content_is_preserved(tmp_path) -> None:
    content_list = tmp_path / "content_list.json"
    content_list.write_text(
        """[
          {"type":"list","list_items":["第一项","第二项"],"page_idx":0},
          {"type":"chart","chart_caption":["增长趋势"],"page_idx":0},
          {"type":"table","table_caption":["表 1"],"table_body":"|A|B|",\n           "table_footnote":["注释"],"page_idx":1}
        ]""",
        encoding="utf-8",
    )

    assert _mineru_pages(content_list) == [
        (1, "- 第一项\n- 第二项\n\n增长趋势"),
        (2, "表 1\n\n|A|B|\n\n注释"),
    ]


def test_utf8_truncation_does_not_split_multibyte_character() -> None:
    value = "中" * 400
    truncated = _truncate_utf8(value, 1024)

    assert len(truncated.encode("utf-8")) <= 1024
    assert truncated == "中" * 341
