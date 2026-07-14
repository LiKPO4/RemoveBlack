"""
统一日志模块。

- 日志文件写入 ``%LOCALAPPDATA%/RemoveBlack/removeblack.log``（轮转，最大 5×1 MiB）。
- 同时输出到 stderr（WARNING 及以上）。

用法：
    from .logger import logger
    logger.info("hello %s", name)
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_log_initialized: bool = False
logger = logging.getLogger("RemoveBlack")


def _default_log_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return Path(base) / "RemoveBlack"


def init_logging(level: int = logging.INFO) -> None:
    """初始化日志系统（幂等，多次调用安全）。"""
    global _log_initialized, logger
    if _log_initialized:
        return
    _log_initialized = True

    logger.setLevel(level)

    # 格式：时间 级别 模块:行号 -- 消息
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s:%(lineno)d -- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件 handler（轮转，最多保留 5 个备份，每个 ≤ 1 MiB）
    log_dir = _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        log_dir / "removeblack.log",
        maxBytes=1 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # stderr handler（仅 WARNING+，避免干扰正常交互）
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("RemoveBlack logging started (log: %s)", fh.baseFilename)
