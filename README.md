# 公开信息采集与报表系统

一个可独立部署的数据管道：**异步采集 → 字段抽取 → 清洗入库 → 自动生成可视化报表**。

围绕"每天抓一批公开信息、去重、洗干净、出一张报表"这条真实需求构建。重点不在"能抓到"，而在于**长期无人值守运行时不出事**：限流不打垮对方站点、失败不静默丢弃、中断能续跑、站点改版能立刻发现。

数据源通过 YAML 配置接入，**代码中不写死任何站点**。仓库自带一份完全离线的示例源，`git clone` 后无需联网、无需任何配置即可跑通全流程。

## 架构

```text
                ┌──────────────────────────────────────────────┐
                │                  config/*.yaml               │
                │   站点地址 · 分页模板 · XPath 选择器 · 限流参数  │
                └───────────────────────┬──────────────────────┘
                                        │
┌──────────────┐              ┌─────────▼──────────┐
│  Fetcher     │              │   SourceAdapter    │
│  ─────────   │              │   ─────────────    │
│ HttpFetcher  │              │ parse_list()       │
│  全局/单域名 │              │ parse_detail()     │
│  双层限流    │              │  站点结构 ⇄ 数据模型 │
│  指数退避    │              └─────────┬──────────┘
│  429 降速    │                        │
│ FixtureFetcher│                       │
│  离线读本地  │                        │
└──────┬───────┘                        │
       │                                │
       └────────────┬───────────────────┘
                    │
          ┌─────────▼──────────┐
          │    pipeline.py     │  编排：并发调度 · 增量判断 · 失败分流
          └─────────┬──────────┘
                    │
       ┌────────────┼─────────────┐
       │            │             │
┌──────▼──────┐ ┌───▼────┐ ┌──────▼──────┐
│  Storage    │ │ clean  │ │  report     │
│  ────────   │ │ ────── │ │  ────────   │
│ pages       │ │ 日期   │ │ 统计卡      │
│ records     │ │ 文本   │ │ ECharts 图表│
│ review_queue│ │ 去重   │ │ 数据明细表  │
│ jobs/pages  │ │ 缺失   │ │ 单文件 HTML │
└─────────────┘ └────────┘ └─────────────┘
```

## 快速开始

要求 Python >= 3.10。以下命令在仓库根目录执行。

```bash
python -m venv .venv
```

Windows（Git Bash / PowerShell 均可）：

```bash
# 依赖安装
.venv/Scripts/python.exe -m pip install -r requirements.txt
# 以可编辑模式安装本包（提供 datapipe 命令）
.venv/Scripts/python.exe -m pip install -e .
```

macOS / Linux：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
```

跑通示例（**完全离线**，读 `fixtures/` 下的本地 HTML）：

```bash
.venv/Scripts/python.exe -m datapipe run --config config/example.yaml --fetcher fixture --pages 2
```

执行完会打印统计摘要，并在 `out/report.html` 生成报表。

### 装依赖失败时（Windows 常见）

如果 `pip install` 报 `ProxyError('Cannot connect to proxy.')`，是本机系统代理指向了一个未启动的端口（常见于代理软件退出后残留设置）。绕开它：

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export NO_PROXY="*"
.venv/Scripts/python.exe -m pip install --proxy "" -i https://pypi.org/simple -r requirements.txt
```

若公司网络屏蔽官方源，可换镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`（注意部分新版 pip 对镜像有兼容问题，优先试官方源）。

## 命令行

```bash
# 采集 + 清洗 + 生成报表
datapipe run --config config/example.yaml --fetcher fixture --pages 2

# 只看数据库现状，不发请求
datapipe stats --db data/pipeline.db

