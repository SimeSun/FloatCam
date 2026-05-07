"""
DMFT AI 桥接抽象层
====================

用途
----
本模块定义 Python UI 与底层 C++ DMFT AI 管线之间的契约 (contract)。
当真正接入 C++ 实现时（通常通过 ctypes / pybind11 / COM / IPC），
只需要替换 ``DMFTBridge`` 中以 ``_native_*`` 命名的占位实现即可，
UI 层无须任何改动。

设计要点
--------
1. ``FEATURES`` 集中描述所有 AI 能力的元数据 (key、显示名、类别、可调参数)；
   设置面板会基于这份元数据自动生成 UI。
2. ``set_feature_enabled / set_feature_param`` 是稳定的 Python 接口，
   未来对接 C++ 时函数签名保持不变。
3. ``process_frame`` 是当前为了在 Pure-Python 状态下也能预览效果而内置的
   软件级近似实现；C++ 版本可直接将整个方法替换为对原生 SDK 的调用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import cv2
import numpy as np


@dataclass
class AIParam:
    """单个可调参数描述（用于 UI 自动生成滑条）。"""
    key: str
    label: str
    minimum: int = 0
    maximum: int = 100
    default: int = 50


@dataclass
class AIFeature:
    """单个 AI 能力描述。"""
    key: str
    label: str
    category: str
    description: str = ""
    params: List[AIParam] = field(default_factory=list)
    default_enabled: bool = False


# ---------------------------------------------------------------------------
# 与 C++ DMFT 对应的 AI 能力清单
# 这里的 key 应与 C++ 侧的特性常量一一对应（建议在两侧共用一份头文件/yaml）
# ---------------------------------------------------------------------------
FEATURES: List[AIFeature] = [
    AIFeature(
        key="background_blur",
        label="背景虚化",
        category="画面增强",
        description="对人像背景进行虚化，突出主体。",
        params=[AIParam("strength", "虚化强度", 0, 100, 50)],
        default_enabled=False,
    ),
    AIFeature(
        key="face_beautify",
        label="人脸美颜",
        category="画面增强",
        description="磨皮 / 美白 / 锐化。",
        params=[
            AIParam("smooth", "磨皮", 0, 100, 40),
            AIParam("whiten", "美白", 0, 100, 30),
        ],
        default_enabled=False,
    ),
    AIFeature(
        key="auto_framing",
        label="自动取景",
        category="智能构图",
        description="跟随人脸自动裁剪、保持主体居中。",
        params=[],
        default_enabled=False,
    ),
    AIFeature(
        key="eye_contact",
        label="眼神接触",
        category="智能构图",
        description="校正视线方向，使其看向镜头。",
        params=[],
        default_enabled=False,
    ),
    AIFeature(
        key="low_light",
        label="弱光增强",
        category="画质优化",
        description="在低光环境下提升亮度与降噪。",
        params=[AIParam("gain", "增益", 0, 100, 50)],
        default_enabled=False,
    ),
    AIFeature(
        key="hdr",
        label="HDR",
        category="画质优化",
        description="高动态范围合成。",
        params=[],
        default_enabled=False,
    ),
    AIFeature(
        key="gesture_control",
        label="手势识别",
        category="交互",
        description="识别常见手势触发事件。",
        params=[],
        default_enabled=False,
    ),
    AIFeature(
        key="mirror",
        label="镜像",
        category="基础",
        description="水平翻转预览画面。",
        params=[],
        default_enabled=True,
    ),
]


class DMFTBridge:
    """Python <-> C++ DMFT AI 桥接对象（当前包含软件级模拟实现）。"""

    def __init__(self) -> None:
        self._enabled: Dict[str, bool] = {f.key: f.default_enabled for f in FEATURES}
        self._params: Dict[str, Dict[str, int]] = {
            f.key: {p.key: p.default for p in f.params} for f in FEATURES
        }
        # 真实场景中这里应执行原生 SDK 初始化
        self._native_init()

    # ---------------- 公共 API（UI 调用） ----------------

    def features(self) -> List[AIFeature]:
        return FEATURES

    def is_enabled(self, key: str) -> bool:
        return self._enabled.get(key, False)

    def get_param(self, key: str, name: str) -> Optional[int]:
        return self._params.get(key, {}).get(name)

    def set_feature_enabled(self, key: str, enabled: bool) -> None:
        if key in self._enabled:
            self._enabled[key] = bool(enabled)
            self._native_set_enabled(key, bool(enabled))

    def set_feature_param(self, key: str, name: str, value: int) -> None:
        if key in self._params and name in self._params[key]:
            self._params[key][name] = int(value)
            self._native_set_param(key, name, int(value))

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        """对一帧进行 AI 处理。

        - 在接入 C++ DMFT 后，将本方法主体替换为 ``self._native_process(frame)``。
        - 当前为纯 Python 模拟，便于在没有原生 SDK 的环境中预览效果。
        """
        if frame is None or frame.size == 0:
            return frame
        out = frame

        if self.is_enabled("mirror"):
            out = cv2.flip(out, 1)

        if self.is_enabled("low_light"):
            gain = self._params["low_light"]["gain"]
            alpha = 1.0 + gain / 100.0
            out = cv2.convertScaleAbs(out, alpha=alpha, beta=10)

        if self.is_enabled("background_blur"):
            strength = max(1, self._params["background_blur"]["strength"] // 4)
            ksize = strength * 2 + 1
            blurred = cv2.GaussianBlur(out, (ksize, ksize), 0)
            # 简化处理：暂以渐变蒙版近似背景虚化
            mask = self._radial_subject_mask(out.shape[:2])
            mask3 = cv2.merge([mask, mask, mask]).astype(np.float32) / 255.0
            out = (out.astype(np.float32) * mask3 +
                   blurred.astype(np.float32) * (1 - mask3)).astype(np.uint8)

        if self.is_enabled("face_beautify"):
            smooth = self._params["face_beautify"]["smooth"]
            whiten = self._params["face_beautify"]["whiten"]
            if smooth > 0:
                d = max(1, smooth // 10)
                out = cv2.bilateralFilter(out, d=d * 2 + 1, sigmaColor=40, sigmaSpace=40)
            if whiten > 0:
                add = whiten // 4
                out = cv2.add(out, np.full_like(out, add))

        if self.is_enabled("hdr"):
            out = cv2.detailEnhance(out, sigma_s=10, sigma_r=0.15)

        # auto_framing / eye_contact / gesture_control 目前不做软件级 mock，
        # 等待 C++ DMFT 提供真实实现。
        return out

    # ---------------- 与 C++ 对接的占位方法 ----------------

    def _native_init(self) -> None:
        """初始化 C++ DMFT pipeline。当前为占位实现。"""
        # 例：self._lib = ctypes.WinDLL("DMFT.dll"); self._lib.dmft_init()
        return

    def _native_set_enabled(self, key: str, enabled: bool) -> None:
        """通知 C++ 启用/禁用特性。当前为占位实现。"""
        return

    def _native_set_param(self, key: str, name: str, value: int) -> None:
        """通知 C++ 更新某个特性的参数。当前为占位实现。"""
        return

    def _native_process(self, frame: np.ndarray) -> np.ndarray:
        """调用 C++ DMFT 处理一帧。当前为占位实现，直接返回原帧。"""
        return frame

    # ---------------- 内部工具 ----------------

    @staticmethod
    def _radial_subject_mask(shape: tuple) -> np.ndarray:
        """生成一个中心高、四周低的径向蒙版，用于模拟主体保留。"""
        h, w = shape
        cy, cx = h / 2, w / 2
        y, x = np.ogrid[:h, :w]
        dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        max_dist = np.sqrt(cx ** 2 + cy ** 2)
        mask = np.clip(1.0 - dist / max_dist * 1.4, 0, 1)
        return (mask * 255).astype(np.uint8)
