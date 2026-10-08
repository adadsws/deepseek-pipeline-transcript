# 项目上下文

## 结构

- `start.bat`：GUI 入口；通过 `local_patch/sitecustomize.py` 加载项目补丁。
- `batch_videos.bat`：无界面批处理入口。
- `scripts/headless_batch.py`：批量枚举、处理、进度、校验与报告编排。
- `config/batch/settings.toml`、`config/gui/settings.toml`、`config/shared/settings.toml`：三层规范配置；模式层字段成对，未改写项只放 shared。
- `local_patch/videocaptioner_project_config.py`：配置合并、校验、路径解析及 GUI QConfig 临时 JSON 生成。
- `scripts/prepare_runtime.py`：校验资源并维护上游 `AppData/{models,cache,logs}` 与 `resource/bin` Junction。
- `runtime/VideoCaptioner/`：大型运行资源及其锁定清单/重建脚本；真实 bin/models 不纳入 Git。
- `secrets/deepseek-api-key.txt`：GUI 与批处理共用的真实 DeepSeek Key；工作区为明文，Git index/commit 必须是 `git-crypt` 密文。
- `reference/upstream/VideoCaptioner/`：固定的上游源码及共用模型/运行资源。
- `local_patch/`：不直接修改上游代码的本地运行补丁。

## 约定

