# AICamera — Windows / macOS 智能相机预览应用

一款基于 **PyQt5 + OpenCV** 的跨平台相机应用：以预览为中心，搭配紧凑的图标式悬浮工具栏。**Windows** 侧通过 DirectShow / MSMF 枚举与采集；**macOS** 侧优先使用原生 AVFoundation（可选 Center Stage），不可用时回退到 OpenCV `CAP_AVFOUNDATION`。底层预留与 C++ DMFT AI 管线对接的桥接层（主要面向 Windows 驱动生态）。

## 交互逻辑

| 行为 | 结果 |
|---|---|
| 程序启动 | 自动选择默认相机 + `config.ini` 中的默认分辨率（仓库示例为 **1280 x 720**，可自行调高）立即开始预览 |
| **单击预览画面** | 弹出 / 收回紧凑的图标工具栏（位于预览正下方） |
| **拖拽预览画面** | 立即收回工具栏；预览随鼠标移动到任意位置 |
| 工具栏 [📷] | 弹出/收回相机+分辨率下拉小面板（设备已只显示 ≥720p 的分辨率） |
| 工具栏 [□][○][☆] | 即刻切换预览形状：矩形 / 圆形 / 星形 |
| 工具栏底部 SeekBar | 实时缩放预览画面 |
| 右键预览画面 / `Esc` | 退出应用 |

### 工具栏布局

```
┌────────────────────────────────┐
│  [📷] | [□] [○] [☆]            │   <- 设备 + 形状（图标，无文字）
│  ━━━━━━●━━━━━━━━━━━━━━━━━━━━━ │   <- 预览尺寸 SeekBar
└────────────────────────────────┘
```

点击 `[📷]` 后弹出：

```
┌──────────────────────────┐
│  [Integrated Camera ▼] [↻]│
│  [1280 x 720         ▼]   │
└──────────────────────────┘
```

> AI 智能特性面板已按需求暂时屏蔽；底层 `DMFTBridge` 仍接入帧处理管线（默认仅启用 `mirror`），未来一行代码即可重新启用，无需改动其他模块。

## 项目结构

以下为仓库根目录下的主要文件（本地目录名可与远端不一致）。

```
.
├── main.py                      # 应用入口
├── app_config.py                # 读取 config.ini（含 [camera] / [macos]）
├── config.ini                   # 默认分辨率、FOURCC、FPS、macOS 原生采集开关等
├── main_window.py               # 紧凑图标工具栏 + 相机弹出面板 + AppController
├── preview_window.py            # 无边框、可拖拽、异形预览主窗
├── camera_capture.py            # 相机枚举 + 异步采集（Windows DShow / macOS AVFoundation）
├── macos_avfoundation_camera.py # macOS：PyObjC + AVFoundation 枚举/采集（可选 Center Stage）
├── dmft_bridge.py               # Python <-> C++ DMFT AI 桥接抽象层（含软件级 mock）
├── settings_panel.py            # 备用：完整版 AI 设置面板（当前未挂载）
├── style.qss                    # 现代化深色主题 + 图标按钮样式
├── requirements.txt             # 通用依赖；Windows 额外包含 pygrabber
├── requirements-macos.txt       # macOS：在 requirements.txt 基础上增加 PyObjC / AVFoundation 等
├── AICamera.spec                # PyInstaller 打包配置（可选）
├── LICENSE
└── README.md
```

## 安装

> 依赖 **Python 3.9+**。

### Windows

```powershell
pip install -r requirements.txt
```

> 若 `pygrabber` 安装失败可省略；程序会退化为按序号探测相机。

### macOS

```bash
pip3 install -r requirements-macos.txt
```

> `requirements-macos.txt` 已包含 `requirements.txt`，并追加 PyObjC 与 AVFoundation 相关包，用于原生枚举、采集及可选 Center Stage。若不想安装 PyObjC，可只装 `requirements.txt`，此时将主要依赖 OpenCV 的 AVFoundation 后端（能力与设备名展示可能受限）。

## 运行

**Windows（PowerShell）**

```powershell
python main.py
```

**macOS（终端）**

```bash
python3 main.py
```

## 配置

运行参数集中在 `config.ini`。当前仓库默认示例为较快起流的 720p，可按设备能力改大分辨率。

