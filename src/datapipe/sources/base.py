"""源适配器的抽象基类与解析异常。

适配器是"站点结构"与"统一数据模型"之间唯一的耦合点。
它的职责被刻意压到最小：把 HTML 变成 :class:`ItemRef` / :class:`Announcement`，
不做去重、不做入库、不做清洗——那些属于编排层与清洗层。

失败语义
--------
解析失败必须抛 :class:`ParseError`。静默返回空列表会让"站点改版"
伪装成"今天没有新公告"，这是数据管道里最难排查的一类故障；
抛异常后由编排层写入 ``review_queue``，问题会显式暴露出来。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import ClassVar

import lxml.html

from datapipe.config import SourceConfig
from datapipe.models import Announcement, ItemRef

__all__ = ["ParseError", "SourceAdapter", "parse_html", "node_text", "first_node", "snippet"]

_WS = re.compile(r"[\s\u3000\u200b\u200c\u200d\ufeff]+")


class ParseError(Exception):
    """页面结构与预期不符，无法可靠提取字段。"""

    def __init__(self, message: str, url: str = "") -> None:
        super().__init__(f"{url} -> {message}" if url else message)
        self.message = message
        self.url = url


def parse_html(html: str, url: str = "") -> lxml.html.HtmlElement:
    """把 HTML 解析成 lxml 文档树。

    空文档、纯二进制、严重截断都会抛 :class:`ParseError`，
    而不是让 ``lxml`` 的 ``ParserError`` 泄漏到调用方。
    """
    if not html or not html.strip():
        raise ParseError("响应体为空", url)
    try:
        return lxml.html.document_fromstring(html)
    except (lxml.etree.ParserError, ValueError) as exc:
        raise ParseError(f"HTML 解析失败: {exc}", url) from exc


def first_node(tree: lxml.html.HtmlElement, xpath: str, *, what: str, url: str = "") -> lxml.html.HtmlElement:
    """按 XPath 取首个匹配节点，缺失即抛错。"""
    nodes = tree.xpath(xpath)
    if not nodes:
        raise ParseError(f"未匹配到{what}（xpath={xpath}）", url)
    return nodes[0]


def node_text(node: lxml.html.HtmlElement | None) -> str:
    """取节点纯文本并压缩空白；节点为空返回空串。"""
    if node is None:
        return ""
    return _WS.sub(" ", node.text_content()).strip()


def snippet(html: str, limit: int = 300) -> str:
    """截取一段人类可读的页面摘要，用于待复查队列定位问题。"""
    if not html:
        return ""
    text = _WS.sub(" ", re.sub(r"<[^>]+>", " ", html)).strip()
    return text[:limit]


class SourceAdapter(ABC):
    """所有数据源适配器的基类。

    子类需要提供 ``name``（与 YAML 中的 ``adapter`` 字段对应）并实现三个方法。
    """

    #: 适配器标识，注册表用它索引
    name: ClassVar[str] = ""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.__dict__.get("name"):
            raise TypeError(f"{cls.__name__} 必须定义非空类属性 name")

    # -- 元信息 -----------------------------------------------------------
    @property
    def source_name(self) -> str:
        """数据源名称，最终写入记录的 ``source_name`` 字段。"""
        return self.config.name

    @property
    def base_url(self) -> str:
        return self.config.base_url

    # -- 三个核心方法 -----------------------------------------------------
    def build_list_urls(self, page_count: int) -> list[str]:
        """生成前 ``page_count`` 个列表页地址。

        默认实现直接复用配置里的 ``list_url`` 模板；只有分页规则特殊
        （例如游标式翻页）的站点才需要覆盖此方法。
        """
        return self.config.page_urls(page_count)

    @abstractmethod
    def parse_list(self, html: str, url: str) -> list[ItemRef]:
        """解析列表页，返回条目引用列表。

        Raises
        ------
        ParseError
            页面结构不符合预期，或存在无法定位详情链接的条目。
        """

    @abstractmethod
    def parse_detail(self, html: str, url: str) -> Announcement:
        """解析详情页，返回完整公告。

        Raises
        ------
        ParseError
            标题/正文等关键字段缺失。
        """

    def __repr__(self) -> str:  # pragma: no cover - 仅便于调试
        return f"<{type(self).__name__} source={self.config.name!r}>"
