"""
macOS 原生相机采集（AVFoundation）与系统级画面能力
=================================================

用途
----
- 在 **macOS** 上提供与 ``CameraThread`` 相同的信号契约（``frame_ready`` /
  ``error`` / ``started_ok``），供 ``camera_capture.CameraThread.run`` 首行
  委托调用。
- 通过 ``AVCaptureDevice`` / ``AVCaptureSession`` 在应用可控的前提下尽量开启
  **Center Stage（人物居中）** 等系统/硬件能力；这些无法通过 OpenCV 内部的
  ``AVCaptureSession`` 可靠注入。

可选依赖：PyObjC（``pyobjc-framework-AVFoundation`` 等）。导入失败时由上层回退
到 ``cv2.VideoCapture(..., cv2.CAP_AVFOUNDATION)``，不影响 Windows。

内置 **FaceTime 相机**（用于系统/FaceTime 相关能力）在枚举结果中会排在最前，
作为默认索引 ``0``。不建议设置 ``OPENCV_AVFOUNDATION_SKIP_AUTH=1``：该变量会
限制 OpenCV 仅能按极少索引打开设备，与多路 AVFoundation 枚举不一致；请优先走
原生采集并授予终端「相机」权限。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from typing import List, Optional, Tuple

import numpy as np
from PyQt5.QtCore import QCoreApplication, QMetaObject, QObject, Qt, QThread, pyqtSlot
from PyQt5.QtWidgets import QApplication


def _debug_enabled() -> bool:
    """``AICAMERA_MAC_DEBUG=1`` 时打开原生采集链路的诊断日志。"""
    return os.environ.get("AICAMERA_MAC_DEBUG", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _dlog(msg: str) -> None:
    if _debug_enabled():
        ts = time.strftime("%H:%M:%S")
        print(f"[AICamera/mac {ts}] {msg}", file=sys.stderr, flush=True)

try:
    import objc
    from AVFoundation import (
        AVCaptureDevice,
        AVCaptureDeviceDiscoverySession,
        AVCaptureDeviceInput,
        AVCaptureDevicePositionUnspecified,
        AVCaptureDeviceTypeBuiltInWideAngleCamera,
        AVCaptureSession,
        AVCaptureVideoDataOutput,
        AVMediaTypeVideo,
        AVVideoScalingModeResizeAspect,
    )
    from CoreMedia import CMSampleBufferGetImageBuffer

    # CoreVideo 符号在不同 pyobjc 版本中位置不同：
    # - 旧版（<10）独立的 ``CoreVideo`` 模块；
    # - 新版（>=10，如 12.x）通过 ``Quartz`` 伞模块导出。
    # 任何一个能拿到就算原生栈可用，避免在新版 pyobjc 下静默 fallback 到
    # OpenCV CAP_AVFOUNDATION（那条路径无法可靠选中 FaceTime 相机）。
    _CV_SOURCE = ""
    try:
        from CoreVideo import (
            CVPixelBufferGetBaseAddress,
            CVPixelBufferGetBytesPerRow,
            CVPixelBufferGetHeight,
            CVPixelBufferGetWidth,
            CVPixelBufferLockBaseAddress,
            CVPixelBufferUnlockBaseAddress,
            kCVPixelBufferLock_ReadOnly,
            kCVPixelFormatType_32BGRA,
        )
        _CV_SOURCE = "CoreVideo"
    except Exception:
        from Quartz import (  # type: ignore[no-redef]
            CVPixelBufferGetBaseAddress,
            CVPixelBufferGetBytesPerRow,
            CVPixelBufferGetHeight,
            CVPixelBufferGetWidth,
            CVPixelBufferLockBaseAddress,
            CVPixelBufferUnlockBaseAddress,
            kCVPixelBufferLock_ReadOnly,
            kCVPixelFormatType_32BGRA,
        )
        _CV_SOURCE = "Quartz"

    from Foundation import NSObject

    # dispatch_queue_create / DISPATCH_QUEUE_SERIAL 同样：
    # - 旧 pyobjc 暴露 ``dispatch`` 模块；
    # - 新 pyobjc（pyobjc-framework-libdispatch）暴露 ``libdispatch`` 模块。
    _DISPATCH_SOURCE = ""
    try:
        from dispatch import DISPATCH_QUEUE_SERIAL, dispatch_queue_create
        _DISPATCH_SOURCE = "dispatch"
    except Exception:
        from libdispatch import (  # type: ignore[no-redef]
            DISPATCH_QUEUE_SERIAL,
            dispatch_queue_create,
        )
        _DISPATCH_SOURCE = "libdispatch"

    _DEVICE_TYPES = [AVCaptureDeviceTypeBuiltInWideAngleCamera]
    try:
        from AVFoundation import AVCaptureDeviceTypeBuiltInTrueDepthCamera

        _DEVICE_TYPES.append(AVCaptureDeviceTypeBuiltInTrueDepthCamera)
    except Exception:
        pass
    try:
        from AVFoundation import AVCaptureDeviceTypeDeskViewCamera

        _DEVICE_TYPES.append(AVCaptureDeviceTypeDeskViewCamera)
    except Exception:
        pass
    try:
        from AVFoundation import AVCaptureDeviceTypeContinuityCamera

        _DEVICE_TYPES.append(AVCaptureDeviceTypeContinuityCamera)
    except Exception:
        pass
    try:
        from AVFoundation import AVCaptureDeviceTypeExternalUnknown

        _DEVICE_TYPES.append(AVCaptureDeviceTypeExternalUnknown)
    except Exception:
        pass

    _HAS_AV = True
    _AV_IMPORT_ERROR: Optional[BaseException] = None
except Exception as _av_imp_err:  # pragma: no cover
    _HAS_AV = False
    _AV_IMPORT_ERROR = _av_imp_err
    _CV_SOURCE = ""
    _DISPATCH_SOURCE = ""
    objc = None  # type: ignore
    NSObject = object  # type: ignore


def is_native_stack_available() -> bool:
    return bool(_HAS_AV)


# 相机权限状态码（与 ``AVAuthorizationStatus`` 的整数取值对齐）。
_AUTH_STATUS_LABELS = {
    0: "NotDetermined(未询问)",
    1: "Restricted(系统限制)",
    2: "Denied(已拒绝)",
    3: "Authorized(已授权)",
}


def _authorization_status_value() -> int:
    if not _HAS_AV:
        return -1
    try:
        return int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeVideo))
    except Exception:
        return -1


def _authorization_status_label(status: int) -> str:
    return _AUTH_STATUS_LABELS.get(status, f"Unknown({status})")


def _calling_process_hint() -> str:
    """给出 macOS 把权限挂在哪里的提示——通常是终端/IDE 这个 GUI 父进程。"""
    binary = sys.executable or "python"
    parent_hint = os.environ.get("__CFBundleIdentifier", "")
    if parent_hint:
        return f"宿主进程: {parent_hint}; Python: {binary}"
    return f"Python: {binary}"


def _request_camera_access_blocking(timeout_sec: float = 120.0) -> bool:
    """同步发起相机授权弹窗，并阻塞等待用户作答。

    内部使用 ``threading.Event`` 接收 ObjC completion handler 的回调结果；
    macOS 会在主 GUI 进程上下文里弹出系统权限提示框，与本调用线程无关。
    """
    if not _HAS_AV:
        return False
    import threading

    done = threading.Event()
    result = {"granted": False}

    def _handler(granted):  # 由 AVFoundation 在任意线程回调
        result["granted"] = bool(granted)
        done.set()

    try:
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeVideo, _handler
        )
    except Exception as exc:
        _dlog(f"requestAccessForMediaType 调用失败: {exc!r}")
        return False
    if not done.wait(timeout=timeout_sec):
        _dlog("等待相机授权超时（用户未在系统弹窗上作答）")
        return False
    return bool(result["granted"])


def _ensure_camera_authorized(thread: QThread) -> bool:
    """确保有相机权限；未授权就显式触发系统授权流程（必要时阻塞等待）。

    返回 ``False`` 时调用方应放弃启动：本函数已 ``error.emit`` 一条
    可操作的中文提示。返回 ``True`` 表示当前进程已经被 macOS 标记为
    "可访问相机"，``startRunning`` 之后能拿到真实硬件画面。
    """
    if not _HAS_AV:
        return True
    status = _authorization_status_value()
    _dlog(f"相机授权状态: {_authorization_status_label(status)}")
    if status == 3:  # Authorized
        return True
    if status == 0:  # NotDetermined → 弹系统授权框
        _dlog("发起相机授权请求，等待系统弹窗用户作答 ...")
        granted = _request_camera_access_blocking()
        new_status = _authorization_status_value()
        _dlog(
            f"授权结果 granted={granted}, 新状态={_authorization_status_label(new_status)}"
        )
        if granted:
            return True
        thread.error.emit(
            "相机权限未授予。请在系统弹窗中点击「允许」，或前往"
            "「系统设置 → 隐私与安全性 → 相机」勾选当前 Python 解释器/终端后重启程序。"
        )
        return False
    # Denied / Restricted —— 必须由用户去系统设置里手动开
    hint = _calling_process_hint()
    thread.error.emit(
        "相机权限被拒绝或受限，无法打开 FaceTime 相机。"
        "请前往「系统设置 → 隐私与安全性 → 相机」中，"
        "为当前的终端/IDE/Python 解释器启用相机权限后重启程序。"
        f"\n（{hint}；当前授权状态: {_authorization_status_label(status)}）"
    )
    return False


_LOGGED_STACK_INFO = False


def _log_stack_info_once() -> None:
    global _LOGGED_STACK_INFO
    if _LOGGED_STACK_INFO or not _debug_enabled():
        return
    _LOGGED_STACK_INFO = True
    if _HAS_AV:
        _dlog(
            f"原生 AVFoundation 栈就绪 (CoreVideo 来自 {_CV_SOURCE} / dispatch 来自 {_DISPATCH_SOURCE})"
        )
    else:
        _dlog(f"原生栈不可用，导入失败: {_AV_IMPORT_ERROR!r}")


def _pixel_bytes_from_base(base, nbytes: int) -> bytes:
    """从 ``CVPixelBufferGetBaseAddress`` 的返回值中安全读出 ``nbytes`` 字节。

    在不同 pyobjc 实现下返回值形态不同：
    - 旧 ``CoreVideo`` 模块：返回原始整数地址 / ``c_void_p``，``ctypes.string_at``
      可直接读取；
    - 新 pyobjc ``Quartz`` 伞模块：返回 ``objc.varlist`` 包装对象，需要
      ``base.as_buffer(nbytes)`` 拿到 ``memoryview`` 再读。

    本函数兼容上述两种形态，避免 ``ctypes.string_at`` 在 ``objc.varlist`` 上
    抛出 ``ArgumentError: argument 1: TypeError: wrong type`` 的回调错误。
    """
    if base is None:
        raise ValueError("CVPixelBufferGetBaseAddress 返回空")
    as_buffer = getattr(base, "as_buffer", None)
    if callable(as_buffer):
        return bytes(as_buffer(nbytes))
    if isinstance(base, int):
        return ctypes.string_at(base, nbytes)
    try:
        return ctypes.string_at(base, nbytes)
    except Exception as exc:
        raise TypeError(
            f"无法从 base address 读取像素字节: type={type(base).__name__}, err={exc!r}"
        ) from exc


class _AVDarwinDiscoverGate(QObject):
    """在 **GUI 线程** 上执行 AVFoundation 枚举；后台线程调用会走 ``_discovered_video_devices`` 的调度。"""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._last_devices: list = []

    @pyqtSlot()
    def discover(self) -> None:
        self._last_devices = _discovered_video_devices_impl()


def create_and_register_darwin_av_discover_gate(parent: Optional[QObject] = None) -> Optional[_AVDarwinDiscoverGate]:
    """把枚举闸门挂到 ``QApplication``，供非主线程通过 ``BlockingQueuedConnection`` 调用。"""
    app = QApplication.instance()
    if app is None:
        return None
    gate = _AVDarwinDiscoverGate(parent)
    app._aicamera_darwin_av_gate = gate  # type: ignore[attr-defined]
    return gate


def _av_device_unique_id(device) -> str:
    try:
        return str(device.uniqueID() or "")
    except Exception:
        return str(id(device))


def _av_device_localized_name(device) -> str:
    try:
        return str(device.localizedName() or "")
    except Exception:
        return ""


def _dedupe_av_devices(devices: list) -> list:
    """同一物理设备可能出现在多种 ``deviceTypes`` 查询结果中，按 ``uniqueID`` 去重。"""
    if not devices:
        return []
    seen: set[str] = set()
    out: list = []
    for dev in devices:
        uid = _av_device_unique_id(dev)
        if uid in seen:
            continue
        seen.add(uid)
        out.append(dev)
    return out


def _is_continuity_camera(dev) -> bool:
    """检测 iPhone Continuity Camera。"""
    try:
        if dev.respondsToSelector_("isContinuityCamera"):
            m = getattr(dev, "isContinuityCamera", None)
            if m is not None:
                v = m() if callable(m) else m
                if bool(v):
                    return True
    except Exception:
        pass
    name = _av_device_localized_name(dev).lower()
    uid = _av_device_unique_id(dev).lower()
    if "continuity" in name or "continuity" in uid:
        return True
    if "iphone" in name and ("camera" in name or "相机" in _av_device_localized_name(dev)):
        return True
    return False


def _device_type_str(dev) -> str:
    try:
        return str(dev.deviceType() or "")
    except Exception:
        return ""


def _device_position_value(dev):
    try:
        pos = getattr(dev, "position", None)
        if pos is None:
            return None
        return pos() if callable(pos) else pos
    except Exception:
        return None


def _is_facetime_builtin(dev) -> bool:
    """权威的 FaceTime（内置广角前置）相机判定，避开本地化名差异。

    FaceTime 摄像头在所有本地化下都满足：
    ``deviceType == AVCaptureDeviceTypeBuiltInWideAngleCamera`` 且
    ``position == AVCaptureDevicePositionFront``，且不是 Continuity Camera。
    名字命中（"facetime" / "高清相机"）作为兜底，覆盖个别旧 macOS 上 position
    可能为 Unspecified 的边缘情形。
    """
    if _is_continuity_camera(dev):
        return False
    name = _av_device_localized_name(dev)
    nl = name.lower()
    try:
        from AVFoundation import AVCaptureDevicePositionFront
    except Exception:
        AVCaptureDevicePositionFront = None  # type: ignore[misc, assignment]

    dtype = _device_type_str(dev)
    pos = _device_position_value(dev)
    is_builtin_wide = dtype == str(AVCaptureDeviceTypeBuiltInWideAngleCamera)
    is_front = (
        AVCaptureDevicePositionFront is not None
        and pos == AVCaptureDevicePositionFront
    )
    if is_builtin_wide and is_front:
        return True
    if "facetime" in nl:
        return True
    return False


def _sort_devices_facetime_first(devices: list) -> list:
    """把内置 FaceTime / 用户朝向相机排在最前（索引 0），Continuity / iPhone 靠后。

    这样默认启动与 OpenCV 仅暴露单路索引时的行为一致，且便于使用系统 FaceTime
    相关能力所期望的那颗内置摄像头。
    """
    if len(devices) <= 1:
        return list(devices)
    try:
        from AVFoundation import AVCaptureDevicePositionFront
    except Exception:
        AVCaptureDevicePositionFront = None  # type: ignore[misc, assignment]

    def tier(dev) -> tuple:
        name = _av_device_localized_name(dev)
        uid = _av_device_unique_id(dev)
        nl = name.lower()
        ul = uid.lower()
        if _is_facetime_builtin(dev):
            rank = 0
        else:
            rank = 40
            if _is_continuity_camera(dev):
                rank = 60
            if rank == 40 and AVCaptureDevicePositionFront is not None:
                pv = _device_position_value(dev)
                if pv is not None and pv == AVCaptureDevicePositionFront:
                    rank = 8
            if rank == 40 and ("built-in" in nl or "内建" in name):
                rank = 12
            if rank == 40 and ("studio display" in nl or "thunderbolt display" in nl):
                rank = 25
        return (rank, nl, ul)

    return sorted(devices, key=tier)


def _discovered_video_devices_impl() -> list:
    """在 **主线程** 调用（或通过闸门调度）；实际查询 AVFoundation。"""
    if not _HAS_AV:
        return []
    devices: list = []
    # 先只查内置广角：部分系统在多 type 组合下会返回空，但单 type 能列出 FaceTime。
    try:
        session = AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
            [AVCaptureDeviceTypeBuiltInWideAngleCamera],
            AVMediaTypeVideo,
            AVCaptureDevicePositionUnspecified,
        )
        devices = list(session.devices()) if session is not None else []
    except Exception:
        devices = []
    if not devices:
        try:
            session = AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
                _DEVICE_TYPES,
                AVMediaTypeVideo,
                AVCaptureDevicePositionUnspecified,
            )
            devices = list(session.devices()) if session is not None else []
        except Exception:
            devices = []
    if not devices:
        try:
            devices = list(AVCaptureDevice.devicesWithMediaType_(AVMediaTypeVideo) or [])
        except Exception:
            return []
    devices = _dedupe_av_devices(devices)
    return _sort_devices_facetime_first(devices)


def _discovered_video_devices() -> list:
    """任意线程入口：非 GUI 线程在事件循环已运行时通过闸门阻塞调度到主线程。"""
    if not _HAS_AV:
        return []
    app = QApplication.instance()
    if app is not None and QThread.currentThread() is not app.thread():
        if not QCoreApplication.startingUp():
            gate = getattr(app, "_aicamera_darwin_av_gate", None)
            if gate is not None:
                QMetaObject.invokeMethod(
                    gate,
                    "discover",
                    Qt.BlockingQueuedConnection,
                )
                return list(getattr(gate, "_last_devices", []))
    return _discovered_video_devices_impl()


def list_avfoundation_device_pairs() -> List[Tuple[int, str]]:
    """返回 ``(index, localized_name)``，供 ``camera_capture`` 包装为
    :class:`CameraDevice`，避免与 ``camera_capture`` 循环导入。"""
    out: List[Tuple[int, str]] = []
    for i, dev in enumerate(_discovered_video_devices()):
        try:
            name = str(dev.localizedName())
        except Exception:
            name = f"Camera {i}"
        out.append((i, name))
    return out


def _format_dimensions(fmt) -> Optional[Tuple[int, int]]:
    if not _HAS_AV:
        return None
    try:
        desc = fmt.formatDescription()
        if desc is None:
            return None
        from CoreMedia import CMVideoFormatDescriptionGetDimensions

        d = CMVideoFormatDescriptionGetDimensions(desc)
        w, h = int(d.width), int(d.height)
        if w > 0 and h > 0:
            return w, h
    except Exception:
        return None
    return None


def probe_avfoundation_resolutions(camera_index: int) -> List[Tuple[int, int]]:
    devices = _discovered_video_devices()
    if camera_index < 0 or camera_index >= len(devices):
        return []
    device = devices[camera_index]
    seen = set()
    out: List[Tuple[int, int]] = []
    try:
        formats = list(device.formats()) if device.respondsToSelector_("formats") else []
    except Exception:
        formats = []
    for fmt in formats:
        wh = _format_dimensions(fmt)
        if wh is None or wh in seen:
            continue
        seen.add(wh)
        out.append(wh)
    return sorted(out)


def _center_stage_supported_for_format(fmt) -> bool:
    if not _HAS_AV:
        return False
    try:
        if fmt.respondsToSelector_("isCenterStageSupported"):
            return bool(fmt.isCenterStageSupported())
    except Exception:
        pass
    return False


def _pick_format(device, target_w: int, target_h: int, prefer_center_stage: bool):
    try:
        formats = list(device.formats()) if device.respondsToSelector_("formats") else []
    except Exception:
        formats = []
    if not formats:
        return None
    scored = []
    for fmt in formats:
        wh = _format_dimensions(fmt)
        if wh is None:
            continue
        w, h = wh
        score_res = -abs(w - target_w) - abs(h - target_h)
        cs = 1 if (prefer_center_stage and _center_stage_supported_for_format(fmt)) else 0
        scored.append((cs, score_res, w * h, fmt))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return scored[-1][3]


def _choose_format(device, target_w: int, target_h: int, prefer_center_stage: bool):
    """在评分选格式基础上增加兜底，避免虚拟/特殊设备因格式表解析失败而无法开流。"""
    chosen = _pick_format(device, target_w, target_h, prefer_center_stage)
    if chosen is not None:
        return chosen
    try:
        formats = list(device.formats()) if device.respondsToSelector_("formats") else []
    except Exception:
        formats = []
    for fmt in formats:
        if _format_dimensions(fmt) is not None:
            return fmt
    if formats:
        return formats[0]
    try:
        if device.respondsToSelector_("activeFormat"):
            af = device.activeFormat()
            if af is not None:
                return af
    except Exception:
        pass
    return None


def _lock_apply_format_and_center_stage(device, active_format, enable_center_stage: bool) -> None:
    if not _HAS_AV:
        return
    ok = device.lockForConfiguration_(None)
    if not ok:
        return
    try:
        if device.respondsToSelector_("setActiveFormat:"):
            device.setActiveFormat_(active_format)
        if not enable_center_stage:
            return
        try:
            if device.respondsToSelector_("setCenterStageControlMode:error:"):
                from AVFoundation import AVCaptureDeviceCenterStageControlModeApplication

                device.setCenterStageControlMode_error_(
                    AVCaptureDeviceCenterStageControlModeApplication,
                    None,
                )
        except Exception:
            pass
        if device.respondsToSelector_("setCenterStageEnabled:"):
            device.setCenterStageEnabled_(True)
    except Exception:
        pass
    finally:
        device.unlockForConfiguration()


if _HAS_AV:

    def _device_input_for(device) -> Optional[object]:
        """创建 ``AVCaptureDeviceInput``；兼容 ``deviceInputWithDevice_error_`` 与 ``initWithDevice_error_``。"""
        try:
            if hasattr(AVCaptureDeviceInput, "deviceInputWithDevice_error_"):
                out = AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
                if isinstance(out, tuple):
                    out = out[0]
                if out is not None:
                    return out
        except Exception:
            pass
        try:
            out = AVCaptureDeviceInput.alloc().initWithDevice_error_(device, None)
            if isinstance(out, tuple):
                out = out[0]
            return out
        except Exception:
            return None

    def _native_session_components(device, thread: QThread):
        """配置 ``AVCaptureSession``；成功返回 ``(session, output, delegate)``。"""
        session = AVCaptureSession.alloc().init()
        session.beginConfiguration()
        try:
            inp = _device_input_for(device)
            if inp is None or not session.canAddInput_(inp):
                return None
            session.addInput_(inp)

            output = AVCaptureVideoDataOutput.alloc().init()
            output.setAlwaysDiscardsLateVideoFrames_(True)
            try:
                output.setVideoScalingMode_(AVVideoScalingModeResizeAspect)
            except Exception:
                pass
            output.setVideoSettings_({"PixelFormatType": kCVPixelFormatType_32BGRA})

            delegate = _SampleDelegate.alloc().init()
            delegate._owner = thread  # noqa: SLF001
            queue = dispatch_queue_create(b"ai.camera.frames", DISPATCH_QUEUE_SERIAL)
            output.setSampleBufferDelegate_queue_(delegate, queue)

            if not session.canAddOutput_(output):
                return None
            session.addOutput_(output)
        except Exception:
            return None
        finally:
            try:
                session.commitConfiguration()
            except Exception:
                pass
        return session, output, delegate

    class _SampleDelegate(NSObject):  # type: ignore[misc, valid-type]
        def captureOutput_didOutputSampleBuffer_fromConnection_(  # noqa: N802
            self,
            _output,
            sample_buffer,
            _connection,
        ):
            owner = getattr(self, "_owner", None)
            if owner is None or not getattr(owner, "_native_running", False):
                return
            try:
                buf = CMSampleBufferGetImageBuffer(sample_buffer)
                if buf is None:
                    return
                CVPixelBufferLockBaseAddress(buf, kCVPixelBufferLock_ReadOnly)
                try:
                    w = CVPixelBufferGetWidth(buf)
                    h = CVPixelBufferGetHeight(buf)
                    row = CVPixelBufferGetBytesPerRow(buf)
                    base = CVPixelBufferGetBaseAddress(buf)
                    if not base or w <= 0 or h <= 0:
                        return
                    nbytes = row * h
                    raw = _pixel_bytes_from_base(base, nbytes)
                finally:
                    CVPixelBufferUnlockBaseAddress(buf, kCVPixelBufferLock_ReadOnly)

                bgra = np.frombuffer(raw, dtype=np.uint8).reshape((h, row // 4, 4))
                bgr = bgra[:, :w, :3].copy()
                if not getattr(owner, "_native_announced", False):
                    owner._native_announced = True
                    owner._native_frame_count = 0  # type: ignore[attr-defined]
                    owner._native_first_frame_ts = time.time()  # type: ignore[attr-defined]
                    owner._native_last_log_ts = owner._native_first_frame_ts  # type: ignore[attr-defined]
                    owner._native_last_log_count = 0  # type: ignore[attr-defined]
                    if _debug_enabled():
                        try:
                            mean_val = float(bgr.mean())
                        except Exception:
                            mean_val = -1.0
                        _dlog(
                            f"首帧到达 {int(w)}x{int(h)} (row={int(row)}) "
                            f"mean_pixel={mean_val:.1f} → started_ok"
                        )
                        if mean_val >= 0 and mean_val < 1.0:
                            _dlog(
                                "⚠ 首帧像素均值≈0，疑似 macOS 因相机权限被拒绝而下发空帧；"
                                "请检查「系统设置 → 隐私与安全性 → 相机」是否给当前进程打勾"
                            )
                    owner.started_ok.emit(int(w), int(h))
                owner._native_frame_count += 1  # type: ignore[attr-defined]
                if _debug_enabled():
                    now = time.time()
                    last_log = getattr(owner, "_native_last_log_ts", now)
                    if now - last_log >= 3.0:
                        cnt = owner._native_frame_count  # type: ignore[attr-defined]
                        delta = cnt - getattr(owner, "_native_last_log_count", 0)
                        fps = delta / max(now - last_log, 1e-3)
                        owner._native_last_log_ts = now  # type: ignore[attr-defined]
                        owner._native_last_log_count = cnt  # type: ignore[attr-defined]
                        try:
                            mean_val = float(bgr.mean())
                        except Exception:
                            mean_val = -1.0
                        _dlog(
                            f"采集中 frames={cnt} fps≈{fps:.1f} mean_pixel={mean_val:.1f}"
                        )
                owner.frame_ready.emit(bgr)
            except Exception as ex:
                _dlog(f"采集回调异常: {ex!r}")
                try:
                    owner.error.emit(f"macOS 采集回调错误: {ex}")
                except Exception:
                    pass


def try_run_native_capture(thread: QThread) -> bool:
    """返回 True 表示已由本函数处理（含失败并 ``error.emit``）；False 表示应
    回退到 OpenCV ``CAP_AVFOUNDATION``。"""
    _log_stack_info_once()
    if not _HAS_AV or sys.platform != "darwin":
        _dlog(f"原生路径跳过：_HAS_AV={_HAS_AV} platform={sys.platform}")
        return False
    if not getattr(thread, "_prefer_native_mac", True):
        _dlog("原生路径被 _prefer_native_mac=False 跳过")
        return False

    devices = _discovered_video_devices()
    if not devices:
        _dlog("未枚举到任何 AVFoundation 设备 → 回退 OpenCV 路径")
        return False
    if _debug_enabled():
        for i, d in enumerate(devices):
            _dlog(
                f"枚举[{i}] name={_av_device_localized_name(d)!r} "
                f"facetime={_is_facetime_builtin(d)} continuity={_is_continuity_camera(d)}"
            )
    idx = getattr(thread, "_camera_index", 0)
    if idx < 0 or idx >= len(devices):
        thread.error.emit(f"相机索引无效: {idx}")
        return True

    # 默认启动（idx==0）时强制锁定到内置 FaceTime 摄像头：即便排序逻辑被
    # 未来扩展无意改动、或某些 macOS 版本返回的设备顺序异常，也能保证
    # "执行程序默认打开 FaceTime"。仅在用户显式切换到其它相机（idx>0）
    # 时才尊重该选择。
    if idx == 0:
        for cand in devices:
            if _is_facetime_builtin(cand):
                device = cand
                break
        else:
            device = devices[0]
    else:
        device = devices[idx]
    _dlog(
        f"选中设备 idx={idx} → name={_av_device_localized_name(device)!r} "
        f"facetime={_is_facetime_builtin(device)}"
    )

    # 关键：在创建会话之前就把相机权限拿到位。否则 AVFoundation 会"看似启动
    # 成功"——session 进入 running、delegate 偶尔被回调一次空 buffer——但物理
    # 相机不通电（绿灯不亮），导致用户看到的现象是"日志显示首帧到达，但
    # FaceTime 相机其实并没有打开"。
    if not _ensure_camera_authorized(thread):
        return True
    target_w = int(getattr(thread, "_width", 1280))
    target_h = int(getattr(thread, "_height", 720))
    prefer_cs = bool(getattr(thread, "_mac_center_stage", True))

    chosen = _choose_format(device, target_w, target_h, prefer_cs)
    if chosen is None:
        if idx <= 1:
            return False
        thread.error.emit(
            "未找到可用的视频格式。请在「系统设置 → 隐私与安全性 → 相机」"
            "中为终端或 Python 解释器开启权限后重试。"
        )
        return True

    cs_tries = (True, False) if prefer_cs else (False,)
    session = output = delegate = None
    for use_cs in cs_tries:
        _lock_apply_format_and_center_stage(device, chosen, use_cs)
        built = _native_session_components(device, thread)
        if built is None:
            continue
        session, output, delegate = built
        thread._native_announced = False  # type: ignore[attr-defined]
        thread._native_session = session  # type: ignore[attr-defined]
        thread._native_delegate = delegate  # type: ignore[attr-defined]
        session.startRunning()
        _dlog(
            f"会话已 startRunning（use_center_stage={use_cs}），等待硬件出帧 ..."
        )
        thread._running = True  # type: ignore[attr-defined]
        thread._native_running = True  # type: ignore[attr-defined]
        thread._native_announced = False  # type: ignore[attr-defined]
        thread._native_frame_count = 0  # type: ignore[attr-defined]
        no_frame_warned = False
        loop_start_ts = time.time()
        try:
            while getattr(thread, "_running", True) and not thread.isInterruptionRequested():
                thread.msleep(20)
                if (
                    not no_frame_warned
                    and not getattr(thread, "_native_announced", False)
                    and time.time() - loop_start_ts > 3.0
                ):
                    no_frame_warned = True
                    status = _authorization_status_value()
                    _dlog(
                        f"会话已运行 3s 仍未收到任何采集帧 — 授权状态={_authorization_status_label(status)}, "
                        f"isRunning={bool(session.isRunning())}"
                    )
                    if status != 3:
                        thread.error.emit(
                            "FaceTime 相机未能真正打开（无采集帧）。最常见原因是当前 Python "
                            "解释器尚未被授予相机权限——请前往「系统设置 → 隐私与安全性 → 相机」"
                            "勾选当前 Python/终端后重启程序。"
                        )
        finally:
            thread._native_running = False  # type: ignore[attr-defined]
            session.stopRunning()
            _dlog(
                f"会话已 stopRunning，总帧数={getattr(thread, '_native_frame_count', 0)}"
            )
            thread._native_session = None  # type: ignore[attr-defined]
            thread._native_delegate = None  # type: ignore[attr-defined]
        return True

    if idx <= 1:
        return False
    thread.error.emit(
        "无法创建 AVFoundation 采集会话（已尝试关闭 Center Stage）。"
        "请检查相机权限；或在 config.ini 的 [macos] 将 prefer_center_stage 设为 false。"
    )
    return True
