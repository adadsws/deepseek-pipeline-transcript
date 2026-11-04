# 媒体损坏检查

[简体中文](README.md) | [English](README_EN.md)

只读检查视频、音频和静态／多帧图片，可输入单文件或递归扫描目录。音视频按时间窗口解码，报告逐流损坏区间；图片验证结构并逐帧加载。

## 安装

随 [主项目](../../README.md) 提供。在项目根目录使用 Python 3.12 环境，安装 `python -m pip install Pillow`。图片检查无需显卡、模型或 API Key；音视频检查另需 PATH 中的 FFmpeg，或主项目已下载的 `runtime/VideoCaptioner/bin/Faster-Whisper-XXL/ffmpeg.exe`。可用时通过 FFprobe 探测媒体，否则回退到 FFmpeg。

## 使用

双击 `tools/check_media.bat` 后粘贴路径，或在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m tools.media_check
.\.venv\Scripts\python.exe -m tools.media_check "媒体文件或目录路径" --window-seconds 60
```

BAT 优先使用项目 `.venv`，否则使用 `py -3.12`；结束后显示状态并等待按键。模块无参数时提示输入路径，可粘贴带外层引号、空格、中文或 `%` 的路径。旧 `check_media.bat` 和 `scripts/media_check.py` 已迁移至此子工具。

默认 JSON 报告写入项目 `~outputs-final/media-check-reports/`，可用 `--output "报告.json"` 指定未存在的报告路径。`--window-seconds` 必须大于 0，默认 60；窗口越短区间越精细，检查次数越多。目录中的不支持文件也会记录到报告。

逐文件状态为 `ok`、`corrupt`、`unsupported` 或 `check_failed`。退出码 0 表示未发现损坏，1 表示发现损坏或检查失败，2 表示输入无效或含不支持文件。实际格式支持取决于 FFmpeg 和 Pillow；损坏区间是解码窗口定位结果。

字幕批处理的音频转换回退继续调用同一核心；本工具只检查源文件，不生成字幕或修复媒体。
