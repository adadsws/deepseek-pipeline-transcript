# VideoCaptioner 本地运行封装

本项目在上游 VideoCaptioner 基础上提供本地 GUI 启动方式，以及无需打开 GUI 的批量视频字幕脚本。

## 环境

- Windows
- 项目虚拟环境：`.venv/`
- 固定上游源码：`reference/upstream/VideoCaptioner/`
- 大型运行资源：`runtime/VideoCaptioner/`（不纳入 Git，可按同目录说明重建）
- FFmpeg：复用 `runtime/VideoCaptioner/bin/Faster-Whisper-XXL/ffmpeg.exe`
- 本地识别：FasterWhisper medium / CUDA
- 翻译：DeepSeek `deepseek-flash`

GUI 与批处理共用固定上游、模型、FasterWhisper、FFmpeg 及 `secrets/` 中的 DeepSeek Key；行为配置采用 `shared + mode` 三层 TOML。

## 安装

```powershell
git clone --recurse-submodules https://github.com/adadsws/deepseek-video_transcript-pipeline.git
cd deepseek-video_transcript-pipeline
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\reference\upstream\VideoCaptioner
.\runtime\VideoCaptioner\download-bin.ps1
.\runtime\VideoCaptioner\download-models.ps1
.\.venv\Scripts\python.exe .\scripts\prepare_runtime.py
```

若克隆时未带 `--recurse-submodules`，先运行 `git submodule update --init --recursive`。复制 `secrets/deepseek-api-key.example` 为 `secrets/deepseek-api-key.txt` 并填写自己的 Key；真实 Key 不得提交到公开仓库。

## 配置

`config/` 只保存文本配置：

```text
config/
├── batch/settings.toml
├── gui/settings.toml
├── gui/settings.example.toml
└── shared/settings.toml
```

- 批处理有效配置为 `shared + batch`，GUI 有效配置为 `shared + gui`。
- `batch` 与 `gui` 只保存成对的模式差异；批处理未改写的上游设置只保存于 `shared`。
- 视频源语言和字幕目标语言位于两个模式文件首节 `[language.video]`。
- 只有实际参与运行或校验的项目功能开关保留在配置中，并在 `gui` 对照层关闭；批处理 VAD、DeepSeek 思考模式和请求级自动重试均关闭。
- 真实 Key 只放在 `secrets/`；TOML 只记录 Key 文件路径，生成的 GUI 临时 JSON 不含 Key。

## GUI

双击 `start.bat`。持久设置编辑 `config/gui/settings.toml` 与 `config/shared/settings.toml`；启动器会校验运行资源和 Junction，并在 `~temp/VideoCaptioner/gui/settings.json` 生成上游 QConfig 兼容文件。GUI 内临时修改在下次启动时会由 TOML 重新生成。

## 无界面批处理

1. 打开 `config/batch/settings.toml`。
2. 最常改的 `language.video.source` 与 `language.video.target` 位于文件最前；其他批处理参数已按常用程度分章节列全。
3. 双击 `batch_videos.bat`，第一次直接输入视频文件或目录；扫描显示视频数量和语言后，选择 `[1]`（或直接回车）确认处理，选择 `[0]` 取消。一次处理结束后可返回数字菜单继续处理下一个路径。也可把路径作为参数传入并只处理一次，此模式不询问确认。
4. 若有视频需要重新转录和翻译，确认共享 Key 已保存到 `secrets/deepseek-api-key.txt`。全部视频都已有同名 SRT、只需注入署名时不读取 Key。真实文件在 Git 中必须由 `git-crypt` 加密。

目录输入会被递归扫描。当前配置流程为：关闭 VAD 的 FasterWhisper medium/CUDA 转录 → 连续相同 ASR 文本只保留第一条 → 关闭思考的 DeepSeek 视频级翻译 → 按配置加入署名 → 在每个源视频旁只输出一个 `<视频名>.srt`。公开配置中的 `[intro_subtitle]` 默认关闭、文字为空、时长为 3 秒；启用并填写 `text` 后，首条真实字幕在配置时长或更晚开始时把署名放在片头，否则寻找后续第一个足够长的无字幕间隙，并把署名居中放入该间隙。没有合适间隙时不添加。整轮视频进入同一条端到端滑动流水线：预检、GPU 门禁、转录、源字幕清理、翻译、终检、原子发布和报告依次衔接，不再按固定 4 个视频分组并等待整组完成。调度线程严格单路转录；默认由 `max_in_flight = 5` 限制同时在途视频，下游最多 4 路并发，使 1 个转录视频可与 4 个下游视频重叠。某视频只有在全部阶段成功后才原子创建最终 SRT。每次 DeepSeek 请求都发送该视频清理后的完整字幕，禁止按区间拆分或只补发缺失句；若完整字幕超过 `[translation_batching]` 安全预算，则明确失败，不发送残缺请求。转录中间文件、翻译中间文件和批次报告都不会留在视频目录。每个确实需要转录的视频开始前会按 `[gpu_gate]` 检查 GPU；默认 GPU 0 高于 60% 时每 10 秒检查一次，连续两次不高于阈值后继续。已有同名 SRT 只走署名配置处理，不等待 GPU，也不调用转录或翻译；若 `nvidia-smi` 查询失败则记录警告并继续。

署名配置位于 `config/batch/settings.toml`：

```toml
[intro_subtitle]
enabled = false
text = ""
duration_seconds = 3.0
```

`enabled` 控制是否注入署名，`text` 是显示文字，`duration_seconds` 是完整显示时长。启用时必须填写非空文字。

