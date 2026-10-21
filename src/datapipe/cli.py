"""命令行入口。

三个子命令覆盖日常使用的全部场景：

``run``
    跑一次采集 + 清洗 + 生成报表。支持 ``--fetcher fixture`` 完全离线演示，
    也支持 ``--job-id`` 指定任务标识做断点续跑。
``stats``
    只看数据库现状，不发任何请求。
``export``
    把库里的记录导成 CSV / Excel，交给下游工具继续加工。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

from datapipe.clean import clean_records, summarize
from datapipe.config import AppConfig, ConfigError, load_config, validate_adapters
from datapipe.fetch import build_fetcher
from datapipe.logging_setup import setup_logging
from datapipe.pipeline import run_source
from datapipe.report import build_report, write_report
from datapipe.storage import Storage

__all__ = ["main", "build_parser"]

logger = logging.getLogger("datapipe.cli")


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器（独立出来便于测试与 ``--help`` 复用）。"""
    parser = argparse.ArgumentParser(
        prog="datapipe",
        description="公开信息采集与报表工具：异步抓取 → 清洗入库 → 可视化报表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  datapipe run --config config/example.yaml --fetcher fixture --pages 2\n"
            "  datapipe stats --db data/pipeline.db\n"
            "  datapipe export --db data/pipeline.db --format xlsx --out out/records.xlsx\n"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 级日志")
    sub = parser.add_subparsers(dest="command", required=True)

    # -- run ---------------------------------------------------------------
    run = sub.add_parser("run", help="执行采集、清洗并生成报表")
    run.add_argument("--config", default="config/example.yaml", help="YAML 配置文件路径")
    run.add_argument(
        "--fetcher",
        default="fixture",
        choices=("fixture", "http"),
        help="fixture=读本地样例（离线演示）；http=真实网络抓取",
    )
    run.add_argument("--pages", type=int, default=2, help="处理的列表页数量")
    run.add_argument("--db", default=None, help="SQLite 路径；默认取配置里的 output.db_path")
    run.add_argument("--out", default=None, help="输出目录；默认取配置里的 output.out_dir")
    run.add_argument("--source", default=None, help="只跑指定数据源（默认跑全部启用的源）")
    run.add_argument("--job-id", default=None, help="任务标识，相同值再次运行即断点续跑")
    run.add_argument(
        "--no-resume",
        action="store_true",
        help="忽略已完成页码，强制重跑（用于验证或修复数据）",
    )
    run.add_argument(
        "--no-report", action="store_true", help="只入库、不生成 HTML 报表"
    )
    run.add_argument(
        "--no-robots",
        action="store_true",
        help="跳过 robots.txt 检查（仅在你已确认目标站点允许时使用）",
    )

    # -- stats -------------------------------------------------------------
    stats = sub.add_parser("stats", help="查看数据库现状统计")
    stats.add_argument("--db", default=None, help="SQLite 路径")

    # -- export ------------------------------------------------------------
    export = sub.add_parser("export", help="导出记录为 CSV / Excel")
    export.add_argument("--db", default=None, help="SQLite 路径")
    export.add_argument("--format", default="csv", choices=("csv", "xlsx"), help="导出格式")
    export.add_argument("--out", default=None, help="输出文件路径")

    return parser


def _resolve_db(args: argparse.Namespace, config: AppConfig | None) -> Path:
    """确定数据库路径：命令行 > 配置 > 默认值。"""
    if getattr(args, "db", None):
        return Path(args.db)
    if config is not None:
        return config.root / config.output.db_path
    return Path("data/pipeline.db")


def _load_config_lenient(path: str) -> AppConfig | None:
    """尽力加载配置；失败时返回 None，让纯数据库类命令仍可用。"""
    try:
        return load_config(path)
    except ConfigError:
        return None


# -- run -------------------------------------------------------------------
def _cmd_run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    try:
        validate_adapters(config)
    except ConfigError as exc:
        print(f"[适配器错误] {exc}", file=sys.stderr)
        return 2

    db_path = _resolve_db(args, config)
    out_dir = Path(args.out) if args.out else config.root / config.output.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    setup_logging(verbose=args.verbose, log_file=out_dir / "run.log")

    sources = (
        (config.get_source(args.source),)
        if args.source
        else config.enabled_sources()
    )
    if not sources:
        print("[空任务] 配置里没有启用的数据源", file=sys.stderr)
        return 1

    fixture_dir = None
    if args.fetcher == "fixture":
        # 允许为每个源单独指定 fixture 目录，缺省回落到项目根下的 fixtures/
        first = sources[0]
        fixture_dir = (
            (config.root / first.fixture_dir)
            if first.fixture_dir
            else (config.root / "fixtures")
        )

    fetcher = build_fetcher(
        args.fetcher,
        http_config=config.http,
        fixture_dir=fixture_dir,
    )

    storage = Storage(db_path)
    total_stats = None
    all_failed: list[tuple[str, int, str, str]] = []

    async def _go() -> None:
        nonlocal total_stats, all_failed
        try:
            for source in sources:
                # 任务标识按「源 + 当天日期」生成，而不是固定值。
                #
                # 这是个容易踩的坑：列表页必须**每次重新抓**（新公告就出现在列表里），
                # 只有详情页才该做增量。如果 job_id 固定，第二次运行会因
                # job_pages 里"2 页已完成"而直接跳过全部列表页，
                # 结果就是永远抓不到新增公告——而统计看着像"什么都没做"。
                # 按天生成后：同一天内重跑＝续跑已完成页，跨天＝新任务全量抓列表。
                job_id = args.job_id or f"{source.name}:{date.today():%Y%m%d}"
                result = await run_source(
                    config=config,
                    source=source,
                    fetcher=fetcher,
                    storage=storage,
                    page_count=args.pages,
                    job_id=job_id,
                    resume=not args.no_resume,
                    respect_robots=(args.fetcher == "http" and not args.no_robots),
                )
                for page_no, url, error in result.failed_pages:
                    all_failed.append((source.name, page_no, url, error))
                if total_stats is None:
                    total_stats = result.stats
                else:
                    total_stats.merge(result.stats)
        finally:
            await fetcher.aclose()

    started_note = f"fetcher={args.fetcher} pages={args.pages}"
    print(f"▶ 开始采集：{len(sources)} 个源，{started_note}，数据库 {db_path}")
    asyncio.run(_go())

    # 清洗 + 报表基于库内全量数据，保证增量多次运行后报表仍然完整
    rows = storage.fetch_records()
    frame, clean_report = clean_records(rows)
    summary = summarize(frame)
    review = storage.fetch_review(limit=20)

    stats = total_stats
    if stats is not None:
        stats.elapsed_s = round(stats.elapsed_s, 3)

    report_path = None
    if not args.no_report and stats is not None:
        content = build_report(
            title=config.output.report_title,
            frame=frame,
            summary=summary,
            clean_report=clean_report,
            stats=stats,
            review=review,
        )
        report_path = write_report(out_dir / "report.html", content)

    storage.close()

    # -- 控制台摘要 --------------------------------------------------------
    print()
    print("═" * 58)
    print(" 采集结果")
    print("═" * 58)
    if stats is not None:
        print(f"  列表页      共 {stats.pages_total} 页，本次实际抓取 {stats.pages_fetched} 页")
        print(f"  条目        列表页发现 {stats.items_seen} 条")
        print(f"  新增入库    {stats.records_new} 条")
        print(f"  内容重复    {stats.records_duplicate} 条（按内容指纹）")
        if stats.pages_skipped:
            print(f"  详情页跳过  {stats.pages_skipped} 条（此前已抓过，未重复请求）")
        print(f"  抓取失败    {stats.records_failed} 次")
        if stats.rate_limited:
            print(f"  触发限流    {stats.rate_limited} 次（已自动降速）")
        print(f"  耗时        {stats.elapsed_s:.2f}s")
    print()
    print(" 数据库现状")
    print("─" * 58)
    print(f"  记录总数    {clean_report.total} 条（原始）→ {clean_report.kept} 条（清洗后）")
    print(f"  标题去重    {clean_report.duplicates} 条")
    print(f"  日期异常    {clean_report.date_failed} 条")
    print(f"  待复查      {len(review)} 条")
    if clean_report.missing:
        missing_text = "、".join(f"{k} {v}" for k, v in clean_report.missing.items() if v)
        print(f"  字段缺失    {missing_text or '无'}")
    if report_path:
        print()
        print(f" 报表        {report_path}")
    print("═" * 58)

    if all_failed:
        print()
        print("⚠ 以下列表页失败（已进待复查队列）：")
        for source_name, page_no, url, error in all_failed:
            print(f"  [{source_name}] 第 {page_no} 页 {url} → {error}")
        return 1
    return 0


# -- stats -----------------------------------------------------------------
def _cmd_stats(args: argparse.Namespace) -> int:
    db_path = _resolve_db(args, _load_config_lenient("config/example.yaml"))
    if not db_path.is_file():
        print(f"[未找到数据库] {db_path}", file=sys.stderr)
        return 2

    with Storage(db_path) as storage:
        rows = storage.fetch_records()
        frame, clean_report = clean_records(rows)
        summary = summarize(frame)
        review = storage.fetch_review(limit=5)

        print("═" * 58)
        print(f" 数据库 {db_path}")
        print("═" * 58)
        print(f"  记录总数      {clean_report.total}")
        print(f"  清洗后        {clean_report.kept}")
        print(f"  标题重复      {clean_report.duplicates}")
        print(f"  日期异常      {clean_report.date_failed}")
        print(f"  待复查        {storage.count_review()}")
        print()
        if summary["by_source"]:
            print("  按来源：")
            for item in summary["by_source"]:
                print(f"    {item['name']:<24} {item['value']:>4}")
            print()
        if summary["by_category"]:
            print("  按分类：")
            for item in summary["by_category"]:
                print(f"    {item['name']:<24} {item['value']:>4}")
            print()
        if review:
            print("  最近待复查（最多 5 条）：")
            for item in review:
                print(f"    [{item['reason'][:36]}] {item['url']}")
            print()
        print("═" * 58)
    return 0


# -- export ----------------------------------------------------------------
def _cmd_export(args: argparse.Namespace) -> int:
    db_path = _resolve_db(args, _load_config_lenient("config/example.yaml"))
    if not db_path.is_file():
        print(f"[未找到数据库] {db_path}", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else Path("out") / f"records.{args.format}"
    out.parent.mkdir(parents=True, exist_ok=True)

    with Storage(db_path) as storage:
        rows = storage.fetch_records()

    frame, _ = clean_records(rows)
    if frame.empty:
        print("[空数据] 数据库里没有可导出的记录", file=sys.stderr)
        return 1

    # 导出时去掉内部字段，只留对人类有意义的列
    export_frame = frame.drop(columns=["content_hash"], errors="ignore")

    if args.format == "xlsx":
        try:
            export_frame.to_excel(out, index=False, sheet_name="records")
        except ImportError:
            print("[缺少依赖] 导出 xlsx 需要 openpyxl：pip install openpyxl", file=sys.stderr)
            return 2
    else:
        # utf-8-sig 让 Excel 直接打开不乱码
        export_frame.to_csv(out, index=False, encoding="utf-8-sig")

    print(f"✔ 已导出 {len(export_frame)} 条记录 → {out.resolve()}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 主入口，返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    # 注意：这里不要调 logging.basicConfig。``run`` 子命令会在确定输出目录后
    # 调用 setup_logging 挂上自己的 handler；若此处先 basicConfig，
    # 根 logger 上就会同时存在两个 handler，每条日志打印两遍。

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "stats":
        return _cmd_stats(args)
    if args.command == "export":
        return _cmd_export(args)

    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
