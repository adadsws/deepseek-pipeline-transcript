# 媒体检查子工具整理

项目专项：独立检查 CLI 放入 `tools/media_check/`，解码核心仍由 `local_patch/videocaptioner_media_checker.py` 提供，避免与字幕音频回退形成两份实现。

跨项目通用实践：移动 BAT 后同时核对工作目录、解释器路径和模块项目根路径；从不同工作目录以真实入口验证参数、交互和退出码。
