"""核心算法单元测试。

运行：
    python -m pytest tests/
或直接：
    python tests/test_algorithms.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# 让脚本独立运行也能 import src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.algorithms import (  # noqa: E402
    apply_protection,
    chroma_key_black,
    color_key,
    hsv_key,
    magic_wand_select,
    threshold_black,
    unmult_black,
    unmult_color,
)


def _make_test_image() -> np.ndarray:
    """构造一张 4x4 黑底图：左上是亮红色，左下是暗灰，其余全黑。"""
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    img[0, 0] = (200, 100, 50)   # 亮红
    img[3, 0] = (10, 10, 10)     # 暗灰
    return img


# ---------------------------------------------------------------------------
# UnMult
# ---------------------------------------------------------------------------
def test_unmult_pure_black_becomes_transparent():
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    out = unmult_black(img)
    assert out.shape == (2, 2, 4)
    assert (out[..., 3] == 0).all(), "纯黑像素必须完全透明"


def test_unmult_recompose_equals_original():
    """关键不变量：把 RGB' * A 合成回去，应当恢复原图。"""
    img = _make_test_image()
    # 使用纯 UnMult 参数，避免 black_cutoff / black_desaturate 破坏不变量
    out = unmult_black(img, black_cutoff=0, black_desaturate=0.0, body_density=0.0)
    rgb = out[..., :3].astype(np.float32) / 255.0
    a = out[..., 3:4].astype(np.float32) / 255.0
    recomposed = np.clip(rgb * a * 255.0 + 0.5, 0, 255).astype(np.uint8)
    # 允许 ±1 的取整误差
    assert np.all(np.abs(recomposed.astype(int) - img.astype(int)) <= 1), (
        "UnMult 合成回黑底应当与原图视觉一致"
    )


def test_unmult_alpha_equals_max_rgb():
    # 纯 UnMult 下 alpha 必须严格等于 max(R,G,B)
    img = _make_test_image()
    out = unmult_black(img, black_cutoff=0, black_desaturate=0.0, body_density=0.0)
    expected_a = img.max(axis=-1)
    assert np.array_equal(out[..., 3], expected_a)


def test_unmult_strength_lifts_alpha_for_saturated_color():
    """关键：strength 应把饱和色从半透明抹到接近不透明，且不偏色。"""
    img = np.array([[[200, 30, 30]]], dtype=np.uint8)  # 饱和红
    base = unmult_black(img, strength=1.0)
    boosted = unmult_black(img, strength=2.0)
    full = unmult_black(img, strength=3.0)
    # 默认下 alpha 约 200，加倍后应 clip 到 255
    assert base[0, 0, 3] == 200
    assert boosted[0, 0, 3] == 255
    assert full[0, 0, 3] == 255
    # strength=3 时 RGB 应该完全等于原图（避免过饱和偏色）
    # 因为 t = 1/3, RGB = unmult_rgb*1/3 + orig*2/3，但 strength 越大越偏向原图
    # strength→∞ 等价于阈值法，颜色等于原图
    assert tuple(full[0, 0, :3]) != (255, 38, 38), "不应是 UnMult 极致饱和色"
    # 大 strength 时颜色应更接近原图 (200,30,30)
    diff_full = abs(int(full[0, 0, 0]) - 200) + abs(int(full[0, 0, 1]) - 30)
    diff_base = abs(int(base[0, 0, 0]) - 255) + abs(int(base[0, 0, 1]) - 38)
    assert diff_full < 50, f"strength=3 颜色应接近原图，实际 {tuple(full[0,0,:3])}"


def test_unmult_strength_keeps_pure_black_transparent():
    """strength 再大也不能让真黑像素变可见。"""
    img = np.zeros((1, 1, 3), dtype=np.uint8)
    out = unmult_black(img, strength=3.0)
    assert out[0, 0, 3] == 0


