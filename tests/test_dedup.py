"""去重与存储测试：URL 去重、内容指纹去重、待复查队列、断点续跑状态。"""

from __future__ import annotations

import pytest

from datapipe.models import Announcement, ItemRef, record_hash
from datapipe.pipeline import _dedupe_refs
from datapipe.storage import Storage


@pytest.fixture()
def storage(tmp_path) -> Storage:
    store = Storage(tmp_path / "test.db")
    yield store
    store.close()


def make_record(title: str, date: str, content: str = "正文") -> Announcement:
    return Announcement(
        title=title,
        publish_date=date,
        content=content,
        detail_url=f"https://example.test/{abs(hash(title)) % 10000}",
        source_name="unit_test",
    )


# -- 内容指纹 --------------------------------------------------------------
def test_record_hash_ignores_whitespace() -> None:
    """仅排版差异（换行、缩进、全角空格）不应产生新指纹。"""
    first = record_hash("某 公告", "2026-10-20", "正文  内容")
    second = record_hash("某\u3000公告", "2026-10-20", "正文\n\t内容")

    assert first == second


def test_record_hash_differs_on_content() -> None:
    assert record_hash("标题", "2026-10-20", "甲") != record_hash("标题", "2026-10-20", "乙")


def test_record_hash_differs_on_date() -> None:
    assert record_hash("标题", "2026-10-20", "正文") != record_hash("标题", "2026-10-21", "正文")


# -- 内容级去重 ------------------------------------------------------------
def test_save_record_returns_true_for_new(storage: Storage) -> None:
    assert storage.save_record(make_record("新公告", "2026-10-20")) is True
    assert storage.count_records() == 1


def test_save_record_returns_false_for_duplicate(storage: Storage) -> None:
    record = make_record("重复公告", "2026-10-20", "同一份正文")

    assert storage.save_record(record) is True
    # 第二次写入同一内容指纹：唯一索引挡住，返回 False 而非抛异常
    assert storage.save_record(record) is False
    assert storage.count_records() == 1


def test_is_duplicate(storage: Storage) -> None:
    record = make_record("某公告", "2026-10-20")
    storage.save_record(record)

    assert storage.is_duplicate(record.fingerprint()) is True
    assert storage.is_duplicate("不存在的指纹") is False


def test_save_records_batch_counts(tmp_path) -> None:
    store = Storage(tmp_path / "batch.db")
    try:
        records = [
            make_record("甲", "2026-10-20"),
            make_record("乙", "2026-10-20"),
            make_record("甲", "2026-10-20"),  # 与第一条完全相同
        ]
        new_count, dup_count = store.save_records(records)

        assert (new_count, dup_count) == (2, 1)
    finally:
        store.close()


# -- 列表页引用去重 --------------------------------------------------------
def test_dedupe_refs_by_url() -> None:
    """同一公告在多个列表页出现时，详情页只应请求一次。"""
    refs = [
        ItemRef(detail_url="https://example.test/a"),
        ItemRef(detail_url="https://example.test/b"),
        ItemRef(detail_url="https://example.test/a"),
    ]
    unique = _dedupe_refs(refs)

    assert len(unique) == 2
    assert [ref.detail_url for ref in unique] == [
        "https://example.test/a",
        "https://example.test/b",
    ]


def test_dedupe_refs_preserves_order() -> None:
    refs = [ItemRef(detail_url=f"https://example.test/{i}") for i in (3, 1, 2, 1)]
    assert [ref.detail_url[-1] for ref in _dedupe_refs(refs)] == ["3", "1", "2"]


# -- 待复查队列 ------------------------------------------------------------
def test_push_review_records_entry(storage: Storage) -> None:
    storage.push_review("https://example.test/bad", "标题缺失", "<html>原文片段</html>")

    items = storage.fetch_review()
    assert len(items) == 1
    assert items[0]["reason"] == "标题缺失"
    assert "原文片段" in items[0]["snippet"]
    assert storage.count_review() == 1


def test_push_review_truncates_long_snippet(storage: Storage) -> None:
    storage.push_review("https://example.test/x", "解析失败", "字" * 2000)

    items = storage.fetch_review()
    assert len(items[0]["snippet"]) <= 500


def test_push_review_accumulates(storage: Storage) -> None:
    storage.push_review("https://example.test/x", "第一次")
    storage.push_review("https://example.test/x", "第二次")

    assert storage.count_review() == 2


# -- 断点续跑状态 ----------------------------------------------------------
def test_page_mark_and_query(storage: Storage) -> None:
    url = "https://example.test/detail/1"
    assert storage.is_page_fetched(url) is False

    storage.mark_page(url, status="ok", content_hash="abc")
    assert storage.is_page_fetched(url) is True


def test_parse_error_page_is_retried(storage: Storage) -> None:
    """解析失败的页不算"已抓取"，下次运行仍会重试。"""
    url = "https://example.test/detail/2"
    storage.mark_page(url, status="parse_error", error="标题缺失")

    assert storage.is_page_fetched(url) is False


def test_job_pages_done_and_pending(storage: Storage) -> None:
    storage.mark_page_done("job-1", 1, "https://example.test/p1")
    storage.mark_page_done("job-1", 2, "https://example.test/p2", error="超时")

    assert storage.done_pages("job-1") == {1}
    assert storage.pending_pages("job-1") == {2}


def test_job_pages_isolation_between_jobs(storage: Storage) -> None:
    storage.mark_page_done("job-a", 1, "https://example.test/p1")

    assert storage.done_pages("job-a") == {1}
    assert storage.done_pages("job-b") == set()


def test_finish_job_persists_stats(storage: Storage) -> None:
    from datapipe.models import RunStats

    storage.start_job("job-2", "unit_test")
    stats = RunStats(source="unit_test", records_new=3)
    storage.finish_job("job-2", stats)

    summary = storage.job_summary("job-2")
    assert summary is not None
    assert summary["status"] == "ok"
    assert '"records_new": 3' in summary["stats_json"]


def test_distinct_values_rejects_unknown_column(storage: Storage) -> None:
    with pytest.raises(ValueError, match="不支持的列"):
        storage.distinct_values("content")
