"""
GUI 启动器（PyInstaller 入口）。

放在项目根目录，使用绝对导入，避免 PyInstaller 把 src/app.py 当顶层脚本时
出现 "attempted relative import with no known parent package" 报错。
"""

from __future__ import annotations

import sys


def _entry() -> int:
    from src.app import main
    return main()


if __name__ == "__main__":
    sys.exit(_entry())