def test_unmult_body_density_lifts_glass_but_not_smoke():
    """body_density 应只抬中亮度区域（瓶身），不动很暗的烟雾。"""
    img = np.array([[
        [60, 60, 60],   # 暗玻璃 max=60，UnMult alpha=60，应被抬高
        [20, 20, 20],   # 稀薄烟雾 max=20，应保持低 alpha 不动
        [0,  0,  0],    # 真黑，必须保持 alpha=0
    ]], dtype=np.uint8)
    base = unmult_black(img, strength=1.0, body_density=0.0)
    boosted = unmult_black(img, strength=1.0, body_density=1.0,
                           body_low=30, body_high=120)
    # 真黑：永远透明
    assert base[0, 2, 3] == 0
    assert boosted[0, 2, 3] == 0
    # 烟雾（亮度 20 < body_low=30）：alpha 不应被显著抬高
    assert abs(int(boosted[0, 1, 3]) - int(base[0, 1, 3])) <= 2
    # 暗玻璃（亮度 60 在 [30, 120] 区间）：alpha 应明显抬高
    assert int(boosted[0, 0, 3]) > int(base[0, 0, 3]) + 30


def test_unmult_body_density_full_makes_bright_pixels_opaque():
    """亮度 ≥ body_high 的像素 + body_density=1 应直接到 255。"""
    img = np.array([[[150, 150, 150]]], dtype=np.uint8)
    out = unmult_black(img, strength=1.0, body_density=1.0,
                       body_low=30, body_high=120)
    assert out[0, 0, 3] == 255


# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------
def test_threshold_dark_pixels_transparent():
    img = _make_test_image()
    out = threshold_black(img, threshold=16)
    # 暗灰 (10,10,10) 低于 16，应该透明
    assert out[3, 0, 3] == 0
    # 亮红 (200,100,50) 高于 16，应该完全不透明
    assert out[0, 0, 3] == 255


def test_threshold_preserves_rgb():
    img = _make_test_image()
    out = threshold_black(img, threshold=16)
    assert np.array_equal(out[..., :3], img)


# ---------------------------------------------------------------------------
# Chroma key
# ---------------------------------------------------------------------------
def test_chroma_key_linear_ramp():
    # 4 个像素分别亮度 0 / 16 / 32 / 64，使用 lower=0, upper=64
    img = np.zeros((1, 4, 3), dtype=np.uint8)
    img[0, 0] = (0, 0, 0)
    img[0, 1] = (16, 0, 0)
    img[0, 2] = (32, 0, 0)
    img[0, 3] = (64, 0, 0)
    out = chroma_key_black(img, lower=0, upper=64)
    a = out[0, :, 3]
    # 应当线性： 0, 64, 128, 255
    assert a[0] == 0
    assert a[3] == 255
    # 中间允许 ±2 误差
    assert abs(int(a[1]) - 64) <= 2
    assert abs(int(a[2]) - 128) <= 2


def test_chroma_key_invalid_args():
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    try:
        chroma_key_black(img, lower=64, upper=32)
    except ValueError:
        return
    raise AssertionError("upper <= lower 时应当抛 ValueError")


# ---------------------------------------------------------------------------
# apply_protection
# ---------------------------------------------------------------------------
def test_protection_alpha_floor_lifts_dark_foreground():
    """暗色前景被 UnMult 误判透明后，alpha_floor 应当抬回来。"""
    img = np.zeros((1, 3, 3), dtype=np.uint8)
    img[0, 0] = (200, 200, 200)  # 亮像素
    img[0, 1] = (40, 40, 40)     # 暗色前景
    img[0, 2] = (0, 0, 0)        # 真黑底
    out = unmult_black(img)
    # 暗色像素 alpha 原本只有 40，应该被抬到 100
    fixed = apply_protection(out, src_rgb=img, alpha_floor=100)
    assert fixed[0, 1, 3] >= 100, "暗色前景的 alpha 应被抬到 floor"
    assert fixed[0, 0, 3] == 200, "亮像素 alpha 不变"
    assert fixed[0, 2, 3] == 0, "真黑底像素必须保持透明"


