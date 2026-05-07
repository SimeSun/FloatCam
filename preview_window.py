"""
预览窗口（应用的主窗口）
- 无边框、置顶、半透明
- 通过透明背景 + 抗锯齿路径绘制实现矩形 / 圆形 / 星形外框
- 区分"短按一下" (clicked) 与"拖拽" (drag_started)
  * clicked 用于切换工具菜单的显示状态
  * drag_started 用于在拖动时立即隐藏菜单
- 提供 show_message() 用于在尚未出帧时显示加载/错误文本
"""
from __future__ import annotations

import math
import sys
from enum import Enum
from typing import Optional

import cv2
import numpy as np
from PyQt5.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt5.QtWidgets import QApplication, QWidget


class PreviewShape(Enum):
    RECT = "矩形"
    SQUARE = "正方形"
    CIRCLE = "圆形"
    STAR = "星形"


def _star_path(rect: QRectF, points: int = 5, inner_ratio: float = 0.45) -> QPainterPath:
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


class PreviewWindow(QWidget):
    """无边框、可拖拽、可变形的相机预览窗口（应用主窗口）。"""

    clicked = pyqtSignal()        # 短按一下（非拖动）
    drag_started = pyqtSignal()   # 开始拖动
    moved = pyqtSignal()          # 位置 / 尺寸变化（用于联动菜单）
    closed = pyqtSignal()         # 窗口关闭

    # ---------------- 关闭按钮（右上角红色 ✕）外观参数 ----------------
    CLOSE_BTN_SIZE = 7
    CLOSE_BTN_MARGIN = 10
    CLOSE_BTN_HOVER_SCALE = 1.25

    # ---------------- 加载动画参数 ----------------
    # 旋转一圈所需毫秒数；Tick 间隔越小、动画越平滑但 CPU 开销略增。
    _LOADING_TICK_MS = 40
    _LOADING_PERIOD_MS = 1100

    def __init__(self, parent=None) -> None:
        super().__init__(
            parent,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setMinimumSize(32, 18)

        self._shape: PreviewShape = PreviewShape.SQUARE
        self._pixmap: QPixmap = QPixmap()
        self._frame_size: tuple[int, int] = (0, 0)
        self._message: str = "正在初始化"

        self._press_global: QPoint | None = None
        self._window_origin: QPoint | None = None
        self._dragging: bool = False

        # 关闭按钮可见性 / 悬停状态。仅在工具栏显示时由控制器开启，
        # 避免常态下污染纯净预览画面。
        self._close_button_visible: bool = False
        self._close_button_hovered: bool = False
        self._pressed_on_close: bool = False

        # 加载动画状态：phase ∈ [0, 1) 用于驱动旋转角度与省略号脉动。
        self._loading_active: bool = True
        self._loading_phase: float = 0.0
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(self._LOADING_TICK_MS)
        self._loading_timer.timeout.connect(self._tick_loading)
        self._loading_timer.start()

        self.resize(560, 420)

    # ---------------- 公开 API ----------------

    def shape(self) -> PreviewShape:
        return self._shape

    def set_shape(self, shape: PreviewShape) -> None:
        if shape != self._shape:
            self._shape = shape
            self.update()

    def frame_resolution(self) -> tuple[int, int]:
        """返回当前相机帧的原始分辨率，不受窗口缩放影响。"""
        return self._frame_size

    def set_preview_window_size(self, width: int, height: int) -> None:
        """以当前中心为锚点改变预览窗口尺寸，不改动当前视频帧分辨率。"""
        safe_width = max(self.minimumWidth(), int(width))
        safe_height = max(self.minimumHeight(), int(height))
        center = self.geometry().center()
        new_geo = self.geometry()
        new_geo.setSize(QSize(safe_width, safe_height))
        new_geo.moveCenter(center)
        self.setGeometry(new_geo)

    def update_frame(self, frame_bgr: np.ndarray) -> None:
        if frame_bgr is None or frame_bgr.size == 0:
            return
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        self._frame_size = (w, h)
        rgb = np.ascontiguousarray(rgb)
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self._pixmap = QPixmap.fromImage(qimg)
        self._message = ""
        self._set_loading_active(False)
        self.update()

    def show_message(self, text: str, loading: Optional[bool] = None) -> None:
        """在预览区域中央显示一段文本（用于加载 / 错误提示）。

        :param text: 居中显示的文本。结尾的省略号会在加载状态下被自动替换为
            动态的脉动点，因此调用方既可以传 "正在加载相机..." 也可以传
            "正在加载相机"——两者展示效果一致。
        :param loading: 是否显示旋转加载指示器。``None`` 时按文本启发式
            判断（包含"正在"或以省略号结尾视为加载中），便于历史调用点
            无需修改。
        """
        # 规范化文本：去掉结尾的省略号，由动画统一接管脉动效果。
        normalized = text.rstrip(" .。…") if text else text
        self._message = normalized
        self._pixmap = QPixmap()
        self._frame_size = (0, 0)
        if loading is None:
            loading = bool(text) and ("正在" in text or text.endswith("..."))
        self._set_loading_active(loading)
        self.update()

    def _set_loading_active(self, active: bool) -> None:
        if active == self._loading_active:
            if active and not self._loading_timer.isActive():
                self._loading_timer.start()
            return
        self._loading_active = active
        if active:
            self._loading_phase = 0.0
            if not self._loading_timer.isActive():
                self._loading_timer.start()
        else:
            if self._loading_timer.isActive():
                self._loading_timer.stop()

    def _tick_loading(self) -> None:
        # phase 在 [0, 1) 区间循环；除以周期得到平滑的小数推进量。
        step = self._LOADING_TICK_MS / self._LOADING_PERIOD_MS
        self._loading_phase = (self._loading_phase + step) % 1.0
        # 仅在仍处于加载状态、且当前没有相机帧时才需要重绘。
        if self._loading_active and self._pixmap.isNull():
            self.update()

    def set_close_button_visible(self, visible: bool) -> None:
        """显示 / 隐藏右上角的红色关闭按钮。隐藏时同步清理悬停态与光标。"""
        if visible == self._close_button_visible:
            return
        self._close_button_visible = visible
        if not visible:
            self._close_button_hovered = False
            self._pressed_on_close = False
            self.unsetCursor()
        self.update()

    # ---------------- 事件 ----------------

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.moved.emit()

    def moveEvent(self, event):  # noqa: N802
        super().moveEvent(event)
        self.moved.emit()

    def closeEvent(self, event):  # noqa: N802
        self.closed.emit()
        super().closeEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        # 鼠标离开窗口时，取消关闭按钮的悬停高亮，避免边框红框残留。
        if self._close_button_hovered:
            self._close_button_hovered = False
            self.unsetCursor()
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.Antialiasing | QPainter.SmoothPixmapTransform
        )

        rect = self.rect()
        paint_rect = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
        path = self._shape_path(paint_rect)

        # 关键：按设备像素比（DPR）分配离屏画布。
        # 否则在 HiDPI（如 Windows 125%/150% 缩放）下，离屏 surface 以逻辑像素分配，
        # 1) 相机帧会先被降采样到逻辑像素分辨率；2) 再被 Qt 放大到物理像素绘制，
        # 双线性插值的二次缩放会显著降低预览画质（拖动 SeekBar 时尤其明显）。
        dpr = self.devicePixelRatioF()
        phys_w = max(1, int(round(rect.width() * dpr)))
        phys_h = max(1, int(round(rect.height() * dpr)))

        surface = QPixmap(phys_w, phys_h)
        surface.setDevicePixelRatio(dpr)
        surface.fill(Qt.transparent)
        surface_painter = QPainter(surface)
        surface_painter.setRenderHints(
            QPainter.Antialiasing | QPainter.SmoothPixmapTransform
        )

        surface_painter.setPen(Qt.NoPen)
        surface_painter.setBrush(QBrush(QColor(0, 0, 0, 220)))
        surface_painter.drawPath(path)

        if not self._pixmap.isNull():
            source_rect = self._source_rect_for_target(rect.width(), rect.height())
            # 这里只改变绘制目标窗口大小；self._pixmap 仍保持相机输出的原始分辨率。
            surface_painter.drawPixmap(QRectF(rect), self._pixmap, source_rect)

        if self._message:
            surface_painter.setPen(QColor(220, 230, 245, 230))
            if sys.platform == "darwin":
                surface_painter.setFont(QFont(".AppleSystemUIFont", 13))
            else:
                surface_painter.setFont(QFont("Microsoft YaHei UI", 13))
            if self._loading_active:
                self._draw_loading_indicator(surface_painter, rect)
            else:
                surface_painter.drawText(rect, Qt.AlignCenter, self._message)

        alpha_mask = QPixmap(phys_w, phys_h)
        alpha_mask.setDevicePixelRatio(dpr)
        alpha_mask.fill(Qt.transparent)
        mask_painter = QPainter(alpha_mask)
        mask_painter.setRenderHint(QPainter.Antialiasing)
        mask_painter.setPen(Qt.NoPen)
        mask_painter.setBrush(QBrush(QColor(0, 0, 0, 255)))
        mask_painter.drawPath(path)
        mask_painter.end()

        surface_painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        surface_painter.drawPixmap(0, 0, alpha_mask)
        surface_painter.end()

        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.drawPixmap(0, 0, surface)

        # 悬停在关闭按钮上时，沿当前形状路径描一圈红色高亮边框，
        # 给用户「点击此处会关闭这块预览」的明确视觉反馈。
        if self._close_button_visible and self._close_button_hovered:
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setRenderHint(QPainter.Antialiasing, True)
            highlight = QPen(QColor(232, 60, 60, 235), 2.5)
            highlight.setJoinStyle(Qt.RoundJoin)
            painter.setPen(highlight)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

        if self._close_button_visible:
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            self._draw_close_button(painter)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        # 优先级最高：在关闭按钮上按下，吞掉事件、不参与拖动 / 工具栏切换。
        if self._point_on_close_button(event.pos()):
            self._pressed_on_close = True
            self._press_global = None
            self._window_origin = None
            self._dragging = False
            event.accept()
            return
        self._press_global = event.globalPos()
        self._window_origin = self.frameGeometry().topLeft()
        self._dragging = False
        self._pressed_on_close = False
        event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        # 不论是否按住按键，都更新关闭按钮的悬停态（mouseTracking 已开）。
        self._update_close_button_hover(event.pos())

        # 在关闭按钮上按下时不允许进入拖动，避免误触移动窗口。
        if self._pressed_on_close:
            return
        if not (event.buttons() & Qt.LeftButton):
            return
        if self._press_global is None:
            return
        delta = event.globalPos() - self._press_global
        threshold = QApplication.startDragDistance()
        if not self._dragging and delta.manhattanLength() >= threshold:
            self._dragging = True
            self.drag_started.emit()
        if self._dragging and self._window_origin is not None:
            self.move(self._window_origin + delta)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        if self._pressed_on_close:
            self._pressed_on_close = False
            # 必须在按下与抬起时都落在按钮区域内，才视为有效点击，
            # 这是平台一致的按钮交互约定（按下后拖出再松开 = 取消）。
            if self._point_on_close_button(event.pos()):
                event.accept()
                self.close()
                return
            event.accept()
            return
        if not self._dragging and self._press_global is not None:
            self.clicked.emit()
        self._press_global = None
        self._window_origin = None
        self._dragging = False
        event.accept()

    # ---------------- 内部 ----------------

    def _draw_loading_indicator(self, painter: QPainter, rect) -> None:
        """绘制旋转弧形加载圈 + 文字 + 脉动省略号。

        - 旋转圈：底环 + 一段亮色弧线随时间旋转，模仿 Material 风格。
        - 省略号：3 个点循环淡入淡出，比单纯静态 "..." 更有节奏感。
        - 整体相对预览区域居中，在小尺寸窗口下会自动收缩并退化为
          单行文字，避免内容溢出。
        """
        avail = min(rect.width(), rect.height())
        # 小到一定程度时省略图形圈，仅显示文字以避免拥挤。
        show_spinner = avail >= 120

        # 估算文本行高，给 spinner 与文字之间留出协调间距。
        font_metrics = painter.fontMetrics()
        text_h = font_metrics.height()

        spinner_radius = 0.0
        spacing = 0.0
        if show_spinner:
            spinner_radius = max(10.0, min(18.0, avail * 0.07))
            spacing = max(8.0, spinner_radius * 0.7)

        spinner_diameter = spinner_radius * 2 if show_spinner else 0.0
        block_h = spinner_diameter + (spacing if show_spinner else 0.0) + text_h
        block_top = rect.center().y() - block_h / 2.0
        cx = rect.center().x()

        if show_spinner:
            spin_cy = block_top + spinner_radius
            self._draw_spinner(painter, cx, spin_cy, spinner_radius)
            text_top = spin_cy + spinner_radius + spacing
        else:
            text_top = block_top

        # 三点循环：每点的不透明度按相位偏移生成 0..1 的正弦脉动。
        # 使用平滑函数比硬切 "." -> ".." -> "..." 视觉更柔和。
        dots_phase = self._loading_phase
        dot_alphas = []
        for i in range(3):
            local = (dots_phase - i * 0.18) % 1.0
            # 在 0..0.5 区间淡入淡出，其余时段保持基线亮度。
            wave = math.sin(local * math.pi * 2.0)
            alpha = 0.35 + 0.65 * max(0.0, wave)
            dot_alphas.append(alpha)

        text = self._message
        text_width = font_metrics.horizontalAdvance(text)
        dot_unit = font_metrics.horizontalAdvance(".")
        gap = max(2, dot_unit // 3)
        total_dots_width = dot_unit * 3 + gap * 2
        # 文本与点之间留半个点宽的间距，更呼吸。
        gap_text_dots = dot_unit
        total_w = text_width + gap_text_dots + total_dots_width

        baseline_y = text_top + font_metrics.ascent()
        text_x = cx - total_w / 2.0
        painter.setPen(QColor(220, 230, 245, 235))
        painter.drawText(QPointF(text_x, baseline_y), text)

        dot_x = text_x + text_width + gap_text_dots
        for alpha in dot_alphas:
            color = QColor(220, 230, 245)
            color.setAlphaF(alpha)
            painter.setPen(color)
            painter.drawText(QPointF(dot_x, baseline_y), ".")
            dot_x += dot_unit + gap

    def _draw_spinner(self, painter: QPainter, cx: float, cy: float, radius: float) -> None:
        """渲染一圈细弧 + 一段旋转的亮弧。"""
        painter.save()
        painter.setBrush(Qt.NoBrush)

        ring_rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        stroke = max(1.6, radius * 0.18)

        # 底环：均匀低亮度，给旋转弧提供视觉锚点。
        bg_pen = QPen(QColor(220, 230, 245, 55), stroke)
        bg_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(bg_pen)
        painter.drawEllipse(ring_rect)

        # 旋转弧：约占 110° 圆周，按相位顺时针旋转。
        # Qt 角度单位 = 1/16 度，逆时针为正；这里通过负 span 实现顺时针。
        sweep_deg = 110.0
        start_deg = 90.0 - self._loading_phase * 360.0  # 从 12 点方向起绕
        head_pen = QPen(QColor(140, 195, 255, 235), stroke)
        head_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(head_pen)
        painter.drawArc(
            ring_rect,
            int(round(start_deg * 16)),
            int(round(-sweep_deg * 16)),
        )

        painter.restore()

    def _close_button_rect(self) -> QRectF:
        """根据当前形状计算关闭按钮的位置，使其始终落在可见区域内。

        - 矩形 / 正方形：右上角内缩。
        - 圆形：沿圆心 45° 上方向放置在内切位置，避免落到圆外的透明区域。
        - 星形：放置在右上角附近，向中心略缩，避免落在星形凹角的外面。
        """
        size = self.CLOSE_BTN_SIZE
        if self._close_button_hovered:
            size = max(int(round(size * self.CLOSE_BTN_HOVER_SCALE)), size + 1)
        margin = self.CLOSE_BTN_MARGIN
        rect = self.rect()

        if self._shape == PreviewShape.CIRCLE:
            cx = rect.center().x()
            cy = rect.center().y()
            r = min(rect.width(), rect.height()) / 2.0
            offset = max(r - size / 2.0 - margin, 0.0)
            diag = offset / math.sqrt(2.0)
            return QRectF(
                cx + diag - size / 2.0,
                cy - diag - size / 2.0,
                size,
                size,
            )
        if self._shape == PreviewShape.STAR:
            cx = rect.center().x()
            cy = rect.center().y()
            outer = min(rect.width(), rect.height()) / 2.0
            bx = cx + outer * 0.42
            by = cy - outer * 0.42
            return QRectF(bx - size / 2.0, by - size / 2.0, size, size)

        return QRectF(
            rect.right() - size - margin,
            rect.top() + margin,
            float(size),
            float(size),
        )

    def _point_on_close_button(self, pos: QPoint) -> bool:
        if not self._close_button_visible:
            return False
        return self._close_button_rect().contains(QPointF(pos))

    def _update_close_button_hover(self, pos: QPoint) -> None:
        if not self._close_button_visible:
            if self._close_button_hovered:
                self._close_button_hovered = False
                self.unsetCursor()
                self.update()
            return
        over = self._close_button_rect().contains(QPointF(pos))
        if over == self._close_button_hovered:
            return
        self._close_button_hovered = over
        if over:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()
        self.update()

    def _draw_close_button(self, painter: QPainter) -> None:
        btn_rect = self._close_button_rect()
        hover = self._close_button_hovered

        bg_color = QColor(235, 70, 70, 255) if hover else QColor(210, 55, 55, 215)
        edge_color = QColor(255, 255, 255, 235) if hover else QColor(255, 255, 255, 175)

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(edge_color, 0.6))
        painter.setBrush(QBrush(bg_color))
        painter.drawEllipse(btn_rect)

        pad = btn_rect.width() * 0.28
        cross_pen = QPen(QColor(255, 255, 255, 245), 0.9 if hover else 0.7)
        cross_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(cross_pen)
        painter.drawLine(
            QPointF(btn_rect.left() + pad, btn_rect.top() + pad),
            QPointF(btn_rect.right() - pad, btn_rect.bottom() - pad),
        )
        painter.drawLine(
            QPointF(btn_rect.right() - pad, btn_rect.top() + pad),
            QPointF(btn_rect.left() + pad, btn_rect.bottom() - pad),
        )

    def _shape_path(self, rect: QRectF) -> QPainterPath:
        path = QPainterPath()
        if self._shape == PreviewShape.RECT:
            path.addRoundedRect(rect, 14, 14)
        elif self._shape == PreviewShape.SQUARE:
            path.addRoundedRect(rect, 10, 10)
        elif self._shape == PreviewShape.CIRCLE:
            size = min(rect.width(), rect.height())
            square = QRectF(
                rect.x() + (rect.width() - size) / 2.0,
                rect.y() + (rect.height() - size) / 2.0,
                size,
                size,
            )
            path.addEllipse(square)
        elif self._shape == PreviewShape.STAR:
            path = _star_path(rect)
        return path

    def _source_rect_for_target(self, target_width: int, target_height: int) -> QRectF:
        """从原始相机帧中取当前目标比例对应的居中裁剪区域。"""
        source_width = self._pixmap.width()
        source_height = self._pixmap.height()
        if (
            source_width <= 0
            or source_height <= 0
            or target_width <= 0
            or target_height <= 0
        ):
            return QRectF()

        source_ratio = source_width / source_height
        target_ratio = target_width / target_height
        if source_ratio > target_ratio:
            crop_width = source_height * target_ratio
            x = (source_width - crop_width) / 2.0
            return QRectF(x, 0.0, crop_width, float(source_height))

        crop_height = source_width / target_ratio
        y = (source_height - crop_height) / 2.0
        return QRectF(0.0, y, float(source_width), crop_height)
