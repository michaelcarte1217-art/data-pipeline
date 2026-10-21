"""清洗层：把原始记录规整成可分析的表格。

职责边界很清楚——只做"格式统一"与"重复剔除"，不做业务判断：

* 日期统一为 ``YYYY-MM-DD``，解析失败的**不丢弃**，返回失败清单交给
  编排层写入 ``review_queue``，原始值同时保留在 ``publish_date_raw``；
* 文本去标签、压空白、全角空格归一；
* 同一「标题 + 日期」只保留一条（不同来源转载同一公告的常见情形）。

pandas 3.x 的字符串列默认是 ``str`` dtype 且用 ``NaN`` 表示缺失，
直接写库会得到字面量 ``"nan"``。因此这里统一 ``fillna("")`` 后再
``astype(object)``，保证下游拿到的永远是干净的字符串。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import pandas as pd

__all__ = [
    "DATE_PATTERNS",
    "CleanReport",
    "normalize_date",
    "strip_markup",
    "clean_records",
    "summarize",
]

#: 按优先级尝试的日期格式。``%Y年%m月%d日`` 一类中文格式在政务站点极常见。
#: 带时间的变体必须列全：``2026/10/20 09:00`` 这类"斜杠+时分"写法在
#: 列表页里出现频率很高，漏掉会导致一批记录被判为日期异常。
DATE_PATTERNS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y年%m月%d日",
    "%Y年%m月%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y%m%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[\s\u3000\u200b\u200c\u200d\ufeff]+")
#: 形如 "2026年10月20日 14:35" 里夹带的非日期尾巴，先砍掉再解析
_TRAILING = re.compile(r"[（(].*?[)）]\s*$")


def strip_markup(text: str) -> str:
    """去掉 HTML 标签并压缩空白。

    >>> strip_markup('<p>某  公告</p>')
    '某 公告'
    """
    if not text:
        return ""
    plain = _TAG.sub(" ", str(text))
    return _WS.sub(" ", plain).strip()


def normalize_date(raw: str) -> str:
    """把各种写法的日期统一成 ``YYYY-MM-DD``；无法识别返回空串。

    >>> normalize_date('2026年10月20日')
    '2026-10-20'
    >>> normalize_date('2026/10/20 14:35')
    '2026-10-20'
    >>> normalize_date('不详')
    ''
    """
    if not raw:
        return ""
    candidate = _WS.sub(" ", str(raw)).strip()
    candidate = _TRAILING.sub("", candidate).strip()
    if not candidate:
        return ""

    for pattern in DATE_PATTERNS:
        try:
            parsed = pd.to_datetime(candidate, format=pattern)
        except (ValueError, TypeError):
            continue
        if pd.isna(parsed):
            continue
        # 过滤明显不合理的年份，避免把 "0000-00-00" 之类脏值放行
        if not 1970 <= parsed.year <= 2999:
            continue
        return parsed.strftime("%Y-%m-%d")
    return ""


@dataclass(slots=True)
class CleanReport:
    """一次清洗的结果与统计，直接用于控制台摘要与报表页。"""

    total: int = 0
    kept: int = 0
    duplicates: int = 0
    date_failed: int = 0
    missing: dict[str, int] = field(default_factory=dict)
    date_failures: list[dict[str, str]] = field(default_factory=list)
    duplicate_titles: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "kept": self.kept,
            "duplicates": self.duplicates,
            "date_failed": self.date_failed,
            "missing": dict(self.missing),
        }


#: 参与缺失率统计的字段
WATCHED_FIELDS: tuple[str, ...] = ("publish_date", "region", "category", "content")


def clean_records(rows: Iterable[dict[str, Any]]) -> tuple[pd.DataFrame, CleanReport]:
    """清洗原始记录，返回 ``(清洗后的 DataFrame, 清洗报告)``。

    DataFrame 列（固定顺序）：
        title, publish_date, publish_date_raw, region, category,
        source_name, content, url, content_hash
    """
    frame = pd.DataFrame(list(rows))
    report = CleanReport()

    if frame.empty:
        return _empty_frame(), report

    report.total = len(frame)

    text_columns = ["title", "publish_date", "region", "category", "source_name", "content", "url"]
    for column in text_columns:
        if column not in frame.columns:
            frame[column] = ""
        frame[column] = frame[column].fillna("").astype(str).map(strip_markup)

    # 原始日期留档，便于复查时对照
    frame["publish_date_raw"] = frame["publish_date"]

    normalized: list[str] = []
    failures: list[dict[str, str]] = []
    for index, raw in enumerate(frame["publish_date"]):
        value = normalize_date(raw)
        if not value and str(raw).strip():
            failures.append(
                {
                    "url": str(frame.at[index, "url"]),
                    "raw": str(raw),
                    "title": str(frame.at[index, "title"]),
                }
            )
        normalized.append(value)
    frame["publish_date"] = normalized
    report.date_failed = len(failures)
    report.date_failures = failures

    # 缺失率：空串即缺失
    report.missing = {
        column: int((frame[column] == "").sum())
        for column in WATCHED_FIELDS
        if column in frame.columns
    }

    # 标题 + 日期去重；日期为空时退回仅用标题，避免把真重复当成不同记录
    dedup_key = frame["title"] + "\x1f" + frame["publish_date"]
    blank_date = frame["publish_date"] == ""
    if bool(blank_date.any()):
        dedup_key = dedup_key.where(~blank_date, frame["title"])
    duplicated = dedup_key.duplicated(keep="first")
    report.duplicates = int(duplicated.sum())
    if report.duplicates:
        report.duplicate_titles = (
            frame.loc[duplicated, "title"].astype(str).head(20).tolist()
        )
    frame = frame.loc[~duplicated].reset_index(drop=True)

    # 空标题的行没有检索价值，剔除并计入缺失
    blank_title = frame["title"] == ""
    if bool(blank_title.any()):
        removed = int(blank_title.sum())
        report.missing["title"] = report.missing.get("title", 0) + removed
        frame = frame.loc[~blank_title].reset_index(drop=True)

    if "content_hash" not in frame.columns:
        frame["content_hash"] = ""
    frame["content_hash"] = frame["content_hash"].fillna("").astype(str)

    ordered = [
        "title",
        "publish_date",
        "publish_date_raw",
        "region",
        "category",
        "source_name",
        "content",
        "url",
        "content_hash",
    ]
    for column in ordered:
        if column not in frame.columns:
            frame[column] = ""
    frame = frame[ordered]

    # pandas 3 的 str dtype 会把空值重新变回 NaN，落库前强制成 object
    frame = frame.astype(object).where(frame.notna(), "")
    report.kept = len(frame)
    return frame, report


def _empty_frame() -> pd.DataFrame:
    columns = [
        "title",
        "publish_date",
        "publish_date_raw",
        "region",
        "category",
        "source_name",
        "content",
        "url",
        "content_hash",
    ]
    return pd.DataFrame({column: pd.Series(dtype=object) for column in columns})


def summarize(frame: pd.DataFrame) -> dict[str, Any]:
    """生成报表用的聚合数据：按来源、按日期、按分类、按地区。"""
    if frame.empty:
        return {
            "by_source": [],
            "by_date": [],
            "by_category": [],
            "by_region": [],
        }

    def _counts(column: str, limit: int = 20) -> list[dict[str, Any]]:
        series = frame[column].fillna("").astype(str)
        series = series[series != ""]
        if series.empty:
            return []
        counts = series.value_counts().head(limit)
        return [{"name": str(name), "value": int(value)} for name, value in counts.items()]

    by_date: list[dict[str, Any]] = []
    dated = frame[frame["publish_date"] != ""]
    if not dated.empty:
        group = dated.groupby("publish_date").size().sort_index()
        by_date = [{"name": str(name), "value": int(value)} for name, value in group.items()]

    return {
        "by_source": _counts("source_name"),
        "by_date": by_date,
        "by_category": _counts("category"),
        "by_region": _counts("region"),
    }
