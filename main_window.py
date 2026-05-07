"""
紧凑图标式工具栏 + 应用控制器
==================================

UI 概要
-------
应用主窗口仍是 :class:`PreviewWindow`。围绕预览悬浮 **一个**
紧凑的图标式工具栏 :class:`ControlBar`：

    ┌─────────────────────────────────┐
    │  [📷] | [□] [○] [☆]              │   <- 设备/形状（图标，无文字）
    │  ━━━━━━●━━━━━━━━━━━━━━━━━━━━━━━ │   <- 预览尺寸 SeekBar
    └─────────────────────────────────┘

点击工具栏左侧的相机图标弹出 :class:`CameraPopup`，包含
"设备" 与 "分辨率" 下拉框（分辨率仅展示 ≥720p 的项）。
"""
from __future__ import annotations

import math
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
from PyQt5.QtCore import (
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QMenu,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app_config import load_config
from camera_capture import (
    COMMON_RESOLUTIONS,
    CameraDevice,
    CameraListerThread,
    CameraThread,
    ResolutionProbeThread,
    list_cameras,
)
from dmft_bridge import DMFTBridge
from preview_window import PreviewShape, PreviewWindow


# ---------------------------------------------------------------------------
# 图标工厂（用 QPainter 绘制，避免依赖外部资源文件）
# ---------------------------------------------------------------------------
ICON_COLOR = "#e8ecf2"


def _star_icon_path(rect: QRectF, points: int = 5, inner_ratio: float = 0.42) -> QPainterPath:
    cx = rect.center().x()
    cy = rect.center().y()
    outer = min(rect.width(), rect.height()) / 2.0
    inner = outer * inner_ratio
    path = QPainterPath()
    angle_step = math.pi / points
    for i in range(2 * points):
        r = outer if i % 2 == 0 else inner
        a = -math.pi / 2 + i * angle_step
        x = cx + r * math.cos(a)
        y = cy + r * math.sin(a)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


def make_shape_icon(shape: PreviewShape, size: int = 22, color: str = ICON_COLOR) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), 1.8)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    margin = 3
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    if shape == PreviewShape.RECT:
        rect_icon = QRectF(
            margin,
            margin + size * 0.16,
            size - 2 * margin,
            size - 2 * (margin + size * 0.16),
        )
        p.drawRoundedRect(rect_icon, 2.5, 2.5)
    elif shape == PreviewShape.SQUARE:
        p.drawRoundedRect(rect, 2.5, 2.5)
    elif shape == PreviewShape.CIRCLE:
        p.drawEllipse(rect)
    elif shape == PreviewShape.STAR:
        p.drawPath(_star_icon_path(rect))
    p.end()
    return QIcon(px)


def make_camera_icon(size: int = 22, color: str = ICON_COLOR) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), 1.6)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    # 顶部取景器小凸起
    p.drawRoundedRect(QRectF(7, 4, 8, 3), 1, 1)
    # 主体
    p.drawRoundedRect(QRectF(2, 6, 18, 13), 2, 2)
    # 镜头
    p.drawEllipse(QPointF(11, 13), 4, 4)
    # 镜头内圆点
    p.setBrush(QColor(color))
    p.drawEllipse(QPointF(11, 13), 1.2, 1.2)
    p.end()
    return QIcon(px)


def make_refresh_icon(size: int = 18, color: str = ICON_COLOR) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), 1.6)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    margin = 3
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    # Qt 的角度单位是 1/16 度，逆时针为正；起点 3 点钟方向
    p.drawArc(rect, 50 * 16, 290 * 16)

    cx = rect.center().x()
    cy = rect.center().y()
    r = rect.width() / 2.0
    angle = math.radians(50)
    end_x = cx + r * math.cos(angle)
    end_y = cy - r * math.sin(angle)
    arrow = 3.4
    p.drawLine(QPointF(end_x, end_y), QPointF(end_x - arrow, end_y - arrow * 0.3))
    p.drawLine(QPointF(end_x, end_y), QPointF(end_x - arrow * 0.3, end_y + arrow))
    p.end()
    return QIcon(px)


