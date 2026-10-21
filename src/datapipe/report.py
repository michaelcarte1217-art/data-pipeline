"""静态 HTML 报表生成。

产物是**单文件、可直接托管**的 HTML：样式内联，数据以 JSON 内嵌，
图表用 ECharts 渲染。之所以选 ECharts 而不是服务端画图：

* 交互（悬浮提示、图例切换）在纯静态页面上即可用；
* 与作者另一个作品「ETF 数据看板」保持同一套视觉语言，深色金融风；

ECharts 从 CDN 引入。离线打开时页面不会白屏——统计卡与数据表格是
服务端渲染好的纯 HTML，只有图表区域会显示降级提示，这一点由
``window.echarts`` 的存在性判断来处理。
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from datapipe.clean import CleanReport
from datapipe.models import RunStats

__all__ = ["build_report", "write_report", "ECHARTS_CDN"]

ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js"

#: 与 ETF 看板共用的深色主题色值
THEME = {
    "bg": "#0B0F16",
    "surface": "#131A26",
    "surface2": "#1A2231",
    "line": "#1E2836",
    "text": "#E6EAF2",
    "text2": "#8A94A6",
    "text3": "#5C6675",
    "accent": "#4C8DFF",
    "up": "#F0425A",
    "down": "#0BBF7A",
    "warn": "#F0A742",
}


def _card(label: str, value: str, hint: str = "", accent: str = "") -> str:
    """渲染一张统计卡。"""
    color = f"color:{accent};" if accent else ""
    hint_html = f'<p class="stat__hint">{html.escape(hint)}</p>' if hint else ""
    return (
        f'<div class="stat">'
        f'<p class="stat__label">{html.escape(label)}</p>'
        f'<p class="stat__value" style="{color}">{html.escape(value)}</p>'
        f"{hint_html}"
        f"</div>"
    )


def _table_rows(frame: pd.DataFrame, limit: int = 200) -> str:
    """渲染数据表格的 tbody。超出上限的部分在页面上提示。"""
    if frame.empty:
        return '<tr><td colspan="6" class="empty">暂无记录</td></tr>'

    cells: list[str] = []
    for _, row in frame.head(limit).iterrows():
        title = html.escape(str(row.get("title", "")))
        url = str(row.get("url", ""))
        cells.append(
            "<tr>"
            f'<td class="col-date">{html.escape(str(row.get("publish_date", "")) or "—")}</td>'
            f'<td class="col-title"><a href="{html.escape(url)}" target="_blank" '
            f'rel="noopener noreferrer">{title}</a></td>'
            f'<td>{html.escape(str(row.get("region", "")) or "—")}</td>'
            f'<td>{html.escape(str(row.get("category", "")) or "—")}</td>'
            f'<td class="col-source">{html.escape(str(row.get("source_name", "")) or "—")}</td>'
            f'<td class="col-len">{len(str(row.get("content", "")))}</td>'
            "</tr>"
        )
    return "".join(cells)


def _review_rows(review: Sequence[Mapping[str, Any]], limit: int = 10) -> str:
    if not review:
        return '<tr><td colspan="3" class="empty">没有待复查项 🎉</td></tr>'
    rows = []
    for item in list(review)[:limit]:
        rows.append(
            "<tr>"
            f'<td class="col-reason">{html.escape(str(item.get("reason", "")))}</td>'
            f'<td class="col-url">{html.escape(str(item.get("url", "")))}</td>'
            f'<td class="col-snippet">{html.escape(str(item.get("snippet", ""))[:120])}</td>'
            "</tr>"
        )
    return "".join(rows)


def build_report(
    *,
    title: str,
    frame: pd.DataFrame,
    summary: Mapping[str, Sequence[Mapping[str, Any]]],
    clean_report: CleanReport,
    stats: RunStats,
    review: Sequence[Mapping[str, Any]] = (),
    truncated_at: int = 200,
) -> str:
    """生成完整 HTML 字符串。"""
    total = len(frame)
    truncated = total > truncated_at

    cards = "".join(
        [
            _card("记录总数", f"{total:,}", "清洗去重后"),
            _card("本次新增", f"{stats.records_new:,}", "写入数据库", THEME["down"]),
            _card("重复剔除", f"{clean_report.duplicates:,}", "标题+日期重复", THEME["text2"]),
            _card(
                "日期异常",
                f"{clean_report.date_failed:,}",
                "已进待复查队列",
                THEME["warn"] if clean_report.date_failed else "",
            ),
            _card(
                "待复查处",
                f"{len(review):,}",
                "解析失败留档",
                THEME["up"] if review else "",
            ),
            _card("耗时", f"{stats.elapsed_s:.1f}s", f"{stats.pages_fetched} 页已抓取"),
        ]
    )

    payload = {
        "by_source": list(summary.get("by_source", [])),
        "by_date": list(summary.get("by_date", [])),
        "by_category": list(summary.get("by_category", [])),
        "by_region": list(summary.get("by_region", [])),
        "theme": THEME,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    missing_items = "".join(
        f'<li><span>{html.escape(name)}</span><strong>{count:,}</strong></li>'
        for name, count in clean_report.missing.items()
    ) or "<li><span>无缺失字段</span><strong>0</strong></li>"

    styles = f"""
