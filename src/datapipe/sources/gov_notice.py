"""政务公告类站点的示例适配器。

页面结构是政务网站最常见的"两段式"布局：

* 列表页：``ul.notice-list > li.notice-item``，每项含标题链接与日期/地区/分类；
* 详情页：``h1.article-title`` + ``div.article-meta``（日期、来源、地区、分类）
  + ``div.article-content`` 正文。

所有选择器都从 ``SourceConfig.selectors`` 读取，因此同一份 Python 代码
可以服务所有结构相同的站点——换站点只改 YAML。
"""

from __future__ import annotations

from urllib.parse import urljoin

import lxml.html

from datapipe.models import Announcement, ItemRef
from datapipe.sources.base import (
    ParseError,
    SourceAdapter,
    first_node,
    node_text,
    parse_html,
)

__all__ = ["GovNoticeAdapter"]

#: 缺失选择器时的兜底默认值，保证不配 selectors 也能跑通示例源
DEFAULT_SELECTORS: dict[str, str] = {
    "list_item": ".//ul[contains(@class,'notice-list')]/li[contains(@class,'notice-item')]",
    "list_link": ".//a[contains(@class,'title')]",
    "list_date": ".//span[contains(@class,'date')]",
    "list_region": ".//span[contains(@class,'region')]",
    "list_category": ".//span[contains(@class,'category')]",
    "detail_title": ".//h1[contains(@class,'article-title')]",
    "detail_date": ".//div[contains(@class,'article-meta')]//span[contains(@class,'date')]",
    "detail_source": ".//div[contains(@class,'article-meta')]//span[contains(@class,'source')]",
    "detail_region": ".//div[contains(@class,'article-meta')]//span[contains(@class,'region')]",
    "detail_category": ".//div[contains(@class,'article-meta')]//span[contains(@class,'category')]",
    "detail_content": ".//div[contains(@class,'article-content')]",
}


class GovNoticeAdapter(SourceAdapter):
    """政务通知公告适配器。"""

    name = "gov_notice"

    # -- 内部工具 ---------------------------------------------------------
    def _selector(self, key: str) -> str:
        """取选择器：配置优先，缺省回落到内置默认值。"""
        return self.config.selectors.get(key) or DEFAULT_SELECTORS[key]

    def _text_of(self, node: lxml.html.HtmlElement, key: str) -> str:
        """在给定节点内按选择器取纯文本，缺失返回空串（非关键字段容忍缺失）。"""
        nodes = node.xpath(self._selector(key))
        if not nodes:
            return ""
        return node_text(nodes[0])

    # -- 解析 -------------------------------------------------------------
    def parse_list(self, html: str, url: str) -> list[ItemRef]:
        """解析列表页。

        单条目的标题链接或日期缺失即视为结构异常：政务公告列表里
        "没有标题的条目"几乎一定是选择器失效，继续下去只会入库脏数据。
        地区、分类属于可选字段，缺失时留空。
        """
        tree = parse_html(html, url)
        items = tree.xpath(self._selector("list_item"))
        if not items:
            raise ParseError(
                f"未匹配到列表项（xpath={self._selector('list_item')}）",
                url,
            )

        refs: list[ItemRef] = []
        for index, item in enumerate(items):
            links = item.xpath(self._selector("list_link"))
            if not links:
                raise ParseError(f"第 {index + 1} 个列表项缺少标题链接", url)

            anchor = links[0]
            href = (anchor.get("href") or "").strip()
            title = node_text(anchor)
            if not href:
                raise ParseError(f"第 {index + 1} 个列表项 href 为空", url)
            if not title:
                raise ParseError(f"第 {index + 1} 个列表项标题为空", url)

            refs.append(
                ItemRef(
                    detail_url=urljoin(url, href),
                    title=title,
                    publish_date=self._text_of(item, "list_date"),
                    region=self._text_of(item, "list_region"),
                    category=self._text_of(item, "list_category"),
                    list_url=url,
                )
            )
        return refs

    def parse_detail(self, html: str, url: str) -> Announcement:
        """解析详情页。

        标题是硬性要求（没有标题的记录没有检索价值，直接进待复查队列）；
        正文允许为空——有些公告正文以附件形式发布，此时保留标题与元信息
        比丢弃整条更有价值。
        """
        tree = parse_html(html, url)
        title_node = first_node(tree, self._selector("detail_title"), what="详情标题", url=url)
        title = node_text(title_node)
        if not title:
            raise ParseError("详情标题为空", url)

        content_node = tree.xpath(self._selector("detail_content"))
        content = node_text(content_node[0]) if content_node else ""

        return Announcement(
            title=title,
            publish_date=self._text_of(tree, "detail_date"),
            region=self._text_of(tree, "detail_region"),
            category=self._text_of(tree, "detail_category"),
            content=content,
            detail_url=url,
            source_name=self.source_name or self._text_of(tree, "detail_source"),
            list_url="",
        )
