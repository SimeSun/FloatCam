"""
相机枚举 + 采集线程模块
- **Windows**：通过 DirectShow 枚举摄像头；通过 pygrabber 的 ``IAMStreamConfig``
  读取分辨率清单（:func:`probe_supported_resolutions`），可与正在运行的采集并发。
- **macOS**：优先通过 ``macos_avfoundation_camera``（PyObjC + AVFoundation）枚举
  分辨率并采集（可开启 Center Stage）；不可用时回退到 ``CAP_AVFOUNDATION``。
- 在独立 QThread 中持续采集帧，并通过信号交付给 UI（:class:`CameraThread`）
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence

import cv2
import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

DEFAULT_FOURCCS = ("MJPG", "YUY2", "NV12")
DEFAULT_FPS = 30

IS_WINDOWS = sys.platform == "win32"
IS_DARWIN = sys.platform == "darwin"
# OpenCV VideoCapture(CAP_AVFOUNDATION) on macOS 通常只暴露 0、1 两个索引；更高索引需走原生 AVFoundation。
MACOS_OPENCV_MAX_CAMERA_INDEX = 1

# 采集后端：Windows 上 MSMF → DSHOW；其他平台使用 AVFoundation（macOS）等。
if IS_WINDOWS:
    _CAPTURE_BACKENDS: tuple = (
        (cv2.CAP_MSMF, "MSMF"),
        (cv2.CAP_DSHOW, "DSHOW"),
    )
else:
    _CAPTURE_BACKENDS = (
        (cv2.CAP_AVFOUNDATION, "AVFOUNDATION"),
    )


# 常见分辨率候选集合，用于在打开相机后探测真实支持的分辨率
COMMON_RESOLUTIONS: List[tuple] = [
    (1280, 720),
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
]


@dataclass
class CameraDevice:
    index: int
    name: str

    def __str__(self) -> str:
        return f"[{self.index}] {self.name}"


def list_cameras() -> List[CameraDevice]:
    """枚举系统上可用的相机设备。"""
    devices: List[CameraDevice] = []
    if IS_WINDOWS:
        try:
            from pygrabber.dshow_graph import FilterGraph

            names = FilterGraph().get_input_devices()
            for i, name in enumerate(names):
                devices.append(CameraDevice(index=i, name=name))
        except Exception:
            for i in range(5):
                cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
                if cap.isOpened():
                    devices.append(CameraDevice(index=i, name=f"Camera {i}"))
                    cap.release()
        return devices

    if IS_DARWIN:
        try:
            from macos_avfoundation_camera import (
                is_native_stack_available,
                list_avfoundation_device_pairs,
            )

            if is_native_stack_available():
                pairs = list_avfoundation_device_pairs()
                if pairs:
                    return [CameraDevice(index=i, name=n) for i, n in pairs]
        except Exception:
            pass
        # macOS 上 OpenCV AVFoundation 常仅有效索引 0；多索引探测只会刷警告且无 FaceTime 语义。
        for i in range(1):
            cap = cv2.VideoCapture(i, cv2.CAP_AVFOUNDATION)
            if cap.isOpened():
                devices.append(CameraDevice(index=i, name=f"Camera {i}"))
                cap.release()
        return devices

    # 其他 Unix：尽力用默认后端探测少量索引
    for i in range(5):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            devices.append(CameraDevice(index=i, name=f"Camera {i}"))
            cap.release()
    return devices


def probe_supported_resolutions(camera_index: int) -> List[tuple]:
    """查询指定相机声明支持的分辨率清单（不开流；Windows 为 COM 查询）。"""
    if IS_WINDOWS:
        try:
            from pygrabber.dshow_graph import FilterGraph
        except Exception:
            return []

        try:
            graph = FilterGraph()
            graph.add_video_input_device(camera_index)
            formats = graph.get_input_device().get_formats()
        except Exception:
            return []

        supported: List[tuple] = []
        seen = set()
        for fmt in formats or ():
            try:
                w = int(fmt.get("width", 0))
                h = int(fmt.get("height", 0))
            except Exception:
                continue
            if w <= 0 or h <= 0:
                continue
            key = (w, h)
            if key not in seen:
                seen.add(key)
                supported.append(key)
        return sorted(supported)

    if IS_DARWIN:
        try:
            from macos_avfoundation_camera import (
                is_native_stack_available,
                probe_avfoundation_resolutions,
            )

            if is_native_stack_available():
                found = probe_avfoundation_resolutions(camera_index)
                if found:
                    return found
        except Exception:
            pass
        return []

    return []


def configure_capture(
    cap: cv2.VideoCapture,
    width: int,
    height: int,
    preferred_fourccs: Sequence[str] = DEFAULT_FOURCCS,
    fps: int = DEFAULT_FPS,
) -> None:
    """在读帧前应用采集设置，尽量拿到所选分辨率的高质量源帧。"""
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    _apply_preferred_fourcc(cap, preferred_fourccs)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, max(1, fps))


def _apply_preferred_fourcc(
    cap: cv2.VideoCapture,
    preferred_fourccs: Sequence[str],
) -> None:
    for fourcc in preferred_fourccs:
        normalized = fourcc.strip().upper()
        if len(normalized) != 4:
            continue
        if cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*normalized)):
            return


def read_best_initial_frame(
    cap: cv2.VideoCapture,
    width: int,
    height: int,
    preferred_fourccs: Sequence[str] = DEFAULT_FOURCCS,
    fps: int = DEFAULT_FPS,
) -> Optional[np.ndarray]:
    """快速取第一帧。

    旧实现会依次切换 ``preferred_fourccs`` 中每个 FOURCC 并比较哪种
    能拿到最接近目标分辨率的流——每次切换都意味着 DirectShow 重启
    底层视频流，3 个候选累计耗时 3~6 秒，是首帧延迟的主要来源之一。

    新实现：``configure_capture`` 内部已经按偏好顺序选择第一个可用的
    FOURCC（绝大多数 Windows 相机就是 MJPG），只需一次配置 + 一次读帧
    即可拿到首帧。仅当首选配置完全读不到任何帧时，才退化到逐 FOURCC
    重试，作为兜底。
    """
    configure_capture(cap, width, height, preferred_fourccs, fps)
    frame = _read_valid_frame(cap)
    if frame is not None:
        return frame

    # 兜底：极少数相机在自动选定的 FOURCC 下持续读不到帧时，
    # 显式逐个 FOURCC 再试一次。
    for fourcc in _normalized_fourccs(preferred_fourccs):
        configure_capture(cap, width, height, (fourcc,), fps)
        frame = _read_valid_frame(cap)
        if frame is not None:
            return frame
    return None


def _normalized_fourccs(preferred_fourccs: Sequence[str]) -> tuple[str, ...]:
    values = tuple(
        fourcc.strip().upper()
        for fourcc in preferred_fourccs
        if len(fourcc.strip()) == 4
    )
    return values or DEFAULT_FOURCCS


def _read_valid_frame(
    cap: cv2.VideoCapture,
    attempts: int = 8,
) -> Optional[np.ndarray]:
    for _ in range(attempts):
        ok, frame = cap.read()
        if ok and frame is not None and frame.size > 0:
            return frame
    return None


def open_capture_with_fallback(
    camera_index: int,
    width: int,
    height: int,
    preferred_fourccs: Sequence[str] = DEFAULT_FOURCCS,
    fps: int = DEFAULT_FPS,
):
    """按优先级尝试多个 OpenCV 后端打开相机并取出首帧。

    在 modern Windows 上 Media Foundation (CAP_MSMF) 通常比 DirectShow
    (CAP_DSHOW) 快 1~2 秒；但部分老旧 webcam 驱动只支持 DSHOW。本函数
    先试 MSMF，若 ``cv2.VideoCapture`` 打不开 / 读不到首帧，立即 release
    并回退到 DSHOW。``cv2.VideoCapture`` 失败本身是同步快速失败（百毫秒
    级），回退开销可忽略。

    返回 ``(cap, initial_frame)``——若所有后端都失败则均为 ``None``。
    """
    for backend, _name in _CAPTURE_BACKENDS:
        cap = cv2.VideoCapture(camera_index, backend)
        if not cap.isOpened():
            cap.release()
            continue
        try:
            frame = read_best_initial_frame(
                cap, width, height, preferred_fourccs, fps,
            )
        except Exception:
            cap.release()
            continue
        if frame is not None:
            return cap, frame
        # 后端能打开但读不出帧——常见于 MSMF + 个别老驱动；释放重试下个后端。
        cap.release()
    return None, None


class CameraThread(QThread):
    """异步相机采集线程，将每一帧通过信号广播出去。

    设计上**只**负责"打开相机 → 出首帧 → 持续读帧"。分辨率能力探测
    被有意剥离出去：旧版本在读帧线程内通过 ``cap.set(W/H)`` 反复
    试探的方式会让 DirectShow 重启底层流，单次探测可让预览卡 5~15s。
    现在改由 :class:`ResolutionProbeThread` 用 pygrabber 的纯 COM
    查询完成探测，不会触碰 ``cap``，与本线程互不干扰。
    """

    # 发送 BGR 格式的 numpy.ndarray
    frame_ready = pyqtSignal(np.ndarray)
    error = pyqtSignal(str)
    started_ok = pyqtSignal(int, int)  # 实际生效的宽高

    def __init__(
        self,
        camera_index: int,
        width: int,
        height: int,
        preferred_fourccs: Sequence[str] = DEFAULT_FOURCCS,
        fps: int = DEFAULT_FPS,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._preferred_fourccs = tuple(preferred_fourccs) or DEFAULT_FOURCCS
        self._fps = max(1, fps)
        self._running = False

    def stop(self) -> None:
        self._running = False
        self.requestInterruption()
        if self.isRunning() and not self.wait(2000):
            # 某些 DirectShow 驱动会卡在 cap.read() 内部；最后用 terminate
            # 兜底，避免应用退出被设备拖死。
            self.terminate()
            self.wait(1000)

    def run(self) -> None:
        if IS_DARWIN:
            import os

            self._prefer_native_mac = os.environ.get(
                "AICAMERA_MAC_NATIVE", "1",
            ).strip().lower() not in ("0", "false", "no", "off")
            self._mac_center_stage = os.environ.get(
                "AICAMERA_MAC_CENTER_STAGE", "1",
            ).strip().lower() not in ("0", "false", "no", "off")
            try:
                from macos_avfoundation_camera import (
                    is_native_stack_available,
                    try_run_native_capture,
                )

                if self._prefer_native_mac and is_native_stack_available():
                    if try_run_native_capture(self):
                        return
            except Exception:
                pass

        # 原生枚举可用时索引可能 >1；若原生未接管且 OpenCV 无法映射该索引，直接报错。
        if IS_DARWIN:
            try:
                from macos_avfoundation_camera import is_native_stack_available

                if (
                    is_native_stack_available()
                    and self._camera_index > MACOS_OPENCV_MAX_CAMERA_INDEX
                ):
                    self.error.emit(
                        "该相机需使用 macOS 原生采集，但会话未能启动。"
                        " 请检查「系统设置 → 隐私与安全性 → 相机」是否为终端/Python 授权；"
                        "或在 config.ini 的 [macos] 将 prefer_center_stage 改为 false 后重试。"
                    )
                    return
            except Exception:
                pass

        # 走 MSMF → DSHOW（Windows）或 AVFoundation（macOS）的回退打开路径
        cap, initial_frame = open_capture_with_fallback(
            self._camera_index,
            self._width,
            self._height,
            self._preferred_fourccs,
            self._fps,
        )
        if cap is None:
            self.error.emit(f"无法打开相机 {self._camera_index}")
            return

        self._running = True
        announced_size = False
        try:
            if initial_frame is not None:
                actual_h, actual_w = initial_frame.shape[:2]
                self.started_ok.emit(actual_w, actual_h)
                announced_size = True
                self.frame_ready.emit(initial_frame)

            while self._running and not self.isInterruptionRequested():
                ok, frame = cap.read()
                if not ok or frame is None:
                    # 偶发读失败时短暂休眠，避免空转
                    self.msleep(10)
                    continue
                if not announced_size:
                    actual_h, actual_w = frame.shape[:2]
                    self.started_ok.emit(actual_w, actual_h)
                    announced_size = True
                self.frame_ready.emit(frame)
                # 控制帧率，避免占满 CPU；实际帧率由相机硬件决定
                self.msleep(1)
        finally:
            cap.release()


class CameraListerThread(QThread):
    """后台枚举系统相机设备。

    pygrabber 的 ``FilterGraph().get_input_devices()`` 在某些机器上
    要 200ms~1s（受 DirectShow / WMI 子系统响应影响）；放在主线程会让
    "打开预览窗口 → 启动相机"被串行阻塞。本线程把它和 ``cv2.VideoCapture``
    打开过程并行起来，控制器先以 index 0 乐观启动采集，待枚举结果到达后
    再校正/补全。
    """

    cameras_ready = pyqtSignal(list)  # List[CameraDevice]

    def run(self) -> None:
        try:
            cameras = list_cameras()
        except Exception:
            cameras = []
        self.cameras_ready.emit(cameras)


class ResolutionProbeThread(QThread):
    """后台探测相机声明支持的分辨率（pygrabber + IAMStreamConfig）。

    与 :class:`CameraThread` 共享同一个相机时**完全安全**：本线程不开流、
    不调用 ``cv2.VideoCapture``，仅通过 DirectShow 的能力查询接口读取
    相机驱动自己声明的格式列表，整体在百毫秒量级，完全不会让正在跑的
    预览卡顿。
    """

    resolutions_ready = pyqtSignal(int, list)  # (camera_index, [(w, h), ...])

    def __init__(self, camera_index: int, parent=None) -> None:
        super().__init__(parent)
        self._camera_index = camera_index

    def run(self) -> None:
        resolutions = probe_supported_resolutions(self._camera_index)
        if resolutions:
            self.resolutions_ready.emit(self._camera_index, resolutions)
