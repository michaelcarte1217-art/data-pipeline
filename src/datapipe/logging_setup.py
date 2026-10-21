"""统一日志配置：控制台 + 文件双输出。

格式固定为 ``%(asctime)s %(levelname)s %(name)s %(message)s``，
既便于人读，也能直接被 ``grep``/``awk`` 做简单聚合。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

__all__ = ["LOG_FORMAT", "LOG_DATEFMT", "setup_logging"]

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    *,
    verbose: bool = False,
    log_file: str | Path | None = None,
    quiet: bool = False,
) -> logging.Logger:
    """初始化根 logger。

    Parameters
    ----------
    verbose:
        为 True 时控制台输出 DEBUG，否则 INFO。
    log_file:
        文件日志路径；父目录会自动创建。传 None 则只输出到控制台。
    quiet:
        为 True 时控制台只输出 WARNING 及以上（文件日志不受影响）。

    重复调用是安全的：会先移除本函数此前挂上的 handler，
    避免在测试或多次调用 ``main()`` 时出现重复行。
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for handler in list(root.handlers):
        if getattr(handler, "_datapipe_managed", False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    if not quiet:
        console = logging.StreamHandler(stream=sys.stderr)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(formatter)
        console._datapipe_managed = True  # type: ignore[attr-defined]
        root.addHandler(console)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        # 文件日志始终记录 DEBUG，便于事后排查
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        file_handler._datapipe_managed = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)

    # 第三方库的噪声压低
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logging.getLogger("datapipe")