# 导出为 CSV（Excel 可直接打开，已带 BOM）或 xlsx
datapipe export --db data/pipeline.db --format csv  --out out/records.csv
datapipe export --db data/pipeline.db --format xlsx --out out/records.xlsx
```

### `run` 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--config` | `config/example.yaml` | 配置文件路径 |
| `--fetcher` | `fixture` | `fixture` 读本地样例（离线）；`http` 真实抓取 |
| `--pages` | `2` | 处理的列表页数量 |
| `--db` | 配置里的 `output.db_path` | SQLite 路径 |
| `--out` | 配置里的 `output.out_dir` | 输出目录（报表与日志） |
| `--source` | 全部启用的源 | 只跑指定数据源 |
| `--job-id` | `源名:YYYYMMDD` | 任务标识，相同值再次运行即断点续跑 |
| `--no-resume` | 关 | 忽略已完成页码强制重跑 |
| `--no-report` | 关 | 只入库、不生成报表 |
| `--no-robots` | 关 | 跳过 robots.txt 检查（**仅在你已确认站点允许时使用**） |
| `--verbose` | 关 | 输出 DEBUG 日志 |

## 配置说明

```yaml
version: 1

http:
  timeout_s: 15                 # 单请求超时（秒）
  user_agent: "DataPipeBot/0.1" # 带项目标识，便于对方站点识别流量来源
  retry:
    max_retries: 3              # 重试次数（不含首次请求）
    base_delay: 1.0             # 首次退避基数（秒）
    factor: 2.0                 # 每次翻倍
    jitter_ratio: 0.1           # 抖动比例，打散并发重试节奏
  concurrency:
    global_limit: 8             # 全局并发上限
    per_host_limit: 2           # 单域名并发上限（对同一站点的克制）
    cooldown_s: 30              # 命中 429 后的冷却窗口

output:
  db_path: data/pipeline.db
  out_dir: out
  report_title: 政务公告采集报表

sources:
  - name: gov_notice_demo       # 数据源名称，写入记录的 source_name
    adapter: gov_notice         # 适配器名，对应 sources/ 下注册的实现
    enabled: true
    base_url: "https://notice.example.gov.cn"
    list_url: "https://notice.example.gov.cn/notice/list_page_{page}.html"
    page_start: 1               # 第 1 页对应的真实页码
    fixture_dir: fixtures       # 离线模式下的 HTML 目录
    selectors:                  # 全部 XPath，站点改版只改这里
      list_item: ".//ul[contains(@class,'notice-list')]/li[contains(@class,'notice-item')]"
      list_link: ".//a[contains(@class,'title')]"
      detail_title: ".//h1[contains(@class,'article-title')]"
      detail_content: ".//div[contains(@class,'article-content')]"
```

要点：

- `list_url` **必须**包含 `{page}` 占位符，实际页码从 `page_start` 起算；
- `base_url` 用于 robots.txt 检查；
- 选择器缺失时会回落到适配器内置的默认值，但**建议显式写全**——站点结构是配置，不该藏在代码里。

## 工程要点

### 双层限流，且能在被限流时主动收缩

全局 8 + 单域名 2 的组合保证：多个站点可以并行，但对同一个站点始终克制。

真正的难点在收到 `429`：普通信号量的容量创建后不可变，无法表达"临时减半"。这里用**可变计数 + `asyncio.Condition`**，命中 429 时把额度减半（`8→4→2→1`，下限 1）并进入冷却窗口，冷却到期后再逐级翻倍回升，避免刚恢复就以全速再次冲击对方。

```python
limiter.on_rate_limited()   # => (4, 30.0)  额度减半 + 冷却 30 秒
limiter.recover()           # 冷却结束后 -> 8
```

### 两级去重

| 层级 | 判据 | 作用 |
| --- | --- | --- |
| `records.content_hash` | 标题 + 发布日期 + 正文，去空白后 sha256 | 同一公告被不同页面重复收录时，只入库一次 |
| 清洗层 | 标题 + 日期 | 转载场景下正文略有差异（加"【转载】"前缀）也能识别 |

哈希前统一剥离空白、全角空格、零宽字符与 BOM，防止仅靠排版差异就被判成新内容。

### 增量抓取与断点续跑

两个不同层级的状态，职责不要混：

- **条目级增量**：`pages` 表记录已成功解析的详情页 URL，下次直接跳过，不重复请求；
- **页码级续跑**：`job_pages` 表记录每个列表页的处理结果，中断后从失败页继续。

