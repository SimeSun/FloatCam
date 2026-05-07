"""
应用配置读取。

用户可修改同目录下的 config.ini 来调整默认相机分辨率和采集格式偏好。
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from typing import Tuple


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")


@dataclass(frozen=True)
class CameraConfig:
    # 720p 协商最快，作为启动默认分辨率以最快出首帧；
    # config.ini 可覆盖。
    default_resolution: Tuple[int, int] = (1280, 720)
    preferred_fourccs: Tuple[str, ...] = ("MJPG", "YUY2", "NV12")
    fps: int = 30


@dataclass(frozen=True)
class MacOSConfig:
    """仅 macOS 生效；通过环境变量传给 ``camera_capture.CameraThread``。"""

    use_native_avfoundation: bool = True
    prefer_center_stage: bool = True


@dataclass(frozen=True)
class AppConfig:
    camera: CameraConfig = field(default_factory=CameraConfig)
    macos: MacOSConfig = field(default_factory=MacOSConfig)


def load_config(path: str = CONFIG_PATH) -> AppConfig:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")

    camera = CameraConfig(
        default_resolution=_read_resolution(parser),
        preferred_fourccs=_read_fourccs(parser),
        fps=_read_int(parser, "camera", "fps", CameraConfig.fps),
    )
    macos = MacOSConfig(
        use_native_avfoundation=_read_bool(
            parser, "macos", "use_native_avfoundation", True,
        ),
        prefer_center_stage=_read_bool(
            parser, "macos", "prefer_center_stage", True,
        ),
    )
    return AppConfig(camera=camera, macos=macos)


def _read_resolution(parser: configparser.ConfigParser) -> Tuple[int, int]:
    width = _read_int(parser, "camera", "default_width", 1280)
    height = _read_int(parser, "camera", "default_height", 720)
    return (max(1, width), max(1, height))


def _read_fourccs(parser: configparser.ConfigParser) -> Tuple[str, ...]:
    raw = parser.get("camera", "preferred_fourccs", fallback="MJPG,YUY2,NV12")
    values = tuple(
        item.strip().upper()
        for item in raw.split(",")
        if len(item.strip()) == 4
    )
    return values or CameraConfig.preferred_fourccs


def _read_int(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    fallback: int,
) -> int:
    try:
        return parser.getint(section, option, fallback=fallback)
    except ValueError:
        return fallback


def _read_bool(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    fallback: bool,
) -> bool:
    if not parser.has_section(section):
        return fallback
    try:
        return parser.getboolean(section, option, fallback=fallback)
    except ValueError:
        return fallback
