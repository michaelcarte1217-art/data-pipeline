"""适配器解析测试：证明能正确抽取字段，且结构异常时明确报错。"""

from __future__ import annotations

import pytest

from datapipe.models import Announcement, ItemRef
from datapipe.sources import ParseError, get_adapter
from datapipe.sources.gov_notice import GovNoticeAdapter


@pytest.fixture()
def adapter(source_config) -> GovNoticeAdapter:
    return get_adapter("gov_notice")(source_config)


# -- 列表页 ----------------------------------------------------------------
def test_parse_list_returns_all_items(adapter: GovNoticeAdapter, list_html: str) -> None:
    url = "https://notice.example.gov.cn/notice/list_page_1.html"
    refs = adapter.parse_list(list_html, url)

    assert len(refs) == 3
    assert all(isinstance(ref, ItemRef) for ref in refs)


def test_parse_list_extracts_fields(adapter: GovNoticeAdapter, list_html: str) -> None:
    url = "https://notice.example.gov.cn/notice/list_page_1.html"
    first = adapter.parse_list(list_html, url)[0]

    assert first.title == "关于2026年度数据平台运维服务采购的公告"
    assert first.publish_date == "2026-10-20"
    assert first.region == "示例省"
    assert first.category == "采购公告"


def test_parse_list_resolves_relative_url(adapter: GovNoticeAdapter, list_html: str) -> None:
    """列表页 href 是相对路径，必须用 urljoin 补成绝对地址。"""
    url = "https://notice.example.gov.cn/notice/list_page_1.html"
    first = adapter.parse_list(list_html, url)[0]

    assert first.detail_url == "https://notice.example.gov.cn/notice/detail_001.html"
    assert first.list_url == url


def test_parse_list_keeps_source_order(adapter: GovNoticeAdapter, list_html: str) -> None:
    url = "https://notice.example.gov.cn/notice/list_page_1.html"
    titles = [ref.title for ref in adapter.parse_list(list_html, url)]

    assert titles[0].startswith("关于2026年度数据平台")
    assert titles[1].startswith("关于开展政务数据质量")
    assert titles[2].startswith("关于信息系统安全测评")


@pytest.mark.parametrize(
    "html",
    [
        "<html><body><p>没有任何列表结构</p></body></html>",
        "<html><body><ul class='notice-list'></ul></body></html>",
    ],
)
def test_parse_list_raises_when_no_items(adapter: GovNoticeAdapter, html: str) -> None:
    """列表结构缺失必须抛 ParseError，而不是返回空列表。

    返回空列表会让"站点改版"伪装成"今天没有新公告"，是最难排查的故障。
    """
    with pytest.raises(ParseError):
        adapter.parse_list(html, "https://example.test/list")


def test_parse_list_raises_when_href_missing(adapter: GovNoticeAdapter) -> None:
    html = """
    <html><body>
      <ul class="notice-list">
        <li class="notice-item"><a class="title">没有 href 的条目</a></li>
      </ul>
    </body></html>
    """
    with pytest.raises(ParseError, match="href"):
        adapter.parse_list(html, "https://example.test/list")


def test_parse_list_raises_on_empty_html(adapter: GovNoticeAdapter) -> None:
    with pytest.raises(ParseError):
        adapter.parse_list("", "https://example.test/list")


# -- 详情页 ----------------------------------------------------------------
def test_parse_detail_extracts_fields(adapter: GovNoticeAdapter, detail_html: str) -> None:
    url = "https://notice.example.gov.cn/notice/detail_001.html"
    record = adapter.parse_detail(detail_html, url)

    assert isinstance(record, Announcement)
    assert record.title == "关于2026年度数据平台运维服务采购的公告"
    assert record.publish_date == "2026-10-20"
    assert record.region == "示例省"
    assert record.category == "采购公告"
    assert record.detail_url == url
    assert "数据平台稳定运行" in record.content


def test_parse_detail_content_is_plain_text(adapter: GovNoticeAdapter, detail_html: str) -> None:
    """正文必须已去除标签，不能残留 ``<p>``。"""
    record = adapter.parse_detail(detail_html, "https://example.test/d")
    assert "<p>" not in record.content
    assert "</p>" not in record.content


def test_parse_detail_raises_without_title(adapter: GovNoticeAdapter) -> None:
    html = """
    <html><body>
      <div class="article-content"><p>只有正文，没有标题</p></div>
    </body></html>
    """
    with pytest.raises(ParseError, match="详情标题"):
        adapter.parse_detail(html, "https://example.test/d")


def test_parse_detail_allows_empty_content(adapter: GovNoticeAdapter) -> None:
    """正文可缺失（如以附件发布），此时保留标题与元信息比丢弃整条更有价值。"""
    html = """
    <html><body>
      <h1 class="article-title">只有标题的公告</h1>
      <div class="article-meta"><span class="date">2026-10-20</span></div>
    </body></html>
    """
    record = adapter.parse_detail(html, "https://example.test/d")

    assert record.title == "只有标题的公告"
    assert record.content == ""


def test_unknown_adapter_name_raises() -> None:
    with pytest.raises(ValueError, match="未注册的适配器"):
        get_adapter("does_not_exist")


def test_adapter_registered() -> None:
    assert "gov_notice" in __import__("datapipe.sources", fromlist=["ADAPTERS"]).ADAPTERS
