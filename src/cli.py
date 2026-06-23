"""
RemoveBlack 命令行入口。

使用方式：
    removeblack <图片或目录> [图片或目录] ...

- 输入是单图：输出到同目录 <name>_nobg.png
- 输入是目录：批量处理目录下所有支持的图片，输出到同目录 <name>_nobg.png

可选参数：
    --algo {unmult,threshold,chroma,hsv}   选择算法（默认 unmult）
    --alpha-floor N                        透明度下限（0~255，默认 0）

  UnMult 算法专用：
    --strength FLOAT                       不透明度增益（0.5~3.0，默认 1.0）
    --black-cutoff INT                     黑底清理阈值（0~32，默认 8）
    --black-desaturate FLOAT               黑晕抑制（0.0~1.0，默认 0.6）
    --body-density FLOAT                   实体增益（0.0~1.0，默认 0.0）
    --body-low INT                         实体增益下界（0~255，默认 30）
    --body-high INT                        实体增益上界（1~255，默认 120）

  阈值法专用：
    --threshold INT                        黑色阈值（0~128，默认 16）

  颜色键专用：
    --lower INT                            低阈值（默认 8）
    --upper INT                            高阈值（默认 64）

  HSV 去色背景专用：
    --hue INT                              目标色相（0~359，默认 120）
    --hue-tolerance INT                    色相容差（0~180，默认 20）
    --min-saturation INT                   最小饱和度（0~255，默认 40）
    --min-value INT                        最小明度（0~255，默认 40）
    --softness INT                         柔边宽度（1~180，默认 30）

  批量：
    --recursive                            批量时递归子目录
    --out DIR                              指定输出目录（保持相对路径）

也支持把多个文件 / 文件夹直接拖到 .exe 图标上。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import process_file, process_folder
from .core.algorithms import ALGORITHMS


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="removeblack",
        description="批量把黑底图转换为带透明通道的 PNG。",
    )
    p.add_argument("inputs", nargs="+", help="一个或多个图片 / 目录")
    p.add_argument(
        "--algo",
        choices=list(ALGORITHMS.keys()),
        default="unmult",
        help="去黑底算法（默认：unmult）",
    )
    p.add_argument(
        "--alpha-floor",
        type=int,
        default=0,
        help="透明度下限（0~255），仅对非纯黑前景生效（默认：0）",
    )
    p.add_argument(
        "--bg-color",
        default=None,
        help='吸管背景色，格式 "R,G,B"（默认：0,255,0）。对 unmult_color / color_key 算法生效',
    )

    # UnMult
    p.add_argument(
        "--strength",
        type=float,
        default=1.0,
        help="UnMult 不透明度增益（默认：1.0）",
    )
    p.add_argument(
        "--black-cutoff",
        type=int,
        default=8,
        help="UnMult 黑底清理阈值（默认：8）",
    )
    p.add_argument(
        "--black-desaturate",
        type=float,
        default=0.6,
        help="UnMult 黑晕抑制（默认：0.6）",
    )
    p.add_argument(
        "--color-cutoff",
        type=int,
        default=8,
        help="unmult_color 背景清理阈值（默认：8）",
    )
    p.add_argument(
        "--color-desaturate",
        type=float,
        default=0.6,
        help="unmult_color 背景外扩抑制（默认：0.6）",
    )
    p.add_argument(
        "--body-density",
        type=float,
        default=0.0,
        help="UnMult 实体增益（默认：0.0）",
    )
    p.add_argument(
        "--body-low",
        type=int,
        default=30,
        help="UnMult 实体增益下界（默认：30）",
    )
    p.add_argument(
        "--body-high",
        type=int,
        default=120,
        help="UnMult 实体增益上界（默认：120）",
    )

    # Threshold
    p.add_argument(
        "--threshold",
        type=int,
        default=16,
        help="threshold 算法阈值（默认：16）",
    )

    # Chroma key
    p.add_argument(
        "--lower",
        type=int,
        default=8,
        help="chroma 算法低阈值（默认：8）",
    )
    p.add_argument(
        "--upper",
        type=int,
        default=64,
        help="chroma 算法高阈值（默认：64）",
    )

    # HSV
    p.add_argument(
        "--hue",
        type=int,
        default=120,
        help="hsv 算法目标色相（默认：120）",
    )
    p.add_argument(
        "--hue-tolerance",
        type=int,
        default=20,
        help="hsv 算法色相容差（默认：20）",
    )
    p.add_argument(
        "--min-saturation",
        type=int,
        default=40,
        help="hsv 算法最小饱和度（默认：40）",
    )
    p.add_argument(
        "--min-value",
        type=int,
        default=40,
        help="hsv 算法最小明度（默认：40）",
    )
    p.add_argument(
        "--softness",
        type=int,
        default=30,
        help="hsv 算法柔边宽度（默认：30）",
    )

    p.add_argument(
        "--recursive", action="store_true", help="批量时递归处理子目录"
    )
    p.add_argument(
        "--out",
        default=None,
        help="输出目录（不指定则与源同目录、加 _nobg 后缀）",
    )
    return p


def _parse_bg_color(raw: str | None) -> dict:
    if raw is None:
        return {}
    parts = raw.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f'--bg-color must be "R,G,B", got: {raw}'
        )
    try:
        return {
            "bg_r": int(parts[0]),
            "bg_g": int(parts[1]),
            "bg_b": int(parts[2]),
        }
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f'--bg-color must be "R,G,B" integers, got: {raw}'
        ) from e


def _algo_kwargs(args) -> dict:
    if args.algo == "threshold":
        return {"threshold": args.threshold}
    if args.algo == "chroma":
        return {"lower": args.lower, "upper": args.upper}
    if args.algo == "hsv":
        return {
            "hue": args.hue,
            "hue_tolerance": args.hue_tolerance,
            "min_saturation": args.min_saturation,
            "min_value": args.min_value,
            "softness": args.softness,
        }
    if args.algo == "unmult_color":
        return {
            **_parse_bg_color(args.bg_color),
            "strength": args.strength,
            "color_cutoff": args.color_cutoff,
            "color_desaturate": args.color_desaturate,
            "body_density": args.body_density,
            "body_low": args.body_low,
            "body_high": args.body_high,
        }
    if args.algo == "color_key":
        return {
            **_parse_bg_color(args.bg_color),
            "lower": args.lower,
            "upper": args.upper,
        }
    # unmult
    return {
        "strength": args.strength,
        "black_cutoff": args.black_cutoff,
        "black_desaturate": args.black_desaturate,
        "body_density": args.body_density,
        "body_low": args.body_low,
        "body_high": args.body_high,
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    kwargs = _algo_kwargs(args)
    out_root = Path(args.out) if args.out else None

    total_ok = 0
    total_fail = 0
    for raw in args.inputs:
        p = Path(raw)
        if not p.exists():
            print(f"[SKIP] not found: {p}")
            total_fail += 1
            continue

        if p.is_dir():
            print(f"[BATCH] {p}")
            written = process_folder(
                p,
                out_root,
                algorithm=args.algo,
                recursive=args.recursive,
                alpha_floor=args.alpha_floor,
                progress=lambda d, t, c: print(f"  [{d}/{t}] {Path(c).name}"),
                **kwargs,
            )
            total_ok += len(written)
        else:
            try:
                if out_root is not None:
                    out_root.mkdir(parents=True, exist_ok=True)
                    dst = out_root / f"{p.stem}_nobg.png"
                else:
                    dst = None
                out = process_file(
                    p, dst, algorithm=args.algo, alpha_floor=args.alpha_floor, **kwargs
                )
                print(f"[OK] {p}  ->  {out}")
                total_ok += 1
            except Exception as e:
                print(f"[FAIL] {p}: {e}")
                total_fail += 1

    print(f"\nDone. success={total_ok}, failed={total_fail}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
