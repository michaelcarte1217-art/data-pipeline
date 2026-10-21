"""SQLite 存储层：去重、入库、待复查队列、任务状态。

四张表各司其职
--------------
``pages``
    列表页/详情页的抓取台账，主键是 URL。区分"抓过但解析失败"
    （``status='parse_error'``）与"抓成功"（``status='ok'``），
    前者下次仍会重试，后者可跳过。
``records``
    结构化公告。``content_hash`` 唯一索引承担内容级去重：
    同一公告被不同列表页重复收录时，第二次写不进去。
``review_queue``
    解析失败的落点。设计意图是**永不静默丢弃**——宁可留一条
    "这条我没解析出来，原文片段在此"，也不要让它凭空消失。
``jobs`` / ``job_pages``
    断点续跑的最小状态机。页码状态独立成表而不是塞进
    ``jobs.stats_json``，这样每页只需一次小写入，也能按页查失败原因。

为什么用同步 ``sqlite3`` 而不是 aiosqlite：单条读写都在微秒级，
管道瓶颈在网络 I/O；引入异步驱动只会增加依赖与复杂度。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from datapipe.models import Announcement, RunStats

__all__ = ["Storage", "utc_now"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url          TEXT PRIMARY KEY,
    content_hash TEXT,
    fetched_at   TEXT NOT NULL,
    status       TEXT NOT NULL,
    http_status  INTEGER DEFAULT 0,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    publish_date TEXT DEFAULT '',
    region       TEXT DEFAULT '',
    category     TEXT DEFAULT '',
    source_name  TEXT DEFAULT '',
    content      TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_date   ON records(publish_date);
CREATE INDEX IF NOT EXISTS idx_records_source ON records(source_name);
CREATE INDEX IF NOT EXISTS idx_records_region ON records(region);

CREATE TABLE IF NOT EXISTS review_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL,
    reason     TEXT NOT NULL,
    snippet    TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_url ON review_queue(url);

CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    stats_json  TEXT
);

CREATE TABLE IF NOT EXISTS job_pages (
    job_id      TEXT NOT NULL,
    page_no     INTEGER NOT NULL,
    url         TEXT NOT NULL,
    status      TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT,
    PRIMARY KEY (job_id, page_no)
);
"""


