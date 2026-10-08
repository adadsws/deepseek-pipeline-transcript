# [Whisper 转录 + DeepSeek 翻译] 流水线地制作字幕

自动识别和翻译视频语音，并把 `.srt` 字幕保存到原视频旁边。支持一次处理单个视频或整个文件夹。

## 首次安装

适用于 Windows，需要 NVIDIA 显卡、Python 3.12 和 DeepSeek API Key。

1. 打开 PowerShell，依次运行：

```powershell
git clone --recurse-submodules https://github.com/adadsws/deepseek-pipeline-transcript.git
cd deepseek-pipeline-transcript
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\reference\upstream\VideoCaptioner
.\runtime\VideoCaptioner\download-bin.ps1
.\runtime\VideoCaptioner\download-models.ps1
.\.venv\Scripts\python.exe .\scripts\prepare_runtime.py
```

2. 复制 `secrets/deepseek-api-key.example`，将副本命名为 `deepseek-api-key.txt`，打开后填入自己的 DeepSeek API Key。

请勿公开或提交自己的 DeepSeek API Key。

## 使用方法

1. 双击 `batch_videos.bat`。
2. 输入视频文件或文件夹路径。
3. 确认后等待处理完成。

文件夹会自动递归扫描。处理成功后，每个视频旁会出现同名 `.srt` 字幕。

## 修改语言

在 `config/batch/settings.toml` 中设置视频语言和字幕语言：

```toml
[language.video]
source = "ja"
target = "zh-Hans"
```

`ja` 表示日语，`zh-Hans` 表示简体中文。