:root {{
  --bg: {THEME['bg']};
  --surface: {THEME['surface']};
  --surface-2: {THEME['surface2']};
  --line: {THEME['line']};
  --text: {THEME['text']};
  --text-2: {THEME['text2']};
  --text-3: {THEME['text3']};
  --accent: {THEME['accent']};
  --up: {THEME['up']};
  --down: {THEME['down']};
  --warn: {THEME['warn']};
  --radius: 12px;
}}
* {{ box-sizing: border-box; }}
html {{ color-scheme: dark; }}
body {{
  margin: 0;
  padding: 0 0 3rem;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  font-size: 14px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}}
.mono, .num {{ font-variant-numeric: tabular-nums; }}
.container {{ width: min(100% - 2.5rem, 1180px); margin-inline: auto; }}
header.top {{
  padding: 2rem 0 1.25rem;
  border-bottom: 1px solid var(--line);
  margin-bottom: 1.5rem;
}}
header.top h1 {{ margin: 0 0 .35rem; font-size: 1.5rem; letter-spacing: -.01em; }}
header.top p {{ margin: 0; color: var(--text-2); font-size: .875rem; }}
.stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: .75rem;
  margin-bottom: 1.5rem;
}}
.stat {{
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: .9rem 1rem;
}}
.stat__label {{ margin: 0 0 .25rem; font-size: .75rem; color: var(--text-2); }}
.stat__value {{ margin: 0; font-size: 1.5rem; font-weight: 650; font-variant-numeric: tabular-nums; }}
.stat__hint {{ margin: .15rem 0 0; font-size: .7rem; color: var(--text-3); }}
.grid {{
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
  margin-bottom: 1rem;
}}
.panel {{
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  overflow: hidden;
}}
.panel__head {{
  display: flex; align-items: baseline; gap: .6rem;
  padding: .8rem 1rem;
  border-bottom: 1px solid var(--line);
}}
.panel__head h2 {{ margin: 0; font-size: .95rem; font-weight: 600; }}
.panel__head span {{ font-size: .72rem; color: var(--text-3); }}
.panel__body {{ padding: .75rem; }}
.chart {{ width: 100%; height: 260px; }}
.chart-fallback {{
  display: none;
  padding: 1.5rem 1rem;
  color: var(--text-3);
  font-size: .8rem;
  text-align: center;
}}
table {{ width: 100%; border-collapse: collapse; font-size: .8rem; }}
th, td {{ padding: .5rem .6rem; text-align: left; border-bottom: 1px solid var(--line); }}
th {{ color: var(--text-2); font-weight: 500; font-size: .72rem; white-space: nowrap; }}
td {{ color: var(--text); }}
tbody tr:last-child td {{ border-bottom: none; }}
tbody tr:hover {{ background: var(--surface-2); }}
.col-date, .col-len {{ white-space: nowrap; font-variant-numeric: tabular-nums; color: var(--text-2); }}
.col-title a {{ color: var(--text); text-decoration: none; }}
.col-title a:hover {{ color: var(--accent); text-decoration: underline; }}
.col-source, .col-url {{ color: var(--text-2); }}
.col-url {{ max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.col-reason {{ color: var(--warn); white-space: nowrap; }}
.col-snippet {{ color: var(--text-3); font-size: .75rem; }}
.empty {{ text-align: center; color: var(--text-3); padding: 1.5rem 0; }}
.table-wrap {{ max-height: 460px; overflow: auto; }}
.missing {{ list-style: none; margin: 0; padding: 0; display: grid; gap: .4rem; }}
.missing li {{
  display: flex; justify-content: space-between;
  padding: .4rem .6rem;
  background: var(--surface-2);
  border-radius: 8px;
  font-size: .8rem;
}}
.missing span {{ color: var(--text-2); }}
.missing strong {{ font-variant-numeric: tabular-nums; }}
footer.foot {{
  margin-top: 2rem; padding-top: 1rem;
  border-top: 1px solid var(--line);
  color: var(--text-3); font-size: .75rem;
}}
footer.foot strong {{ color: var(--text-2); }}
@media (max-width: 900px) {{
  .grid {{ grid-template-columns: 1fr; }}
}}
"""

    script = """
(function () {
  var fallbacks = document.querySelectorAll('.chart-fallback');
  function showFallback() {
    document.querySelectorAll('.chart').forEach(function (el) { el.style.display = 'none'; });
    fallbacks.forEach(function (el) { el.style.display = 'block'; });
  }
  if (!window.echarts) { showFallback(); return; }

  var DATA = JSON.parse(document.getElementById('report-data').textContent);
  var T = DATA.theme;
  var axis = {
    axisLine: { lineStyle: { color: T.line } },
    axisLabel: { color: T.text2, fontSize: 11 },
    splitLine: { lineStyle: { color: T.line, type: 'dashed' } }
  };

  function barChart(id, rows, color) {
    var el = document.getElementById(id);
    if (!el) { return; }
    if (!rows.length) { el.parentNode.querySelector('.chart-fallback').style.display = 'block'; el.style.display = 'none'; return; }
    var chart = echarts.init(el);
    chart.setOption({
      backgroundColor: 'transparent',
      grid: { left: 8, right: 16, top: 20, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: T.surface2, borderColor: T.line, textStyle: { color: T.text, fontSize: 12 } },
      xAxis: Object.assign({ type: 'category', data: rows.map(function (r) { return r.name; }) }, axis, { splitLine: { show: false }, axisLabel: { color: T.text2, fontSize: 11, interval: 0, rotate: rows.length > 6 ? 30 : 0 } }),
      yAxis: Object.assign({ type: 'value' }, axis),
      series: [{
        type: 'bar',
        data: rows.map(function (r) { return r.value; }),
        barMaxWidth: 32,
        itemStyle: { color: color, borderRadius: [4, 4, 0, 0] }
      }]
    });
    return chart;
  }

  function lineChart(id, rows) {
    var el = document.getElementById(id);
    if (!el) { return; }
    if (!rows.length) { el.parentNode.querySelector('.chart-fallback').style.display = 'block'; el.style.display = 'none'; return; }
    var chart = echarts.init(el);
    chart.setOption({
      backgroundColor: 'transparent',
      grid: { left: 8, right: 16, top: 20, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis', backgroundColor: T.surface2, borderColor: T.line, textStyle: { color: T.text, fontSize: 12 } },
      xAxis: Object.assign({ type: 'category', boundaryGap: false, data: rows.map(function (r) { return r.name; }) }, axis, { splitLine: { show: false } }),
      yAxis: Object.assign({ type: 'value' }, axis),
      series: [{
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 5,
        data: rows.map(function (r) { return r.value; }),
        lineStyle: { color: T.accent, width: 2.5 },
        itemStyle: { color: T.accent },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(76,141,255,.28)' },
            { offset: 1, color: 'rgba(76,141,255,0)' }
          ])
        }
      }]
    });
    return chart;
  }

  function pieChart(id, rows) {
    var el = document.getElementById(id);
    if (!el) { return; }
    if (!rows.length) { el.parentNode.querySelector('.chart-fallback').style.display = 'block'; el.style.display = 'none'; return; }
    var chart = echarts.init(el);
    var palette = [T.accent, T.down, T.up, T.warn, '#9B7BFF', '#2AB4A6', '#C86AD9'];
    chart.setOption({
      backgroundColor: 'transparent',
      tooltip: { trigger: 'item', backgroundColor: T.surface2, borderColor: T.line, textStyle: { color: T.text, fontSize: 12 } },
      legend: { bottom: 0, textStyle: { color: T.text2, fontSize: 11 }, itemWidth: 10, itemHeight: 10 },
      color: palette,
      series: [{
        type: 'pie',
        radius: ['45%', '68%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: T.surface, borderWidth: 2 },
        label: { color: T.text2, fontSize: 11 },
        data: rows.map(function (r) { return { name: r.name, value: r.value }; })
      }]
    });
    return chart;
  }

  var charts = [
    barChart('chart-source', DATA.by_source, T.accent),
    lineChart('chart-date', DATA.by_date),
    pieChart('chart-category', DATA.by_category),
    barChart('chart-region', DATA.by_region, T.down)
  ].filter(Boolean);

  window.addEventListener('resize', function () {
    charts.forEach(function (c) { c.resize(); });
  });
})();
"""

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="color-scheme" content="dark" />
    <meta name="theme-color" content="{THEME['bg']}" />
    <title>{html.escape(title)}</title>
    <style>{styles}</style>
  </head>
  <body>
    <div class="container">
      <header class="top">
        <h1>{html.escape(title)}</h1>
        <p>数据管道自动生成 · 共 {total:,} 条记录 · 图表由 ECharts 渲染（需联网加载）</p>
      </header>

      <section class="stats">{cards}</section>

      <div class="grid">
        <section class="panel">
          <div class="panel__head"><h2>按来源分布</h2><span>条</span></div>
          <div class="panel__body">
            <div id="chart-source" class="chart"></div>
            <p class="chart-fallback">离线模式：无法加载 ECharts 脚本，图表已跳过；下方数据表格不受影响。</p>
          </div>
        </section>
        <section class="panel">
          <div class="panel__head"><h2>按日期趋势</h2><span>条 / 天</span></div>
          <div class="panel__body">
            <div id="chart-date" class="chart"></div>
            <p class="chart-fallback">离线模式：无法加载 ECharts 脚本，图表已跳过；下方数据表格不受影响。</p>
          </div>
        </section>
        <section class="panel">
          <div class="panel__head"><h2>按分类占比</h2><span>条</span></div>
          <div class="panel__body">
            <div id="chart-category" class="chart"></div>
            <p class="chart-fallback">离线模式：无法加载 ECharts 脚本，图表已跳过；下方数据表格不受影响。</p>
          </div>
        </section>
        <section class="panel">
          <div class="panel__head"><h2>按地区分布</h2><span>条</span></div>
          <div class="panel__body">
            <div id="chart-region" class="chart"></div>
            <p class="chart-fallback">离线模式：无法加载 ECharts 脚本，图表已跳过；下方数据表格不受影响。</p>
          </div>
        </section>
      </div>

      <section class="panel" style="margin-bottom:1rem">
        <div class="panel__head">
          <h2>数据明细</h2>
          <span>{f"仅显示前 {truncated_at} 条（共 {total:,} 条）" if truncated else f"共 {total:,} 条"}</span>
        </div>
        <div class="panel__body">
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>发布日期</th><th>标题</th><th>地区</th><th>分类</th><th>来源</th><th>正文字数</th>
                </tr>
              </thead>
              <tbody>{_table_rows(frame, truncated_at)}</tbody>
            </table>
          </div>
        </div>
      </section>

      <div class="grid">
        <section class="panel">
          <div class="panel__head"><h2>待复查队列</h2><span>解析失败留档</span></div>
          <div class="panel__body">
            <div class="table-wrap">
              <table>
                <thead><tr><th>原因</th><th>地址</th><th>原文片段</th></tr></thead>
                <tbody>{_review_rows(review)}</tbody>
              </table>
            </div>
          </div>
        </section>
        <section class="panel">
          <div class="panel__head"><h2>字段缺失统计</h2><span>清洗后</span></div>
          <div class="panel__body">
            <ul class="missing">{missing_items}</ul>
          </div>
        </section>
      </div>

      <footer class="foot">
        <p>抓取 {stats.pages_fetched} 页 / 跳过 {stats.pages_skipped} 页 / 新增 {stats.records_new} 条 /
           重复 {stats.records_duplicate} 条 / 失败 {stats.records_failed} 条，耗时 {stats.elapsed_s:.2f}s。</p>
        <p>本报表由 data-pipeline 生成，仅用于展示数据采集与清洗流程，<strong>不构成任何政策解读</strong>。</p>
      </footer>
    </div>

    <script type="application/json" id="report-data">{payload_json}</script>
    <script src="{ECHARTS_CDN}"></script>
    <script>{script}</script>
  </body>
</html>
"""


def write_report(path: str | Path, content: str) -> Path:
    """把报表写到磁盘，返回绝对路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target.resolve()