ASR 清理先删除结束时间早于开始时间、无法可靠恢复边界的损坏 cue，再以 NFKC 统一全半角并忽略空白、标点；字幕序列中规范化文本连续相同的组只保留第一条，不要求时间轴紧邻，也不延长第一条。遇到不同文本后组即结束，之后再次出现相同文本会重新保留。纯标点因没有可比较的字母或数字而不参与删除。不再执行长周期统计，也不合并极短碎片。此功能只在 batch 开启，GUI 对照配置保持关闭。

### 当前处理步骤

1. 解析输入文件或递归扫描目录，只收集配置中的视频扩展名。
2. 若视频旁已有同名 SRT 且未传入 `--overwrite`，校验后原子加入片头署名，不转录或翻译；其他文件名的 SRT 不影响处理。重复运行不会重复添加署名。
3. 每个视频经 GPU 门禁进入唯一的转录工位，严格单路提取音轨并以 FasterWhisper medium/CUDA 转录；VAD 固定关闭。
4. 严格校验原始单语 SRT 的结构、连续序号、时间轴和非空正文，并记录原始 cue 数；释放转录工位，让下一个视频立即进入。
5. 已转录视频在下游工位删除反向时间轴并规范化每条原文；连续相同组保留第一条、删除后项，重新编号后再次严格校验并分别记录两类删除数。该步骤可与下一视频的转录重叠。
6. 将清理后的整片字幕作为不可拆分的完整请求提交给 DeepSeek；首次响应漏键、空译文或 JSON 格式错误时，允许一次携带完整原始输入、首次完整输出和校验错误的修复请求，并要求重新返回完整 JSON。修复仍不合格就立即失败，不发送第三次请求、不局部补译，也不进入轮末重试。超出安全预算直接失败；思考模式、SDK 和传输层自动重试固定关闭。
7. 多视频时由滑动窗口维持最多 5 个在途视频；任一视频完成全部下游阶段后立即补入下一个，不存在固定分组屏障，同一视频内部不并发拆散。
8. 校验双语 SRT 每条都有非空译文和原文、句数与清理后源 SRT 一致。
9. `[intro_subtitle]` 启用且文字非空时，向最终字幕加入完整配置时长的署名：片头空闲时从 0 秒放置；首条真实字幕过早出现时，改放到后续第一个足够长的无字幕间隙中央；没有合适间隙时不添加。随后才在视频旁原子创建同名 SRT，任何阶段失败都不发布部分结果。
10. 唯一处理轮结束后集中列出失败项并生成 JSON/CSV 报告，不提供重试菜单。

CMD 中每个在途视频按输入序号固定占用一个多行动态区域：首行显示唯一进度条和预计剩余时间，后续行显示当前状态及完整视频路径，长路径只在本视频区域内按终端宽度换行。转录、翻译、完成或失败始终只原位刷新该视频区域，不创建重复进度条、改变排列顺序或串到其他视频下方。即使后面的视频先完成，其最终状态也保留在自己的原位置；重定向输出会缓存乱序完成项并按输入顺序写出最终多行快照。详细日志保存到 `~outputs-intermediate/VideoCaptioner/logs/`，实时批次 JSONL 保存到其 `batch-runs/` 子目录。每条事件都会立即刷新到磁盘，进程异常退出后也可定位最后处理文件。每个视频只进入一次完整处理流程，失败不会重新转录或进入视频级重试轮。

DeepSeek 首次返回格式错误、漏键或空译文时不会发送局部补译，而是把完整原始输入、首次完整输出和具体校验错误一并放入唯一一次修复请求，要求重发完整 JSON。修复响应仍不完整时，当前视频归为 `deepseek_incomplete_output`；同批其他视频继续独立校验和发布。

纯标点、连字符和音乐符号等非空 cue 会原样保留；句子数量不设下限，1 句也正常继续处理。0 句、去除标签后无可见正文，或删除反向时间轴后没有剩余 cue 才视为无效。批处理固定关闭 VAD，每个视频只转录一次且不自动重转。无有效 cue 的结果不继续翻译和发布，只记录失败。项目绕过上游 tenacity，并显式设置 OpenAI SDK `max_retries=0`；连接错误、超时、响应体中断、服务错误或 HTTP 200 空内容不会触发修复请求。首次非空响应结构不完整时才允许一次完整上下文修复。批处理的实际请求均绕过上游一小时 LLM 响应缓存，确保结果来自本次处理。纯 `，。` 译文不会再被上游句尾清理变成空串。

SRT 只有在全部字幕块结构合法、序号连续、正文非空、翻译前后句数一致且整个处理流程成功后才会原子发布。批次结束后在项目的 `~outputs-final/batch-reports/` 生成 JSON 和 CSV 报告，其中包含每个视频的 ASR 原始/清理后/最终真实字幕句子数（不含片头署名）、删除的连续重复数与反向时间轴数、耗时、状态、错误类别、DeepSeek 输入/输出 Token、缓存命中情况及按请求发生时段估算的人民币费用。DeepSeek 官方人民币价格快照和来源会一并写入 JSON 报告。

视频旁存在同名 `<视频名>.srt` 且未传入 `--overwrite` 时，只校验并原子注入片头署名；已有字幕内容不会重新转录或翻译。命令行传入 `--overwrite` 可强制重新处理：

```powershell
.\batch_videos.bat "D:\Videos" --overwrite
```

## 测试

```powershell
.\.venv\Scripts\python.exe .\tests\test_headless_batch.py -v
```

完整回归：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```
