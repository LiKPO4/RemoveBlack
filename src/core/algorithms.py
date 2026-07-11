"""
图片去黑底核心算法。

包含三种主流算法：
- unmult_black:  Unmultiply Black（AE UnMult 插件原理），对应原文方案。
                 公式：A = max(R, G, B); RGB' = RGB / A
                 合成回黑底时 pixel = RGB' * A = RGB，视觉零损失。
- threshold:     纯阈值法。低于阈值的像素直接设为透明，高于阈值的全不透明。
- chroma_key:    颜色键（双阈值线性映射）。在 lower/upper 之间线性渐变 alpha，
                 能保留半透明边缘和发光特效。

所有函数输入 / 输出均为 numpy uint8 数组，shape 为 (H, W, 4)，通道顺序 RGBA。
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "unmult_black",
    "unmult_color",
    "color_key",
    "threshold_black",
    "chroma_key_black",
    "hsv_key",
    "apply_protection",
    "magic_wand_select",
    "ALGORITHMS",
]


def _to_rgba_float(img: np.ndarray) -> np.ndarray:
    """把任意 RGB / RGBA / 灰度 uint8 图转成 (H, W, 4) float32（0~1）。"""
    if img.dtype != np.uint8:
        raise ValueError(f"expected uint8 image, got {img.dtype}")

    if img.ndim == 2:  # 灰度
        img = np.stack([img, img, img], axis=-1)

    if img.shape[2] == 3:
        h, w = img.shape[:2]
        alpha = np.full((h, w, 1), 255, dtype=np.uint8)
        img = np.concatenate([img, alpha], axis=-1)
    elif img.shape[2] != 4:
        raise ValueError(f"unsupported channel count: {img.shape[2]}")

    return img.astype(np.float32) / 255.0


def _to_uint8(img_f: np.ndarray) -> np.ndarray:
    """float32 [0,1] -> uint8 [0,255]，并裁剪。"""
    return np.clip(img_f * 255.0 + 0.5, 0, 255).astype(np.uint8)


def unmult_black(
    img: np.ndarray,
    strength: float = 1.0,
    body_density: float = 0.0,
    black_cutoff: int = 8,
    black_desaturate: float = 0.6,
    body_low: int = 30,
    body_high: int = 120,
) -> np.ndarray:
    """
    Unmultiply Black（AE UnMult 插件算法）+ 强度调节 + 实体增益。

    **原理（strength = 1, body_density = 0 时）**

        A  = max(R, G, B)
        R' = R / A,  G' = G / A,  B' = B / A

    合成回黑底时 (RGB' * A) 与原图完全一致，因而视觉零损失。

    **black_cutoff（黑底清理）**

    很多看起来纯黑的背景，实际会有 RGB=1~8 的暗灰噪声或压缩痕迹。
    UnMult 会把这些近黑点变成极低 alpha 的半透明黑雾；在橙色/白色底色预览时会显出一块矩形脏底。
    本参数把亮度 <= black_cutoff 的像素强制归零。默认 8，一般不会伤到有效烟雾/发光边缘。

    **strength（不透明度增益，全图）**

    UnMult 的天生缺陷是把饱和色当成"半透明"——例如纯红 (200,30,30)
    的 alpha 只有 200/255 ≈ 78%，瓶身就半透明了。
    本参数同时调整 alpha 和 RGB 让物体逐渐变得不透明而**不偏色**：

        - 1.0 → 纯 UnMult（最干净的去黑边，但饱和色偏透）
        - 1.5 ~ 2.5 → 推荐区间，物体接近不透明且色彩自然
        - 3.0 → 等价于"阈值法"：颜色完全等于原图，alpha 全开

    **body_density（实体增益，仅作用于"中亮度区域"）**

    专门解决"暗色玻璃 / 暗色物体表面漏底"——瓶身玻璃 max(RGB)≈60
    被 UnMult 算成 24% alpha，即使 strength=3 也只有 72%；而周围烟雾
    max(RGB)≈30 用 alpha floor 一抹就脏。
    本参数用 smoothstep 仅对"亮度 ∈ [body_low, body_high]"的像素抬高 alpha：

        new_alpha = alpha + (1 - alpha) * body_density * smoothstep(low, high, max_rgb)

        - body_density = 0.0 → 不动，原 UnMult 行为
        - body_density = 0.6 ~ 1.0 → 瓶身这种中等亮度区域趋近不透明
        - 烟雾（亮度 < body_low）和真黑（=0）完全不受影响 → 保持柔和透明

    Parameters
    ----------
    img          : np.ndarray  uint8, (H, W, 3) 或 (H, W, 4)
    strength     : float, 0.5 ~ 3.0，默认 1.0
    body_density : float, 0.0 ~ 1.0，默认 0.0
    black_cutoff : int, 0 ~ 32，近黑背景清理阈值，默认 8
    black_desaturate : float, 0.0 ~ 1.0，近黑外扩抑制，默认 0.6
    body_low     : int, 0 ~ 255，软阈值下界，低于此亮度不受影响
    body_high    : int, 1 ~ 255，软阈值上界，高于此亮度直接拉满

    Returns
    -------
    np.ndarray  uint8, (H, W, 4)，RGBA
    """
    f = _to_rgba_float(img)
    rgb = f[..., :3]

    # alpha = max(R, G, B)
    alpha = np.max(rgb, axis=-1, keepdims=True)  # (H, W, 1)

    # 防止除零：alpha == 0 的像素本来就是黑色，输出全透明即可
    safe_alpha = np.where(alpha > 1e-6, alpha, 1.0)
    unmult_rgb = rgb / safe_alpha
    unmult_rgb = np.where(alpha > 1e-6, unmult_rgb, 0.0)

    s = max(0.01, float(strength))
    if s == 1.0:
        new_rgb = unmult_rgb
        new_alpha = alpha
    else:
        new_alpha = np.clip(alpha * s, 0.0, 1.0)
        # RGB 向原图回退：strength 越大越接近原图色，避免过饱和偏色
        t = min(1.0, 1.0 / s)
        new_rgb = unmult_rgb * t + rgb * (1.0 - t)

    # ---- 实体增益：仅作用于中亮度区域 ----
    bd = float(body_density)
    if bd > 0.0:
        lo = max(0, min(255, int(body_low))) / 255.0
        hi = max(lo + 1e-3, min(255, int(body_high))) / 255.0
        x = np.clip((alpha - lo) / (hi - lo), 0.0, 1.0)
        # smoothstep: 3x^2 - 2x^3
        weight = x * x * (3.0 - 2.0 * x)
        boost = (1.0 - new_alpha) * bd * weight
        new_alpha = np.clip(new_alpha + boost, 0.0, 1.0)

    # ---- 近黑外扩抑制 ----
    # black_cutoff 只负责"一刀切"清掉很暗的背景点；但 AI 图常见的黑色外扩/暗晕
    # 往往亮度已经高于 cutoff（例如 40~90），直接切会误伤烟雾/外发光。
    # 这里改用"软抑制"：越接近黑色，alpha 越被压低；越亮越不受影响。
    # 这样可以把黑色阴影外扩压下去，同时尽量保留青色烟雾和高光。
    cutoff = max(0, min(255, int(black_cutoff))) / 255.0
    if cutoff > 0.0:
        near_black = alpha <= cutoff
        new_alpha = np.where(near_black, 0.0, new_alpha)
        new_rgb = np.where(near_black, 0.0, new_rgb)

    desat = max(0.0, min(1.0, float(black_desaturate)))
    if desat > 0.0:
        # 抑制区间随 cutoff 扩展：cutoff=32 时，约 32~128 进入软过渡。
        hi = min(1.0, max(cutoff + 1.0 / 255.0, cutoff * 4.0))
        x = np.clip((alpha - cutoff) / max(1e-6, hi - cutoff), 0.0, 1.0)
        smooth = x * x * (3.0 - 2.0 * x)
        # desat=0 不处理；desat=1 时 cutoff 附近几乎完全压掉，并向高亮度平滑恢复。
        suppress = (1.0 - desat) + desat * smooth
        new_alpha = new_alpha * suppress

    out = np.concatenate([new_rgb, new_alpha], axis=-1)
    return _to_uint8(out)


def unmult_color(
    img: np.ndarray,
    bg_r: int = 0,
    bg_g: int = 255,
    bg_b: int = 0,
    strength: float = 1.0,
    body_density: float = 0.0,
    color_cutoff: int = 8,
    color_desaturate: float = 0.6,
    body_low: int = 30,
    body_high: int = 120,
) -> np.ndarray:
    """
    Unmultiply Color（推广版 UnMult）：以任意吸管吸取的背景色为基准去底。

    合成公式：

        C_result = B × (1 - A) + C_fg × A

    移项得到：

        D      = C_result - B
        A      = max(|D_r|, |D_g|, |D_b|) / 255
        C_fg   = B + D / A

    当背景色 B = (0,0,0) 时，此函数与 ``unmult_black()`` 完全一致。

    Parameters
    ----------
    img             : np.ndarray uint8, (H, W, 3) 或 (H, W, 4)
    bg_r, bg_g, bg_b: int 0~255，吸管吸取的背景色
    strength        : float 0.5~3.0，不透明度增益（同 UnMult）
    body_density    : float 0.0~1.0，实体增益（同 UnMult）
    color_cutoff    : int 0~32，近背景色清理阈值（对应 black_cutoff）
    color_desaturate: float 0.0~1.0，背景色外扩抑制（对应 black_desaturate）
    body_low, body_high: int，实体增益的亮度区间

    Returns
    -------
    np.ndarray uint8, (H, W, 4)，RGBA
    """
    f = _to_rgba_float(img)
    rgb = f[..., :3]

    bg = np.array([bg_r, bg_g, bg_b], dtype=np.float32) / 255.0
    bg = bg.reshape(1, 1, 3)

    diff = rgb - bg
    # 背景相对最大偏差：对每个通道，背景可以往上或往下走到 0/255 的极值。
    max_dev = np.maximum(bg, 1.0 - bg)
    alpha = np.max(np.abs(diff) / np.maximum(max_dev, 1e-6), axis=-1, keepdims=True)

    safe_alpha = np.where(alpha > 1e-6, alpha, 1.0)
    unmult_rgb = bg + diff / safe_alpha
    unmult_rgb = np.where(alpha > 1e-6, unmult_rgb, 0.0)

    s = max(0.01, float(strength))
    if s == 1.0:
        new_rgb = unmult_rgb
        new_alpha = alpha
    else:
        new_alpha = np.clip(alpha * s, 0.0, 1.0)
        t = min(1.0, 1.0 / s)
        new_rgb = unmult_rgb * t + rgb * (1.0 - t)

    # ---- 实体增益：仅作用于中亮度区域 ----
    bd = float(body_density)
    if bd > 0.0:
        lo = max(0, min(255, int(body_low))) / 255.0
        hi = max(lo + 1e-3, min(255, int(body_high))) / 255.0
        x = np.clip((alpha - lo) / (hi - lo), 0.0, 1.0)
        weight = x * x * (3.0 - 2.0 * x)
        boost = (1.0 - new_alpha) * bd * weight
        new_alpha = np.clip(new_alpha + boost, 0.0, 1.0)

    # ---- 近背景色清理 ----
    cutoff = max(0, min(255, int(color_cutoff))) / 255.0
    if cutoff > 0.0:
        near_bg = alpha <= cutoff
        new_alpha = np.where(near_bg, 0.0, new_alpha)
        new_rgb = np.where(near_bg, 0.0, new_rgb)

    desat = max(0.0, min(1.0, float(color_desaturate)))
    if desat > 0.0:
        hi = min(1.0, max(cutoff + 1.0 / 255.0, cutoff * 4.0))
        x = np.clip((alpha - cutoff) / max(1e-6, hi - cutoff), 0.0, 1.0)
        smooth = x * x * (3.0 - 2.0 * x)
        suppress = (1.0 - desat) + desat * smooth
        new_alpha = new_alpha * suppress

    out = np.concatenate([new_rgb, new_alpha], axis=-1)
    return _to_uint8(out)


def color_key(
    img: np.ndarray,
    bg_r: int = 0,
    bg_g: int = 255,
    bg_b: int = 0,
    lower: int = 8,
    upper: int = 64,
) -> np.ndarray:
    """
    背景色键控（Color Key）：按像素与背景色的距离直接映射 alpha，保留原 RGB。

    与 UnMult 不同，本算法不对 RGB 做除法，因此不会产生色相偏移；
    代价是边缘可能残留少量背景色（如绿边）。适合对色彩准确度要求高的场景。

    距离度量采用 max-norm（各通道绝对偏差的最大值），对色相变化更鲁棒。

    Parameters
    ----------
    img             : np.ndarray uint8, (H, W, 3) 或 (H, W, 4)
    bg_r, bg_g, bg_b: int 0~255，吸管吸取的背景色
    lower           : int 0~255，距离 ≤ lower 完全透明
    upper           : int 0~255，距离 ≥ upper 完全不透明

    Returns
    -------
    np.ndarray uint8, (H, W, 4)，RGBA
    """
    if upper <= lower:
        raise ValueError(f"upper ({upper}) must be greater than lower ({lower})")

    f = _to_rgba_float(img)
    rgb = f[..., :3]

    bg = np.array([bg_r, bg_g, bg_b], dtype=np.float32) / 255.0
    bg = bg.reshape(1, 1, 3)

    dist = np.max(np.abs(rgb - bg), axis=-1, keepdims=True)

    lo = lower / 255.0
    up = upper / 255.0
    alpha = np.clip((dist - lo) / (up - lo), 0.0, 1.0)

    out = np.concatenate([rgb, alpha], axis=-1)
    return _to_uint8(out)


def threshold_black(img: np.ndarray, threshold: int = 16) -> np.ndarray:
    """
    阈值法去黑底。

    亮度（max(R,G,B)）小于等于 threshold 的像素直接置为完全透明，
    其余像素保持原色且完全不透明。

    Parameters
    ----------
    img : np.ndarray  uint8
    threshold : int   0 ~ 255，建议 8 ~ 32

    Returns
    -------
    np.ndarray  uint8 RGBA
    """
    f = _to_rgba_float(img)
    rgb = f[..., :3]
    luminance = np.max(rgb, axis=-1, keepdims=True)  # (H, W, 1)

    t = threshold / 255.0
    keep = (luminance > t).astype(np.float32)
    out = np.concatenate([rgb, keep], axis=-1)
    return _to_uint8(out)


def chroma_key_black(
    img: np.ndarray, lower: int = 8, upper: int = 64
) -> np.ndarray:
    """
    颜色键（黑色版）—— 双阈值线性映射。

    亮度 <= lower：完全透明
    亮度 >= upper：完全不透明
    中间区域：alpha 线性插值。

    比 threshold 更柔和，能保留抗锯齿边缘与半透明发光特效。

    Parameters
    ----------
    img : np.ndarray  uint8
    lower : int       0 ~ 255，低阈值
    upper : int       0 ~ 255，高阈值，必须大于 lower

    Returns
    -------
    np.ndarray  uint8 RGBA
    """
    if upper <= lower:
        raise ValueError(f"upper ({upper}) must be greater than lower ({lower})")

    f = _to_rgba_float(img)
    rgb = f[..., :3]
    luminance = np.max(rgb, axis=-1, keepdims=True)

    lo = lower / 255.0
    up = upper / 255.0
    alpha = np.clip((luminance - lo) / (up - lo), 0.0, 1.0)

    out = np.concatenate([rgb, alpha], axis=-1)
    return _to_uint8(out)


def hsv_key(
    img: np.ndarray,
    hue: int = 120,
    hue_tolerance: int = 20,
    min_saturation: int = 40,
    min_value: int = 40,
    softness: int = 30,
) -> np.ndarray:
    """HSV 去色背景：按目标色相范围把背景变透明。

    hue 为目标色相 0~359；hue_tolerance 控制完全透明核心范围；
    softness 是额外柔边宽度，范围外逐渐恢复不透明。
    min_saturation/min_value 用于避免灰色、黑色、白色被误判为彩色背景。
    """
    if not (0 <= int(hue) <= 359):
        raise ValueError("hue 必须在 0~359 之间")
    if int(hue_tolerance) < 0:
        raise ValueError("hue_tolerance 不能为负数")
    if int(softness) < 0:
        raise ValueError("softness 不能为负数")
    if int(min_saturation) < 0 or int(min_value) < 0:
        raise ValueError("min_saturation / min_value 不能为负数")

    f = _to_rgba_float(img)
    rgb = f[..., :3]
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = np.max(rgb, axis=-1)
    mn = np.min(rgb, axis=-1)
    delta = mx - mn

    h = np.zeros_like(mx)
    mask = delta > 1e-6
    rmax = mask & (mx == r)
    gmax = mask & (mx == g)
    bmax = mask & (mx == b)
    h = np.where(rmax, ((g - b) / np.maximum(delta, 1e-6)) % 6.0, h)
    h = np.where(gmax, ((b - r) / np.maximum(delta, 1e-6)) + 2.0, h)
    h = np.where(bmax, ((r - g) / np.maximum(delta, 1e-6)) + 4.0, h)
    h = h * 60.0

    s = np.where(mx <= 1e-6, 0.0, delta / np.maximum(mx, 1e-6))
    v = mx

    target = float(int(hue) % 360)
    dh = np.abs(((h - target + 180.0) % 360.0) - 180.0)
    core = max(0.0, float(hue_tolerance))
    soft = max(1.0, float(softness))
    color_ok = (s >= min_saturation / 255.0) & (v >= min_value / 255.0)

    # dist<=core 完全透明；core~core+soft 柔边恢复；更远完全不透明
    alpha = np.clip((dh - core) / soft, 0.0, 1.0)
    alpha = np.where(color_ok, alpha, 1.0)
    alpha = alpha[..., None]
    out = np.concatenate([rgb, alpha], axis=-1)
    return _to_uint8(out)
def apply_protection(
    result_rgba: np.ndarray,
    src_rgb: np.ndarray | None = None,
    mask: np.ndarray | None = None,
    alpha_floor: int = 0,
    bg_threshold: int = 8,
) -> np.ndarray:
    """
    去黑底后处理：两种独立、可叠加的补救手段。

    1. **alpha_floor**（透明度下限，全图）
       把所有"原图非纯黑"的像素 alpha 抬高到至少 alpha_floor，
       防止 UnMult 把暗色前景一并吃掉。
       仅对原图亮度 > bg_threshold 的像素生效，避免把真背景变可见。

    2. **mask**（保护蒙版，"原图直通"语义）
       传入 (H, W) uint8 数组。该区域的语义是：
       **请保留原图本来的样子，不要做任何去黑底处理**。
       因此 mask 区域内：
           - RGB 用原图 RGB（不再做 UnMult 的归一化）
           - alpha = max(算法 alpha, mask 值)
       mask=0 像素完全不动；中间值在算法结果与原图之间按权重线性混合。

    Parameters
    ----------
    result_rgba : np.ndarray  uint8 (H, W, 4)，算法输出
    src_rgb     : np.ndarray  uint8 (H, W, 3)，原图（mask 模式必须传）
    mask        : np.ndarray  uint8 (H, W) 或 None
    alpha_floor : int 0~255
    bg_threshold: int 0~64，alpha_floor 的真背景判定阈值

    Returns
    -------
    np.ndarray  uint8 RGBA
    """
    if result_rgba.ndim != 3 or result_rgba.shape[2] != 4:
        raise ValueError(f"expected RGBA, got shape {result_rgba.shape}")

    out = result_rgba.copy()

    # ---- 1. alpha_floor：仅作用于"非真背景" ----
    if alpha_floor > 0:
        a = out[..., 3].astype(np.int32)
        if src_rgb is not None:
            src_max = src_rgb.max(axis=-1).astype(np.int32)
            floor_arr = np.where(src_max <= bg_threshold, 0, alpha_floor)
        else:
            floor_arr = np.full_like(a, alpha_floor)
        out[..., 3] = np.clip(np.maximum(a, floor_arr), 0, 255).astype(np.uint8)

    # ---- 2. mask：在该区域用"原图直通"覆盖算法结果 ----
    if mask is not None and mask.any():
        if mask.shape[:2] != out.shape[:2]:
            raise ValueError(
                f"mask shape {mask.shape[:2]} does not match image {out.shape[:2]}"
            )
        if src_rgb is None:
            # 没有原图就只能抬 alpha
            a = out[..., 3].astype(np.int32)
            out[..., 3] = np.clip(
                np.maximum(a, mask.astype(np.int32)), 0, 255
            ).astype(np.uint8)
        else:
            # 权重 w ∈ [0,1]：1 = 完全用原图，0 = 完全用算法结果
            w = (mask.astype(np.float32) / 255.0)[..., None]  # (H, W, 1)
            algo_rgb = out[..., :3].astype(np.float32)
            orig_rgb = src_rgb.astype(np.float32)
            blended_rgb = orig_rgb * w + algo_rgb * (1.0 - w)
            out[..., :3] = np.clip(blended_rgb + 0.5, 0, 255).astype(np.uint8)

            # alpha 与 mask 取较大值（保护区原图视为完全不透明）
            a = out[..., 3].astype(np.int32)
            out[..., 3] = np.clip(
                np.maximum(a, mask.astype(np.int32)), 0, 255
            ).astype(np.uint8)

    return out


def magic_wand_select(
    src_rgb: np.ndarray,
    seed_xy: tuple[int, int],
    tolerance: int = 30,
    connected: bool = True,
) -> np.ndarray:
    """
    魔棒选区：从种子像素开始，按颜色相似 + 4 连通漫水填充。

    Parameters
    ----------
    src_rgb   : (H, W, 3) uint8，原图 RGB
    seed_xy   : (x, y) 种子像素坐标（图像坐标系，x 为列，y 为行）
    tolerance : 颜色容差。候选像素与种子的 RGB 欧氏距离 ≤ tolerance 视为同色
    connected : True = 仅取与种子 4 连通的同色块；False = 全图所有同色像素

    Returns
    -------
    np.ndarray  uint8 (H, W)，选中位置 = 255，其余 = 0
    """
    if src_rgb.ndim != 3 or src_rgb.shape[2] < 3:
        raise ValueError(f"expected RGB(A) image, got shape {src_rgb.shape}")
    h, w = src_rgb.shape[:2]
    x, y = int(seed_xy[0]), int(seed_xy[1])
    if not (0 <= x < w and 0 <= y < h):
        return np.zeros((h, w), dtype=np.uint8)

    # 必须用 int32：uint8/int16 做平方会溢出，导致相似区域判断异常甚至卡死。
    rgb = src_rgb[..., :3].astype(np.int32)
    seed = rgb[y, x]
    diff = rgb - seed
    # 平方欧氏距离比较，避免开方（tolerance^2 阈值）
    dist2 = np.sum(diff * diff, axis=-1)
    tol2 = int(tolerance) * int(tolerance)
    similar = dist2 <= tol2

    if not connected:
        return (similar.astype(np.uint8) * 255)

    # 4 连通漫水：从 (y, x) 开始，仅保留与种子相连的 similar 区域。
    # 这里用扫描线 flood fill；关键点：入栈/出栈时都要检查 visited，
    # 且处理一整段后立刻把整段标记为 visited，避免同一大黑块被重复入栈导致无响应。
    if not similar[y, x]:
        return np.zeros((h, w), dtype=np.uint8)

    visited = np.zeros((h, w), dtype=bool)
    out = np.zeros((h, w), dtype=np.uint8)
    stack = [(y, x)]

    while stack:
        cy, cx = stack.pop()
        if cy < 0 or cy >= h or cx < 0 or cx >= w:
            continue
        if visited[cy, cx] or not similar[cy, cx]:
            continue

        # 向左右扩展，找到当前行的一整段连续相似区域
        lx = cx
        while lx > 0 and similar[cy, lx - 1] and not visited[cy, lx - 1]:
            lx -= 1
        rx = cx
        while rx < w - 1 and similar[cy, rx + 1] and not visited[cy, rx + 1]:
            rx += 1

        visited[cy, lx:rx + 1] = True
        out[cy, lx:rx + 1] = 255

        # 在上下两行寻找与 [lx, rx] 相交的新连续段，每段只压一个种子点
        for ny in (cy - 1, cy + 1):
            if not (0 <= ny < h):
                continue
            i = lx
            while i <= rx:
                while i <= rx and (visited[ny, i] or not similar[ny, i]):
                    i += 1
                if i > rx:
                    break
                stack.append((ny, i))
                # 跳过这一整段，避免同一父行重复压栈
                while i <= rx and similar[ny, i] and not visited[ny, i]:
                    i += 1
    return out


# 注册表，便于 GUI / CLI 按名调用
ALGORITHMS = {
    "unmult": {
        "label": "UnMult（推荐，AE 同款）",
        "tooltip": (
            "特点：去底最干净，能保留发光、烟雾等半透明细节。\n"
            "场景：黑底特效图、火焰、烟雾、粒子、技能图标。\n"
            "注意：会轻微改变 RGB 颜色（除以 alpha），"
            "若后续要当遮罩用请改用「阈值法」。"
        ),
        "func": unmult_black,
        "params": [
            # scale=100：UI 滑块 50~300 表示 0.50~3.00
            {"name": "strength", "min": 50, "max": 300, "default": 100,
             "scale": 100, "label": "不透明度增益"},
            # 黑底清理：把压缩/生成带来的近黑噪声直接变透明，防止底色预览下出现灰黑矩形
            {"name": "black_cutoff", "min": 0, "max": 64, "default": 8,
             "label": "黑底清理"},
            # 黑晕抑制：对 cutoff 以上的暗色外扩做软压制，减少黑色阴影残留
            {"name": "black_desaturate", "min": 0, "max": 100, "default": 60,
             "scale": 100, "label": "黑晕抑制"},
            # 实体增益：仅作用于中亮度区域，专门救瓶身这种"暗色实体"
            {"name": "body_density", "min": 0, "max": 100, "default": 0,
             "scale": 100, "label": "实体增益"},
        ],
    },
    "unmult_color": {
        "label": "UnMult（吸管背景色）",
        "tooltip": (
            "特点：用吸管吸取任意纯色背景后去底，原理同 UnMult。\n"
            "场景：绿幕、蓝幕、纯色棚拍图。\n"
            "用法：先点「吸管」在背景上取色，再微调清理阈值。"
        ),
        "func": unmult_color,
        "params": [
            {"name": "bg_r", "min": 0, "max": 255, "default": 0,
             "label": "背景色 R"},
            {"name": "bg_g", "min": 0, "max": 255, "default": 255,
             "label": "背景色 G"},
            {"name": "bg_b", "min": 0, "max": 255, "default": 0,
             "label": "背景色 B"},
            {"name": "strength", "min": 50, "max": 300, "default": 100,
             "scale": 100, "label": "不透明度增益"},
            {"name": "color_cutoff", "min": 0, "max": 64, "default": 8,
             "label": "背景清理"},
            {"name": "color_desaturate", "min": 0, "max": 100, "default": 60,
             "scale": 100, "label": "背景外扩抑制"},
            {"name": "body_density", "min": 0, "max": 100, "default": 0,
             "scale": 100, "label": "实体增益"},
        ],
    },
    "color_key": {
        "label": "背景色键控（保色）",
        "tooltip": (
            "特点：保留原图 RGB 不变，按与背景色的距离直接算透明度。\n"
            "场景：对颜色准确度要求高，如绿幕上的金黄色文字、产品图。\n"
            "代价：边缘可能残留少量背景色（如绿边），可用吸管微调。"
        ),
        "func": color_key,
        "params": [
            {"name": "bg_r", "min": 0, "max": 255, "default": 0,
             "label": "背景色 R"},
            {"name": "bg_g", "min": 0, "max": 255, "default": 255,
             "label": "背景色 G"},
            {"name": "bg_b", "min": 0, "max": 255, "default": 0,
             "label": "背景色 B"},
            {"name": "lower", "min": 0, "max": 128, "default": 8,
             "label": "低阈值（透明）"},
            {"name": "upper", "min": 1, "max": 255, "default": 64,
             "label": "高阈值（不透明）"},
        ],
    },
    "threshold": {
        "label": "阈值法",
        "tooltip": (
            "特点：简单直接，亮度低于阈值就透明，高于就完全不透明。\n"
            "场景：背景非常干净、纯黑的图片，或需要生成硬边遮罩。\n"
            "注意：会丢失半透明边缘和发光效果。"
        ),
        "func": threshold_black,
        "params": [
            {"name": "threshold", "min": 0, "max": 128, "default": 16,
             "label": "黑色阈值"},
        ],
    },
    "chroma": {
        "label": "颜色键（柔和边缘）",
        "tooltip": (
            "特点：按亮度做双阈值线性渐变，边缘柔和自然。\n"
            "场景：干净黑底图，但想保留抗锯齿边缘、光晕、半透明特效。\n"
            "对比：比「阈值法」柔和，比「UnMult」简单。"
        ),
        "func": chroma_key_black,
        "params": [
            {"name": "lower", "min": 0, "max": 128, "default": 8,
             "label": "低阈值（透明）"},
            {"name": "upper", "min": 1, "max": 255, "default": 64,
             "label": "高阈值（不透明）"},
        ],
    },
    "hsv": {
        "label": "HSV 去色背景",
        "tooltip": (
            "特点：按色相范围去掉指定颜色背景，不看亮度。\n"
            "场景：绿幕、蓝幕、红幕等纯彩色背景。\n"
            "用法：先点「吸管」取背景色，程序会自动换算成色相。"
        ),
        "func": hsv_key,
        "params": [
            {"name": "hue", "min": 0, "max": 359, "default": 120,
             "label": "目标色相H"},
            {"name": "hue_tolerance", "min": 0, "max": 180, "default": 20,
             "label": "色相容差"},
            {"name": "min_saturation", "min": 0, "max": 255, "default": 40,
             "label": "最小饱和度"},
            {"name": "min_value", "min": 0, "max": 255, "default": 40,
             "label": "最小明度"},
            {"name": "softness", "min": 1, "max": 180, "default": 30,
             "label": "柔边"},
        ],
    },
}