# ---------------------------------------------------------------------------
# 浮动面板基类
# ---------------------------------------------------------------------------
class _FloatingPanel(QWidget):
    """悬浮面板基类：无边框 + 半透明 + 置顶。"""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._content = QFrame()
        self._content.setObjectName("ToolPanel")
        outer.addWidget(self._content)
        self._build()

    def _build(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 主工具栏（图标式，无文字）
# ---------------------------------------------------------------------------
class ControlBar(_FloatingPanel):
    # 最大预览尺寸取旧 SeekBar 约 1/5 位置对应的尺寸：
    # 240 + (1280 - 240) * 0.2 = 448。
    SLIDER_MIN = 32
    SLIDER_MAX = 448
    # 保持启动后滑块在原默认值 560 于旧范围 240..1280 中的相对位置。
    SLIDER_DEFAULT = 160
    BAR_WIDTH = SLIDER_DEFAULT
    ICON_SIZE = 14
    BUTTON_SIZE = 22

    shape_changed = pyqtSignal(object)        # PreviewShape
    size_changed = pyqtSignal(int)
    camera_button_clicked = pyqtSignal()      # 由 AppController 决定弹/收 popup

    def _build(self) -> None:
        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(7, 6, 7, 6)
        layout.setSpacing(5)

        # ---- 第一行：相机图标 + 分隔 + 形状图标 ----
        row = QHBoxLayout()
        row.setSpacing(4)

        self._camera_btn = self._make_icon_button(make_camera_icon(), checkable=True)
        self._camera_btn.clicked.connect(self.camera_button_clicked)
        row.addWidget(self._camera_btn)

        sep = QFrame()
        sep.setObjectName("VSep")
        sep.setFixedSize(1, 14)
        row.addWidget(sep)

        self._shape_buttons: dict = {}
        self._shape_group = QButtonGroup(self)
        self._shape_group.setExclusive(True)
        for shape in PreviewShape:
            btn = self._make_icon_button(make_shape_icon(shape), checkable=True)
            btn.clicked.connect(lambda _checked, s=shape: self.shape_changed.emit(s))
            row.addWidget(btn)
            self._shape_group.addButton(btn)
            self._shape_buttons[shape] = btn
        self._shape_buttons[PreviewShape.SQUARE].setChecked(True)

        row.addStretch(1)
        layout.addLayout(row)

        # ---- 第二行：尺寸 SeekBar ----
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(self.SLIDER_MIN, self.SLIDER_MAX)
        self._slider.setValue(self.SLIDER_DEFAULT)
        self._slider.valueChanged.connect(self.size_changed)
        layout.addWidget(self._slider)

        self.setFixedWidth(self.BAR_WIDTH)

    @staticmethod
    def _make_icon_button(icon: QIcon, checkable: bool = False) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName("IconButton")
        btn.setIcon(icon)
        btn.setIconSize(QSize(ControlBar.ICON_SIZE, ControlBar.ICON_SIZE))
        btn.setFixedSize(ControlBar.BUTTON_SIZE, ControlBar.BUTTON_SIZE)
        btn.setCheckable(checkable)
        btn.setCursor(Qt.PointingHandCursor)
        return btn

    # ---- 公开 API ----
    def value(self) -> int:
        return self._slider.value()

    def set_shape(self, shape: PreviewShape) -> None:
        if shape in self._shape_buttons:
            self._shape_buttons[shape].setChecked(True)

    def set_camera_button_active(self, active: bool) -> None:
        self._camera_btn.setChecked(active)

    def camera_button(self) -> QPushButton:
        return self._camera_btn


# ---------------------------------------------------------------------------
# 相机 / 分辨率 弹出面板
# ---------------------------------------------------------------------------
class CameraPopup(_FloatingPanel):
    POPUP_WIDTH = ControlBar.BAR_WIDTH
    POPUP_MIN_WIDTH = 120
    _MARGIN_X = 7
    _COMBO_TEXT_PADDING = 12
    # macOS：QComboBox 弹出层在无边框置顶窗口 + QSS 下易出现白块、与下层控件重叠；
    # 用 QToolButton + QMenu 与 Windows 上「点选一项即生效」的逻辑一致，且菜单为独立原生层。
    _USE_MENU_PICKERS = sys.platform == "darwin"

    camera_changed = pyqtSignal(int)
    resolution_changed = pyqtSignal(int, int)
    refresh_requested = pyqtSignal()

    def _build(self) -> None:
        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(self._MARGIN_X, 6, self._MARGIN_X, 6)
        layout.setSpacing(5)

        self._picker_updating = False

        cam_row = QHBoxLayout()
        cam_row.setSpacing(4)

        if self._USE_MENU_PICKERS:
            self._camera_combo = None  # type: ignore[assignment]
            self._camera_menu = QMenu(self)
            self._camera_menu.setObjectName("CameraPopupMenu")
            self._camera_menu.setToolTipsVisible(True)
            self._camera_menu_btn = QToolButton(self)
            self._camera_menu_btn.setObjectName("CompactMenuButton")
            self._camera_menu_btn.setMenu(self._camera_menu)
            self._camera_menu_btn.setPopupMode(QToolButton.InstantPopup)
            self._camera_menu_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            self._camera_menu_btn.setFixedHeight(24)
            self._camera_menu_btn.setCursor(Qt.PointingHandCursor)
            self._camera_menu.triggered.connect(self._on_camera_menu_action)
            cam_row.addWidget(self._camera_menu_btn, 1)
        else:
            self._camera_menu = None  # type: ignore[assignment]
            self._camera_menu_btn = None  # type: ignore[assignment]
            self._camera_combo = QComboBox()
            self._camera_combo.setObjectName("CompactCombo")
            self._camera_combo.setFixedHeight(24)
            self._camera_combo.currentIndexChanged.connect(self._on_camera)
            cam_row.addWidget(self._camera_combo, 1)

        refresh_btn = QPushButton()
        refresh_btn.setObjectName("IconButton")
        refresh_btn.setIcon(make_refresh_icon(size=ControlBar.ICON_SIZE))
        refresh_btn.setIconSize(QSize(ControlBar.ICON_SIZE, ControlBar.ICON_SIZE))
        refresh_btn.setFixedSize(ControlBar.BUTTON_SIZE, ControlBar.BUTTON_SIZE)
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh_requested)
        cam_row.addWidget(refresh_btn)
        layout.addLayout(cam_row)

        if self._USE_MENU_PICKERS:
            self._res_combo = None  # type: ignore[assignment]
            self._res_menu = QMenu(self)
            self._res_menu.setObjectName("CameraPopupMenu")
            self._res_menu.setToolTipsVisible(True)
            self._res_menu_btn = QToolButton(self)
            self._res_menu_btn.setObjectName("CompactMenuButton")
            self._res_menu_btn.setMenu(self._res_menu)
            self._res_menu_btn.setPopupMode(QToolButton.InstantPopup)
            self._res_menu_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            self._res_menu_btn.setFixedHeight(24)
            self._res_menu_btn.setCursor(Qt.PointingHandCursor)
            self._res_menu.triggered.connect(self._on_resolution_menu_action)
            layout.addWidget(self._res_menu_btn)
        else:
            self._res_menu = None  # type: ignore[assignment]
            self._res_menu_btn = None  # type: ignore[assignment]
            self._res_combo = QComboBox()
            self._res_combo.setObjectName("CompactCombo")
            self._res_combo.setFixedHeight(24)
            self._res_combo.currentIndexChanged.connect(self._on_resolution)
            layout.addWidget(self._res_combo)

        self.setFixedWidth(self.POPUP_WIDTH)

    # ---- public ----
    def set_cameras(self, cameras: List[CameraDevice]) -> None:
        if self._USE_MENU_PICKERS:
            self._picker_updating = True
            self._camera_menu.clear()
            if not cameras:
                self._camera_menu_btn.setText("未检测到相机")
                self._camera_menu_btn.setEnabled(False)
            else:
                self._camera_menu_btn.setEnabled(True)
                group = QActionGroup(self)
                group.setExclusive(True)
                first_action: Optional[QAction] = None
                for c in cameras:
                    act = QAction(c.name, self._camera_menu)
                    act.setData(c.index)
                    act.setCheckable(True)
                    self._camera_menu.addAction(act)
                    group.addAction(act)
                    if first_action is None:
                        first_action = act
                if first_action is not None:
                    first_action.setChecked(True)
                    self._camera_menu_btn.setText(first_action.text())
            self._picker_updating = False
        else:
            self._camera_combo.blockSignals(True)
            self._camera_combo.clear()
            if not cameras:
                self._camera_combo.addItem("未检测到相机", -1)
                self._camera_combo.setEnabled(False)
            else:
                self._camera_combo.setEnabled(True)
                for c in cameras:
                    self._camera_combo.addItem(c.name, c.index)
            self._camera_combo.blockSignals(False)
        self._update_content_width()

    def set_resolutions(
        self,
        resolutions: List[Tuple[int, int]],
        default: Optional[Tuple[int, int]] = None,
    ) -> None:
        if self._USE_MENU_PICKERS:
            self._picker_updating = True
            self._res_menu.clear()
            group = QActionGroup(self)
            group.setExclusive(True)
            chosen: Optional[QAction] = None
            for w, h in resolutions:
                label = f"{w} x {h}"
                act = QAction(label, self._res_menu)
                act.setData((w, h))
                act.setCheckable(True)
                self._res_menu.addAction(act)
                group.addAction(act)
                if default is not None and (w, h) == default:
                    chosen = act
            if resolutions:
                if chosen is None:
                    chosen = group.actions()[0]
                chosen.setChecked(True)
                self._res_menu_btn.setText(chosen.text())
            self._picker_updating = False
        else:
            self._res_combo.blockSignals(True)
            self._res_combo.clear()
            for w, h in resolutions:
                self._res_combo.addItem(f"{w} x {h}", (w, h))
            if default and default in resolutions:
                self._res_combo.setCurrentIndex(resolutions.index(default))
            elif resolutions:
                self._res_combo.setCurrentIndex(0)
            self._res_combo.blockSignals(False)
        self._update_content_width()

    # ---- private ----
    def _combo_text_width(self, combo: QComboBox) -> int:
        metrics = QFontMetrics(combo.font())
        texts = [combo.itemText(i) for i in range(combo.count())]
        widest = max((metrics.horizontalAdvance(text) for text in texts), default=0)
        return widest + self._COMBO_TEXT_PADDING

    def _menu_btn_min_width(self, btn: QToolButton, texts: List[str]) -> int:
        metrics = QFontMetrics(btn.font())
        widest = max((metrics.horizontalAdvance(t) for t in texts), default=0)
        # 与小号 CSS 三角箭头 + 右侧留白一致（见 style.qss #CompactMenuButton）
        return widest + self._COMBO_TEXT_PADDING + 14

    def _update_content_width(self) -> None:
        if self._USE_MENU_PICKERS:
            cam_texts = [a.text() for a in self._camera_menu.actions()]
            if not cam_texts:
                cam_texts = [self._camera_menu_btn.text() or " "]
            res_texts = [a.text() for a in self._res_menu.actions()]
            if not res_texts:
                res_texts = [self._res_menu_btn.text() or " "]
            camera_row_width = (
                self._menu_btn_min_width(self._camera_menu_btn, cam_texts)
                + 4
                + ControlBar.BUTTON_SIZE
            )
            resolution_width = self._menu_btn_min_width(self._res_menu_btn, res_texts)
        else:
            camera_row_width = (
                self._combo_text_width(self._camera_combo)
                + 4
                + ControlBar.BUTTON_SIZE
            )
            resolution_width = self._combo_text_width(self._res_combo)
        width = max(
            self.POPUP_MIN_WIDTH,
            camera_row_width,
            resolution_width,
        ) + self._MARGIN_X * 2
        self.setFixedWidth(width)
        if self._USE_MENU_PICKERS:
            inner_cam = max(1, camera_row_width - 4 - ControlBar.BUTTON_SIZE)
            inner_res = max(1, resolution_width)
            self._camera_menu_btn.setMinimumWidth(inner_cam)
            self._res_menu_btn.setMinimumWidth(inner_res)

    def _on_camera_menu_action(self, action: QAction) -> None:
        if self._picker_updating:
            return
        data = action.data()
        if data is not None and int(data) >= 0:
            self._camera_menu_btn.setText(action.text())
            self.camera_changed.emit(int(data))

    def _on_resolution_menu_action(self, action: QAction) -> None:
        if self._picker_updating:
            return
        data = action.data()
        if isinstance(data, tuple) and len(data) == 2:
            w, h = int(data[0]), int(data[1])
            self._res_menu_btn.setText(action.text())
            self.resolution_changed.emit(w, h)

    def _on_camera(self, idx: int) -> None:
        if idx < 0:
            return
        data = self._camera_combo.itemData(idx)
        if data is not None and data >= 0:
            self.camera_changed.emit(int(data))

    def _on_resolution(self, idx: int) -> None:
        data = self._res_combo.itemData(idx)
        if data:
            w, h = data
            self.resolution_changed.emit(int(w), int(h))


