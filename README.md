# [Whisper 转录 + DeepSeek 翻译] 字幕工具

自动识别和翻译视频语音，并把 `.srt` 字幕保存到原视频旁边。支持一次处理单个视频或整个文件夹。

## 首次安装

适用于 Windows，需要 NVIDIA 显卡、Python 3.12 和 DeepSeek API Key。

打开 PowerShell，依次运行：

```powershell
git clone --recurse-submodules https://github.com/adadsws/deepseek-pipeline-transcript.git
cd deepseek-pipeline-transcript
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\reference\upstream\VideoCaptioner
.\runtime\VideoCaptioner\download-bin.ps1
.\runtime\VideoCaptioner\download-models.ps1
.\.venv\Scripts\python.exe .\scripts\prepare_runtime.py
```

复制 `secrets/deepseek-api-key.example`，将副本命名为 `deepseek-api-key.txt`，打开后填入自己的 DeepSeek API Key。

## 使用方法

1. 双击 `batch_videos.bat`。
2. 输入视频文件或文件夹路径。
3. 确认后等待处理完成。

文件夹会自动递归扫描。处理成功后，每个视频旁会出现同名 `.srt` 字幕。

如果视频旁已经有同名 SRT，只会更新署名，不会重新识别和翻译。需要强制重新处理时运行：

```powershell
.\batch_videos.bat "D:\Videos" --overwrite
```

## 修改语言

打开 `config/batch/settings.toml`：

```toml
[language.video]
source = "ja"
target = "zh-Hans"
```

`ja` 表示日语，`zh-Hans` 表示简体中文。

## 设置署名

在同一个配置文件中修改：

```toml
[intro_subtitle]
enabled = true
text = "字幕制作：github.com/adadsws"
duration_seconds = 3.0
```

署名会显示 3 秒。如果片头已有字幕，会自动放到后面第一个足够长的字幕间隙；没有合适间隙时不会添加。

请勿公开或提交自己的 DeepSeek API Key。