- GUI 与批处理都读取 shared，再读取自己的模式覆盖层；模式层不得跨读，真实密钥继续独立读取。
- GUI 仅在创建 DeepSeek 任务或测试 DeepSeek 连接时读取共享 Key，不把它写回 `settings.json`。
- 批处理复用上游 `TranscriptThread` 和 `SubtitleThread`，但自行编排唯一一轮端到端滑动流水线。`max_in_flight` 表示最大同时在途视频数，默认 5；不切成固定视频组或设置组间屏障。调度线程同步执行唯一的转录工位，避免无意义的“提交后立即等待”单线程池；视频转录完成并通过原始 SRT 校验后立即把源字幕清理、翻译、终检和发布交给最多 4 路的下游执行器，同时开始下一视频转录，允许 1 个转录视频与 4 个下游视频重叠。单个视频只有全部阶段成功后才原子发布 SRT；失败不暂停、不重新转录或进入视频级重试轮，统一进入最终报告。
- 对外发布 SRT 前使用 `validate_complete_srt` 严格校验全部字幕块、连续序号、非空正文及翻译前后句数；失败属于 `invalid_or_blank_srt`。
- 批处理在上游 LLM JSON 键校验之外追加非空译文校验；首次响应格式错误、漏键或空译文时，携带完整原始输入、首次完整输出及校验错误进行唯一一次完整修复。修复仍不合格则立即失败，不做局部补译、第三次请求或轮末重试。最终双语校验仍作为发布前兜底。
- 源 SRT 保留纯标点、连字符和音乐符号等非空 cue；只有空白或去除标签后无可见正文的 cue 才视为无效。
- 批处理固定关闭 VAD，每个视频只转录一次且不自动重转；源 SRT 为 0 句、无正文，或删除含 `U+FFFD` 的损坏 cue 与反向时间轴后没有剩余 cue 时记录 `invalid_or_blank_srt`，不继续翻译或发布，也不提供轮末人工重试。非零有效句数不设下限。
- 关闭 VAD 后的源 SRT 在翻译前先删除正文含 Unicode 替换字符 `U+FFFD` 的整个 cue 及上游时间优化可能产生的反向时间轴 cue，再执行连续重复清理：以 NFKC 统一全半角并忽略空白、标点，字幕序列中规范化文本连续相同的组只保留第一条，不限制时间间隔、不延长时间轴；遇到不同文本后重新分组。纯标点和零时长 cue 保留，不再执行长周期统计或极短碎片合并。清理前只校验 UTF-8、结构、时间轴与非空正文，允许 `U+FFFD` cue 进入清理；清理后重新编号并再次严格校验。三类删除分别进入报告，batch 功能开关开启，GUI 对照层关闭。
- 翻译完成后直接校验实际双语 SRT，逐句确认原文仍在且其上方存在非空译文；不依赖可能延迟或丢失的 Qt `update_all` 信号快照，任一句缺译都让当前视频立即失败。
- 批处理在每次真实 DeepSeek 响应边界累计 usage；正常视频一次，触发完整修复的视频两次。磁盘缓存命中不计 API 用量，并直接按官方人民币峰/谷价格逐请求估算费用。最终报告同时提供整批总额和最终失败视频小计。
- 批处理使用不可拆分的视频级 DeepSeek 请求并显式关闭思考模式；每一次请求都携带清理后的完整视频字幕，禁止按区间拆分或只补发缺失句。预计超过 120,000 输入或 32,000 输出 token 安全预算时直接失败。GUI 不启用该功能，继续保持上游分块。
- 每个视频只转录一次，不存在视频级自动重试或轮末人工重试。DeepSeek 绕过上游 tenacity，OpenAI SDK 使用 `max_retries=0`；连接错误、超时、响应体中断、服务错误或 HTTP 200 空内容立即失败。仅首次非空响应的格式、键或译文完整性校验失败时允许一次业务级完整修复；第二次请求必须同时携带完整原始输入、首次完整输出和校验错误，并重新返回完整 JSON。所有实际请求都绕过上游一小时响应缓存。
- `local_patch/videocaptioner_translation_resilience_fix.py` 统一负责完整视频预算检查、非思考/JSON 请求参数、首次响应校验、单次完整上下文修复、非空响应与缓存校验，以及防止上游 `remove_punctuation()` 把纯 `，。` 原文或译文清空。
- 最终 SRT 统一通过 `write_srt_with_intro` 读取 `[intro_subtitle]`；公开配置默认 `enabled = false`、`text = ""`、`duration_seconds = 3.0`。启用且文字非空时，首条真实字幕在配置时长或更晚开始便从 0 秒放置；否则按时间顺序寻找第一段足够长的相邻真实字幕间隙，将署名居中插入并保持 cue 时间顺序；没有合适间隙时不添加。函数会先移除已有的同文署名再重新定位，因此重复执行保持幂等，报告句数只统计真实字幕。
- 视频旁存在同名 SRT 且未传入 `--overwrite` 时，不再跳过，而是严格校验并原子注入片头署名；不进入 GPU 门禁、转录或翻译，也不需要 DeepSeek Key。新结果同样采用同目录临时文件原子替换发布。
- 每个需要转录的视频开始前通过 `nvidia-smi` 检查 GPU 0 整体核心利用率；高于 60% 时每 10 秒轮询，连续两次不高于 60% 才继续。查询不可用时记录警告并放行，避免永久阻塞；该门禁不保证处理过程中 GPU 不被其他程序抢占。
- 批处理跳过上游每视频一次且无法取得 usage 的连接探测，首个真实翻译请求同时完成连接与鉴权验证。
- 批处理将所有上游 logger 的控制台处理器静音；每个在途视频持有独立、线程安全的进度句柄，并按输入序号固定占用一个多行终端区域。首行只含批次/文件百分比、唯一进度条和 ETA，后续行显示状态及完整视频路径，长路径在本区域内换行。转录与并发下游更新均按视频路由并只原位刷新所属区域，乱序完成不会改变区域顺序；完成或失败在原区域收尾后保留最终快照。重定向输出无法原位刷新时会缓存后续完成项，等前序完成后按输入顺序写出最终多行快照。上游配置、阶段、警告、错误与堆栈继续完整写入 `AppData/logs/app.log`。
- `local_patch/videocaptioner_logging_fix.py` 将指向同一日志文件的上游 `RotatingFileHandler` 合并为单一共享处理器；Windows 外部进程占用导致轮转失败时延迟 60 秒重试，并继续写入当前日志。
- 每次批处理在 `~outputs-intermediate/VideoCaptioner/logs/batch-runs/` 建立独立 JSONL；视频尝试开始/结束、轮次结束与批次结束事件均立即 `flush` 和 `fsync`，可在无最终报告时恢复最后状态。每个 `round_finished` 同时记录本轮新增 SRT、更新已有 SRT、失败的数量及路径，并另存当前仍失败列表。

## 相对固定上游的项目差异

| 差异 | 保留位置 | 最小化结论 |
|---|---|---|
| 无界面递归批处理、GPU 门禁、单次处理、严格发布和报告 | `scripts/headless_batch.py` | 保持独立编排，不修改上游 GUI 线程 |
| 三层 TOML 与 GUI QConfig 兼容 | `videocaptioner_project_config.py`、`videocaptioner_gui_config_bridge.py` | 原单用途路径补丁已归档，统一为一个配置桥 |
| DeepSeek secret 注入 | `videocaptioner_shared_secret.py` | 临时 JSON 不含 Key，只在实际任务/连接测试时读取 |
| GUI 输出格式修复 | `videocaptioner_output_format_fix.py` | 上游仍会生成多格式，保留最小钩子和回归测试 |
| Windows 日志轮转修复 | `videocaptioner_logging_fix.py` | 与请求日志分责，保持幂等安装 |
| 并发请求/响应日志配对 | `videocaptioner_request_logger_fix.py` | 上游全局队列和任务上下文无法支持多视频并发，保留 HTTP 请求对象关联与线程隔离上下文 |
| 视频级请求、空响应、缓存和纯标点修复 | `videocaptioner_translation_resilience_fix.py` | 成为批处理翻译策略和完整性校验的唯一适配点；不改上游源码 |
| VAD 关闭后的损坏 cue 与连续重复清理 | `videocaptioner_asr_cleanup.py` | 独立纯函数后处理，只由批处理编排调用；删除含 `U+FFFD` 或反向时间轴的 cue，连续相同组保留第一条，不改转录线程或上游源码 |
| 运行资源目录分流 | `scripts/prepare_runtime.py` | 只维护 Junction，不改上游路径常量或源码 |

