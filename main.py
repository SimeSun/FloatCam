"""
AICamera 应用入口
=================

运行方式:
    python main.py
"""
from __future__ import annotations

import os
import sys

# === 启动加速环境变量（必须在 import cv2 之前生效；仅 Windows）===
# OpenCV 在 Windows 上有两个视频后端：DirectShow (CAP_DSHOW) 和
# Media Foundation (CAP_MSMF)。系统自带"相机"App 用的就是 MSMF，
# 启动速度通常比 DSHOW 快 1~2 秒。但 MSMF 默认会在打开相机时初始化
# 一组硬件 transform（GPU 解码 / 颜色转换链路），这一步在大多数
# Windows 设备上反而会让首次 open 慢 2~3 秒，是已知的"MSMF 启动慢"
# 元凶。把 OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS 设为 0 让 OpenCV
# 跳过这条链路，软件路径直出帧——首次 open 通常能压到几百毫秒。
if sys.platform == "win32":
    os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import signal  # noqa: E402

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtGui import QFont  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from main_window import AppController  # noqa: E402


def _load_stylesheet(app: QApplication) -> None:
    qss_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.qss")
    if os.path.isfile(qss_path):
        with open(qss_path, "r", encoding="utf-8") as fp:
            app.setStyleSheet(fp.read())


def _install_signal_handlers(app: QApplication, controller: AppController) -> None:
    def request_shutdown(_signum=None, _frame=None) -> None:
        controller.shutdown()

    signal.signal(signal.SIGINT, request_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_shutdown)

    # Give Python regular chances to run signal handlers while Qt owns the loop.
    signal_timer = QTimer(app)
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(100)
    app._signal_timer = signal_timer  # type: ignore[attr-defined]


def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("AICamera")
    if sys.platform == "darwin":
        app.setFont(QFont(".AppleSystemUIFont", 13))
    elif sys.platform == "win32":
        app.setFont(QFont("Segoe UI", 13))
    else:
        app.setFont(QFont("sans-serif", 13))
    _load_stylesheet(app)

    controller = AppController()
    app.aboutToQuit.connect(controller.shutdown)
    _install_signal_handlers(app, controller)
    controller.start()
    # 防止局部变量被 GC（控制器持有所有窗口的引用）
    app._controller = controller  # type: ignore[attr-defined]
    try:
        return app.exec_()
    except KeyboardInterrupt:
        controller.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())
