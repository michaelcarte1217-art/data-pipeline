"""适配器注册表。

新增一个数据源适配器的完整流程只有三步：

1. 在 ``sources/`` 下新建模块，继承 :class:`SourceAdapter` 并实现
   ``parse_list`` / ``parse_detail``，类属性 ``name`` 即 YAML 里的 ``adapter`` 值；
2. 在本文件的 ``ADAPTERS`` 里登记一行；
3. 在 YAML 的 ``sources`` 里引用该 ``adapter``。

``get_adapter`` 是编排层唯一的入口，未注册的名字会给出可读的错误，
而不是让 ``KeyError`` 泄漏到调用方。
"""

from __future__ import annotations

from datapipe.sources.base import (
    ParseError,
    SourceAdapter,
    first_node,
    node_text,
    parse_html,
    snippet,
)
from datapipe.sources.gov_notice import GovNoticeAdapter

__all__ = [
    "ADAPTERS",
    "ParseError",
    "SourceAdapter",
    "first_node",
    "get_adapter",
    "node_text",
    "parse_html",
    "snippet",
]

#: 适配器名 -> 实现类。键即配置里的 ``adapter`` 字段。
ADAPTERS: dict[str, type[SourceAdapter]] = {
    GovNoticeAdapter.name: GovNoticeAdapter,
}


def get_adapter(name: str) -> type[SourceAdapter]:
    """按名称取适配器类。

    Raises
    ------
    ValueError
        名称未注册，错误信息会列出当前可用名称。
    """
    try:
        return ADAPTERS[name]
    except KeyError:
        known = ", ".join(sorted(ADAPTERS)) or "(空)"
        raise ValueError(f"未注册的适配器 {name!r}，可用：{known}") from None
