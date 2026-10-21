"""清洗层测试：日期标准化、文本净化、标题去重、缺失统计。"""

from __future__ import annotations

import pandas as pd
import pytest

from datapipe.clean import clean_records, normalize_date, strip_markup, summarize


# -- 日期标准化 ------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-20", "2026-10-20"),
        ("2026/10/20", "2026-10-20"),
        ("2026.10.20", "2026-10-20"),
        ("2026年10月20日", "2026-10-20"),
        ("2026年10月20", "2026-10-20"),
        ("2026-10-20 14:35", "2026-10-20"),
        ("2026-10-20 14:35:08", "2026-10-20"),
        ("2026/10/20 09:00", "2026-10-20"),
        ("20261020", "2026-10-20"),
        ("2026-10-20（星期二）", "2026-10-20"),
        ("  2026-10-20  ", "2026-10-20"),
    ],
)
def test_normalize_date_formats(raw: str, expected: str) -> None:
    assert normalize_date(raw) == expected


@pytest.mark.parametrize("raw", ["", "另行通知", "不详", "N/A", "0000-00-00", "abc"])
def test_normalize_date_invalid_returns_empty(raw: str) -> None:
    assert normalize_date(raw) == ""


def test_normalize_date_rejects_out_of_range_year() -> None:
    """明显不合理的年份（如 1900 年前）不应放行。"""
    assert normalize_date("1899-01-01") == ""


# -- 文本净化 --------------------------------------------------------------
def test_strip_markup_removes_tags() -> None:
    assert strip_markup("<p>某公告</p>") == "某公告"


def test_strip_markup_collapses_whitespace() -> None:
    assert strip_markup("某\u3000 公告\n\n正文") == "某 公告 正文"


def test_strip_markup_handles_empty() -> None:
    assert strip_markup("") == ""


def test_strip_markup_handles_nested_tags() -> None:
    assert strip_markup("<div><p>甲</p><p>乙</p></div>") == "甲 乙"


# -- 整体清洗 --------------------------------------------------------------
def _rows() -> list[dict]:
    return [
        {
            "title": "关于A事项的公告",
            "publish_date": "2026年10月20日",
            "region": "甲省",
            "category": "通知",
            "source_name": "站点一",
            "content": "<p>正文甲</p>",
            "url": "https://example.test/1",
            "content_hash": "h1",
        },
        {
            "title": "关于B事项的公告",
            "publish_date": "2026/10/19",
            "region": "乙省",
            "category": "公示",
            "source_name": "站点一",
            "content": "正文乙",
            "url": "https://example.test/2",
            "content_hash": "h2",
        },
        {
            # 标题+日期与第一条相同 → 应被判定重复
            "title": "关于A事项的公告",
            "publish_date": "2026-10-20",
            "region": "甲省",
            "category": "通知",
            "source_name": "站点二",
            "content": "转载的正文甲，措辞略有不同",
            "url": "https://example.test/3",
            "content_hash": "h3",
        },
    ]


def test_clean_records_dedupes_by_title_and_date() -> None:
    frame, report = clean_records(_rows())

    assert report.total == 3
    assert report.kept == 2
    assert report.duplicates == 1
    assert len(frame) == 2


def test_clean_records_keeps_first_occurrence() -> None:
    frame, _ = clean_records(_rows())

    assert frame.iloc[0]["url"] == "https://example.test/1"
    assert frame.iloc[1]["url"] == "https://example.test/2"


def test_clean_records_normalizes_all_dates() -> None:
    frame, _ = clean_records(_rows())

    assert list(frame["publish_date"]) == ["2026-10-20", "2026-10-19"]


def test_clean_records_preserves_raw_date() -> None:
    """原始日期必须留档，便于复查时对照。"""
    frame, _ = clean_records(_rows())

    assert frame.iloc[0]["publish_date_raw"] == "2026年10月20日"


def test_clean_records_strips_markup_from_content() -> None:
    frame, _ = clean_records(_rows())

    assert frame.iloc[0]["content"] == "正文甲"


def test_clean_records_reports_date_failures() -> None:
    rows = _rows()
    rows.append(
        {
            "title": "日期不明的公告",
            "publish_date": "另行通知",
            "region": "",
            "category": "",
            "source_name": "站点一",
            "content": "正文",
            "url": "https://example.test/4",
            "content_hash": "h4",
        }
    )
    _, report = clean_records(rows)

    assert report.date_failed == 1
    assert report.date_failures[0]["url"] == "https://example.test/4"
    assert report.date_failures[0]["raw"] == "另行通知"


def test_clean_records_counts_missing_fields() -> None:
    rows = [
        {
            "title": "只有标题",
            "publish_date": "",
            "region": "",
            "category": "",
            "source_name": "站点一",
            "content": "",
            "url": "https://example.test/5",
            "content_hash": "h5",
        }
    ]
    _, report = clean_records(rows)

    assert report.missing["publish_date"] == 1
    assert report.missing["region"] == 1
    assert report.missing["content"] == 1


def test_clean_records_drops_blank_title() -> None:
    rows = _rows()
    rows.append(
        {
            "title": "   ",
            "publish_date": "2026-10-18",
            "region": "",
            "category": "",
            "source_name": "站点一",
            "content": "无标题的记录",
            "url": "https://example.test/6",
            "content_hash": "h6",
        }
    )
    frame, _ = clean_records(rows)

    assert "   " not in frame["title"].tolist()
    assert len(frame) == 2


def test_clean_records_empty_input() -> None:
    frame, report = clean_records([])

    assert frame.empty
    assert report.total == 0
    assert report.kept == 0


def test_clean_records_no_nan_leaks() -> None:
    """空值必须是空串，不能变成字符串 "nan" —— pandas 3 的 str dtype 常见坑。"""
    rows = [
        {
            "title": "某公告",
            "publish_date": "",
            "region": None,
            "category": None,
            "source_name": "站点",
            "content": None,
            "url": "https://example.test/7",
            "content_hash": "h7",
        }
    ]
    frame, _ = clean_records(rows)

    for column in ("publish_date", "region", "category", "content"):
        value = frame.iloc[0][column]
        assert value == "", f"{column} 应为空串，实际 {value!r}"
        assert isinstance(value, str)


# -- 聚合 ------------------------------------------------------------------
def test_summarize_groups_by_dimensions() -> None:
    frame, _ = clean_records(_rows())
    summary = summarize(frame)

    assert {item["name"] for item in summary["by_source"]} == {"站点一"}
    assert len(summary["by_date"]) == 2
    assert {item["name"] for item in summary["by_category"]} == {"通知", "公示"}


def test_summarize_empty_frame() -> None:
    summary = summarize(pd.DataFrame())

    assert summary == {
        "by_source": [],
        "by_date": [],
        "by_category": [],
        "by_region": [],
    }


def test_summarize_skips_blank_categories() -> None:
    rows = [
        {
            "title": "甲",
            "publish_date": "2026-10-20",
            "region": "",
            "category": "",
            "source_name": "站点",
            "content": "x",
            "url": "https://example.test/a",
            "content_hash": "ha",
        }
    ]
    frame, _ = clean_records(rows)
    summary = summarize(frame)

    assert summary["by_category"] == []
