"""编排层：把适配器、抓取器、存储串成一条流水线。

流水线形状
----------
::

    列表页(并发) ──解析──> ItemRef[] ──去重──> 详情页(并发) ──解析──> Announcement
                                                          │
                                            重复?跳过 ──────┤──> records
                                            解析失败 ──────┴──> review_queue

三个刻意的设计决定
------------------
1. **列表页与详情页都并发**，但真正的速率闸门在 ``HttpFetcher`` 内部
   （全局 8 / 单域名 2）。编排层不再叠加自己的信号量，否则两级限流
   会互相掩盖，调参时难以判断到底是哪一层在起作用。
2. **单条失败不打断整批**。任何一个详情页 404、超时或改版，都只是
   计数 + 进待复查队列；只有列表页整体解析失败才算该页失败。
3. **增量判断放在详情页抓取之前**。``pages`` 表里已成功解析过的 URL
   直接跳过，这是"每天只抓新增"的关键——不先判断就会白白请求一遍。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Sequence
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from datapipe.config import AppConfig, SourceConfig
from datapipe.fetch import FetchError, Fetcher
from datapipe.models import Announcement, ItemRef, RunStats
from datapipe.sources import ParseError, get_adapter, snippet
from datapipe.storage import Storage

__all__ = ["PipelineResult", "run_source", "check_robots"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineResult:
    """一次源级运行的产出：统计 + 已入库记录（供清洗层直接消费）。"""

    stats: RunStats
    records: list[Announcement]
    failed_pages: list[tuple[int, str, str]]


async def check_robots(fetcher: Fetcher, base_url: str, path: str) -> bool:
    """检查 ``path`` 是否被 robots.txt 允许抓取。

    设计取舍：**取不到 robots.txt 时视为允许**（``RobotFileParser`` 默认
    语义）。原因是很多政务站点根本不提供该文件，若因此拒绝抓取，
    会把"没有规定"误判成"禁止"。真正的克制体现在限流与 UA 标识上。

    仅支持 HTTP 抓取器；``FixtureFetcher`` 没有真实的 robots 概念，
    调用方应跳过本检查。
    """
    robots_url = f"{base_url.rstrip('/')}/robots.txt"
    try:
        result = await fetcher.get(robots_url)
    except FetchError as exc:  # pragma: no cover - 防御性
        logger.warning("robots.txt 获取失败，按允许处理：%s", exc)
        return True

    if not result.ok:
        logger.info("robots.txt 不可用（status=%s），按允许处理", result.status)
        return True

    parser = RobotFileParser()
    parser.parse(result.text.splitlines())
    allowed = parser.can_fetch("*", f"{base_url.rstrip('/')}{path}")
    if not allowed:
        logger.warning("robots.txt 禁止抓取 %s", path)
    return allowed


async def _fetch_list_pages(
    fetcher: Fetcher,
    adapter: object,
    urls: Sequence[str],
) -> list[tuple[int, str, str, list[ItemRef]]]:
    """并发抓取并解析列表页。

    返回 ``[(page_no, url, error, items)]``；``error`` 非空表示该页失败。
    """

    async def one(page_no: int, url: str) -> tuple[int, str, str, list[ItemRef]]:
        result = await fetcher.get(url)
        if not result.ok:
            return page_no, url, result.error or f"HTTP {result.status}", []
        try:
            items = adapter.parse_list(result.text, url)  # type: ignore[attr-defined]
        except ParseError as exc:
            return page_no, url, exc.message, []
        return page_no, url, "", items

    tasks = [one(index, url) for index, url in enumerate(urls, start=1)]
    return list(await asyncio.gather(*tasks))


async def _process_details(
    fetcher: Fetcher,
    adapter: object,
    storage: Storage,
    refs: Sequence[ItemRef],
    stats: RunStats,
) -> list[Announcement]:
    """并发处理详情页：抓取 → 解析 → 入库。"""

    async def one(ref: ItemRef) -> Announcement | None:
        nonlocal stats

        # 增量：已成功解析过的详情页直接跳过，不再发请求
        if storage.is_page_fetched(ref.detail_url):
            stats.pages_skipped += 1
            return None

        result = await fetcher.get(ref.detail_url)
        if result.status == 429:
            stats.rate_limited += 1

        if not result.ok:
            stats.http_errors += 1
            storage.mark_page(
                ref.detail_url,
                status="http_error",
                http_status=result.status,
                error=result.error or "",
            )
            storage.push_review(
                ref.detail_url,
                f"抓取失败：{result.error or 'HTTP ' + str(result.status)}",
                ref.title,
            )
            return None

        try:
            record = adapter.parse_detail(result.text, ref.detail_url)  # type: ignore[attr-defined]
        except ParseError as exc:
            storage.mark_page(ref.detail_url, status="parse_error", error=exc.message)
            storage.push_review(ref.detail_url, exc.message, snippet(result.text))
            stats.review_pushed += 1
            stats.records_failed += 1
            return None

        # 列表页摘要作为详情页缺失字段的回退
        record.publish_date = record.publish_date or ref.publish_date
        record.region = record.region or ref.region
        record.category = record.category or ref.category
        record.list_url = ref.list_url
        if not record.title:
            record.title = ref.title

        saved = storage.save_record(record)
        storage.mark_page(
            ref.detail_url,
            status="ok",
            content_hash=record.fingerprint(),
            http_status=result.status,
        )
        if saved:
            stats.records_new += 1
            return record

        stats.records_duplicate += 1
        return None

    results = await asyncio.gather(*(one(ref) for ref in refs))
    return [record for record in results if record is not None]


def _dedupe_refs(refs: Sequence[ItemRef]) -> list[ItemRef]:
    """按详情页 URL 去重，保留首次出现的顺序。"""
    seen: set[str] = set()
    unique: list[ItemRef] = []
    for ref in refs:
        if ref.detail_url in seen:
            continue
        seen.add(ref.detail_url)
        unique.append(ref)
    return unique


async def run_source(
    *,
    config: AppConfig,
    source: SourceConfig,
    fetcher: Fetcher,
    storage: Storage,
    page_count: int,
    job_id: str,
    resume: bool = True,
    respect_robots: bool = True,
) -> PipelineResult:
    """跑完一个数据源的完整流水线。

    Parameters
    ----------
    page_count:
        本次要处理的列表页数量。
    job_id:
        任务标识。相同 ``job_id`` 再次运行即"续跑"：已成功的页会跳过。
    resume:
        为 True 时跳过 ``job_pages`` 中已成功的页；为 False 时全量重跑。
    respect_robots:
        为 True 时先检查 robots.txt（仅对 HTTP 抓取器有意义）。
    """
    started = perf_counter()
    stats = RunStats(source=source.name)
    adapter = get_adapter(source.adapter)(source)

    all_urls = adapter.build_list_urls(page_count)
    stats.pages_total = len(all_urls)

    # 断点续跑：过滤掉已成功的页
    if resume:
        done = storage.done_pages(job_id)
        if done:
            logger.info("续跑：跳过已完成页面 %s", sorted(done))
        pending = [(no, url) for no, url in enumerate(all_urls, start=1) if no not in done]
    else:
        pending = list(enumerate(all_urls, start=1))

    if respect_robots:
        probe = urlparse(all_urls[0]).path or "/"
        if not await check_robots(fetcher, source.base_url, probe):
            logger.error("robots.txt 不允许抓取 %s，终止该源", probe)
            storage.finish_job(job_id, stats, status="blocked_by_robots")
            return PipelineResult(stats=stats, records=[], failed_pages=[])

    storage.start_job(job_id, source.name)

    # 1) 列表页
    list_results = await _fetch_list_pages(
        fetcher, adapter, [url for _, url in pending]
    )

    refs: list[ItemRef] = []
    failed_pages: list[tuple[int, str, str]] = []
    page_no_by_url = {url: no for no, url in pending}

    for page_no, url, error, items in list_results:
        real_page_no = page_no_by_url.get(url, page_no)
        if error:
            stats.records_failed += 1
            failed_pages.append((real_page_no, url, error))
            storage.mark_page_done(job_id, real_page_no, url, error=error)
            storage.push_review(url, f"列表页解析失败：{error}", "")
            continue
        stats.pages_fetched += 1
        stats.items_seen += len(items)
        storage.mark_page_done(job_id, real_page_no, url)
        refs.extend(items)

    # 2) 详情页
    unique_refs = _dedupe_refs(refs)
    logger.info(
        "列表页完成：%d 页 / %d 条引用（去重后 %d）",
        len(list_results),
        len(refs),
        len(unique_refs),
    )

    records = await _process_details(fetcher, adapter, storage, unique_refs, stats)

    stats.elapsed_s = perf_counter() - started
    status = "ok" if not failed_pages else "partial"
    storage.finish_job(job_id, stats, status=status)

    logger.info(
        "源 %s 完成：新增 %d / 重复 %d / 失败 %d / 跳过 %d，耗时 %.2fs",
        source.name,
        stats.records_new,
        stats.records_duplicate,
        stats.records_failed,
        stats.pages_skipped,
        stats.elapsed_s,
    )
    return PipelineResult(stats=stats, records=records, failed_pages=failed_pages)
