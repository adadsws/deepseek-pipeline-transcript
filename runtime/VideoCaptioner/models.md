# FasterWhisper medium 模型

此目录保存 `Systran/faster-whisper-medium` 的 CTranslate2 模型，约 1.43 GiB，因体积不纳入 Git。

来源与关键文件 SHA-256 固定在 [resources.lock.toml](resources.lock.toml)。缺失时运行 [download-models.ps1](download-models.ps1)；脚本使用项目虚拟环境中的 `huggingface_hub` 下载，并验证 `model.bin`。

最小结构：

```text
runtime/VideoCaptioner/models/
└── faster-whisper-medium/
    ├── config.json
    ├── model.bin
    ├── tokenizer.json
    └── vocabulary.txt
```
