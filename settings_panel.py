"""
AI 设置面板
- 根据 ``DMFTBridge.features()`` 自动生成 UI
- 按 category 分组展示
- 切换开关 / 调整滑条会通过 bridge 转发到 (未来的) C++ DMFT 层
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Dict

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from dmft_bridge import AIFeature, DMFTBridge


class FeatureCard(QFrame):
    """单个 AI 能力卡片：开关 + 若干参数滑条。"""

    toggled = pyqtSignal(str, bool)
    param_changed = pyqtSignal(str, str, int)

    def __init__(self, feature: AIFeature, enabled: bool, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("FeatureCard")
        self._feature = feature

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        # 标题行：开关 + 名称
        head = QHBoxLayout()
        head.setSpacing(10)
        self._switch = QCheckBox(feature.label)
        self._switch.setObjectName("FeatureSwitch")
        self._switch.setChecked(enabled)
        self._switch.toggled.connect(self._on_toggled)
        head.addWidget(self._switch)
        head.addStretch(1)
        layout.addLayout(head)

        if feature.description:
            desc = QLabel(feature.description)
            desc.setObjectName("FeatureDesc")
            desc.setWordWrap(True)
            layout.addWidget(desc)

        # 参数滑条
        if feature.params:
            grid = QGridLayout()
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(4)
            for row, p in enumerate(feature.params):
                label = QLabel(p.label)
                label.setObjectName("ParamLabel")
                slider = QSlider(Qt.Horizontal)
                slider.setRange(p.minimum, p.maximum)
                slider.setValue(p.default)
                slider.setEnabled(enabled)
                value_label = QLabel(str(p.default))
                value_label.setObjectName("ParamValue")
                value_label.setMinimumWidth(28)
                value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

                slider.valueChanged.connect(
                    lambda v, name=p.key, lbl=value_label: self._on_param(name, v, lbl)
                )

                grid.addWidget(label, row, 0)
                grid.addWidget(slider, row, 1)
                grid.addWidget(value_label, row, 2)
            layout.addLayout(grid)
            self._sliders = [grid.itemAtPosition(r, 1).widget() for r in range(len(feature.params))]
        else:
            self._sliders = []

    def _on_toggled(self, checked: bool) -> None:
        for s in self._sliders:
            s.setEnabled(checked)
        self.toggled.emit(self._feature.key, checked)

    def _on_param(self, name: str, value: int, label: QLabel) -> None:
        label.setText(str(value))
        self.param_changed.emit(self._feature.key, name, value)


class SettingsPanel(QScrollArea):
    """AI 设置主面板：按类别分组的卡片列表。"""

    feature_toggled = pyqtSignal(str, bool)
    feature_param_changed = pyqtSignal(str, str, int)

    def __init__(self, bridge: DMFTBridge, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setObjectName("SettingsPanel")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        container.setObjectName("SettingsContainer")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(14)

        title = QLabel("AI 智能特性")
        title.setObjectName("PanelTitle")
        outer.addWidget(title)

        subtitle = QLabel("以下开关与 C++ 层 DMFT AI Pipeline 一一对应，可独立启停。")
        subtitle.setObjectName("PanelSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        # 按 category 分组
        groups: "OrderedDict[str, list[AIFeature]]" = OrderedDict()
        for f in bridge.features():
            groups.setdefault(f.category, []).append(f)

        for category, feats in groups.items():
            section_title = QLabel(category)
            section_title.setObjectName("SectionTitle")
            outer.addWidget(section_title)

            for f in feats:
                card = FeatureCard(f, bridge.is_enabled(f.key))
                card.toggled.connect(self.feature_toggled)
                card.param_changed.connect(self.feature_param_changed)
                outer.addWidget(card)

        outer.addStretch(1)
        self.setWidget(container)
