"""
Bootstrapper 启动引导器：
单文件 RemoveBlack.exe 运行时，优先检测本地固定缓存目录 (%LOCALAPPDATA%\\RemoveBlack\\app_{VERSION})。

- 若已存在完整核心程序：直接启动核心 exe，实现秒开 (耗时 < 0.3s)。
- 若首次使用/版本不一致：自动释放内置运行环境至固定目录，之后拉起核心程序。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

APP_NAME = "RemoveBlack"
CURRENT_VERSION = "v1.6.1"


def get_cache_dir() -> Path:
    """获取本地固定缓存根目录 (%LOCALAPPDATA%\\RemoveBlack\\app_{VERSION})。"""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        base = Path(local_appdata)
    else:
        base = Path.home() / "AppData" / "Local"
    return base / APP_NAME / f"app_{CURRENT_VERSION}"


def run_bootstrapper() -> None:
    cache_dir = get_cache_dir()
    target_exe = cache_dir / "RemoveBlack_Core.exe"
    version_file = cache_dir / "version.txt"

    # 判断是否已有完整核心缓存
    if target_exe.exists() and version_file.exists():
        try:
            if version_file.read_text(encoding="utf-8").strip() == CURRENT_VERSION:
                # 调起已解压的核心程序，传递所有命令行参数
                cmd = [str(target_exe)] + sys.argv[1:]
                kwargs = {}
                if sys.platform == "win32":
                    kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                subprocess.Popen(cmd, **kwargs)
                sys.exit(0)
        except Exception as e:
            print(f"[Bootstrapper] 缓存校验异常，重新解压: {e}")

    # 若未找到缓存或校验失败，进行首次释放
    print(f"[RemoveBlack] 首次启动准备中，正在解压运行环境至: {cache_dir}")
    
    # 找内置包 (bundled core zip 或 _MEIPASS 资源)
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    core_zip = base_dir / "core_bundle.zip"
    
    cache_dir.mkdir(parents=True, exist_ok=True)

    if core_zip.exists():
        with zipfile.ZipFile(core_zip, "r") as zf:
            zf.extractall(cache_dir)
    else:
        # 如果是开发调试状态或直接源码复制
        core_dir = base_dir / "core_bundle"
        if core_dir.exists():
            for item in core_dir.iterdir():
                dst = cache_dir / item.name
                if item.is_dir():
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(item, dst)
                else:
                    shutil.copy2(item, dst)

    # 写入版本标记
    version_file.write_text(CURRENT_VERSION, encoding="utf-8")

    # 启动刚解压完成的核心程序
    if target_exe.exists():
        cmd = [str(target_exe)] + sys.argv[1:]
        subprocess.Popen(cmd)
        sys.exit(0)
    else:
        print("[ERROR] 未能在缓存目录找到 RemoveBlack_Core.exe")
        sys.exit(1)


if __name__ == "__main__":
    run_bootstrapper()