```ini
[camera]
default_width = 1280
default_height = 720
preferred_fourccs = MJPG,YUY2,NV12
fps = 30
```

- **Windows**：`preferred_fourccs` 按顺序请求 DirectShow 视频格式；优先 `MJPG` 时通常更容易协商到高分辨率实况流。
- **macOS**：可增加 `[macos]` 段，控制是否优先走原生 AVFoundation、是否尝试开启 Center Stage（需安装 `requirements-macos.txt` 中的依赖）。示例：

```ini
[macos]
use_native_avfoundation = true
prefer_center_stage = true
```

## 关键实现要点

### 1. 预览即主窗口（`preview_window.py`）
- `Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool` —— 无边框置顶
- `WA_TranslucentBackground` + `setMask(QRegion(...))` —— 真正异形窗口
- 通过 `QApplication.startDragDistance()` 阈值区分**单击**与**拖拽**：
  - 短按一下 → `clicked` 信号 → 切换工具栏
  - 越过阈值 → `drag_started` 信号 → 立即收起工具栏并跟随鼠标移动整窗

### 2. 紧凑图标工具栏（`main_window.py` → `ControlBar`）
- 单一悬浮窗口，宽度固定 280px，圆角 14px，半透明深色背景
- 所有图标使用 `QPainter` 现绘 `QPixmap` 生成 `QIcon`，**完全无外部资源依赖**
  - `make_shape_icon(shape)` —— 矩形/圆形/星形
  - `make_camera_icon()` —— 相机外形+镜头点
  - `make_refresh_icon()` —— 圆弧+箭头
- 形状按钮通过 `QButtonGroup(setExclusive=True)` 实现单选

### 3. 相机弹出面板（`CameraPopup`）
- 同样是悬浮 `_FloatingPanel`，由 `[📷]` 按钮的勾选状态控制显隐
- 自动定位在工具栏正下方、与相机按钮左对齐；越界时改放在工具栏上方
- 关闭工具栏（点击/拖拽预览）时一并隐藏

### 4. 启动即预览
- `start()` → 居中预览窗 → 显示加载文字 → `QTimer` 触发 `refresh_cameras`
- 首次启动按 `config.ini` 的 `default_width` / `default_height` 请求分辨率；Windows 下还会按 `preferred_fourccs` 顺序协商视频格式
- 分辨率下拉仅展示常见的 `h >= 720` 选项；需要更高清晰度时可手动切换

### 5. 与 C++ DMFT 对接
`dmft_bridge.DMFTBridge` 暴露三个稳定方法：

```python
bridge.set_feature_enabled(key, enabled)
bridge.set_feature_param(key, name, value)
processed = bridge.process_frame(frame_bgr)  # 输入/输出均 np.ndarray
```

接入真实 DLL 时，只需替换文件中以 `_native_*` 开头的 4 个占位方法（README 内含 `ctypes` 示例）。当未来要重新启用 AI 设置面板，在 `AppController` 中：

```python
from settings_panel import SettingsPanel
self.ai_panel = SettingsPanel(self.bridge)
self.ai_panel.feature_toggled.connect(self.bridge.set_feature_enabled)
self.ai_panel.feature_param_changed.connect(self.bridge.set_feature_param)
self.ai_panel.show()
```

## 常见问题

- **Windows：打不开相机 / 分辨率列表为空**：检查 *设置 → 隐私 → 相机* 是否允许桌面应用访问；相机被其他程序占用时也会失败。
- **macOS：无画面或无法枚举设备**：在 *系统设置 → 隐私与安全性 → 相机* 中允许终端或你使用的 Python/IDE 访问相机；未安装 PyObjC 套件时部分能力会回退到 OpenCV，行为可能与 Windows 不一致。
- **分辨率下拉中只有一两项**：当前过滤策略仅展示 ≥720p 的分辨率；如需放开，将 `AppController.MIN_RES_HEIGHT` 改小即可。
- **预览长时间显示「正在打开相机...」**：常见于首次切换分辨率时驱动或硬件协商较慢，等待数秒；若一直无响应，请尝试其他分辨率或重启相机占用进程。
- **找不到关闭按钮**：右键预览画面或按 `Esc` 即可退出。