def test_protection_mask_overrides_alpha():
    """mask=255 区域应保留原图颜色和完全不透明 alpha。"""
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    img[0, 0] = (200, 100, 50)  # 亮像素
    img[1, 1] = (50, 50, 50)    # 暗灰前景
    out = unmult_black(img)
    mask = np.array([[255, 0], [0, 255]], dtype=np.uint8)
    fixed = apply_protection(out, src_rgb=img, mask=mask)
    # mask=255 → 原图直通：RGB 等于原图，alpha=255
    assert fixed[0, 0, 3] == 255
    assert fixed[1, 1, 3] == 255
    assert tuple(fixed[0, 0, :3]) == (200, 100, 50)
    assert tuple(fixed[1, 1, :3]) == (50, 50, 50)
    # mask=0 的位置不变（仍是 UnMult 结果，背景透明）
    assert fixed[0, 1, 3] == 0
    assert fixed[1, 0, 3] == 0


def test_protection_mask_preserves_black_in_mask_region():
    """关键语义：mask 是"原图直通"，黑色像素被涂了 mask 也要原样保留为不透明黑。

    用户场景：在带黑色阴影的骰子上涂一片保护，希望右侧能看到原本的黑色，
    而不是把黑色当背景吃掉。
    """
    img = np.zeros((1, 3, 3), dtype=np.uint8)
    img[0, 0] = (200, 100, 50)  # 亮
    img[0, 1] = (40, 40, 40)    # 暗灰前景
    img[0, 2] = (0, 0, 0)       # 黑色（在保护区内应当被视为有效像素）
    out = unmult_black(img)
    mask = np.full((1, 3), 255, dtype=np.uint8)
    fixed = apply_protection(out, src_rgb=img, mask=mask)
    # 三个像素都在保护区，全部应当不透明且颜色等于原图
    assert fixed[0, 0, 3] == 255
    assert fixed[0, 1, 3] == 255
    assert fixed[0, 2, 3] == 255, "保护区内的黑色应保留为不透明黑（原图直通）"
    assert tuple(fixed[0, 2, :3]) == (0, 0, 0)


def test_protection_mask_zero_does_not_touch():
    """mask=0 的像素，输出应等于算法结果，不被原图污染。"""
    img = np.zeros((1, 1, 3), dtype=np.uint8)
    img[0, 0] = (200, 100, 50)
    out = unmult_black(img)
    mask = np.zeros((1, 1), dtype=np.uint8)
    fixed = apply_protection(out, src_rgb=img, mask=mask)
    assert np.array_equal(fixed, out)


# ---------------------------------------------------------------------------
# Magic wand
# ---------------------------------------------------------------------------
def test_magic_wand_selects_connected_black():
    """种子在黑色背景，应选中所有连通的同色像素。"""
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    img[1, 1] = (200, 100, 50)  # 一个亮像素，把黑色分成 2 块
    img[1, 2] = (200, 100, 50)
    img[2, 1] = (200, 100, 50)
    img[2, 2] = (200, 100, 50)
    # 黑色是外圈一圈连通区域；中间 2x2 是亮像素
    sel = magic_wand_select(img, seed_xy=(0, 0), tolerance=10)
    # 外圈 12 个像素都应被选中
    assert sel.sum() == 12 * 255
    # 中间 4 个亮像素不应被选中
    assert sel[1, 1] == 0
    assert sel[2, 2] == 0


def test_magic_wand_tolerance_groups_similar_colors():
    """容差应把"近似黑"也算进来。"""
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    img[0, 0] = (0, 0, 0)
    img[0, 1] = (10, 10, 10)   # 距离 sqrt(300)≈17，容差 20 应纳入
    img[1, 0] = (200, 0, 0)    # 距离 200，不纳入
    sel = magic_wand_select(img, seed_xy=(0, 0), tolerance=20)
    assert sel[0, 0] == 255
    assert sel[0, 1] == 255
    assert sel[1, 0] == 0


