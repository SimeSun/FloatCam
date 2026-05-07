# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 规格文件：生成 Windows 目录分发包 dist\\AICamera\\
用法（在项目根目录）:
    pip install pyinstaller
    pyinstaller --clean AICamera.spec

PyQt5 / cv2 由 PyInstaller 自带 hook 收集；若目标机运行报缺 DLL 或插件，
可把下面 USE_COLLECT_ALL 改为 True 后重打（体积会明显变大）。
"""
import os

# Analysis / EXE / PYZ / COLLECT 由 pyinstaller 执行本文件时注入。

try:
    spec_root = os.path.abspath(SPECPATH)
except NameError:
    spec_root = os.path.dirname(os.path.abspath(SPEC))

USE_COLLECT_ALL = False

project_datas = [
    (os.path.join(spec_root, "style.qss"), "."),
    (os.path.join(spec_root, "config.ini"), "."),
]

extra_datas = []
extra_binaries = []
extra_hiddenimports = []

if USE_COLLECT_ALL:
    try:
        from PyInstaller.utils.hooks import collect_all

        for pkg in ("PyQt5", "cv2"):
            d, b, h = collect_all(pkg)
            extra_datas += d
            extra_binaries += b
            extra_hiddenimports += h
    except Exception:
        pass

a = Analysis(
    [os.path.join(spec_root, "main.py")],
    pathex=[spec_root],
    binaries=extra_binaries,
    datas=project_datas + extra_datas,
    hiddenimports=[
        "pygrabber.dshow_graph",
        "pygrabber.dshow_core",
        "comtypes",
        "comtypes.client",
    ]
    + extra_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AICamera",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AICamera",
)
