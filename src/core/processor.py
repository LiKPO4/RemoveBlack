"""
单图与批量处理调度。

- process_file:    处理一张图，输出 PNG（带透明通道）。
- process_folder:  扫描目录，批量处理，可选回调汇报进度。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np
from PIL import Image

from .algorithms import ALGORITHMS, apply_protection

SUPPORTED_INPUT_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff", ".webp"}


def _load_image(path: str | os.PathLike) -> np.ndarray:
    """读图为 RGBA uint8。"""
    img = Image.open(path)
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    return np.array(img)


def _save_png(arr: np.ndarray, path: str | os.PathLike) -> None:
    """保存 RGBA numpy 数组到 PNG。"""
    Image.fromarray(arr, mode="RGBA").save(path, format="PNG", optimize=True)


def process_file(
    src: str | os.PathLike,
    dst: Optional[str | os.PathLike] = None,
    algorithm: str = "unmult",
    alpha_floor: int = 0,
    **params,
) -> Path:
    """
    处理单张图片并保存为 PNG。

    Parameters
    ----------
    src         源图片路径
    dst         目标路径；为 None 时输出到源同目录的 ``<name>_nobg.png``
    algorithm   "unmult" / "threshold" / "chroma" / "hsv"
    alpha_floor 透明度下限（0~255），仅对非纯黑前景生效
    **params    传给算法的额外参数

    Returns
    -------
    Path  实际写入的文件路径
    """
    src = Path(src)
    if algorithm not in ALGORITHMS:
        raise ValueError(f"unknown algorithm: {algorithm}")

    arr = _load_image(src)
    func = ALGORITHMS[algorithm]["func"]
    result = func(arr, **params)

    if alpha_floor > 0:
        src_rgb = arr[..., :3] if arr.ndim == 3 and arr.shape[2] >= 3 else None
        result = apply_protection(result, src_rgb=src_rgb, alpha_floor=alpha_floor)

    if dst is None:
        dst = src.with_name(f"{src.stem}_nobg.png")
    else:
        dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _save_png(result, dst)
    return dst


def _iter_images(folder: Path, recursive: bool) -> Iterable[Path]:
    it = folder.rglob("*") if recursive else folder.glob("*")
    for p in it:
        if p.is_file() and p.suffix.lower() in SUPPORTED_INPUT_EXTS:
            yield p


def process_folder(
    src_dir: str | os.PathLike,
    dst_dir: Optional[str | os.PathLike] = None,
    algorithm: str = "unmult",
    recursive: bool = False,
    suffix: str = "_nobg",
    progress: Optional[Callable[[int, int, Path], None]] = None,
    alpha_floor: int = 0,
    **params,
) -> list[Path]:
    """
    批量处理目录下的图片。

    Parameters
    ----------
    src_dir    源目录
    dst_dir    目标目录；为 None 时输出到源同目录（带后缀）
    algorithm  算法名
    recursive  是否递归子目录
    suffix     输出文件名后缀（仅当 dst_dir 为 None 时生效；为空则覆盖会报错）
    progress    回调 (done, total, current_path)
    alpha_floor 透明度下限（0~255）
    **params    算法参数

    Returns
    -------
    list[Path]  成功写入的文件列表
    """
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        raise ValueError(f"not a directory: {src_dir}")

    files = list(_iter_images(src_dir, recursive))
    total = len(files)
    written: list[Path] = []

    for i, src in enumerate(files, 1):
        if dst_dir is None:
            dst = src.with_name(f"{src.stem}{suffix}.png")
        else:
            rel = src.relative_to(src_dir)
            dst = Path(dst_dir) / rel.with_suffix(".png")
        try:
            out = process_file(
                src, dst, algorithm=algorithm, alpha_floor=alpha_floor, **params
            )
            written.append(out)
        except Exception as e:  # 单图失败不影响整批
            print(f"[WARN] failed to process {src}: {e}")
        finally:
            if progress is not None:
                progress(i, total, src)

    return written