def test_magic_wand_disconnected_block_excluded():
    """非连通的同色块在 connected=True 时不应被选中。"""
    img = np.zeros((3, 5, 3), dtype=np.uint8)
    # 把图划成两块黑色区域，中间一列 (x=2) 是亮色屏障
    img[:, 2] = (200, 200, 200)
    sel = magic_wand_select(img, seed_xy=(0, 0), tolerance=10, connected=True)
    # 左侧 3 列 = 9 像素被选中（包括 x=0, x=1 共 2 列 6 个，因 x=2 屏障，
    # 实际是 3 行 × 2 列 = 6）
    assert sel[:, :2].sum() == 6 * 255
    # 右侧 2 列不应被选中（不连通）
    assert sel[:, 3:].sum() == 0


def test_magic_wand_disconnected_block_included_when_not_connected():
    """connected=False 时同色像素无视连通性全部选中。"""
    img = np.zeros((3, 5, 3), dtype=np.uint8)
    img[:, 2] = (200, 200, 200)
    sel = magic_wand_select(img, seed_xy=(0, 0), tolerance=10, connected=False)
    # 所有黑像素（左 2 列 + 右 2 列）都被选中 = 12
    assert sel[:, [0, 1, 3, 4]].sum() == 12 * 255


def test_magic_wand_seed_out_of_bounds_returns_empty():
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    sel = magic_wand_select(img, seed_xy=(100, 100), tolerance=10)
    assert sel.shape == (4, 4)
    assert sel.sum() == 0


# ---------------------------------------------------------------------------
# UnMult Color（吸管背景色）
# ---------------------------------------------------------------------------
def test_unmult_color_pure_bg_becomes_transparent():
    """纯背景色像素必须完全透明。"""
    bg = np.array([[[0, 255, 0]]], dtype=np.uint8)  # 绿幕
    out = unmult_color(bg, bg_r=0, bg_g=255, bg_b=0)
    assert out.shape == (1, 1, 4)
    assert out[0, 0, 3] == 0


def test_unmult_color_solid_foreground():
    """纯色前景在绿幕上应完全不透明且颜色正确。"""
    bg = np.zeros((2, 2, 3), dtype=np.uint8)
    bg[:] = (0, 255, 0)
    img = bg.copy()
    img[0, 0] = (255, 0, 0)   # 纯红
    img[0, 1] = (255, 255, 0) # 黄
    out = unmult_color(img, bg_r=0, bg_g=255, bg_b=0)
    assert out[0, 0, 3] == 255
    assert tuple(out[0, 0, :3]) == (255, 0, 0)
    assert out[0, 1, 3] == 255
    assert tuple(out[0, 1, :3]) == (255, 255, 0)


def test_unmult_color_semi_transparent_edge():
    """半透明红边：alpha 应约为 0.5，反算前景接近纯红。"""
    # 0.5 * 绿 + 0.5 * 红 = (127, 127, 0)
    img = np.array([[[127, 127, 0]]], dtype=np.uint8)
    out = unmult_color(img, bg_r=0, bg_g=255, bg_b=0,
                       color_cutoff=0, color_desaturate=0.0)
    a = out[0, 0, 3]
    # alpha 约 127/255 ≈ 0.5，允许 ±2 取整误差
    assert 120 <= a <= 135
    # 反算出的前景应接近纯红
    assert out[0, 0, 0] >= 240
    assert out[0, 0, 1] <= 30


def test_unmult_color_matches_black_unmult():
    """背景色为黑色时，unmult_color 应与 unmult_black 等价。"""
    img = _make_test_image()
    black = unmult_black(img, black_cutoff=0, black_desaturate=0.0, body_density=0.0)
    color = unmult_color(img, bg_r=0, bg_g=0, bg_b=0,
                         color_cutoff=0, color_desaturate=0.0, body_density=0.0)
    assert np.array_equal(black, color)


def test_unmult_color_cutoff_cleans_noise():
    """color_cutoff 应把接近背景的噪声清理掉。"""
    bg = np.zeros((2, 2, 3), dtype=np.uint8)
    bg[:] = (0, 255, 0)
    img = bg.copy()
    img[0, 0] = (2, 253, 2)  # 非常接近背景色
    out = unmult_color(img, bg_r=0, bg_g=255, bg_b=0, color_cutoff=8)
    assert out[0, 0, 3] == 0