# ---------------------------------------------------------------------------
# 应用控制器
# ---------------------------------------------------------------------------
class AppController(QObject):
    """组装预览窗口、紧凑工具栏和相机弹出面板。"""

    GAP = 10
    MIN_RES_HEIGHT = 720
    DEFAULT_RESOLUTION = (1920, 1080)

    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        if sys.platform == "darwin":
            os.environ["AICAMERA_MAC_NATIVE"] = (
                "1" if self.config.macos.use_native_avfoundation else "0"
            )
            os.environ["AICAMERA_MAC_CENTER_STAGE"] = (
                "1" if self.config.macos.prefer_center_stage else "0"
            )
        self.bridge = DMFTBridge()
        self.preview = PreviewWindow()
        self.control_bar = ControlBar()
        self.camera_popup = CameraPopup()

        self.cameras: List[CameraDevice] = []
        self.current_camera_index: int = -1
        self.aspect_ratio: float = 16 / 9
        self.capture: Optional[CameraThread] = None
        self._bar_visible: bool = False
        self._target_resolution: Tuple[int, int] = self.config.camera.default_resolution
        self._stream_resolution: Tuple[int, int] = self._target_resolution
        self._shutting_down: bool = False
        # 已探测到的相机真实支持分辨率缓存，按设备索引记录，避免重复探测
        self._probed_resolutions: Dict[int, List[Tuple[int, int]]] = {}
        # 后台辅助线程的引用持有：枚举相机设备 / 探测分辨率能力。
        # 用对象成员而非局部变量持有，否则 QThread 会在 start() 后立即被
        # GC 回收导致信号永远收不到。
        self._lister_thread: Optional[CameraListerThread] = None
        self._probe_threads: Dict[int, ResolutionProbeThread] = {}
        self._darwin_av_gate: Optional[object] = None
        if sys.platform == "darwin":
            try:
                from macos_avfoundation_camera import (
                    create_and_register_darwin_av_discover_gate,
                    is_native_stack_available,
                )

                if is_native_stack_available():
                    self._darwin_av_gate = create_and_register_darwin_av_discover_gate(self)
            except Exception:
                pass

        self._wire_signals()

        # ====================== 关键启动加速点 ======================
        # 在 __init__ 末尾立即把"打开相机 + 取首帧"这件最慢的事踢到后台
        # 线程开始跑——此时 main() 还没调用 start()，QApplication 也没进
        # exec_() 进入事件循环，cv2.VideoCapture 可以与下面这些主线程上
        # 的工作**完全并行**：
        #
        #   1) main() 中 _install_signal_handlers / 给 app._controller 赋值；
        #   2) controller.start() 中的 set_shape / 居中 / preview.show()；
        #   3) Qt 事件循环 bootstrap、首次窗口 paint、QSS 应用等。
        #
        # CameraThread 的 frame_ready 信号通过 QueuedConnection 投递到
        # 主线程事件循环——即便首帧在 preview.show() 之前到达，update_frame
        # 也只是更新 _pixmap 并 schedule paint，窗口被 show() 后第一次
        # paint 用的就是这张最新帧。视觉上等同于"窗口出现即播放"。
        self._initial_startup()

    # ------------------------------------------------------------------ 信号连接
    def _wire_signals(self) -> None:
        self.preview.clicked.connect(self._toggle_bar)
        self.preview.drag_started.connect(self._hide_bar)
        self.preview.moved.connect(self._on_preview_moved)
        self.preview.closed.connect(self.shutdown)

        self.control_bar.shape_changed.connect(self._on_shape_changed)
        self.control_bar.size_changed.connect(self._on_size_changed)
        self.control_bar.camera_button_clicked.connect(self._toggle_camera_popup)

        self.camera_popup.camera_changed.connect(self._on_camera_changed)
        self.camera_popup.resolution_changed.connect(self._on_resolution_changed)
        self.camera_popup.refresh_requested.connect(self.refresh_cameras)

    # ------------------------------------------------------------------ 启动
    def start(self) -> None:
        # 注意：相机采集线程已经在 __init__ 末尾就启动了，这里只负责把
        # 预览窗口摆好位置并显示出来，让首帧到达时能立刻被绘制。
        self.preview.set_shape(PreviewShape.SQUARE)
        self._apply_preview_window_size(self.control_bar.value())
        self._center_on_screen()
        self.preview.show()

    def _initial_startup(self) -> None:
        """首启路径：立刻乐观打开 index 0 的相机，并行后台枚举设备列表。

        旧实现是"先 ``list_cameras()``（同步阻塞 200ms~1s）→ 再开相机"，
        对首帧延迟是纯净的串行损耗。新实现并行：

        1. 立即用 index 0 启动 ``CameraThread``（绝大多数机器的默认摄像头
           就是 0）；
        2. 同时让 ``CameraListerThread`` 在后台跑 pygrabber 枚举；
        3. 枚举结果到达后，如果第一个真实存在的设备索引不是 0，再校正
           （这种情况极少；常见单摄像头场景下完全不会发生重启）。
        """
        if self._shutting_down:
            return
        self.preview.show_message("正在加载相机...")
        self.current_camera_index = 0
        # macOS：AVFoundation 在后台 QThread 中常返回空设备列表，导致误走 OpenCV 且索引错位。
        # 在主线程先完成枚举（FaceTime 已排序为索引 0），再开采集；Windows 仍并行枚举。
        if sys.platform == "darwin":
            try:
                self._on_cameras_enumerated(list_cameras())
            except Exception:
                self._on_cameras_enumerated([])
            self._refresh_resolution_combo()
            if self.cameras:
                self._start_capture(*self._target_resolution)
        else:
            self._refresh_resolution_combo()
            self._start_capture(*self._target_resolution)
            self._begin_camera_enumeration()

    def _begin_camera_enumeration(self) -> None:
        if self._shutting_down:
            return
        # 已经有一个枚举线程在跑就不重复启动
        if self._lister_thread is not None and self._lister_thread.isRunning():
            return
        self._lister_thread = CameraListerThread(self)
        self._lister_thread.cameras_ready.connect(self._on_cameras_enumerated)
        self._lister_thread.start()

    def _on_cameras_enumerated(self, cameras: list) -> None:
        if self._shutting_down:
            return
        self.cameras = list(cameras)
        self.camera_popup.set_cameras(self.cameras)
        if not self.cameras:
            self.preview.show_message("未检测到相机")
            self._stop_capture()
            return
        # 清理已不存在的相机的缓存，防止索引错位
        existing = {c.index for c in self.cameras}
        self._probed_resolutions = {
            idx: res
            for idx, res in self._probed_resolutions.items()
            if idx in existing
        }
        # 校正乐观启动：如果当前相机索引不在真实设备列表中（极少见），
        # 切换到第一个真实存在的设备并重启采集。否则什么都不做，让正在
        # 跑的采集继续——这是常见路径，能让首帧延迟与设备枚举解耦。
        if self.current_camera_index not in existing:
            self.current_camera_index = self.cameras[0].index
            self._refresh_resolution_combo()
            self._start_capture(*self._target_resolution)

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        geo = self.preview.frameGeometry()
        geo.moveCenter(screen.center())
        new_top = max(screen.top() + 60, geo.top() - 60)
        geo.moveTop(new_top)
        self.preview.setGeometry(geo)

    # ------------------------------------------------------------------ 相机能力
    def refresh_cameras(self) -> None:
        """刷新按钮触发：重新枚举相机设备列表。

        和首启路径不同，这里**不**重启正在跑的采集——只更新下拉。
        如果当前相机已经被拔走，再切换到第一个可用设备。
        """
        if self._shutting_down:
            return
        cameras = list_cameras()
        self.cameras = cameras
        self.camera_popup.set_cameras(cameras)
        if not cameras:
            self.preview.show_message("未检测到相机")
            self._stop_capture()
            return
        existing = {c.index for c in cameras}
        self._probed_resolutions = {
            idx: res
            for idx, res in self._probed_resolutions.items()
            if idx in existing
        }
        if self.current_camera_index not in existing:
            self.current_camera_index = cameras[0].index
            self._refresh_resolution_combo()
            self._start_capture(*self._target_resolution)

    def _refresh_resolution_combo(self) -> None:
        """根据真实探测结果（如有）刷新分辨率下拉，仅展示 ≥720p 的项。

        首次启动尚未探测时，临时使用 :data:`COMMON_RESOLUTIONS` 占位，
        让用户能立刻看到下拉；探测完成后再用真实能力替换。
        """
        cached = self._probed_resolutions.get(self.current_camera_index)
        source: List[Tuple[int, int]]
        if cached is not None:
            source = list(cached)
        else:
            source = list(COMMON_RESOLUTIONS)

        resolutions = [(w, h) for w, h in source if h >= self.MIN_RES_HEIGHT]

        # 始终把当前实际生效（或用户选择的）分辨率纳入选项，
        # 否则下拉无法显示当前状态，会让用户以为切换失败。
        current = (
            self._stream_resolution
            if self._stream_resolution and self._stream_resolution[1] > 0
            else self._target_resolution
        )
        if current not in resolutions:
            resolutions = sorted(set(resolutions) | {current})
        if not resolutions:
            resolutions = [current]

        self.camera_popup.set_resolutions(resolutions, current)

    # ------------------------------------------------------------------ 工具栏可见性
    def _toggle_bar(self) -> None:
        if self._bar_visible:
            self._hide_bar()
        else:
            self._show_bar()

    def _show_bar(self) -> None:
        self._position_bar()
        self.control_bar.show()
        # 工具栏可见 = 进入"操作模式"，同步亮出右上角红色 ✕，
        # 让用户随时能直接关闭，无需依赖隐藏的右键菜单。
        self.preview.set_close_button_visible(True)
        self.preview.raise_()
        self.preview.activateWindow()
        self._bar_visible = True

    def _hide_bar(self) -> None:
        if not self._bar_visible:
            return
        self.control_bar.hide()
        self._hide_camera_popup()
        self.preview.set_close_button_visible(False)
        self._bar_visible = False

    def _position_bar(self) -> None:
        geo = self.preview.frameGeometry()
        bar_w = self.control_bar.width() or ControlBar.BAR_WIDTH
        bar_h = self.control_bar.sizeHint().height()
        bar_x = geo.x() + (geo.width() - bar_w) // 2
        bar_y = geo.y() + geo.height() + self.GAP
        self.control_bar.setGeometry(bar_x, bar_y, bar_w, bar_h)

        if self.camera_popup.isVisible():
            self._position_camera_popup()

    def _on_preview_moved(self) -> None:
        if self._bar_visible:
            self._position_bar()

    # ------------------------------------------------------------------ 相机弹出面板
    def _toggle_camera_popup(self) -> None:
        if self.camera_popup.isVisible():
            self._hide_camera_popup()
        else:
            self._show_camera_popup()

    def _show_camera_popup(self) -> None:
        self._position_camera_popup()
        self.camera_popup.show()
        self.camera_popup.raise_()
        self.control_bar.set_camera_button_active(True)
        # 兜底触发一次能力探测：正常情况下首帧到达时已经在后台启动过，
        # 但如果用户在采集还没出帧时就打开了弹窗，这里再补一次。
        # 探测线程只跑 pygrabber 的 COM 查询，不会触碰 cv2.VideoCapture，
        # 所以**完全不会**让正在跑的预览出现卡顿（修复了上一版"打开
        # 弹窗预览卡住十几秒"的问题）。
        self._begin_resolution_probe(self.current_camera_index)

    def _hide_camera_popup(self) -> None:
        if self.camera_popup.isVisible():
            self.camera_popup.hide()
        self.control_bar.set_camera_button_active(False)

    def _position_camera_popup(self) -> None:
        bar_geo = self.control_bar.frameGeometry()
        cam_btn = self.control_bar.camera_button()
        anchor_global = cam_btn.mapToGlobal(QPoint(0, cam_btn.height()))
        popup_w = self.camera_popup.width() or CameraPopup.POPUP_WIDTH
        popup_h = self.camera_popup.sizeHint().height()
        # 默认放在工具栏下方，左侧与相机按钮对齐
        x = anchor_global.x()
        y = bar_geo.y() + bar_geo.height() + 6
        # 屏幕越界保护
        screen = QApplication.primaryScreen().availableGeometry()
        if x + popup_w > screen.right():
            x = screen.right() - popup_w - 4
        if y + popup_h > screen.bottom():
            y = bar_geo.y() - popup_h - 6
        self.camera_popup.setGeometry(x, y, popup_w, popup_h)

    # ------------------------------------------------------------------ 设置回调
    def _on_camera_changed(self, index: int) -> None:
        if index == self.current_camera_index:
            return
        self.current_camera_index = index
        self._refresh_resolution_combo()
        self._start_capture(self._target_resolution[0], self._target_resolution[1])

    def _on_resolution_changed(self, w: int, h: int) -> None:
        self._target_resolution = (w, h)
        if self.current_camera_index >= 0:
            self._start_capture(w, h)

    def _on_shape_changed(self, shape: PreviewShape) -> None:
        self.preview.set_shape(shape)
        self._apply_preview_window_size(self.control_bar.value())

    def _on_size_changed(self, value: int) -> None:
        # SeekBar 只缩放预览窗口，不触碰 CameraThread 或相机采集分辨率。
        self._apply_preview_window_size(value)

    def _apply_preview_window_size(self, base: int) -> None:
        if self.preview.shape() == PreviewShape.RECT:
            w = base
            h = max(1, int(round(base / max(self.aspect_ratio, 0.1))))
        else:
            w = h = base
        self.preview.set_preview_window_size(w, h)
        if self._bar_visible:
            self._position_bar()

    # ------------------------------------------------------------------ 采集
    def _start_capture(self, w: int, h: int) -> None:
        if self._shutting_down:
            return
        self._stop_capture()
        self.aspect_ratio = w / h if h else 16 / 9
        self._target_resolution = (w, h)
        self._stream_resolution = self._target_resolution
        self._apply_preview_window_size(self.control_bar.value())

        # CameraThread 现在只负责"打开 + 出首帧 + 持续读帧"，分辨率
        # 能力探测交给独立的 ResolutionProbeThread（pygrabber COM 查询，
        # 不开流），首帧到达后由 _on_capture_started 自动触发。
        self.capture = CameraThread(
            self.current_camera_index,
            w,
            h,
            preferred_fourccs=self.config.camera.preferred_fourccs,
            fps=self.config.camera.fps,
        )
        # 关键：必须显式使用 ``QueuedConnection`` 把回调投递到主 GUI 线程。
        # 否则 macOS 路径上 AVFoundation 的回调发生在 libdispatch 队列里，
        # PyQt 的 ``AutoConnection`` 会因 ``CameraThread`` 对象的 thread
        # affinity 是主线程而误判为同线程，结果以 ``DirectConnection`` 同步
        # 调用——``update_frame`` 中的 ``QPixmap`` / ``self.update()`` 就会
        # 在 GUI 线程之外执行，画面不会更新（即"日志显示帧持续到达，但
        # 预览窗口始终不出画"的根因）。Windows 的 ``CameraThread.run`` 直接
        # 在 QThread 自身线程发信号，行为不变。
        self.capture.frame_ready.connect(self._on_frame, Qt.QueuedConnection)
        self.capture.error.connect(self._on_capture_error, Qt.QueuedConnection)
        self.capture.started_ok.connect(self._on_capture_started, Qt.QueuedConnection)
        self.preview.show_message("正在打开相机...")
        self.capture.start()

    def _stop_capture(self) -> None:
        if self.capture is not None:
            try:
                self.capture.frame_ready.disconnect()
                self.capture.error.disconnect()
                self.capture.started_ok.disconnect()
            except Exception:
                pass
            self.capture.stop()
            self.capture = None

    def _begin_resolution_probe(self, camera_index: int) -> None:
        """启动一次后台分辨率探测；同一相机已缓存或已在探测则跳过。"""
        if self._shutting_down or camera_index < 0:
            return
        if camera_index in self._probed_resolutions:
            return
        existing = self._probe_threads.get(camera_index)
        if existing is not None and existing.isRunning():
            return
        thread = ResolutionProbeThread(camera_index, self)
        thread.resolutions_ready.connect(self._on_resolutions_probed)
        # 线程结束后把引用清掉，避免长期堆积
        thread.finished.connect(
            lambda idx=camera_index: self._probe_threads.pop(idx, None)
        )
        self._probe_threads[camera_index] = thread
        thread.start()

    def _on_capture_started(self, actual_w: int, actual_h: int) -> None:
        if self._shutting_down:
            return
        if actual_w <= 0 or actual_h <= 0:
            return
        self._stream_resolution = (actual_w, actual_h)
        actual_ratio = actual_w / actual_h
        if abs(actual_ratio - self.aspect_ratio) > 0.01:
            self.aspect_ratio = actual_ratio
            self._apply_preview_window_size(self.control_bar.value())
        # 实际生效分辨率可能与请求不一致（相机就近落点），让下拉如实反映状态
        if (actual_w, actual_h) != self._target_resolution:
            self._target_resolution = (actual_w, actual_h)
        self._refresh_resolution_combo()
        # 首帧落地后立刻把分辨率能力探测放到后台跑（pygrabber 纯 COM 查询，
        # 不会触碰 cv2.VideoCapture，不会让预览掉帧）。等用户打开相机弹窗
        # 时下拉里就已经是真实能力清单了。
        self._begin_resolution_probe(self.current_camera_index)

    def _on_resolutions_probed(self, camera_index: int, resolutions: list) -> None:
        if self._shutting_down or not resolutions:
            return
        normalized = [(int(w), int(h)) for w, h in resolutions]
        self._probed_resolutions[int(camera_index)] = normalized
        # 仅当探测结果对应的就是当前相机时才刷新下拉显示，避免在用户
        # 已经切换到别的相机后被旧结果"覆盖"。
        if int(camera_index) == self.current_camera_index:
            self._refresh_resolution_combo()

    def _on_capture_error(self, msg: str) -> None:
        self.preview.show_message(f"相机错误: {msg}")
        self._stop_capture()

    def _on_frame(self, frame: np.ndarray) -> None:
        if self._shutting_down:
            return
        # AI 设置面板已隐藏，仅保留 mirror 等基础处理
        processed = self.bridge.process_frame(frame)
        self.preview.update_frame(processed)
        if os.environ.get("AICAMERA_MAC_DEBUG", "0").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            self._dbg_frame_count = getattr(self, "_dbg_frame_count", 0) + 1
            if self._dbg_frame_count == 1 or self._dbg_frame_count % 60 == 0:
                vis = self.preview.isVisible()
                geo = self.preview.geometry()
                import sys as _sys
                print(
                    f"[AICamera/ui] _on_frame #{self._dbg_frame_count} "
                    f"shape={processed.shape} preview.visible={vis} "
                    f"geo=({geo.x()},{geo.y()},{geo.width()}x{geo.height()})",
                    file=_sys.stderr, flush=True,
                )

    # ------------------------------------------------------------------ 关闭
    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._stop_capture()
        # 后台辅助线程跑的是 pygrabber 的 COM 调用，正常情况下都是百毫秒级
        # 完成。这里给一个合理 wait 兜底——避免 QApplication.quit 后 QThread
        # 被悬空析构警告；超时后让进程整体退出顺势带走它们。
        if self._lister_thread is not None:
            self._lister_thread.wait(500)
            self._lister_thread = None
        for thread in list(self._probe_threads.values()):
            thread.wait(500)
        self._probe_threads.clear()
        self.control_bar.close()
        self.camera_popup.close()
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)