固定上游目录必须保持 Git clean。每个保留补丁均要求幂等安装和独立回归测试；上游修复对应缺口后再归档项目补丁。
- `local_patch/videocaptioner_request_logger_fix.py` 在 HTTP 响应钩子中按 `response.request` 记录 DeepSeek 请求与响应；响应体读取中断时记录并原样抛出网络异常，禁止访问未读取的 `response.text` 覆盖真正原因。
- 唯一处理轮完成后，CMD 统计新增 SRT、更新已有 SRT 和失败数量，逐条列出新增及更新 SRT 的完整路径和真实字幕句数，再集中显示所有失败视频的完整路径、错误类别和最后错误原因；不显示重试菜单。
- 单行进度按 Unicode 终端列宽清理宽字符残影，并根据整批已完成比例与实际耗时动态估算剩余时间。
- 报告句子数定义为同时具有合法时间轴及非空正文的真实字幕 cue 数，不包含自动片头署名。
- 视频目录只保留与视频同名的最终 `<视频名>.srt`；中间文件使用临时目录，JSON/CSV 报告统一写入项目的 `~outputs-final/batch-reports/`。
- 无参数启动批处理时第一次直接输入路径，扫描并确认数量后才处理；完成后使用持续数字菜单处理后续路径。命令行传入路径时保持不询问确认的一次性模式。

## 可复用测试样例

- 纯本地测试：运行 `python -m unittest discover -s tests -p "test_*.py"`，覆盖枚举、SRT 校验、句子计数、固定配置、错误分类、实时批次日志、并发请求/响应配对、单次处理约束、安全发布及 DeepSeek 峰/谷计价。
- 真实本地转录样例：使用用户提供但不纳入仓库的外部 MP4；已验证约 20 分钟和 51 分钟的视频能够通过共用 FasterWhisper medium/CUDA 生成有效 SRT。
- DeepSeek 冒烟测试：使用一条临时日语 SRT，预期得到一个有效目标语言 cue；不得在日志中显示完整或局部 Key。
- VAD 与清理路线对照样例：对第 3 个视频禁用缓存并保持其他参数一致，`silero_v5` 0.1/0.2 在 25–35 分钟均为 0 条且出现跨几十秒断句，因此继续关闭 VAD。用户最终选择“序列连续相同组保留第一条”：真实端到端运行把 103 条清理为 66 条，删除 37 条，DeepSeek 1 次请求、1,653 token，最终 66 条双语 cue 完整。该规则会删除相隔最长 242 秒但序列连续相同的短句，并让被其他文本打断的主要幻觉保留为多个组，这是已确认接受的行为。
- FasterWhisper 模型对照样例：同一第 3 个视频关闭 VAD、缓存和 DeepSeek，预下载后顺序比较 `tiny/small/medium/large-v3-turbo`。耗时分别为 312.68/194.99/288.04/约 189.82 秒；`tiny` 混语幻觉严重，`small` 原始 202 条中有 127 条连续重复，`large-v3-turbo` 漏掉前 13 分钟大部分对白并输出一个使严格校验失败的 `U+FFFD`。`medium` 的对白连贯性及分时段覆盖最佳，正式配置继续使用 `medium`。详细产物位于 `~outputs-final/model-comparisons/20261002-mdvr00423-3/`。
- 整体流水线真实冒烟样例：从第 3 个视频抽取两个 30 秒音频 MP4，使用真实 `medium`/CUDA、关闭 VAD 和 ASR 缓存，并以本地假译文代替 DeepSeek。两段分别生成 4/2 条有效 SRT；第 1 段下游 20.817–32.819 秒与第 2 段转录 20.893–27.815 秒重叠 6.922 秒。可重建证据位于 `~outputs-final/pipeline-smoke/20261002/result.json`；单元测试以事件强制验证调度线程中的单路转录与下游线程清理真实交错，不依赖线程名称证明流水线存在。