def test_unmult_color_strength_lifts_alpha():
    """strength 应把饱和色从半透明抹到不透明。"""
    # 红 (255,0,0) 在绿幕上，alpha 已经为 1，这里用半饱和红 (128,128,0)
    # 实际 max(|diff|)=128，alpha≈0.5
    img = np.array([[[128, 128, 0]]], dtype=np.uint8)
    base = unmult_color(img, bg_r=0, bg_g=255, bg_b=0,
                        color_cutoff=0, color_desaturate=0.0, strength=1.0)
    boosted = unmult_color(img, bg_r=0, bg_g=255, bg_b=0,
                           color_cutoff=0, color_desaturate=0.0, strength=2.0)
    assert base[0, 0, 3] < 140
    assert boosted[0, 0, 3] == 255


# ---------------------------------------------------------------------------
# Color Key
# ---------------------------------------------------------------------------
def test_color_key_bg_becomes_transparent():
    """纯背景色像素必须完全透明。"""
    bg = np.array([[[0, 255, 0]]], dtype=np.uint8)
    out = color_key(bg, bg_r=0, bg_g=255, bg_b=0)
    assert out.shape == (1, 1, 4)
    assert out[0, 0, 3] == 0


def test_color_key_preserves_original_rgb():
    """背景色键控应保留原始 RGB，不做除法。"""
    bg = np.zeros((2, 2, 3), dtype=np.uint8)
    bg[:] = (0, 255, 0)
    img = bg.copy()
    img[0, 0] = (180, 140, 60)  # 棕色
    out = color_key(img, bg_r=0, bg_g=255, bg_b=0, lower=0, upper=64)
    assert out[0, 0, 3] == 255
    assert tuple(out[0, 0, :3]) == (180, 140, 60)


def test_color_key_linear_edge():
    """半透明边缘 alpha 应在 lower/upper 之间线性渐变。"""
    bg = np.zeros((1, 4, 3), dtype=np.uint8)
    bg[:] = (0, 255, 0)
    img = bg.copy()
    # 与绿色背景的 max-norm 距离：0, 64, 128, 192
    img[0, 0] = (0, 255, 0)
    img[0, 1] = (64, 191, 0)
    img[0, 2] = (128, 127, 0)
    img[0, 3] = (192, 63, 0)
    out = color_key(img, bg_r=0, bg_g=255, bg_b=0, lower=64, upper=192)
    a = out[0, :, 3]
    assert a[0] == 0
    assert a[3] == 255
    # 中间线性：64->0, 128->128, 192->255
    assert abs(int(a[1]) - 0) <= 2
    assert abs(int(a[2]) - 128) <= 2


def test_color_key_invalid_args():
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    try:
        color_key(img, bg_r=0, bg_g=255, bg_b=0, lower=64, upper=32)
    except ValueError:
        return
    raise AssertionError("upper <= lower 时应当抛 ValueError")


# ---------------------------------------------------------------------------
# HSV Key
# ---------------------------------------------------------------------------
def test_hsv_key_pure_green_becomes_transparent():
    """纯绿色（hue=120）在默认参数下应完全透明。"""
    img = np.array([[[0, 255, 0]]], dtype=np.uint8)  # 纯绿
    out = hsv_key(img, hue=120, hue_tolerance=20)
    assert out.shape == (1, 1, 4)
    assert out[0, 0, 3] == 0


def test_hsv_key_non_target_hue_remains_opaque():
    """非目标色相的像素（如纯红 hue≈0）应完全不透明。"""
    img = np.array([[[255, 0, 0]]], dtype=np.uint8)  # 纯红
    out = hsv_key(img, hue=120, hue_tolerance=20)
    assert out[0, 0, 3] == 255


