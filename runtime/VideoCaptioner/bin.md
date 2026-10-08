# Faster-Whisper-XXL 运行资源

此目录保存 Windows GPU 版 Faster-Whisper-XXL、随附 FFmpeg 和上游下载归档。内容约 5.83 GiB，因体积不纳入 Git。

来源、版本入口、大小与 SHA-256 固定在 [resources.lock.toml](resources.lock.toml)。缺失时运行 [download-bin.ps1](download-bin.ps1)；脚本先验证下载归档，再调用本机 `7z.exe` 解压。

最小结构：

```text
runtime/VideoCaptioner/bin/
├── Faster-Whisper-XXL/
│   ├── faster-whisper-xxl.exe
│   └── ffmpeg.exe
├── faster-whisper-gpu.7z
├── faster-whisper.exe
└── license.txt
```
