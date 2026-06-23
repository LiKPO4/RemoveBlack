"""
GUI 启动入口：`python -m src.app` 或打包后双击 exe。

也支持把图片 / 文件夹拖到 .exe 图标上，主窗口会自动加载或进入批处理模式。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_path(rel: str) -> Path:
    """
    返回资源文件的绝对路径。
    - 源码运行时：相对于项目根目录
    - PyInstaller 打包后：相对于 _MEIPASS 解压目录
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / rel
    # src/app.py -> src -> 项目根
    return Path(__file__).resolve().parent.parent / rel


# 顶层导入 GUI，确保 PyInstaller 静态分析能追到 PySide6 依赖
from .gui import run  # noqa: E402


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
