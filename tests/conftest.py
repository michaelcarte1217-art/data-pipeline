"""pytest 公共夹具与路径设置。

把 ``src/`` 插入 ``sys.path``，保证即使没有 ``pip install -e .``，
在仓库根目录直接跑 ``pytest`` 也能导入 ``datapipe``——这对
"clone 下来就能验证"很重要。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402  （必须在 sys.path 调整之后导入）

from datapipe.config import SourceConfig, load_config  # noqa: E402

FIXTURES = ROOT / "fixtures"
CONFIG_PATH = ROOT / "config" / "example.yaml"


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def app_config():
    """加载示例配置（只读，可跨测试共享）。"""
    return load_config(CONFIG_PATH, root=ROOT)


@pytest.fixture()
def source_config() -> SourceConfig:
    """一个最小可用的源配置，供适配器单测使用。"""
    return SourceConfig(
        name="test_source",
        adapter="gov_notice",
        base_url="https://notice.example.gov.cn",
        list_url="https://notice.example.gov.cn/notice/list_page_{page}.html",
        page_start=1,
        selectors={},
    )


@pytest.fixture()
def detail_html(fixtures_dir: Path) -> str:
    return (fixtures_dir / "detail_001.html").read_text(encoding="utf-8")


@pytest.fixture()
def list_html(fixtures_dir: Path) -> str:
    return (fixtures_dir / "list_page_1.html").read_text(encoding="utf-8")
