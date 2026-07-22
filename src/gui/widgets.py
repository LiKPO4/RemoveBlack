"""自定义控件：棋盘格透明背景预览 + 拖拽区 + 保护蒙版绘制。

v1.2 升级：
- CheckerboardView 支持鼠标滚轮缩放 + 中键拖拽平移
- PaintableView 增加矩形画笔 / 矩形橡皮工具
- PaintableView 增加 undo / redo 历史栈
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QPolygon,
    QWheelEvent,
)
from PySide6.QtWidgets import QFrame, QSizePolicy, QWidget

# 主窗口要根据扩展名过滤拖入文件
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff", ".webp", ".heic", ".heif"}

# 绘制模式
TOOL_NONE = "none"
TOOL_BRUSH = "brush"
TOOL_ERASER = "eraser"
TOOL_LASSO_BRUSH = "lasso_brush"
TOOL_LASSO_ERASER = "lasso_eraser"
TOOL_MAGIC = "magic"     # 魔棒：点击选中黑色背景区域
TOOL_BUCKET = "bucket"   # 油漆桶：填充封闭区域
TOOL_EYEDROPPER = "eyedropper"  # 吸管：吸取背景色

# 缩放范围
ZOOM_MIN = 0.1
ZOOM_MAX = 32.0
ZOOM_STEP = 1.25  # 滚轮单步乘数

# 撤销栈上限（按"笔画"计数）
HISTORY_MAX = 60


def _make_checkerboard(size: int = 16) -> QPixmap:
    """生成 PS 风格灰白棋盘 pixmap，用于做透明背景。"""
    pm = QPixmap(size * 2, size * 2)
    pm.fill(QColor(255, 255, 255))
    p = QPainter(pm)
    p.fillRect(0, 0, size, size, QColor(204, 204, 204))
    p.fillRect(size, size, size, size, QColor(204, 204, 204))
    p.end()
    return pm


# ---------------------------------------------------------------------------
# 基础视图：棋盘背景 + 缩放 + 平移
# ---------------------------------------------------------------------------
class CheckerboardView(QFrame):
    """带棋盘格背景、可缩放可平移的图片显示控件。"""

    zoom_changed = Signal(float)  # 当前缩放倍数（相对于原图原始像素，百分比）
    view_changed = Signal()       # zoom 或 pan 任何一个变了都发，用于跨视图同步

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._checker = _make_checkerboard()
        self._bg_color: Optional[QColor] = None  # None = 棋盘，否则纯色背景

        # 视图变换状态
        self._zoom: float = 1.0          # 1.0 = 适配窗口
        self._pan_x: float = 0.0         # 在 widget 坐标里的平移
        self._pan_y: float = 0.0
        self._panning: bool = False
        self._pan_anchor: Optional[QPoint] = None

        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumSize(280, 280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.StrongFocus)

    # ------------------------------------------------------------------
    # 图像 API
    # ------------------------------------------------------------------
    def set_image(self, img: QImage | QPixmap | None) -> None:
        """设置/替换显示图像。

        - 首次加载、清空、或图片尺寸变化 → 自动 fit_view()。
        - 同尺寸刷新（如蒙版改变后重算预览）→ 保留当前 zoom/pan。
          这样用户放大编辑时不会因为预览刷新而跳回 100%。
        """
        old_size = self._pixmap.size() if self._pixmap is not None else None

        if img is None:
            self._pixmap = None
        elif isinstance(img, QImage):
            self._pixmap = QPixmap.fromImage(img)
        else:
            self._pixmap = img

        new_size = self._pixmap.size() if self._pixmap is not None else None

        # 仅在"真的是一张新图"时重置视图
        if new_size is None or old_size is None or new_size != old_size:
            self.fit_view()
        else:
            # 内容变了但尺寸没变：保留视图，只刷新一帧
            self.update()

    def clear(self) -> None:
        self.set_image(None)

    # ------------------------------------------------------------------
    # 缩放 / 平移 API
    # ------------------------------------------------------------------
    def fit_view(self) -> None:
        """重置为适配窗口、居中显示。"""
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.zoom_changed.emit(self.effective_scale_percent())
        self.view_changed.emit()
        self.update()

    def set_zoom(self, zoom: float, anchor: Optional[QPointF] = None) -> None:
        """以 anchor（widget 坐标）为锚点缩放到指定倍数。"""
        if self._pixmap is None or self._pixmap.isNull():
            return
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, float(zoom)))
        if abs(new_zoom - self._zoom) < 1e-6:
            return

        if anchor is None:
            anchor = QPointF(self.width() / 2, self.height() / 2)

        old_rect = self._image_rect()
        if old_rect is None or old_rect.width() <= 0 or old_rect.height() <= 0:
            self._zoom = new_zoom
            self.zoom_changed.emit(self.effective_scale_percent())
            self.view_changed.emit()
            self.update()
            return

        # 锚点在原图归一坐标 (0~1)
        rel_x = (anchor.x() - old_rect.x()) / old_rect.width()
        rel_y = (anchor.y() - old_rect.y()) / old_rect.height()

        self._zoom = new_zoom

        # 用新 zoom 计算 size，再反推 pan 让锚点保持在原位
        iw, ih = self._pixmap.width(), self._pixmap.height()
        base_scale = self._base_scale(iw, ih)
        new_w = iw * base_scale * self._zoom
        new_h = ih * base_scale * self._zoom

        self._pan_x = anchor.x() - rel_x * new_w + new_w / 2 - self.width() / 2
        self._pan_y = anchor.y() - rel_y * new_h + new_h / 2 - self.height() / 2
        self._clamp_pan()
        self.zoom_changed.emit(self.effective_scale_percent())
        self.view_changed.emit()
        self.update()

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom * ZOOM_STEP)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom / ZOOM_STEP)

    def effective_scale_percent(self) -> float:
        """返回相对于原图原始像素的实际缩放（百分比）。"""
        if self._pixmap is None or self._pixmap.isNull():
            return 100.0
        base = self._base_scale(self._pixmap.width(), self._pixmap.height())
        return base * self._zoom * 100.0

    # ------------------------------------------------------------------
    # 几何
    # ------------------------------------------------------------------
    def _base_scale(self, iw: int, ih: int) -> float:
        if iw <= 0 or ih <= 0 or self.width() <= 0 or self.height() <= 0:
            return 1.0
        return min(self.width() / iw, self.height() / ih)

    def _image_rect(self) -> QRect | None:
        """图像在控件上的实际显示矩形（含缩放 + 平移）。"""
        if self._pixmap is None or self._pixmap.isNull():
            return None
        iw, ih = self._pixmap.width(), self._pixmap.height()
        scale = self._base_scale(iw, ih) * self._zoom
        w = iw * scale
        h = ih * scale
        cx = self.width() / 2 + self._pan_x
        cy = self.height() / 2 + self._pan_y
        return QRect(int(cx - w / 2), int(cy - h / 2), int(w), int(h))

    def _clamp_pan(self) -> None:
        """限制 pan：

        - 图比窗口小：水平/垂直方向各自居中（pan = 0）。
        - 图比窗口大：允许任意拖动，但保证图至少留 40px 在窗口内，
          不至于被甩到看不见的地方。
        """
        rect = self._image_rect()
        if rect is None:
            return
        margin = 40

        # —— 水平方向 ——
        if rect.width() <= self.width():
            # 图比窗口窄：硬居中
            self._pan_x = 0.0
        else:
            # 图比窗口宽：左右各允许拖出去，但不能完全消失
            # 图最左边最远只能拖到 width - margin
            # 图最右边最近只能拖到 margin
            # 等价于：pan_x ∈ [width/2 - rect.w/2 - (rect.w - margin), ...]
            min_pan = self.width() / 2 - rect.width() / 2 - (rect.width() - margin)
            max_pan = self.width() / 2 - rect.width() / 2 + (rect.width() - margin)
            # 简化为：图必须至少留 margin 在窗口内
            if rect.right() < margin:
                self._pan_x += margin - rect.right()
            if rect.left() > self.width() - margin:
                self._pan_x -= rect.left() - (self.width() - margin)

        # —— 垂直方向 ——
        if rect.height() <= self.height():
            self._pan_y = 0.0
        else:
            if rect.bottom() < margin:
                self._pan_y += margin - rect.bottom()
            if rect.top() > self.height() - margin:
                self._pan_y -= rect.top() - (self.height() - margin)

    # ------------------------------------------------------------------
    # 子类挂钩
    # ------------------------------------------------------------------
    def _paint_overlay(self, p: QPainter, target_rect: QRect) -> None:
        """子类重写以在缩放后的图像区域上叠加内容。"""
        return

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 背景设置
    # ------------------------------------------------------------------
    def set_background_color(self, color: Optional[QColor]) -> None:
        """None 表示用棋盘背景，否则使用纯色填充。"""
        self._bg_color = color
        self.update()

    def get_background_color(self) -> Optional[QColor]:
        return self._bg_color

    # ------------------------------------------------------------------
    # 视图状态同步（左右两个视图共享 zoom + pan）
    # ------------------------------------------------------------------
    def get_view_state(self) -> tuple[float, float, float]:
        """返回 (zoom, pan_x_norm, pan_y_norm)。

        pan 用控件归一坐标（pan_x / max(width, 1)），
        这样不同尺寸的 widget 也能正确对齐图像中心点。
        """
        w = max(self.width(), 1)
        h = max(self.height(), 1)
        return self._zoom, self._pan_x / w, self._pan_y / h

    def set_view_state(self, zoom: float, pan_x_norm: float, pan_y_norm: float) -> None:
        """从外部强制设置视图（不触发信号，避免循环）。"""
        self._zoom = max(ZOOM_MIN, min(ZOOM_MAX, float(zoom)))
        self._pan_x = float(pan_x_norm) * self.width()
        self._pan_y = float(pan_y_norm) * self.height()
        self._clamp_pan()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        # 背景：棋盘格 或 纯色
        if self._bg_color is None:
            p.fillRect(self.rect(), QBrush(self._checker))
        else:
            p.fillRect(self.rect(), self._bg_color)

        if self._pixmap is None or self._pixmap.isNull():
            p.setPen(QColor(120, 120, 120))
            p.drawText(
                self.rect(),
                Qt.AlignCenter,
                "拖拽图片到此处\n或点击下方按钮选择\n\n"
                "滚轮缩放  ·  中键拖动平移  ·  Ctrl+0 适配窗口",
            )
            p.end()
            return

        target = self._image_rect()
        if target is not None:
            # 平滑缩放
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawPixmap(target, self._pixmap, self._pixmap.rect())
            self._paint_overlay(p, target)
        p.end()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._pixmap is None or self._pixmap.isNull():
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
        self.set_zoom(self._zoom * factor, anchor=event.position())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_anchor = event.pos()
            self.setCursor(QCursor(Qt.ClosedHandCursor))
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._panning and self._pan_anchor is not None:
            d = event.pos() - self._pan_anchor
            self._pan_anchor = event.pos()
            self._pan_x += d.x()
            self._pan_y += d.y()
            self._clamp_pan()
            self.view_changed.emit()
            self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self._pan_anchor = None
            self.unsetCursor()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        super().resizeEvent(event)
        self._clamp_pan()
        self.zoom_changed.emit(self.effective_scale_percent())


# ---------------------------------------------------------------------------
# 拖拽接收
# ---------------------------------------------------------------------------
class DropArea(CheckerboardView):
    """支持拖拽接收图片文件 / 文件夹的预览区。"""

    files_dropped = Signal(list)  # list[Path]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            paths = self._collect_paths(event.mimeData().urls())
            if paths:
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = self._collect_paths(event.mimeData().urls())
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()

    @staticmethod
    def _collect_paths(urls: Iterable) -> list[Path]:
        out: list[Path] = []
        for u in urls:
            p = Path(u.toLocalFile())
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                out.append(p)
            elif p.is_dir():
                out.append(p)
        return out


# ---------------------------------------------------------------------------
# 可绘制保护蒙版的预览区（含 undo/redo）
# ---------------------------------------------------------------------------
class PaintableView(DropArea):
    """
    在原图上绘制保护蒙版的预览控件。

    统一语义：
        - _mask 中 255 表示「被选中 / 受保护的前景区域」
        - 画笔、矩形、油漆桶直接扩展 _mask
        - 魔棒点击背景后，会先把背景选区反转为前景选区，再合并到 _mask
        - 「反选」按钮直接对 _mask 做 0↔255 翻转

    工具：
        TOOL_BRUSH        圆形画笔（涂抹保护区）
        TOOL_ERASER       圆形橡皮（擦除保护区）
        TOOL_LASSO_BRUSH  套索画笔（拖动绘制闭合区域 → 内部全部保护）
        TOOL_LASSO_ERASER 套索橡皮（拖动绘制闭合区域 → 内部全部清除）
        TOOL_MAGIC        魔棒选区（点击背景，反转为前景后写入 _mask）
        TOOL_BUCKET       油漆桶填充（填充封闭区域）
        TOOL_EYEDROPPER   吸管吸取背景色
        TOOL_NONE         浏览（内部默认，不显示在工具栏）

    撤销 / 重做：
        每次"按下→拖动→松开"算一笔，自动入栈。
        history_changed 信号在栈位置改变时发出，主窗口据此刷新按钮可用状态。
    """

    selection_changed = Signal(int, int, str)  # (selected_pixels, total_pixels, mode)
    mask_changed = Signal()
    history_changed = Signal(bool, bool)  # (can_undo, can_redo)
    color_picked = Signal(int, int, int)     # (R, G, B)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mask: Optional[np.ndarray] = None
        self._image_size: Optional[tuple[int, int]] = None
        self._tool: str = TOOL_NONE
        self._brush_size: int = 30
        self._cursor_pt: Optional[QPoint] = None  # widget 坐标，画光标用

        # 圆形笔刷连续描边
        self._stroking: bool = False
        self._last_pt: Optional[QPoint] = None  # 图像坐标
        self._stroke_backup: Optional[np.ndarray] = None

        # 套索拖动
        self._lasso_dragging: bool = False
        self._lasso_points: list[tuple[int, int]] = []  # 图像坐标点序列

        # 魔棒
        self._src_rgb: Optional[np.ndarray] = None  # (H, W, 3) uint8
        self._magic_tol: int = 30

        # 历史栈：list of (y0, y1, x0, x1, before, after)
        # 为节省内存，只记录发生变化的 bbox；全图操作（如清空/反选）记录 (0,H,0,W)
        self._history: list[tuple[int, int, int, int, np.ndarray, np.ndarray]] = []
        self._history_pos: int = 0

        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def set_image(self, img: QImage | QPixmap | None) -> None:  # noqa: D401
        super().set_image(img)
        if img is None:
            self._image_size = None
        else:
            self._image_size = (img.width(), img.height())

    def set_image_size(self, w: int, h: int) -> None:
        """主窗口在 set_image 后调用。重置蒙版与历史。"""
        if self._mask is None or self._mask.shape != (h, w):
            self._mask = np.zeros((h, w), dtype=np.uint8)
        else:
            self._mask[:] = 0
        self._image_size = (w, h)
        self._history.clear()
        self._history_pos = 0
        self.history_changed.emit(False, False)
        self.update()

    def get_mask(self) -> Optional[np.ndarray]:
        return self._mask

    def set_tool(self, tool: str) -> None:
        self._tool = tool
        if tool == TOOL_NONE:
            self.unsetCursor()
        elif tool in (TOOL_LASSO_BRUSH, TOOL_LASSO_ERASER):
            # 套索工具用系统十字光标，准确好认
            self.setCursor(QCursor(Qt.CrossCursor))
        elif tool == TOOL_MAGIC:
            # 魔棒用 PointingHand 光标更直观
            self.setCursor(QCursor(Qt.PointingHandCursor))
        elif tool == TOOL_BUCKET:
            self.setCursor(QCursor(Qt.CrossCursor))
        elif tool == TOOL_EYEDROPPER:
            self.setCursor(QCursor(Qt.CrossCursor))
        else:
            # 圆形画笔/橡皮：默认十字光标，鼠标进入图像区域后自绘圆圈
            self.setCursor(QCursor(Qt.CrossCursor))
        self.update()

    def set_source_rgb(self, src_rgb: Optional[np.ndarray]) -> None:
        """主窗口加载新图后调用，缓存原图 RGB 供魔棒漫水使用。"""
        self._src_rgb = src_rgb

    def set_magic_tolerance(self, tol: int) -> None:
        self._magic_tol = max(0, min(255, int(tol)))

    def get_tool(self) -> str:
        return self._tool

    def set_brush_size(self, px: int) -> None:
        self._brush_size = max(1, int(px))
        self.update()

    def get_brush_size(self) -> int:
        return self._brush_size

    def mask_coverage(self) -> float:
        if self._mask is None:
            return 0.0
        return float((self._mask > 0).mean())

    # ---- 历史 ----
    def can_undo(self) -> bool:
        return self._history_pos > 0

    def can_redo(self) -> bool:
        return self._history_pos < len(self._history)

    def undo(self) -> None:
        if not self.can_undo():
            return
        self._history_pos -= 1
        y0, y1, x0, x1, before, _after = self._history[self._history_pos]
        self._mask[y0:y1, x0:x1] = before
        self.mask_changed.emit()
        self.history_changed.emit(self.can_undo(), self.can_redo())
        self.update()

    def redo(self) -> None:
        if not self.can_redo():
            return
        y0, y1, x0, x1, _before, after = self._history[self._history_pos]
        self._mask[y0:y1, x0:x1] = after
        self.mask_changed.emit()
        self._history_pos += 1
        self.history_changed.emit(self.can_undo(), self.can_redo())
        self.update()

    def clear_mask(self) -> None:
        """清空蒙版，作为一次可撤销操作。"""
        if self._mask is None or not self._mask.any():
            return
        before = self._mask.copy()
        self._mask[:] = 0
        after = self._mask.copy()
        h, w = self._mask.shape
        self._push_history(0, h, 0, w, before, after)
        self.mask_changed.emit()
        self.update()

    # ------------------------------------------------------------------
    # 魔棒（已统一到 _mask：点击背景 → 反转为前景 → 合并到 _mask）
    # ------------------------------------------------------------------
    def _magic_click(self, x: int, y: int, mode: str = "replace") -> None:
        """魔棒点击：在 (x, y) 处做 flood fill，选中相似背景后反转为前景，合并到 _mask。"""
        if self._src_rgb is None or self._image_size is None:
            self.selection_changed.emit(0, 0, mode)
            return
        from src.core.algorithms import magic_wand_select  # 局部 import 避免循环
        bg_sel = magic_wand_select(
            self._src_rgb, seed_xy=(x, y), tolerance=self._magic_tol, connected=True
        )
        hit_pixels = int(np.count_nonzero(bg_sel))
        total_pixels = int(bg_sel.size)
        if hit_pixels == 0:
            self.selection_changed.emit(0, total_pixels, mode)
            return

        # 背景选区反转为前景选区，与画笔语义一致
        fg_sel = np.where(bg_sel > 0, 0, 255).astype(np.uint8)

        before = self._mask.copy()
        if mode == "add":
            new_mask = np.maximum(self._mask, fg_sel)
        elif mode == "sub":
            new_mask = np.where(fg_sel > 0, 0, self._mask).astype(np.uint8)
        else:  # replace
            new_mask = fg_sel
        self._mask = new_mask
        after = self._mask.copy()
        h, w = self._mask.shape
        self._push_history(0, h, 0, w, before, after)
        self.selection_changed.emit(int(np.count_nonzero(self._mask)), total_pixels, mode)
        self.mask_changed.emit()
        self.update()

    def invert_mask(self) -> None:
        """反选 _mask：0↔255 翻转，作为一次可撤销操作。"""
        if self._mask is None or self._image_size is None:
            return
        before = self._mask.copy()
        self._mask = 255 - self._mask
        after = self._mask.copy()
        h, w = self._mask.shape
        self._push_history(0, h, 0, w, before, after)
        self.mask_changed.emit()
        self.update()

    def bucket_fill(self, x: int, y: int) -> None:
        """油漆桶：在蒙版上做 4 连通漫水填充。

        点击处的像素值作为种子值，把所有连通的同值像素都填成 255。
        典型用法：用笔刷围出一个封闭区域后，用油漆桶一键把内部填满。
        """
        if self._mask is None or self._image_size is None:
            return
        h, w = self._mask.shape
        if not (0 <= x < w and 0 <= y < h):
            return

        seed_val = int(self._mask[y, x])
        if seed_val == 255:
            # 已经是不透明保护区，再填没有意义
            return

        before = self._mask.copy()
        visited = np.zeros((h, w), dtype=bool)
        stack = [(y, x)]
        filled = 0

        while stack:
            cy, cx = stack.pop()
            if cy < 0 or cy >= h or cx < 0 or cx >= w:
                continue
            if visited[cy, cx] or self._mask[cy, cx] != seed_val:
                continue

            lx = cx
            while lx > 0 and not visited[cy, lx - 1] and self._mask[cy, lx - 1] == seed_val:
                lx -= 1
            rx = cx
            while rx < w - 1 and not visited[cy, rx + 1] and self._mask[cy, rx + 1] == seed_val:
                rx += 1

            visited[cy, lx:rx + 1] = True
            self._mask[cy, lx:rx + 1] = 255
            filled += rx - lx + 1

            for ny in (cy - 1, cy + 1):
                if not (0 <= ny < h):
                    continue
                i = lx
                while i <= rx:
                    while i <= rx and (visited[ny, i] or self._mask[ny, i] != seed_val):
                        i += 1
                    if i > rx:
                        break
                    stack.append((ny, i))
                    while i <= rx and not visited[ny, i] and self._mask[ny, i] == seed_val:
                        i += 1

        if filled > 0:
            after = self._mask.copy()
            self._push_history(0, h, 0, w, before, after)
            self.mask_changed.emit()
            self.update()

    # ------------------------------------------------------------------
    # 坐标换算
    # ------------------------------------------------------------------
    def _widget_to_image(
        self, pos: QPoint, clamp: bool = False
    ) -> Optional[QPoint]:
        """把控件坐标映射到原图坐标。clamp=True 时即使在图外也返回最近边界点。"""
        rect = self._image_rect()
        if rect is None or self._image_size is None:
            return None
        if not clamp and not rect.contains(pos):
            return None
        iw, ih = self._image_size
        x = (pos.x() - rect.x()) * iw / max(1, rect.width())
        y = (pos.y() - rect.y()) * ih / max(1, rect.height())
        if clamp:
            x = max(0, min(iw - 1, x))
            y = max(0, min(ih - 1, y))
        return QPoint(int(x), int(y))

    def _image_to_widget_radius(self) -> float:
        rect = self._image_rect()
        if rect is None or self._image_size is None:
            return self._brush_size / 2
        iw, _ = self._image_size
        scale = rect.width() / iw
        return self._brush_size * scale / 2

    # ------------------------------------------------------------------
    # 历史辅助
    # ------------------------------------------------------------------
    def _push_history(
        self,
        y0: int,
        y1: int,
        x0: int,
        x1: int,
        before: np.ndarray,
        after: np.ndarray,
    ) -> None:
        # 截掉 redo 分支
        del self._history[self._history_pos:]
        self._history.append((y0, y1, x0, x1, before, after))
        if len(self._history) > HISTORY_MAX:
            self._history.pop(0)
        self._history_pos = len(self._history)
        self.history_changed.emit(self.can_undo(), self.can_redo())

    def _capture_stroke_diff(self) -> None:
        """对比 stroke_backup 与当前 mask，把差异 bbox 入栈。"""
        if self._stroke_backup is None or self._mask is None:
            return
        diff = self._mask != self._stroke_backup
        if not diff.any():
            self._stroke_backup = None
            return
        ys, xs = np.where(diff)
        y0 = int(ys.min())
        y1 = int(ys.max()) + 1
        x0 = int(xs.min())
        x1 = int(xs.max()) + 1
        before = self._stroke_backup[y0:y1, x0:x1].copy()
        after = self._mask[y0:y1, x0:x1].copy()
        self._push_history(y0, y1, x0, x1, before, after)
        self._stroke_backup = None

    # ------------------------------------------------------------------
    # 叠加层绘制
    # ------------------------------------------------------------------
    def _paint_overlay(self, p: QPainter, target_rect: QRect) -> None:
        # 1. 蒙版红色半透明叠加（保护蒙版 / 统一选区）
        if self._mask is not None and self._mask.any():
            h, w = self._mask.shape
            rgba = np.zeros((h, w, 4), dtype=np.uint8)
            rgba[..., 0] = 255
            rgba[..., 3] = (self._mask.astype(np.uint16) * 110 // 255).astype(np.uint8)
            mask_img = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888).copy()
            p.drawImage(target_rect, mask_img)

        # 2. 套索拖动预览
        if self._lasso_dragging and len(self._lasso_points) > 1 and self._image_size is not None:
            color = (
                QColor(80, 200, 255)
                if self._tool == TOOL_LASSO_ERASER
                else QColor(255, 60, 60)
            )
            iw, ih = self._image_size
            poly = QPolygon()
            for ix, iy in self._lasso_points:
                wx = int(target_rect.x() + ix * target_rect.width() / iw)
                wy = int(target_rect.y() + iy * target_rect.height() / ih)
                poly.append(QPoint(wx, wy))
            pen = QPen(color, 1.5, Qt.SolidLine)
            p.setPen(pen)
            p.setBrush(QColor(color.red(), color.green(), color.blue(), 60))
            p.drawPolygon(poly)

        # 3. 笔刷光标圆圈（仅圆形画笔/橡皮）
        if (
            self._tool in (TOOL_BRUSH, TOOL_ERASER)
            and self._cursor_pt is not None
        ):
            r = self._image_to_widget_radius()
            color = (
                QColor(255, 60, 60) if self._tool == TOOL_BRUSH else QColor(80, 200, 255)
            )
            p.setPen(QPen(color, 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(self._cursor_pt), r, r)
            p.setPen(QPen(QColor(255, 255, 255, 180), 1))
            p.drawEllipse(QPointF(self._cursor_pt), r + 1, r + 1)

    # ------------------------------------------------------------------
    # 鼠标事件
    # ------------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        # 中键平移交给基类
        if event.button() == Qt.MiddleButton:
            super().mousePressEvent(event)
            return

        if event.button() == Qt.LeftButton and self._tool != TOOL_NONE:
            if self._tool in (TOOL_BRUSH, TOOL_ERASER):
                if self._mask is None:
                    return
                ipt = self._widget_to_image(event.pos())
                if ipt is None:
                    return
                self._stroke_backup = self._mask.copy()
                self._stroking = True
                self._last_pt = ipt
                self._stamp(ipt, ipt)
                self.update()
                return

            if self._tool in (TOOL_LASSO_BRUSH, TOOL_LASSO_ERASER):
                if self._mask is None:
                    return
                ipt = self._widget_to_image(event.pos(), clamp=True)
                if ipt is None:
                    return
                self._lasso_dragging = True
                self._lasso_points = [(ipt.x(), ipt.y())]
                self.update()
                return

            if self._tool == TOOL_MAGIC:
                ipt = self._widget_to_image(event.pos())
                if ipt is None or self._src_rgb is None:
                    return
                # Shift = 加选, Alt = 减选, 否则 = 替换
                mods = event.modifiers()
                if mods & Qt.ShiftModifier:
                    mode = "add"
                elif mods & Qt.AltModifier:
                    mode = "sub"
                else:
                    mode = "replace"
                self._magic_click(ipt.x(), ipt.y(), mode)
                return

            if self._tool == TOOL_BUCKET:
                ipt = self._widget_to_image(event.pos())
                if ipt is None:
                    return
                self.bucket_fill(ipt.x(), ipt.y())
                return

            if self._tool == TOOL_EYEDROPPER:
                ipt = self._widget_to_image(event.pos())
                if ipt is None or self._src_rgb is None:
                    return
                y, x = ipt.y(), ipt.x()
                r, g, b = self._src_rgb[y, x].tolist()[:3]
                self.color_picked.emit(int(r), int(g), int(b))
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._cursor_pt = event.pos()
        if self._panning:
            super().mouseMoveEvent(event)
            return

        if self._stroking and (event.buttons() & Qt.LeftButton):
            ipt = self._widget_to_image(event.pos(), clamp=True)
            if ipt is not None:
                if self._last_pt is None:
                    self._last_pt = ipt
                self._stamp(self._last_pt, ipt)
                self._last_pt = ipt
            self.update()
            return

        if self._lasso_dragging and (event.buttons() & Qt.LeftButton):
            ipt = self._widget_to_image(event.pos(), clamp=True)
            if ipt is not None:
                last = self._lasso_points[-1]
                # 距离过近的点跳过，减少数据量
                if (ipt.x() - last[0]) ** 2 + (ipt.y() - last[1]) ** 2 >= 2:
                    self._lasso_points.append((ipt.x(), ipt.y()))
            self.update()
            return

        # 仅光标移动时也要刷新（重画笔刷光标圆圈）
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MiddleButton:
            super().mouseReleaseEvent(event)
            return

        if event.button() == Qt.LeftButton:
            if self._stroking:
                self._stroking = False
                self._last_pt = None
                self._capture_stroke_diff()
                self.mask_changed.emit()
                return

            if self._lasso_dragging:
                self._lasso_dragging = False
                ipt = self._widget_to_image(event.pos(), clamp=True)
                if ipt is not None:
                    last = self._lasso_points[-1] if self._lasso_points else None
                    if last is None or (ipt.x() - last[0]) ** 2 + (ipt.y() - last[1]) ** 2 >= 2:
                        self._lasso_points.append((ipt.x(), ipt.y()))
                if len(self._lasso_points) >= 3:
                    value = 255 if self._tool == TOOL_LASSO_BRUSH else 0
                    self._fill_lasso(self._lasso_points, value)
                self._lasso_points = []
                self.update()
                return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802, ANN001
        self._cursor_pt = None
        self.update()
        super().leaveEvent(event)

    # ------------------------------------------------------------------
    # 实际改 mask 的两种操作
    # ------------------------------------------------------------------
    def _stamp(self, p0: QPoint, p1: QPoint) -> None:
        """沿 p0→p1 用圆形画笔涂一笔（图像坐标）。"""
        if self._mask is None:
            return
        value = 255 if self._tool == TOOL_BRUSH else 0
        x0, y0 = p0.x(), p0.y()
        x1, y1 = p1.x(), p1.y()
        dist = max(abs(x1 - x0), abs(y1 - y0))
        steps = max(1, int(dist))
        r = max(1, self._brush_size // 2)
        h, w = self._mask.shape
        ys, xs = np.ogrid[-r : r + 1, -r : r + 1]
        circle = (xs * xs + ys * ys) <= r * r

        for s in range(steps + 1):
            t = s / steps
            cx = int(round(x0 + (x1 - x0) * t))
            cy = int(round(y0 + (y1 - y0) * t))
            x_lo = max(0, cx - r)
            x_hi = min(w, cx + r + 1)
            y_lo = max(0, cy - r)
            y_hi = min(h, cy + r + 1)
            if x_lo >= x_hi or y_lo >= y_hi:
                continue
            sub_x_lo = x_lo - (cx - r)
            sub_x_hi = sub_x_lo + (x_hi - x_lo)
            sub_y_lo = y_lo - (cy - r)
            sub_y_hi = sub_y_lo + (y_hi - y_lo)
            sub = circle[sub_y_lo:sub_y_hi, sub_x_lo:sub_x_hi]
            if value:
                np.maximum(
                    self._mask[y_lo:y_hi, x_lo:x_hi],
                    np.where(sub, 255, 0).astype(np.uint8),
                    out=self._mask[y_lo:y_hi, x_lo:x_hi],
                )
            else:
                self._mask[y_lo:y_hi, x_lo:x_hi] = np.where(
                    sub, 0, self._mask[y_lo:y_hi, x_lo:x_hi]
                )

    def _fill_lasso(self, points: list[tuple[int, int]], value: int) -> None:
        """套索画笔/橡皮：填充闭合多边形内部为 value，并入历史栈。"""
        if self._mask is None or len(points) < 3:
            return

        pts = np.array(points, dtype=np.int32)
        x0, y0 = pts.min(axis=0).tolist()
        x1, y1 = pts.max(axis=0).tolist()
        h, w = self._mask.shape
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w - 1, x1), min(h - 1, y1)
        if x0 >= x1 or y0 >= y1:
            return

        # 构造 bbox 内的多边形掩膜
        rel_pts = pts - np.array([x0, y0])
        sub_h, sub_w = y1 - y0 + 1, x1 - x0 + 1
        poly_mask = np.zeros((sub_h, sub_w), dtype=np.uint8)
        from PIL import Image, ImageDraw

        img = Image.fromarray(poly_mask)
        draw = ImageDraw.Draw(img)
        draw.polygon([tuple(p) for p in rel_pts], fill=255)
        poly_mask = np.array(img)

        before = self._mask[y0 : y1 + 1, x0 : x1 + 1].copy()
        if value:
            self._mask[y0 : y1 + 1, x0 : x1 + 1] = np.maximum(
                self._mask[y0 : y1 + 1, x0 : x1 + 1], poly_mask
            )
        else:
            self._mask[y0 : y1 + 1, x0 : x1 + 1] = np.where(
                poly_mask, 0, self._mask[y0 : y1 + 1, x0 : x1 + 1]
            )
        after = self._mask[y0 : y1 + 1, x0 : x1 + 1].copy()
        if not np.array_equal(before, after):
            self._push_history(y0, y1 + 1, x0, x1 + 1, before, after)
            self.mask_changed.emit()