> 注意一个实际踩过的坑：列表页**必须每次重新抓**（新公告就出现在列表里），只有详情页才做增量。因此默认 `job_id` 按「源名 + 当天日期」生成——同一天内重跑＝续跑，跨天＝新任务全量抓列表。若 `job_id` 固定不变，第二次运行会因"页已完成"而跳过全部列表页，结果就是永远抓不到新增公告，而统计看着像"什么都没做"。

### 失败不静默

解析失败抛 `ParseError`，由编排层写入 `review_queue` 并保留原文片段。**不会**返回空列表——那会让"站点改版"伪装成"今天没有新公告"，是数据管道里最难排查的一类故障。

单条详情页失败（404、超时、改版）只计数并进队列，不影响同批其他条目。

### 可观测

- 结构化日志：`%(asctime)s %(levelname)s %(name)s %(message)s`，同时输出控制台与 `out/run.log`
- 每次运行打印统计：列表页数、发现条目、新增、重复、跳过、失败、限流触发次数、耗时
- 报表页展示统计卡、四张分布图、数据明细、待复查队列、字段缺失统计

## 接入自己的数据源

不需要改 Python 代码（前提是页面结构与内置适配器一致）：

1. 用浏览器开发者工具确认列表页与详情页的 CSS 类名；
2. 复制 `sources` 里的一项，改 `name`、`base_url`、`list_url`、`selectors`；
3. `--fetcher http` 跑一次，观察日志与 `review_queue`。

页面结构差异较大时，新增一个适配器：

```python
# src/datapipe/sources/my_site.py
from datapipe.models import Announcement, ItemRef
from datapipe.sources.base import SourceAdapter, ParseError, parse_html, node_text

class MySiteAdapter(SourceAdapter):
    name = "my_site"                       # 与 YAML 的 adapter 对应

    def parse_list(self, html: str, url: str) -> list[ItemRef]:
        tree = parse_html(html, url)
        # ... 抽取条目，结构不符时 raise ParseError
        return refs

    def parse_detail(self, html: str, url: str) -> Announcement:
        tree = parse_html(html, url)
        # ... 抽取字段
        return Announcement(title=..., content=..., detail_url=url)
```

在 `src/datapipe/sources/__init__.py` 的 `ADAPTERS` 里登记一行即可。

## 测试

```bash
.venv/Scripts/python.exe -m pytest -q
```

**85 项测试，全部离线、无网络依赖**，覆盖：

| 文件 | 覆盖内容 |
| --- | --- |
| `test_adapter_parse.py` | 列表/详情字段抽取、相对链接转绝对、结构异常抛错、空正文容忍 |
| `test_dedup.py` | 内容指纹（空白归一）、唯一索引去重、批量计数、待复查队列、断点状态 |
| `test_backoff.py` | 指数退避序列、抖动边界、429 降速 `8→4→2→1`、冷却阻塞与逐级恢复、闸门并发上限 |
| `test_clean.py` | 11 种日期格式、非法日期、文本净化、标题+日期去重、缺失统计、NaN 不外泄 |

时间源可注入，降速相关用例用假时钟断言，整个套件跑完约 2 秒。

## 运行时产物

```text
data/pipeline.db         SQLite（pages / records / review_queue / jobs / job_pages）
out/report.html          单文件报表，可直接托管
out/run.log              运行日志
out/records.csv|xlsx     导出文件
```

报表的图表由 ECharts（CDN）渲染。**离线打开不会白屏**：统计卡与数据表格是服务端渲染的纯 HTML，只有图表区域显示降级提示。

## 合规声明

本项目用于采集**依法公开**的信息，并在实现上主动约束自己：

- 遵守目标站点 `robots.txt`（`HttpFetcher` 模式下默认检查；取不到时按"无规定"处理）；
- 使用带项目标识的 User-Agent，便于对方识别流量来源；
- 全局与单域名双层限流，收到 `429` 主动降速，不对目标站点造成压力；
- **不采集个人信息**（姓名、手机号、证件号、简历等）；
- **不实现**验证码识别、登录态伪造、加密签名破解、付费墙绕过；
- 仓库内示例数据全部为虚构内容，不含任何真实业务信息。

请不要将本项目用于抓取非公开数据或规避访问控制。使用者需自行确认目标站点的服务条款与当地法律法规。

## License

[MIT](./LICENSE) · Copyright (c) 2026 李宏洋
