"""核心数据模型与哈希工具。

本模块刻意保持零第三方依赖：只做数据结构与指纹计算，
便于 fetch / storage / clean / report 各层共享，也便于单测直接导入。

约定
----
* 所有对外字段一律使用 ``str``，缺失值用空字符串而不是 ``None``，
  避免下游 pandas / sqlite 里出现 ``"None"``、``"nan"`` 这类脏值。
* 哈希前统一做空白归一化，防止仅靠换行、缩进、全角空格差异就被判成新内容。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ItemRef",
    "Announcement",
    "FetchResult",
    "RunStats",
    "normalize_for_hash",
    "sha256_text",
    "record_hash",
]

# 空白、全角空格、零宽字符、BOM：哈希前全部丢弃
_BLANK = re.compile(r"[\s\u3000\u200b\u200c\u200d\ufeff]+")


def normalize_for_hash(text: str | None) -> str:
    """移除全部空白类字符，用于计算与排版无关的内容指纹。"""
    if not text:
        return ""
    return _BLANK.sub("", str(text))


def sha256_text(text: str) -> str:
    """返回文本的 sha256 十六进制摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_hash(title: str, publish_date: str, content: str) -> str:
    """记录级指纹：标题 + 发布日期 + 正文。

    三者共同决定"同一条公告"，避免不同站点转载时标题雷同被误判。
    使用 ``\\x1f``（单元分隔符）拼接，防止字段边界被字符串内容伪造。
    """
    payload = "\x1f".join(
        (
            normalize_for_hash(title),
            (publish_date or "").strip(),
            normalize_for_hash(content),
        )
    )
    return sha256_text(payload)


@dataclass(slots=True)
class ItemRef:
    """列表页条目的轻量引用。

    列表页通常只暴露标题、日期等摘要字段，正文必须回源详情页，
    因此这里只携带"去哪儿取"以及可直接展示的摘要信息。
    """

    detail_url: str
    title: str = ""
    publish_date: str = ""
    region: str = ""
    category: str = ""
    list_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "detail_url": self.detail_url,
            "title": self.title,
            "publish_date": self.publish_date,
            "region": self.region,
            "category": self.category,
            "list_url": self.list_url,
        }


@dataclass(slots=True)
class Announcement:
    """一条完整的公告记录，对应 ``records`` 表一行。"""

    title: str
    publish_date: str = ""
    region: str = ""
    category: str = ""
    content: str = ""
    detail_url: str = ""
    source_name: str = ""
    list_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """内容指纹，写入 ``records.content_hash`` 并参与去重。"""
        return record_hash(self.title, self.publish_date, self.content)

    def to_row(self) -> dict[str, Any]:
        """转为可直接交给 storage / pandas 的扁平字典。"""
        return {
            "url": self.detail_url,
            "content_hash": self.fingerprint(),
            "title": self.title,
            "publish_date": self.publish_date,
            "region": self.region,
            "category": self.category,
            "source_name": self.source_name,
            "content": self.content,
        }


@dataclass(slots=True)
class FetchResult:
    """一次抓取的完整结果。

    之所以不直接返回 ``str``：上层需要 ``status`` 判断 429/5xx，
    需要 ``attempts`` 与 ``elapsed_ms`` 汇总进报表，需要 ``from_cache``
    区分命中缓存与真实请求。
    """

    url: str
    status: int = 0
    text: str = ""
    elapsed_ms: int = 0
    attempts: int = 1
    from_cache: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        """是否拿到可用于解析的响应体。"""
        return self.error is None and 200 <= self.status < 300 and bool(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "attempts": self.attempts,
            "from_cache": self.from_cache,
            "error": self.error,
        }


@dataclass(slots=True)
class RunStats:
    """单次运行（可跨多个源）的累计统计，最终落到 ``jobs.stats_json``。"""

    source: str = ""
    pages_total: int = 0
    pages_fetched: int = 0
    pages_skipped: int = 0
    items_seen: int = 0
    records_new: int = 0
    records_duplicate: int = 0
    records_failed: int = 0
    review_pushed: int = 0
    http_errors: int = 0
    rate_limited: int = 0
    elapsed_s: float = 0.0

    #: 计数型字段，merge 时逐项相加
    _COUNTERS = (
        "pages_total",
        "pages_fetched",
        "pages_skipped",
        "items_seen",
        "records_new",
        "records_duplicate",
        "records_failed",
        "review_pushed",
        "http_errors",
        "rate_limited",
    )

    def merge(self, other: "RunStats") -> None:
        """并入另一个统计对象（多源或多阶段汇总时使用）。"""
        for name in self._COUNTERS:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.elapsed_s += other.elapsed_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "pages_total": self.pages_total,
            "pages_fetched": self.pages_fetched,
            "pages_skipped": self.pages_skipped,
            "items_seen": self.items_seen,
            "records_new": self.records_new,
            "records_duplicate": self.records_duplicate,
            "records_failed": self.records_failed,
            "review_pushed": self.review_pushed,
            "http_errors": self.http_errors,
            "rate_limited": self.rate_limited,
            "elapsed_s": round(self.elapsed_s, 3),
        }