def test_hsv_key_edge_falloff():
    """接近目标色相但超出容差、在柔边范围内的像素应有半透明 alpha。"""
    # 绿色 hue=120，tolerance=20 意味着 100~140 完全透明
    # softness=30 意味着 100-(30+30)=40 到 100 之间线性渐变
    # 选 hue≈80 的颜色
    img = np.array([[[0, 255, 170]]], dtype=np.uint8)  # 偏蓝绿, hue≈160? 
    # 选一个在 tolerance+softness 范围内的颜色
    # hue_tolerance=20, softness=30 → 完全透明范围 100~140, 半透明范围 70~100 和 140~170
    # 选 hue≈80 → 在柔边范围
    img2 = np.array([[[128, 200, 0]]], dtype=np.uint8)  # 偏黄绿, 大约 hue≈80
    out = hsv_key(img2, hue=120, hue_tolerance=20, softness=30)
    a = out[0, 0, 3]
    # 在柔边范围内，alpha 应在 0~255 之间
    assert 0 < a < 255, f"expected partial alpha in falloff zone, got {a}"


def test_hsv_key_low_saturation_ignored():
    """低饱和度的灰色应被视为非彩色区域，保持不透明。"""
    # 灰色 (128,128,128) 饱和度≈0，即使 hue 可能落在目标范围
    img = np.array([[[128, 128, 128]]], dtype=np.uint8)
    out = hsv_key(img, hue=120, hue_tolerance=20, min_saturation=40, min_value=40)
    assert out[0, 0, 3] == 255


def test_hsv_key_low_value_ignored():
    """暗色（low value）应被视为背景无效区域，保持不透明。"""
    # 很暗的"绿色" (0, 8, 0)，value≈8
    img = np.array([[[0, 8, 0]]], dtype=np.uint8)
    out = hsv_key(img, hue=120, hue_tolerance=20, min_saturation=40, min_value=40)
    # value < min_value，不应被当作彩色背景处理
    assert out[0, 0, 3] == 255


def test_hsv_key_pure_black_remains_opaque():
    """纯黑（value=0）在任何色相参数下都应完全透明？不对，应该是完全 opaque。
    实际上黑色 s=0 ✓，所以被 color_ok 过滤保留。"""
    img = np.array([[[0, 0, 0]]], dtype=np.uint8)
    out = hsv_key(img, hue=120, hue_tolerance=180, min_saturation=40, min_value=40)
    assert out[0, 0, 3] == 255


def test_hsv_key_pure_white_remains_opaque():
    """纯白（saturation=0）应保持不透明。"""
    img = np.array([[[255, 255, 255]]], dtype=np.uint8)
    out = hsv_key(img, hue=120, hue_tolerance=180, min_saturation=40, min_value=40)
    assert out[0, 0, 3] == 255


def test_hsv_key_invalid_hue():
    """hue 超出 0~359 应抛 ValueError。"""
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    try:
        hsv_key(img, hue=400)
    except ValueError:
        return
    raise AssertionError("hue 超出范围时应当抛 ValueError")


def test_hsv_key_invalid_tolerance():
    """负容差应抛 ValueError。"""
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    try:
        hsv_key(img, hue_tolerance=-5)
    except ValueError:
        return
    raise AssertionError("负容差时应当抛 ValueError")


def test_hsv_key_invalid_softness():
    """负柔边应抛 ValueError。"""
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    try:
        hsv_key(img, softness=-5)
    except ValueError:
        return
    raise AssertionError("负柔边时应当抛 ValueError")


def test_hsv_key_zero_tolerance_no_falloff():
    """tolerance=0 时即使接近的色相也应完全透明（core=0），
    但超出 core 则在 softness 内线性渐变。"""
    # 绿色 hue=120 完全透明
    img_green = np.array([[[0, 200, 0]]], dtype=np.uint8)
    out = hsv_key(img_green, hue=120, hue_tolerance=0, softness=30)
    assert out[0, 0, 3] == 0


# ---------------------------------------------------------------------------
# 自运行
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    funcs = [v for k, v in list(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in funcs:
        try:
            fn()
            print(f"[OK]   {fn.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"[FAIL] {fn.__name__}: {e}")
    print(f"\n{len(funcs) - fails}/{len(funcs)} passed.")
    sys.exit(0 if fails == 0 else 1)
