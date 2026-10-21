"""YAML 配置的加载、校验与 dataclass 映射。

设计原则：站点信息（域名、列表地址、页面结构选择器）全部外置到 YAML，
Python 代码里不出现任何真实站点地址。换站点的代价应当只是换一份配置。

校验策略是"尽早失败、错误可读"：所有非法值都抛 :class:`ConfigError`
并带上出错的 YAML 路径，而不是等到抓取阶段才抛 KeyError。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

__all__ = [
    "ConfigError",
    "RetryConfig",
    "ConcurrencyConfig",
    "HttpConfig",
    "SourceConfig",
    "OutputConfig",
    "AppConfig",
    "load_config",
    "validate_adapters",
]


class ConfigError(Exception):
    """配置缺失、类型错误或取值非法。"""


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"{where}.{key} 缺失（必填）")
    return mapping[key]


def _as_int(value: Any, where: str, minimum: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} 需要整数，实际是 {value!r}") from exc
    if result < minimum:
        raise ConfigError(f"{where} 不能小于 {minimum}，实际是 {result}")
    return result


def _as_float(value: Any, where: str, minimum: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} 需要数字，实际是 {value!r}") from exc
    if result < minimum:
        raise ConfigError(f"{where} 不能小于 {minimum}，实际是 {result}")
    return result


def _as_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} 需要非空字符串，实际是 {value!r}")
    return value.strip()


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """指数退避参数：第 n 次重试等待 ``base_delay * factor**n`` 秒，再叠加抖动。"""

    max_retries: int = 3
    base_delay: float = 1.0
    factor: float = 2.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ConfigError("http.retry.max_retries 不能为负")
        if self.base_delay <= 0:
            raise ConfigError("http.retry.base_delay 必须大于 0")
        if self.factor < 1.0:
            raise ConfigError("http.retry.factor 必须 >= 1.0，否则不构成指数增长")
        if not 0.0 <= self.jitter_ratio < 1.0:
            raise ConfigError("http.retry.jitter_ratio 必须落在 [0, 1)")


@dataclass(frozen=True, slots=True)
class ConcurrencyConfig:
    """并发闸门：全局上限 + 单域名上限，外加 429 冷却窗口。"""

    global_limit: int = 8
    per_host_limit: int = 2
    cooldown_s: float = 30.0

    def __post_init__(self) -> None:
        if self.global_limit < 1:
            raise ConfigError("http.concurrency.global_limit 至少为 1")
        if self.per_host_limit < 1:
            raise ConfigError("http.concurrency.per_host_limit 至少为 1")
        if self.per_host_limit > self.global_limit:
            raise ConfigError("http.concurrency.per_host_limit 不能大于 global_limit")
        if self.cooldown_s < 0:
            raise ConfigError("http.concurrency.cooldown_s 不能为负")


@dataclass(frozen=True, slots=True)
class HttpConfig:
    """传输层参数。"""

    timeout_s: float = 15.0
    user_agent: str = "DataPipeBot/0.1"
    retry: RetryConfig = field(default_factory=RetryConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ConfigError("http.timeout_s 必须大于 0")


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """单个数据源的配置。

    ``list_url`` 必须包含 ``{page}`` 占位符，实际页码由 ``page_start`` 起算，
    因此"第 1 页"对应真实站点的哪一页完全由配置决定。
    """

    name: str
    adapter: str
    base_url: str
    list_url: str
    page_start: int = 1
    enabled: bool = True
    fixture_dir: str | None = None
    selectors: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""

    def __post_init__(self) -> None:
        if "{page}" not in self.list_url:
            raise ConfigError(
                f"sources[{self.name}].list_url 必须包含 {{page}} 占位符，当前为 {self.list_url!r}"
            )

    def page_urls(self, page_count: int) -> list[str]:
        """按页码顺序生成列表页 URL 列表。

        这是"列表页地址如何拼"的唯一实现，适配器直接复用，
        避免同一套拼接逻辑在多个适配器里各写一遍。
        """
        if page_count < 0:
            raise ValueError("page_count 不能为负")
        return [self.list_url.format(page=self.page_start + i) for i in range(page_count)]


@dataclass(frozen=True, slots=True)
class OutputConfig:
    """默认输出位置，可被命令行参数覆盖。"""

    db_path: str = "data/pipeline.db"
    out_dir: str = "out"
    report_title: str = "公告数据采集报表"


@dataclass(frozen=True, slots=True)
class AppConfig:
    """整份配置的根对象。"""

    version: int
    http: HttpConfig
    sources: tuple[SourceConfig, ...]
    output: OutputConfig
    path: Path
    root: Path

    def get_source(self, name: str) -> SourceConfig:
        for source in self.sources:
            if source.name == name:
                return source
        known = ", ".join(s.name for s in self.sources) or "(空)"
        raise ConfigError(f"未找到数据源 {name!r}，可用：{known}")

    def enabled_sources(self) -> tuple[SourceConfig, ...]:
        return tuple(s for s in self.sources if s.enabled)


def _parse_retry(raw: Mapping[str, Any], where: str) -> RetryConfig:
    return RetryConfig(
        max_retries=_as_int(raw.get("max_retries", 3), f"{where}.max_retries"),
        base_delay=_as_float(raw.get("base_delay", 1.0), f"{where}.base_delay", 0.0),
        factor=_as_float(raw.get("factor", 2.0), f"{where}.factor", 1.0),
        jitter_ratio=_as_float(raw.get("jitter_ratio", 0.1), f"{where}.jitter_ratio", 0.0),
    )


def _parse_http(raw: Mapping[str, Any]) -> HttpConfig:
    if not isinstance(raw, Mapping):
        raise ConfigError("http 必须是映射（key: value）")
    concurrency_raw = raw.get("concurrency") or {}
    if not isinstance(concurrency_raw, Mapping):
        raise ConfigError("http.concurrency 必须是映射")
    return HttpConfig(
        timeout_s=_as_float(raw.get("timeout_s", 15.0), "http.timeout_s", 0.001),
        user_agent=_as_str(raw.get("user_agent", "DataPipeBot/0.1"), "http.user_agent"),
        retry=_parse_retry(raw.get("retry") or {}, "http.retry"),
        concurrency=ConcurrencyConfig(
            global_limit=_as_int(
                concurrency_raw.get("global_limit", 8), "http.concurrency.global_limit", 1
            ),
            per_host_limit=_as_int(
                concurrency_raw.get("per_host_limit", 2), "http.concurrency.per_host_limit", 1
            ),
            cooldown_s=_as_float(
                concurrency_raw.get("cooldown_s", 30.0), "http.concurrency.cooldown_s", 0.0
            ),
        ),
    )


def _parse_source(raw: Mapping[str, Any], index: int) -> SourceConfig:
    where = f"sources[{index}]"
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} 必须是映射")

    name = _as_str(_require(raw, "name", where), f"{where}.name")
    where = f"sources[{index}]({name})"

    selectors_raw = raw.get("selectors") or {}
    if not isinstance(selectors_raw, Mapping):
        raise ConfigError(f"{where}.selectors 必须是映射")
    selectors: dict[str, str] = {}
    for key, value in selectors_raw.items():
        selectors[str(key)] = _as_str(value, f"{where}.selectors.{key}")

    fixture_dir = raw.get("fixture_dir")
    return SourceConfig(
        name=name,
        adapter=_as_str(_require(raw, "adapter", where), f"{where}.adapter"),
        base_url=_as_str(_require(raw, "base_url", where), f"{where}.base_url").rstrip("/"),
        list_url=_as_str(_require(raw, "list_url", where), f"{where}.list_url"),
        page_start=_as_int(raw.get("page_start", 1), f"{where}.page_start", 0),
        enabled=bool(raw.get("enabled", True)),
        fixture_dir=None if fixture_dir is None else str(fixture_dir),
        selectors=selectors,
        notes=str(raw.get("notes", "")),
    )


def _parse_output(raw: Mapping[str, Any] | None) -> OutputConfig:
    if raw is None:
        return OutputConfig()
    if not isinstance(raw, Mapping):
        raise ConfigError("output 必须是映射")
    return OutputConfig(
        db_path=str(raw.get("db_path", "data/pipeline.db")),
        out_dir=str(raw.get("out_dir", "out")),
        report_title=str(raw.get("report_title", "公告数据采集报表")),
    )


def load_config(path: str | Path, root: str | Path | None = None) -> AppConfig:
    """读取并校验 YAML 配置。

    Parameters
    ----------
    path:
        配置文件路径（如 ``config/example.yaml``）。
    root:
        项目根目录，用于解析相对路径；默认取配置文件的上两级目录，
        也就是 ``<root>/config/example.yaml`` 的常见布局。

    Raises
    ------
    ConfigError
        文件不存在、YAML 语法错误、必填项缺失或取值非法。
    """
    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    config_path = config_path.resolve()

    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在：{config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败：{config_path}：{exc}") from exc

    if not isinstance(raw, Mapping):
        raise ConfigError(f"配置文件顶层必须是映射：{config_path}")

    version = _as_int(raw.get("version", 1), "version", 1)
    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, list) or not sources_raw:
        raise ConfigError("sources 必须是非空列表")

    sources = tuple(_parse_source(item, i) for i, item in enumerate(sources_raw))
    names = [s.name for s in sources]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"数据源名称重复：{', '.join(sorted(duplicates))}")

    project_root = Path(root).resolve() if root is not None else config_path.parent.parent

    return AppConfig(
        version=version,
        http=_parse_http(raw.get("http") or {}),
        sources=sources,
        output=_parse_output(raw.get("output")),
        path=config_path,
        root=project_root,
    )


def validate_adapters(config: AppConfig) -> None:
    """确认配置里引用的适配器都已注册。

    延迟导入 ``datapipe.sources``：适配器模块本身要 import 本模块的
    ``SourceConfig``，在模块顶层互相导入会形成循环。
    """
    from datapipe.sources import ADAPTERS

    for source in config.sources:
        if source.adapter not in ADAPTERS:
            known = ", ".join(sorted(ADAPTERS)) or "(空)"
            raise ConfigError(
                f"sources[{source.name}].adapter = {source.adapter!r} 未注册，可用：{known}"
            )
