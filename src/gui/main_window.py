"""主窗口：左原图 / 右去黑底预览 + 算法切换 + 参数滑块 + 导出 / 批量。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image
from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QClipboard
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QInputDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import ALGORITHMS, apply_protection, process_folder
from ..core.processor import _save_png  # 复用保存逻辑
from .widgets import (
    IMAGE_EXTS,
    TOOL_BRUSH,
    TOOL_BUCKET,
    TOOL_EYEDROPPER,
    TOOL_ERASER,
    TOOL_LASSO_BRUSH,
    TOOL_LASSO_ERASER,
    TOOL_MAGIC,
    TOOL_NONE,
    DropArea,
    PaintableView,
)

APP_VERSION = "1.4.2"

# 底色预览预设（10 个常见颜色 + 棋盘）
BG_PRESETS: list[tuple[str, Optional[tuple[int, int, int]]]] = [
    ("棋盘", None),
    ("纯黑", (0, 0, 0)),
    ("深灰", (60, 60, 60)),
    ("中灰", (128, 128, 128)),
    ("浅灰", (200, 200, 200)),
    ("纯白", (255, 255, 255)),
    ("纯红", (255, 0, 0)),
    ("纯黄", (255, 255, 0)),
    ("品红", (255, 0, 255)),
    ("纯绿", (0, 200, 0)),
    ("天蓝", (90, 170, 230)),
    ("橙色", (255, 140, 30)),
    ("紫色", (130, 60, 200)),
]


def _wrap_in_hscroll(inner: QWidget) -> QScrollArea:
    """把任意横向工具条包进一个仅允许水平滚动的 QScrollArea。

    - 内部 widget 保持自身 sizeHint 宽度，不会被父布局挤压。
    - 父容器变窄时自动出现底部水平滚动条。
    - 高度按内部 sizeHint 锁定，避免出现额外的纵向空白。
    """
    sa = QScrollArea()
    sa.setWidget(inner)
    sa.setWidgetResizable(False)
    sa.setFrameShape(QScrollArea.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    sa.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    h = inner.sizeHint().height()
    # 给水平滚动条留 12px 余量，避免内容被遮挡
    sa.setFixedHeight(h + 12)
    sa.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return sa


def _np_to_qimage(arr: np.ndarray) -> QImage:
    """RGBA uint8 numpy -> QImage（深拷贝，避免内存被释放）。"""
    if arr.ndim == 2:
        arr = np.stack([arr] * 3 + [np.full_like(arr, 255)], axis=-1)
    elif arr.shape[2] == 3:
        a = np.full(arr.shape[:2] + (1,), 255, dtype=np.uint8)
        arr = np.concatenate([arr, a], axis=-1)
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    qimg = QImage(arr.data, w, h, w * 4, QImage.Format_RGBA8888)
    return qimg.copy()


def _qimage_to_np(qimg: QImage) -> np.ndarray:
    """QImage -> RGBA uint8 numpy（深拷贝）。"""
    # 统一转 RGBA8888，避免处理各种 QImage::Format
    qimg = qimg.convertToFormat(QImage.Format_RGBA8888)
    h, w = qimg.height(), qimg.width()
    ptr = qimg.bits()  # PySide6 返回 memoryview
    bytes_per_line = qimg.bytesPerLine()
    # QImage 每行可能有字节对齐，用 bytesPerLine 取实际跨度
    arr = np.frombuffer(ptr, np.uint8).reshape((h, bytes_per_line))
    return arr[:, : w * 4].reshape((h, w, 4)).copy()


# ---------------------------------------------------------------------------
# 批处理后台线程
# ---------------------------------------------------------------------------
class BatchWorker(QThread):
    progress = Signal(int, int, str)  # done, total, current path
    finished_ok = Signal(int)         # 总成功数

    def __init__(
        self,
        src_dir: Path,
        dst_dir: Optional[Path],
        algorithm: str,
        params: dict,
        recursive: bool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.src_dir = src_dir
        self.dst_dir = dst_dir
        self.algorithm = algorithm
        self.params = params
        self.recursive = recursive
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D401
        def cb(done, total, cur):
            if self._cancel:
                # 抛异常中断 process_folder
                raise InterruptedError()
            self.progress.emit(done, total, str(cur))

        try:
            written = process_folder(
                self.src_dir,
                self.dst_dir,
                algorithm=self.algorithm,
                recursive=self.recursive,
                progress=cb,
                **self.params,
            )
            self.finished_ok.emit(len(written))
        except InterruptedError:
            self.finished_ok.emit(-1)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            f"RemoveBlack v{APP_VERSION} — 图片去黑底工具  ·  临江路软件出品"
        )
        self.resize(1320, 760)

        # 窗口图标（标题栏 + 任务栏）
        from ..app import resource_path
        ico = resource_path("assets/icon.ico")
        if ico.exists():
            self.setWindowIcon(QIcon(str(ico)))

        self._src_array: Optional[np.ndarray] = None  # 原图 RGBA
        self._src_path: Optional[Path] = None
        self._result_array: Optional[np.ndarray] = None
        self._param_widgets: dict[str, QSlider] = {}
        self._param_labels: dict[str, QLabel] = {}
        self._settings = QSettings("LinjiangRoad", "RemoveBlack")
        self._templates: dict[str, dict] = self._load_templates()

        # 预览刷新防抖定时器：滑块连续拖动时只算一次，避免主线程卡顿
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self._preview_delay_ms = 80

        self._build_ui()
        self._build_menu()
        self.statusBar().showMessage("拖入一张图片或一个文件夹开始")
        # 状态栏：左侧消息，中间缩放，右侧署名
        self._zoom_label = QLabel("缩放：100%")
        self._zoom_label.setStyleSheet("color:#666;padding:0 12px;")
        self.statusBar().addPermanentWidget(self._zoom_label)
        self._brand_label = QLabel("临江路软件 出品")
        self._brand_label.setStyleSheet("color:#888;padding:0 8px;")
        self.statusBar().addPermanentWidget(self._brand_label)

        # showEvent 里再 setSizes 一次，避免 splitter 在窗口未渲染时
        # 拿不到真实宽度导致 handle 拖不动
        self._first_show = True

    def showEvent(self, event) -> None:  # noqa: N802, ANN001
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            w = max(800, self.width())
            # 左侧给工具栏留更宽空间
            left_w = max(660, w // 2)
            self._splitter.setSizes([left_w, w - left_w])

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        # 左：原图（可绘制蒙版）  |  右：处理结果
        self.src_view = PaintableView()
        self.dst_view = DropArea()
        self.src_view.files_dropped.connect(self._on_files_dropped)
        self.dst_view.files_dropped.connect(self._on_files_dropped)
        self.src_view.mask_changed.connect(self._on_mask_changed)
        self.src_view.selection_changed.connect(self._on_selection_changed)
        self.src_view.history_changed.connect(self._on_history_changed)
        self.src_view.zoom_changed.connect(self._on_zoom_changed)
        self.src_view.color_picked.connect(self._on_color_picked)

        # 双向视图同步：左右始终保持同样的 zoom + pan
        self._sync_lock = False
        self.src_view.view_changed.connect(
            lambda: self._sync_views(src=self.src_view, dst=self.dst_view)
        )
        self.dst_view.view_changed.connect(
            lambda: self._sync_views(src=self.dst_view, dst=self.src_view)
        )

        split = QSplitter(Qt.Horizontal)

        # ---- 左侧：标题 + 工具栏 + 视图 ----
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        ll.addWidget(QLabel("原图（可在此处涂抹保护区域）"))

        # 工具栏（两行：第一行工具按钮，第二行属性与操作）
        tool_bar = QWidget()
        tb = QVBoxLayout(tool_bar)
        tb.setContentsMargins(6, 4, 6, 4)
        tb.setSpacing(4)

        self.btn_tool_none = QToolButton()
        self.btn_tool_none.setText("👆 选择")
        self.btn_tool_brush = QToolButton()
        self.btn_tool_brush.setText("🖌 画笔")
        self.btn_tool_eraser = QToolButton()
        self.btn_tool_eraser.setText("🧽 橡皮")
        self.btn_tool_lasso_brush = QToolButton()
        self.btn_tool_lasso_brush.setText("⛓ 套索画笔")
        self.btn_tool_lasso_eraser = QToolButton()
        self.btn_tool_lasso_eraser.setText("⛓ 套索橡皮")
        self.btn_tool_magic = QToolButton()
        self.btn_tool_magic.setText("🪄 魔棒")
        self.btn_tool_magic.setToolTip(
            "魔棒：点击图中黑色背景区域\n"
            "自动反转为前景并写入保护蒙版\n"
            "Shift+点击 加选 / Alt+点击 减选"
        )
        self.btn_tool_bucket = QToolButton()
        self.btn_tool_bucket.setText("🪣 油漆桶")
        self.btn_tool_bucket.setToolTip(
            "油漆桶：点击封闭区域内部，一键填充保护蒙版\n"
            "适合配合画笔围出区域后快速填满"
        )
        self.btn_tool_eyedropper = QToolButton()
        self.btn_tool_eyedropper.setText("🧪 吸管")
        self.btn_tool_eyedropper.setToolTip(
            "吸管：在原图上点击吸取背景色\n"
            "对「UnMult（吸管背景色）」和「背景色键控」算法生效"
        )
        for b in (
            self.btn_tool_none,
            self.btn_tool_brush,
            self.btn_tool_eraser,
            self.btn_tool_lasso_brush,
            self.btn_tool_lasso_eraser,
            self.btn_tool_magic,
            self.btn_tool_bucket,
            self.btn_tool_eyedropper,
        ):
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setStyleSheet("QToolButton{padding: 3px 6px; margin: 0px;}")
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.btn_tool_none.setChecked(True)
        self.btn_tool_none.clicked.connect(lambda: self._set_tool(TOOL_NONE))
        self.btn_tool_brush.clicked.connect(lambda: self._set_tool(TOOL_BRUSH))
        self.btn_tool_eraser.clicked.connect(lambda: self._set_tool(TOOL_ERASER))
        self.btn_tool_lasso_brush.clicked.connect(lambda: self._set_tool(TOOL_LASSO_BRUSH))
        self.btn_tool_lasso_eraser.clicked.connect(lambda: self._set_tool(TOOL_LASSO_ERASER))
        self.btn_tool_magic.clicked.connect(lambda: self._set_tool(TOOL_MAGIC))
        self.btn_tool_bucket.clicked.connect(lambda: self._set_tool(TOOL_BUCKET))
        self.btn_tool_eyedropper.clicked.connect(
            lambda: self._set_tool(TOOL_EYEDROPPER)
        )

        # 第一行：工具
        row_tools = QHBoxLayout()
        row_tools.setSpacing(4)
        row_tools.addWidget(QLabel("工具："))
        row_tools.addWidget(self.btn_tool_none)
        row_tools.addWidget(self.btn_tool_brush)
        row_tools.addWidget(self.btn_tool_eraser)
        row_tools.addWidget(self.btn_tool_lasso_brush)
        row_tools.addWidget(self.btn_tool_lasso_eraser)
        row_tools.addWidget(self.btn_tool_magic)
        row_tools.addWidget(self.btn_tool_bucket)
        row_tools.addWidget(self.btn_tool_eyedropper)
        row_tools.addStretch(1)
        tb.addLayout(row_tools)

        # 第二行：工具属性 + 选区操作
        row_props = QHBoxLayout()
        row_props.setSpacing(4)

        self.brush_size_slider = QSlider(Qt.Horizontal)
        self.brush_size_slider.setRange(2, 400)
        self.brush_size_slider.setValue(30)
        self.brush_size_slider.setFixedWidth(90)
        self.brush_size_slider.valueChanged.connect(self._on_brush_size_changed)
        self.brush_size_label = QLabel("30")
        self.brush_size_label.setFixedWidth(34)
        self.magic_tol_slider = QSlider(Qt.Horizontal)
        self.magic_tol_slider.setRange(0, 128)
        self.magic_tol_slider.setValue(30)
        self.magic_tol_slider.setFixedWidth(70)
        self.magic_tol_slider.valueChanged.connect(self._on_magic_tol_changed)
        self.magic_tol_label = QLabel("30")
        self.magic_tol_label.setFixedWidth(30)

        row_props.addWidget(QLabel("画笔："))
        row_props.addWidget(self.brush_size_slider)
        row_props.addWidget(self.brush_size_label)
        row_props.addSpacing(12)
        row_props.addWidget(QLabel("魔棒："))
        row_props.addWidget(self.magic_tol_slider)
        row_props.addWidget(self.magic_tol_label)

        row_props.addSpacing(20)

        self.btn_invert_mask = QToolButton()
        self.btn_invert_mask.setText("⤺ 反选")
        self.btn_invert_mask.setToolTip(
            "把当前保护蒙版 0↔255 翻转\n"
            "快捷键 Ctrl+I"
        )
        self.btn_invert_mask.clicked.connect(self._on_invert_mask)
        self.btn_undo = QToolButton()
        self.btn_undo.setText("↶ 撤销")
        self.btn_redo = QToolButton()
        self.btn_redo.setText("↷ 重做")
        self.btn_undo.setEnabled(False)
        self.btn_redo.setEnabled(False)
        self.btn_undo.clicked.connect(self._on_undo)
        self.btn_redo.clicked.connect(self._on_redo)
        self.btn_clear_mask = QToolButton()
        self.btn_clear_mask.setText("清空蒙版")
        self.btn_clear_mask.clicked.connect(self._on_clear_mask)
        for b in (self.btn_invert_mask, self.btn_undo, self.btn_redo, self.btn_clear_mask):
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setStyleSheet("QToolButton{padding: 3px 6px; margin: 0px;}")
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        row_props.addWidget(self.btn_invert_mask)
        row_props.addWidget(self.btn_undo)
        row_props.addWidget(self.btn_redo)
        row_props.addWidget(self.btn_clear_mask)
        row_props.addStretch(1)
        tb.addLayout(row_props)

        ll.addWidget(tool_bar)
        ll.addWidget(self.src_view, 1)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 4, 4, 4)
        rl.addWidget(QLabel("去黑底预览（棋盘格 = 透明）"))

        # 右侧顶部：底色预设条（与左侧工具栏等高）
        bg_bar = QWidget()
        bg_hl = QHBoxLayout(bg_bar)
        bg_hl.setContentsMargins(0, 0, 0, 0)
        bg_hl.setSpacing(3)
        bg_hl.addWidget(QLabel("底色："))
        self._bg_buttons: list[QPushButton] = []
        for name, rgb in BG_PRESETS:
            btn = QPushButton()
            btn.setFixedSize(22, 22)
            btn.setToolTip(name)
            btn.setCheckable(True)
            if rgb is None:
                # 棋盘标记
                btn.setText("◫")
                btn.setStyleSheet(
                    "QPushButton{background:#fff;border:1px solid #888;}"
                    "QPushButton:checked{border:2px solid #1976d2;}"
                )
            else:
                r, g, b = rgb
                # 浅色边框深一点便于看到
                border = "#ddd" if (r + g + b) < 360 else "#666"
                btn.setStyleSheet(
                    f"QPushButton{{background:rgb({r},{g},{b});"
                    f"border:1px solid {border};}}"
                    "QPushButton:checked{border:2px solid #1976d2;}"
                )
            btn.clicked.connect(
                lambda _=False, c=rgb: self._on_bg_color_chosen(c)
            )
            self._bg_buttons.append(btn)
            bg_hl.addWidget(btn)
        # 默认棋盘
        self._bg_buttons[0].setChecked(True)
        bg_hl.addStretch(1)
        rl.addWidget(_wrap_in_hscroll(bg_bar))

        rl.addWidget(self.dst_view, 1)
        # 让两侧 widget 不要因为内容（工具栏、底色条）锁定最小宽度
        left.setMinimumWidth(200)
        right.setMinimumWidth(200)
        # 关键：tool bar / bg bar 内容超宽时允许横向裁剪而非顶死 splitter
        left.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        right.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        split.addWidget(right)
        split.setOpaqueResize(True)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(10)
        split.setStyleSheet(
            "QSplitter::handle:horizontal{background:#888;}"
            "QSplitter::handle:horizontal:hover{background:#1976d2;}"
            "QSplitter::handle:horizontal:pressed{background:#0d47a1;}"
        )
        self._splitter = split

        # ------------------------------------------------------------------
        # 控制面板（分组：算法与操作 | 算法参数 | 全局与模板）
        # ------------------------------------------------------------------
        ctrl = QWidget()
        ctrl.setStyleSheet(
            "QGroupBox{font-weight:bold;border:1px solid #bbb;border-radius:4px;"
            "margin-top:6px;padding-top:6px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;}"
        )
        ctrl_layout = QHBoxLayout(ctrl)
        ctrl_layout.setContentsMargins(8, 2, 8, 6)
        ctrl_layout.setSpacing(10)

        # ---- 算法与操作 ----
        algo_group = QGroupBox("算法与操作")
        algo_layout = QVBoxLayout(algo_group)
        algo_layout.setSpacing(6)
        algo_layout.setContentsMargins(8, 6, 8, 6)

        self.algo_combo = QComboBox()
        self.algo_combo.setMinimumWidth(220)
        self.algo_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        for key, info in ALGORITHMS.items():
            self.algo_combo.addItem(info["label"], key)
            self.algo_combo.setItemData(
                self.algo_combo.count() - 1,
                info.get("tooltip", ""),
                Qt.ToolTipRole,
            )
        self.algo_combo.currentIndexChanged.connect(self._on_algo_changed)
        algo_layout.addWidget(self.algo_combo)

        # 吸管颜色预览（对 unmult_color / color_key 显示）
        self.color_preview_box = QWidget()
        color_preview_hl = QHBoxLayout(self.color_preview_box)
        color_preview_hl.setContentsMargins(0, 0, 0, 0)
        color_preview_hl.addWidget(QLabel("吸管颜色："))
        self.color_preview_btn = QPushButton()
        self.color_preview_btn.setFixedSize(22, 22)
        self.color_preview_btn.setToolTip("当前吸管吸取的背景色")
        self.color_preview_btn.setEnabled(False)
        color_preview_hl.addWidget(self.color_preview_btn)
        self.color_preview_label = QLabel("—")
        self.color_preview_label.setStyleSheet("color:#666;")
        color_preview_hl.addWidget(self.color_preview_label)
        color_preview_hl.addStretch(1)
        self.color_preview_box.setVisible(False)
        algo_layout.addWidget(self.color_preview_box)

        action_layout = QHBoxLayout()
        self.btn_open = QPushButton("打开图片")
        self.btn_save = QPushButton("导出 PNG")
        self.btn_batch = QPushButton("批量处理")
        self.btn_open.clicked.connect(self._on_open)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_batch.clicked.connect(self._on_batch)
        self.btn_save.setEnabled(False)
        for b in (self.btn_open, self.btn_save, self.btn_batch):
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            action_layout.addWidget(b)
        action_layout.addStretch(1)
        algo_layout.addLayout(action_layout)

        ctrl_layout.addWidget(algo_group)

        # ---- 算法参数（可横向滚动，避免拥挤）----
        param_group = QGroupBox("算法参数")
        param_group_layout = QVBoxLayout(param_group)
        param_group_layout.setContentsMargins(6, 6, 6, 6)

        self.param_box = QWidget()
        self.param_grid = QGridLayout(self.param_box)
        self.param_grid.setContentsMargins(4, 0, 4, 0)
        self.param_grid.setHorizontalSpacing(16)
        self.param_grid.setVerticalSpacing(6)

        param_scroll = QScrollArea()
        param_scroll.setWidget(self.param_box)
        param_scroll.setWidgetResizable(True)
        param_scroll.setFrameShape(QScrollArea.NoFrame)
        param_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        param_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        param_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        param_scroll.setFixedHeight(100)
        param_group_layout.addWidget(param_scroll)
        ctrl_layout.addWidget(param_group, 1)

        # ---- 全局与模板 ----
        global_group = QGroupBox("全局与模板")
        global_layout = QVBoxLayout(global_group)
        global_layout.setSpacing(6)
        global_layout.setContentsMargins(8, 6, 8, 6)

        floor_row = QWidget()
        floor_hl = QHBoxLayout(floor_row)
        floor_hl.setContentsMargins(0, 0, 0, 0)
        self.alpha_floor_slider = QSlider(Qt.Horizontal)
        self.alpha_floor_slider.setRange(0, 255)
        self.alpha_floor_slider.setValue(0)
        self.alpha_floor_slider.setFixedWidth(110)
        self.alpha_floor_slider.setTickPosition(QSlider.TicksBelow)
        self.alpha_floor_slider.setTickInterval(32)
        self.alpha_floor_slider.valueChanged.connect(self._on_alpha_floor_changed)
        self.alpha_floor_label = QLabel("0")
        self.alpha_floor_label.setFixedWidth(28)
        self.alpha_floor_label.setAlignment(Qt.AlignCenter)
        floor_hl.addWidget(QLabel("透明度下限："))
        floor_hl.addWidget(self.alpha_floor_slider)
        floor_hl.addWidget(self.alpha_floor_label)
        global_layout.addWidget(floor_row)

        template_row = QWidget()
        template_hl = QHBoxLayout(template_row)
        template_hl.setContentsMargins(0, 0, 0, 0)
        template_hl.addWidget(QLabel("模板："))
        self.template_combo = QComboBox()
        self.template_combo.setFixedWidth(100)
        self.template_combo.addItem("选择模板…", "")
        self._refresh_template_combo()
        self.template_combo.activated.connect(self._on_template_selected)
        self.btn_save_template = QPushButton("保存")
        self.btn_save_template.setToolTip("保存当前算法与参数为模板")
        self.btn_save_template.clicked.connect(self._on_save_template)
        template_hl.addWidget(self.template_combo)
        template_hl.addWidget(self.btn_save_template)
        global_layout.addWidget(template_row)

        # 视图缩放（从顶部工具栏移下来，避免工具栏过宽）
        zoom_row = QWidget()
        zoom_hl = QHBoxLayout(zoom_row)
        zoom_hl.setContentsMargins(0, 0, 0, 0)
        self.btn_zoom_out = QPushButton("－")
        self.btn_zoom_in = QPushButton("＋")
        self.btn_zoom_fit = QPushButton("适配")
        for b in (self.btn_zoom_out, self.btn_zoom_in, self.btn_zoom_fit):
            b.setFixedWidth(40)
        self.btn_zoom_out.clicked.connect(lambda: self.src_view.zoom_out())
        self.btn_zoom_in.clicked.connect(lambda: self.src_view.zoom_in())
        self.btn_zoom_fit.clicked.connect(lambda: self.src_view.fit_view())
        zoom_hl.addWidget(QLabel("视图："))
        zoom_hl.addWidget(self.btn_zoom_out)
        zoom_hl.addWidget(self.btn_zoom_in)
        zoom_hl.addWidget(self.btn_zoom_fit)
        global_layout.addWidget(zoom_row)

        ctrl_layout.addWidget(global_group)

        # 总布局
        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(split, 1)
        v.addWidget(ctrl)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        # 初始化算法面板（默认选第一项）
        self._on_algo_changed(0)

    def _build_menu(self) -> None:
        m_file = self.menuBar().addMenu("文件(&F)")
        a_open = QAction("打开图片…", self)
        a_open.setShortcut("Ctrl+O")
        a_open.triggered.connect(self._on_open)
        m_file.addAction(a_open)

        a_save = QAction("导出 PNG…", self)
        a_save.setShortcut("Ctrl+S")
        a_save.triggered.connect(self._on_save)
        m_file.addAction(a_save)

        a_paste = QAction("粘贴图片", self)
        a_paste.setShortcut("Ctrl+V")
        a_paste.triggered.connect(self._on_paste)
        m_file.addAction(a_paste)

        m_file.addSeparator()
        a_batch = QAction("批量处理文件夹…", self)
        a_batch.triggered.connect(self._on_batch)
        m_file.addAction(a_batch)

        m_file.addSeparator()
        a_quit = QAction("退出", self)
        a_quit.setShortcut("Ctrl+Q")
        a_quit.triggered.connect(self.close)
        m_file.addAction(a_quit)

        # 编辑菜单
        m_edit = self.menuBar().addMenu("编辑(&E)")
        self.act_undo = QAction("撤销", self)
        self.act_undo.setShortcut("Ctrl+Z")
        self.act_undo.triggered.connect(self._on_undo)
        self.act_undo.setEnabled(False)
        m_edit.addAction(self.act_undo)

        self.act_redo = QAction("重做", self)
        self.act_redo.setShortcut("Ctrl+Y")
        self.act_redo.triggered.connect(self._on_redo)
        self.act_redo.setEnabled(False)
        m_edit.addAction(self.act_redo)

        # 备用快捷键 Ctrl+Shift+Z
        a_redo2 = QAction("重做(Z)", self)
        a_redo2.setShortcut("Ctrl+Shift+Z")
        a_redo2.triggered.connect(self._on_redo)
        self.addAction(a_redo2)

        m_edit.addSeparator()
        a_invert = QAction("反选蒙版", self)
        a_invert.setShortcut("Ctrl+I")
        a_invert.triggered.connect(self._on_invert_mask)
        m_edit.addAction(a_invert)

        a_clear = QAction("清空蒙版", self)
        a_clear.triggered.connect(self._on_clear_mask)
        m_edit.addAction(a_clear)

        # 视图菜单
        m_view = self.menuBar().addMenu("视图(&V)")
        a_fit = QAction("适配窗口", self)
        a_fit.setShortcut("Ctrl+0")
        a_fit.triggered.connect(lambda: self.src_view.fit_view())
        m_view.addAction(a_fit)

        a_zin = QAction("放大", self)
        a_zin.setShortcut("Ctrl+=")
        a_zin.triggered.connect(lambda: self.src_view.zoom_in())
        m_view.addAction(a_zin)

        a_zin2 = QAction("放大(+)", self)
        a_zin2.setShortcut("Ctrl++")
        a_zin2.triggered.connect(lambda: self.src_view.zoom_in())
        self.addAction(a_zin2)

        a_zout = QAction("缩小", self)
        a_zout.setShortcut("Ctrl+-")
        a_zout.triggered.connect(lambda: self.src_view.zoom_out())
        m_view.addAction(a_zout)

        m_help = self.menuBar().addMenu("帮助(&H)")
        a_about = QAction("关于…", self)
        a_about.triggered.connect(self._show_about)
        m_help.addAction(a_about)

    # ------------------------------------------------------------------
    # 算法面板动态构建
    # ------------------------------------------------------------------
    def _on_algo_changed(self, index: int) -> None:
        # 清空旧参数控件
        while self.param_grid.count():
            item = self.param_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._param_widgets.clear()
        self._param_labels.clear()

        key = self.algo_combo.itemData(index)
        info = ALGORITHMS[key]

        for i, spec in enumerate(info["params"]):
            scale = spec.get("scale", 1)  # >1 表示 slider 是浮点放大版

            slider = QSlider(Qt.Horizontal)
            slider.setMinimum(spec["min"])
            slider.setMaximum(spec["max"])
            slider.setValue(spec["default"])
            slider.setMinimumWidth(90)
            slider.setMaximumWidth(140)
            slider.setTickPosition(QSlider.TicksBelow)
            slider.setTickInterval(max(1, (spec["max"] - spec["min"]) // 8))

            def _fmt(v: int, s: int = scale) -> str:
                return f"{v / s:.2f}" if s > 1 else str(v)

            value_lbl = QLabel(_fmt(spec["default"]))
            value_lbl.setFixedWidth(40)
            value_lbl.setAlignment(Qt.AlignCenter)

            slider.valueChanged.connect(
                lambda v, lbl=value_lbl, fmt=_fmt: (
                    lbl.setText(fmt(v)),
                    self._schedule_preview_refresh(),
                )
            )

            # 两列网格：每格 = 标签 + 滑条 + 数值
            row, col = divmod(i, 2)
            cell = QWidget()
            hl = QHBoxLayout(cell)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(4)
            lbl = QLabel(spec["label"] + "：")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl.setFixedWidth(120)
            hl.addWidget(lbl)
            hl.addWidget(slider, 1)
            hl.addWidget(value_lbl)
            self.param_grid.addWidget(cell, row, col)

            self._param_widgets[spec["name"]] = slider
            self._param_labels[spec["name"]] = value_lbl
            # 把 scale 挂在 slider 上方便 _current_params 读取
            slider.setProperty("rb_scale", scale)

        # —— chroma/color_key 算法两个滑块联动锁定，避免 upper <= lower 抛错 ——
        if "lower" in self._param_widgets and "upper" in self._param_widgets:
            lo = self._param_widgets["lower"]
            up = self._param_widgets["upper"]

            def _sync_from_lower(v):
                if up.value() <= v:
                    up.blockSignals(True)
                    up.setValue(min(up.maximum(), v + 1))
                    self._param_labels["upper"].setText(str(up.value()))
                    up.blockSignals(False)

            def _sync_from_upper(v):
                if lo.value() >= v:
                    lo.blockSignals(True)
                    lo.setValue(max(lo.minimum(), v - 1))
                    self._param_labels["lower"].setText(str(lo.value()))
                    lo.blockSignals(False)

            lo.valueChanged.connect(_sync_from_lower)
            up.valueChanged.connect(_sync_from_upper)

        # 对需要背景色的算法显示吸管颜色预览
        is_color_algo = key in ("unmult_color", "color_key")
        self.color_preview_box.setVisible(is_color_algo)
        if is_color_algo:
            params = self._current_params()
            self._update_color_preview(
                int(params.get("bg_r", 0)),
                int(params.get("bg_g", 255)),
                int(params.get("bg_b", 0)),
            )

        # 当前算法提示（下拉框收起时也可悬停查看）
        self.algo_combo.setToolTip(info.get("tooltip", ""))

        self._refresh_preview()

    def _schedule_preview_refresh(self) -> None:
        """延迟刷新预览：滑块拖动时每动一下都重算会导致严重卡顿。"""
        self._preview_timer.stop()
        self._preview_timer.start(self._preview_delay_ms)

    def _current_algorithm(self) -> str:
        return self.algo_combo.itemData(self.algo_combo.currentIndex())

    def _current_params(self) -> dict:
        out = {}
        for name, w in self._param_widgets.items():
            scale = w.property("rb_scale") or 1
            v = w.value()
            out[name] = v / scale if scale > 1 else v
        return out

    def _load_templates(self) -> dict[str, dict]:
        raw = self._settings.value("templates", "")
        if not raw:
            return {}
        try:
            data = json.loads(str(raw))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_templates_store(self) -> None:
        self._settings.setValue("templates", json.dumps(self._templates, ensure_ascii=False))

    def _refresh_template_combo(self) -> None:
        if not hasattr(self, "template_combo"):
            return
        cur = self.template_combo.currentData()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("选择模板…", "")
        for name in sorted(self._templates):
            self.template_combo.addItem(name, name)
        idx = self.template_combo.findData(cur)
        self.template_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.template_combo.blockSignals(False)

    def _current_template_data(self) -> dict:
        return {
            "algorithm": self._current_algorithm(),
            "params": self._current_params(),
            "alpha_floor": int(self.alpha_floor_slider.value()),
        }

    def _apply_template_data(self, data: dict) -> None:
        algo = data.get("algorithm")
        idx = self.algo_combo.findData(algo)
        if idx >= 0 and idx != self.algo_combo.currentIndex():
            self.algo_combo.setCurrentIndex(idx)
        params = data.get("params", {}) if isinstance(data.get("params", {}), dict) else {}
        for name, val in params.items():
            w = self._param_widgets.get(name)
            if w is None:
                continue
            scale = w.property("rb_scale") or 1
            raw = int(round(float(val) * scale)) if scale > 1 else int(round(float(val)))
            w.setValue(max(w.minimum(), min(w.maximum(), raw)))
        if "alpha_floor" in data:
            self.alpha_floor_slider.setValue(
                max(self.alpha_floor_slider.minimum(), min(self.alpha_floor_slider.maximum(), int(data["alpha_floor"])))
            )
        self._refresh_preview()

    def _on_template_selected(self, index: int) -> None:
        name = self.template_combo.itemData(index)
        if not name:
            return
        data = self._templates.get(name)
        if not data:
            return
        self._apply_template_data(data)
        self.statusBar().showMessage(f"已应用模板：{name}", 3000)

    def _on_save_template(self) -> None:
        name, ok = QInputDialog.getText(self, "保存参数模板", "模板名称：")
        name = name.strip()
        if not ok or not name:
            return
        self._templates[name] = self._current_template_data()
        self._save_templates_store()
        self._refresh_template_combo()
        idx = self.template_combo.findData(name)
        if idx >= 0:
            self.template_combo.setCurrentIndex(idx)
        self.statusBar().showMessage(f"已保存模板：{name}", 3000)

    # ------------------------------------------------------------------
    # 加载 / 处理 / 显示
    # ------------------------------------------------------------------
    def _load_image(self, path: Path) -> None:
        try:
            img = Image.open(path)
            if img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGBA")
            arr = np.array(img)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法加载图片：\n{e}")
            return

        self._set_source_array(arr, path)

    def _set_source_array(self, arr: np.ndarray, path: Path | None = None) -> None:
        """把 numpy 数组设置为当前源图。"""
        self._src_array = arr
        self._src_path = path
        self.src_view.set_image(_np_to_qimage(arr))
        self.src_view.set_image_size(arr.shape[1], arr.shape[0])  # 重置蒙版尺寸
        # 把 RGB 缓存给 src_view 以供魔棒/吸管使用
        if arr.ndim == 2:
            rgb_for_wand = np.stack([arr, arr, arr], axis=-1).astype(np.uint8)
        elif arr.shape[2] >= 3:
            rgb_for_wand = arr[..., :3].astype(np.uint8)
        else:
            rgb_for_wand = None
        self.src_view.set_source_rgb(rgb_for_wand)
        self.btn_save.setEnabled(True)
        name = path.name if path else "粘贴图片"
        self.statusBar().showMessage(
            f"已加载：{name}    {arr.shape[1]}×{arr.shape[0]}"
        )
        self._refresh_preview()

    def _on_paste(self) -> None:
        """从剪贴板粘贴图片或文件路径。"""
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()

        # 1. 优先粘贴图片数据
        if mime.hasImage():
            qimg = clipboard.image()
            if qimg.isNull():
                self.statusBar().showMessage("剪贴板中没有有效图片", 3000)
                return
            try:
                arr = _qimage_to_np(qimg)
            except Exception as e:
                QMessageBox.critical(self, "错误", f"无法读取剪贴板图片：\n{e}")
                return
            self._set_source_array(arr, None)
            return

        # 2. 如果是文本且是文件路径，则打开文件
        if mime.hasText():
            text = mime.text().strip().strip('"')
            p = Path(text)
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                self._load_image(p)
                return

        self.statusBar().showMessage("剪贴板中没有图片或图片文件路径", 3000)

    def _refresh_preview(self) -> None:
        if self._src_array is None:
            self.dst_view.clear()
            self._result_array = None
            return
        algo = self._current_algorithm()
        func = ALGORITHMS[algo]["func"]
        try:
            result = func(self._src_array, **self._current_params())
        except Exception as e:
            self.statusBar().showMessage(f"算法出错：{e}")
            return

        # 后处理：透明度下限 + 保护蒙版
        floor = self.alpha_floor_slider.value()
        mask = self.src_view.get_mask()
        if floor > 0 or (mask is not None and mask.any()):
            src_rgb = self._src_array[..., :3] if self._src_array.shape[2] >= 3 else None
            result = apply_protection(
                result, src_rgb=src_rgb, mask=mask, alpha_floor=floor
            )

        self._result_array = result
        self.dst_view.set_image(_np_to_qimage(result))

    # ------------------------------------------------------------------
    # 工具栏 / 蒙版 / 透明度下限
    # ------------------------------------------------------------------
    def _set_tool(self, tool: str) -> None:
        self.src_view.set_tool(tool)
        self.btn_tool_none.setChecked(tool == TOOL_NONE)
        self.btn_tool_brush.setChecked(tool == TOOL_BRUSH)
        self.btn_tool_eraser.setChecked(tool == TOOL_ERASER)
        self.btn_tool_lasso_brush.setChecked(tool == TOOL_LASSO_BRUSH)
        self.btn_tool_lasso_eraser.setChecked(tool == TOOL_LASSO_ERASER)
        self.btn_tool_magic.setChecked(tool == TOOL_MAGIC)
        self.btn_tool_bucket.setChecked(tool == TOOL_BUCKET)
        self.btn_tool_eyedropper.setChecked(tool == TOOL_EYEDROPPER)
        msgs = {
            TOOL_BRUSH: "保护画笔：在原图上涂抹要保留的区域",
            TOOL_ERASER: "橡皮擦：擦除已涂抹的保护区",
            TOOL_LASSO_BRUSH: "套索画笔：拖动绘制闭合区域 → 内部全部保护",
            TOOL_LASSO_ERASER: "套索橡皮：拖动绘制闭合区域 → 内部全部清除",
            TOOL_MAGIC: "魔棒：点击黑色背景区域；Shift 加选 / Alt 减选；结果直接写入保护蒙版",
            TOOL_BUCKET: "油漆桶：点击封闭区域内部，一键填充保护蒙版",
            TOOL_EYEDROPPER: "吸管：点击原图吸取背景色（用于吸管背景色 / 背景色键控算法）",
            TOOL_NONE: "浏览模式（滚轮缩放，中键拖动平移）",
        }
        self.statusBar().showMessage(msgs.get(tool, ""))

    def _on_brush_size_changed(self, v: int) -> None:
        self.brush_size_label.setText(str(v))
        self.src_view.set_brush_size(v)

    def _on_magic_tol_changed(self, v: int) -> None:
        self.magic_tol_label.setText(str(v))
        self.src_view.set_magic_tolerance(v)

    def _on_selection_changed(self, selected_pixels: int, total_pixels: int, mode: str) -> None:
        if total_pixels <= 0:
            self.statusBar().showMessage("选区未准备好：请重新打开图片后再试", 4000)
            return
        if selected_pixels <= 0:
            self.statusBar().showMessage("未选中像素：请点在图片内部，或调高容差", 4000)
            return
        mode_name = {"replace": "选区", "add": "加选后选区", "sub": "减选后选区"}.get(mode, "选区")
        pct = selected_pixels / max(1, total_pixels) * 100
        self.statusBar().showMessage(f"魔棒{mode_name}：{selected_pixels} 像素（{pct:.1f}%）", 3000)

    def _on_color_picked(self, r: int, g: int, b: int) -> None:
        """吸管吸取颜色：更新颜色预览，并写入背景色参数。"""
        if self._current_algorithm() not in ("unmult_color", "color_key"):
            # 自动切换到背景色键控算法（更直观、不易偏色）
            idx = self.algo_combo.findData("color_key")
            if idx >= 0:
                self.algo_combo.setCurrentIndex(idx)

        self._set_bg_color(r, g, b)

    def _set_bg_color(self, r: int, g: int, b: int) -> None:
        self._update_color_preview(r, g, b)
        for name, val in (("bg_r", r), ("bg_g", g), ("bg_b", b)):
            w = self._param_widgets.get(name)
            if w is not None:
                w.blockSignals(True)
                w.setValue(val)
                self._param_labels[name].setText(str(val))
                w.blockSignals(False)
        self._refresh_preview()
        self.statusBar().showMessage(
            f"已吸取背景色：R={r}, G={g}, B={b}", 3000
        )

    def _update_color_preview(self, r: int, g: int, b: int) -> None:
        self.color_preview_btn.setStyleSheet(
            f"QPushButton{{background:rgb({r},{g},{b});border:1px solid #888;}}"
        )
        self.color_preview_label.setText(f"({r},{g},{b})")
        self.color_preview_box.setVisible(True)

    def _on_invert_mask(self) -> None:
        self.src_view.invert_mask()
        self._refresh_preview()
        self.statusBar().showMessage("已反选保护蒙版", 3000)

    def _on_clear_mask(self) -> None:
        self.src_view.clear_mask()
        self._refresh_preview()

    def _on_mask_changed(self) -> None:
        self._refresh_preview()
        cov = self.src_view.mask_coverage()
        if cov > 0:
            self.statusBar().showMessage(f"蒙版覆盖：{cov * 100:.1f}%")

    def _on_alpha_floor_changed(self, v: int) -> None:
        self.alpha_floor_label.setText(str(v))
        self._schedule_preview_refresh()

    # ---- 撤销 / 重做 / 历史按钮状态 ----
    def _on_undo(self) -> None:
        self.src_view.undo()
        self._refresh_preview()

    def _on_redo(self) -> None:
        self.src_view.redo()
        self._refresh_preview()

    def _on_history_changed(self, can_undo: bool, can_redo: bool) -> None:
        self.btn_undo.setEnabled(can_undo)
        self.btn_redo.setEnabled(can_redo)
        if hasattr(self, "act_undo"):
            self.act_undo.setEnabled(can_undo)
            self.act_redo.setEnabled(can_redo)

    # ---- 缩放 ----
    def _on_zoom_changed(self, percent: float) -> None:
        self._zoom_label.setText(f"缩放：{percent:.0f}%")

    def _sync_views(self, src, dst) -> None:
        """把 src 的 zoom + pan 同步到 dst，防止递归。"""
        if self._sync_lock:
            return
        self._sync_lock = True
        try:
            zoom, pan_x, pan_y = src.get_view_state()
            dst.set_view_state(zoom, pan_x, pan_y)
        finally:
            self._sync_lock = False

    # ---- 底色 ----
    def _on_bg_color_chosen(self, rgb: Optional[tuple[int, int, int]]) -> None:
        # 单选互斥
        for btn, (_, c) in zip(self._bg_buttons, BG_PRESETS):
            btn.setChecked(c == rgb)
        if rgb is None:
            self.dst_view.set_background_color(None)
        else:
            self.dst_view.set_background_color(QColor(*rgb))

    # ------------------------------------------------------------------
    # 槽函数
    # ------------------------------------------------------------------
    def _on_files_dropped(self, paths: list[Path]) -> None:
        if not paths:
            return
        first = paths[0]
        if first.is_dir():
            # 拖入目录 -> 直接进入批处理
            self._batch_with_src(first)
            return
        # 拖入文件：只取第一张做预览
        self._load_image(first)

    def _on_open(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", f"图片文件 ({exts});;所有文件 (*.*)"
        )
        if path:
            self._load_image(Path(path))

    def _on_save(self) -> None:
        if self._result_array is None:
            return
        if self._src_path is not None:
            default = str(
                self._src_path.with_name(f"{self._src_path.stem}_nobg.png")
            )
        else:
            default = "clipboard_nobg.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 PNG", default, "PNG 图片 (*.png)"
        )
        if not path:
            return
        try:
            _save_png(self._result_array, path)
            self.statusBar().showMessage(f"已导出：{path}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"导出失败：\n{e}")

    def _on_batch(self) -> None:
        src = QFileDialog.getExistingDirectory(self, "选择源文件夹")
        if not src:
            return
        self._batch_with_src(Path(src))

    def _batch_with_src(self, src_dir: Path) -> None:
        ret = QMessageBox.question(
            self,
            "批量处理",
            f"将处理文件夹：\n{src_dir}\n\n"
            f"算法：{ALGORITHMS[self._current_algorithm()]['label']}\n\n"
            f"输出位置：源同目录，文件名加 _nobg 后缀。\n是否继续？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return

        # 进度对话框
        dlg = QProgressDialog("准备中…", "取消", 0, 100, self)
        dlg.setWindowTitle("批量处理")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        worker = BatchWorker(
            src_dir=src_dir,
            dst_dir=None,
            algorithm=self._current_algorithm(),
            params=self._current_params(),
            recursive=False,
            parent=self,
        )

        def on_progress(done: int, total: int, cur: str) -> None:
            dlg.setMaximum(total)
            dlg.setValue(done)
            dlg.setLabelText(f"{done}/{total}\n{Path(cur).name}")

        def on_done(count: int) -> None:
            dlg.close()
            if count < 0:
                QMessageBox.information(self, "已取消", "批量处理已取消。")
            else:
                QMessageBox.information(self, "完成", f"成功处理 {count} 张图片。")

        worker.progress.connect(on_progress)
        worker.finished_ok.connect(on_done)
        dlg.canceled.connect(worker.cancel)
        worker.start()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 RemoveBlack",
            f"<h3>RemoveBlack v{APP_VERSION}</h3>"
            "<p>桌面端图片去黑底工具</p>"
            "<p>核心算法 <b>UnMult</b>：A = max(R,G,B); RGB ÷= A，"
            "合成回黑底视觉零损失。</p>"
            "<p>另含阈值法、颜色键、HSV 去色背景、背景色键控等备选算法。</p>"
            "<p><b>v1.4.2</b>：UI 优化：修复参数标签截断，算法下拉框增加悬停说明；"
            "框选画笔/橡皮改为套索画笔/橡皮。</p>"
            "<p><b>v1.4.1</b>：移除有问题的「前景参考色」，新增「背景色键控（保色）」算法。"
            "该算法按像素与背景色的距离直接映射 alpha 并保留原 RGB，"
            "适合对色彩准确度要求高、UnMult 容易产生色相偏移的场景（如绿幕上的金黄色文字）。</p>"
            "<p><b>v1.4.0</b>：新增「UnMult（吸管背景色）」、Ctrl+V 粘贴图片、吸管工具。</p>"
            "<p><b>v1.3.2</b>：UnMult 新增「实体增益」滑块，专门解决"
            "暗色玻璃 / 暗色实体表面漏底问题。</p>"
            "<p><b>v1.3.1</b>：增益拉高时颜色严重偏色（青瓶变橙瓶）的问题修复。</p>"
            "<p><b>v1.3.0</b>：UnMult 新增「不透明度增益」滑块。</p>"
            "<hr>"
            "<p align='right'><b>临江路软件</b> 出品</p>",
        )


def run() -> None:
    import sys

    # Windows 任务栏图标分组：必须设独立 AppUserModelID，
    # 否则 PyInstaller 打包后任务栏可能显示成默认 Python 图标
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "RemoveBlack.Desktop.1"
            )
        except Exception:
            pass

    app = QApplication.instance() or QApplication(sys.argv)

    # 全局应用图标
    from ..app import resource_path
    ico = resource_path("assets/icon.ico")
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))

    win = MainWindow()
    win.show()

    # 命令行参数中带文件路径时，自动加载
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        p = Path(args[0])
        if p.is_file():
            win._load_image(p)
        elif p.is_dir():
            win._batch_with_src(p)

    sys.exit(app.exec())
