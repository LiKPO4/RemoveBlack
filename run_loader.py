"""
引导程序入口 (Bootstrapper Loader Entry)

独立轻量引导程序，打包为 RemoveBlack.exe 单文件外壳。
用于启动时检测 %LOCALAPPDATA% 缓存目录，实现首次自动解压与后续秒级启动。
"""

from __future__ import annotations

import sys


def _main() -> None:
    from src.bootstrapper import run_bootstrapper
    run_bootstrapper()


if __name__ == "__main__":
    _main()