def utc_now() -> str:
    """当前 UTC 时间的 ISO 字符串（秒级），全库时间字段统一格式。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Storage:
    """存储门面。构造即建表，重复构造是幂等的。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if self.db_path.parent and str(self.db_path.parent) not in ("", "."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        # WAL：读写可以并行，避免长事务把读操作锁住
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """暴露底层连接，供 pandas 读取与 CLI 的只读查询使用。"""
        return self._conn

    # -- pages ------------------------------------------------------------
    def is_page_fetched(self, url: str) -> bool:
        """该 URL 是否已成功抓取并解析过（解析失败的仍会重试）。"""
        row = self._conn.execute(
            "SELECT 1 FROM pages WHERE url = ? AND status = 'ok' LIMIT 1", (url,)
        ).fetchone()
        return row is not None

    def mark_page(
        self,
        url: str,
        *,
        status: str,
        content_hash: str = "",
        http_status: int = 0,
        error: str = "",
    ) -> None:
        """写入或更新抓取台账。

        ``status`` 取值：``ok`` / ``http_error`` / ``parse_error``。
        """
        self._conn.execute(
            """
            INSERT INTO pages (url, content_hash, fetched_at, status, http_status, error)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                content_hash = excluded.content_hash,
                fetched_at   = excluded.fetched_at,
                status       = excluded.status,
                http_status  = excluded.http_status,
                error        = excluded.error
            """,
            (url, content_hash, utc_now(), status, http_status, error),
        )
        self._conn.commit()

    # -- records ----------------------------------------------------------
    def is_duplicate(self, content_hash: str) -> bool:
        """内容指纹是否已入库。"""
        row = self._conn.execute(
            "SELECT 1 FROM records WHERE content_hash = ? LIMIT 1", (content_hash,)
        ).fetchone()
        return row is not None

    def save_record(self, record: Announcement) -> bool:
        """写入一条记录。

        Returns
        -------
        bool
            新记录返回 True；内容指纹重复返回 False（依赖唯一索引，
            而不是"先查后写"，避免并发下的竞态）。
        """
        row = record.to_row()
        try:
            self._conn.execute(
                """
                INSERT INTO records
                    (url, content_hash, title, publish_date, region,
                     category, source_name, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["url"],
                    row["content_hash"],
                    row["title"],
                    row["publish_date"],
                    row["region"],
                    row["category"],
                    row["source_name"],
                    row["content"],
                    utc_now(),
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            # 唯一索引冲突即重复内容，属正常业务分支而非故障
            return False

    def count_records(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()
        return int(row["n"]) if row else 0

    def fetch_records(self) -> list[dict[str, Any]]:
        """取出全部记录，供清洗层交给 pandas。"""
        cursor = self._conn.execute(
            """
            SELECT id, url, content_hash, title, publish_date, region,
                   category, source_name, content, created_at
            FROM records
            ORDER BY publish_date DESC, id DESC
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    # -- review_queue -----------------------------------------------------
    def push_review(self, url: str, reason: str, snippet: str = "") -> int:
        """把无法解析的内容登记到待复查队列，返回此项累计待查条数。"""
        self._conn.execute(
            "INSERT INTO review_queue (url, reason, snippet, created_at) VALUES (?, ?, ?, ?)",
            (url, reason, snippet[:500], utc_now()),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM review_queue WHERE url = ?", (url,)
        ).fetchone()
        return int(row["n"]) if row else 1

    def count_review(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM review_queue").fetchone()
        return int(row["n"]) if row else 0

    def fetch_review(self, limit: int = 20) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            """
            SELECT id, url, reason, snippet, created_at
            FROM review_queue ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    # -- jobs / job_pages -------------------------------------------------
    def start_job(self, job_id: str, source: str) -> None:
        self._conn.execute(
            """
            INSERT INTO jobs (job_id, source, started_at, status)
            VALUES (?, ?, ?, 'running')
            ON CONFLICT(job_id) DO UPDATE SET
                started_at = excluded.started_at,
                status     = 'running',
                finished_at = NULL
            """,
            (job_id, source, utc_now()),
        )
        self._conn.commit()

    def finish_job(self, job_id: str, stats: RunStats, status: str = "ok") -> None:
        self._conn.execute(
            "UPDATE jobs SET finished_at = ?, status = ?, stats_json = ? WHERE job_id = ?",
            (utc_now(), status, json.dumps(stats.to_dict(), ensure_ascii=False), job_id),
        )
        self._conn.commit()

    def mark_page_done(
        self, job_id: str, page_no: int, url: str, *, error: str = ""
    ) -> None:
        """记录某页处理结果；``error`` 非空即标记失败，续跑时会重试该页。"""
        status = "error" if error else "ok"
        self._conn.execute(
            """
            INSERT INTO job_pages (job_id, page_no, url, status, finished_at, error)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, page_no) DO UPDATE SET
                url         = excluded.url,
                status      = excluded.status,
                finished_at = excluded.finished_at,
                error       = excluded.error
            """,
            (job_id, page_no, url, status, utc_now(), error),
        )
        self._conn.commit()

    def pending_pages(self, job_id: str) -> set[int]:
        """返回该任务中**尚未成功**的页码集合，用于断点续跑。

        从未处理过的页不会被记录，调用方应以 `page_no not in pending` 之外的
        方式判断，因此这里同时提供 :meth:`done_pages`。
        """
        cursor = self._conn.execute(
            "SELECT page_no FROM job_pages WHERE job_id = ? AND status = 'error'",
            (job_id,),
        )
        return {int(row["page_no"]) for row in cursor.fetchall()}

    def done_pages(self, job_id: str) -> set[int]:
        """已成功处理过的页码集合。"""
        cursor = self._conn.execute(
            "SELECT page_no FROM job_pages WHERE job_id = ? AND status = 'ok'",
            (job_id,),
        )
        return {int(row["page_no"]) for row in cursor.fetchall()}

    def job_summary(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    # -- 便捷批量 ---------------------------------------------------------
    def save_records(self, records: Iterable[Announcement]) -> tuple[int, int]:
        """批量写入，返回 ``(新增数, 重复数)``。"""
        new_count = 0
        dup_count = 0
        for record in records:
            if self.save_record(record):
                new_count += 1
            else:
                dup_count += 1
        return new_count, dup_count

    def distinct_values(self, column: str) -> Sequence[str]:
        """取某列的去重值，仅供统计展示（列名做白名单校验）。"""
        allowed = {"source_name", "region", "category"}
        if column not in allowed:
            raise ValueError(f"不支持的列：{column!r}，可用：{', '.join(sorted(allowed))}")
        cursor = self._conn.execute(
            f"SELECT DISTINCT {column} FROM records WHERE {column} != '' ORDER BY {column}"
        )
        return [str(row[0]) for row in cursor.fetchall()]
