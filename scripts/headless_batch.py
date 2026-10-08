"""无界面批量转录和翻译视频，并输出进度与错误统计。"""

from __future__ import annotations

import argparse
import ctypes
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PATCH_DIR = PROJECT_ROOT / "local_patch"
if str(LOCAL_PATCH_DIR) not in sys.path:
    sys.path.insert(0, str(LOCAL_PATCH_DIR))

from videocaptioner_project_config import (  # noqa: E402
    MODE_CONFIG_PATHS,
    ProjectConfigError,
    get_value,
    load_mode_settings,
    resolve_project_path,
)
from videocaptioner_asr_cleanup import (  # noqa: E402
    clean_source_srt_file,
    validate_source_srt_file,
)


# 所有运行参数只从三层 TOML 读取；这些模块常量只是本进程的配置快照。
CONFIG_PATH = MODE_CONFIG_PATHS["batch"]
EFFECTIVE_CONFIG = load_mode_settings("batch")
SHARED_ROOT = resolve_project_path(get_value(EFFECTIVE_CONFIG, "paths.upstream_root"))
FASTER_WHISPER_BIN_ROOT = resolve_project_path(
    get_value(EFFECTIVE_CONFIG, "paths.faster_whisper_bin")
)
FASTER_WHISPER_EXECUTABLE = FASTER_WHISPER_BIN_ROOT / get_value(
    EFFECTIVE_CONFIG, "transcription.program"
)
FASTER_WHISPER_DIR = FASTER_WHISPER_EXECUTABLE.parent
SHARED_MODEL_DIR = resolve_project_path(get_value(EFFECTIVE_CONFIG, "paths.models"))
SHARED_FFMPEG_DIR = FASTER_WHISPER_DIR
DEEPSEEK_SECRET_PATH = resolve_project_path(
    get_value(EFFECTIVE_CONFIG, "paths.deepseek_secret")
)


def _configured_project_path(name: str) -> Path:
    """解析运行路径；测试替换 PROJECT_ROOT 时仍保持项目内隔离。"""
    value = Path(str(get_value(EFFECTIVE_CONFIG, f"paths.{name}")))
    return value if value.is_absolute() else PROJECT_ROOT / value

DEEPSEEK_PRICING_SOURCE = get_value(EFFECTIVE_CONFIG, "pricing.deepseek.source")
DEEPSEEK_PRICING_CHECKED_ON = get_value(EFFECTIVE_CONFIG, "pricing.deepseek.checked_on")
DEEPSEEK_PEAK_WEEKDAYS = {
    int(day) - 1 for day in get_value(EFFECTIVE_CONFIG, "pricing.deepseek.peak_weekdays_utc")
}
DEEPSEEK_PEAK_RANGES = [
    (int(start), int(end))
    for start, end in get_value(EFFECTIVE_CONFIG, "pricing.deepseek.peak_ranges_utc")
]
DEEPSEEK_PRICING_CNY_PER_MILLION = {
    period: {
        name: float(get_value(EFFECTIVE_CONFIG, f"pricing.deepseek.{period}.{name}"))
        for name in ("cache_hit_input", "cache_miss_input", "output")
    }
    for period in ("off_peak", "peak")
}

VIDEO_EXTENSIONS = {
    str(item).lower() for item in get_value(EFFECTIVE_CONFIG, "input.video_extensions")
}
GPU_INDEX = int(get_value(EFFECTIVE_CONFIG, "gpu_gate.index"))
GPU_BUSY_THRESHOLD_PERCENT = int(
    get_value(EFFECTIVE_CONFIG, "gpu_gate.busy_threshold_percent")
)
GPU_POLL_INTERVAL_SECONDS = float(
    get_value(EFFECTIVE_CONFIG, "gpu_gate.poll_interval_seconds")
)
GPU_QUERY_TIMEOUT_SECONDS = float(
    get_value(EFFECTIVE_CONFIG, "gpu_gate.query_timeout_seconds")
)
GPU_REQUIRED_LOW_READINGS = int(
    get_value(EFFECTIVE_CONFIG, "gpu_gate.required_low_readings_after_busy")
)
CONSOLE_WRAP_SAFETY_COLUMNS = 8

FIXED_CONFIG = {
    "asr_model": get_value(EFFECTIVE_CONFIG, "transcription.model"),
    "device": get_value(EFFECTIVE_CONFIG, "transcription.device"),
    "vad_filter": get_value(EFFECTIVE_CONFIG, "transcription.vad_filter"),
    "vad_method": get_value(EFFECTIVE_CONFIG, "transcription.vad_method"),
    "vad_threshold": get_value(EFFECTIVE_CONFIG, "transcription.vad_threshold"),
    "voice_extraction": get_value(EFFECTIVE_CONFIG, "transcription.voice_extraction"),
    "one_word": get_value(EFFECTIVE_CONFIG, "transcription.one_word"),
    "word_timestamps": get_value(EFFECTIVE_CONFIG, "transcription.word_timestamps"),
    "audio_track_index": get_value(EFFECTIVE_CONFIG, "transcription.audio_track_index"),
    "prompt": get_value(EFFECTIVE_CONFIG, "transcription.prompt"),
    "api_base": get_value(EFFECTIVE_CONFIG, "translation.api_base"),
    "model": get_value(EFFECTIVE_CONFIG, "translation.model"),
    "reflect": get_value(EFFECTIVE_CONFIG, "translation.reflect"),
    "optimize": get_value(EFFECTIVE_CONFIG, "translation.optimize"),
    "split": get_value(EFFECTIVE_CONFIG, "translation.split"),
    "thread_num": get_value(EFFECTIVE_CONFIG, "translation.thread_num"),
    "batch_size": get_value(EFFECTIVE_CONFIG, "translation.batch_size"),
}
VIDEO_LEVEL_TRANSLATION = bool(
    get_value(EFFECTIVE_CONFIG, "project_features.video_level_translation")
)
PIPELINE_MAX_IN_FLIGHT = int(
    get_value(EFFECTIVE_CONFIG, "translation_batching.max_in_flight")
)
VIDEO_TRANSLATION_CONCURRENCY = int(
    get_value(EFFECTIVE_CONFIG, "translation_batching.video_concurrency")
)
ASR_CLEANUP_ENABLED = bool(
    get_value(EFFECTIVE_CONFIG, "project_features.asr_consecutive_duplicate_cleanup")
)

TIMESTAMP_RE = re.compile(
    r"^\s*(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
    r"(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3}",
)
TIMESTAMP_CAPTURE_RE = re.compile(
    r"^\s*(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})(?P<suffix>.*)$"
)
SECRET_RE = re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|api[_ -]?key\s*[=:]\s*\S+)")
INTRO_SUBTITLE_ENABLED = bool(get_value(EFFECTIVE_CONFIG, "intro_subtitle.enabled"))
INTRO_SUBTITLE_TEXT = str(get_value(EFFECTIVE_CONFIG, "intro_subtitle.text")).strip()
INTRO_SUBTITLE_DURATION_MS = round(
    float(get_value(EFFECTIVE_CONFIG, "intro_subtitle.duration_seconds")) * 1000
)


class ConfigError(ValueError):
    """批处理配置无效。"""


class StageError(RuntimeError):
    """一个处理阶段失败。"""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class BatchConfig:
    source_language: str
    target_language: str


@dataclass
class ErrorEvent:
    category: str
    stage: str
    message: str
    occurred_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class TokenUsage:
    """记录真实 API 响应 usage，并按响应发生时段估算人民币费用。"""

    api_requests: int = 0
    peak_requests: int = 0
    off_peak_requests: int = 0
    input_cache_hit_tokens: int = 0
    input_cache_miss_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_cny: float = 0.0

    def add_response(self, response: object, occurred_at: datetime | None = None) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        hit_tokens = getattr(usage, "prompt_cache_hit_tokens", None)
        if hit_tokens is None:
            details = getattr(usage, "prompt_tokens_details", None)
            hit_tokens = getattr(details, "cached_tokens", 0) if details else 0
        hit_tokens = int(hit_tokens or 0)
        miss_tokens = getattr(usage, "prompt_cache_miss_tokens", None)
        miss_tokens = int(miss_tokens if miss_tokens is not None else max(prompt_tokens - hit_tokens, 0))
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)

        period = deepseek_pricing_period(occurred_at or datetime.now().astimezone())
        rates = DEEPSEEK_PRICING_CNY_PER_MILLION[period]
        cost_cny = (
            hit_tokens * rates["cache_hit_input"]
            + miss_tokens * rates["cache_miss_input"]
            + output_tokens * rates["output"]
        ) / 1_000_000

        self.api_requests += 1
        if period == "peak":
            self.peak_requests += 1
        else:
            self.off_peak_requests += 1
        self.input_cache_hit_tokens += hit_tokens
        self.input_cache_miss_tokens += miss_tokens
        self.output_tokens += output_tokens
        self.total_tokens += total_tokens or hit_tokens + miss_tokens + output_tokens
        self.estimated_cost_cny = round(self.estimated_cost_cny + cost_cny, 10)

    def merge(self, other: "TokenUsage") -> None:
        for name in (
            "api_requests", "peak_requests", "off_peak_requests",
            "input_cache_hit_tokens", "input_cache_miss_tokens",
            "output_tokens", "total_tokens",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.estimated_cost_cny = round(
            self.estimated_cost_cny + other.estimated_cost_cny, 10
        )


def deepseek_pricing_period(occurred_at: datetime) -> str:
    """按配置中的 UTC 工作日和时段判断峰/谷价格。"""
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.astimezone()
    utc_time = occurred_at.astimezone(timezone.utc)
    is_peak_hour = any(start <= utc_time.hour < end for start, end in DEEPSEEK_PEAK_RANGES)
    return "peak" if utc_time.weekday() in DEEPSEEK_PEAK_WEEKDAYS and is_peak_hour else "off_peak"


@dataclass
class VideoResult:
    video: str
    output: str = ""
    status: str = "error"
    raw_source_sentence_count: int = 0
    source_sentence_count: int = 0
    final_sentence_count: int = 0
    asr_removed_consecutive_duplicates: int = 0
    asr_removed_invalid_timelines: int = 0
    asr_removed_invalid_encoding_cues: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    errors: list[ErrorEvent] = field(default_factory=list)


@dataclass
class PreparedVideo:
    """已完成单路转录、等待进入清理到发布下游工位的视频。"""

    video: Path
    config: BatchConfig
    result: VideoResult
    started: float
    temp_owner: object
    source_srt: Path
    translated_srt: Path
    destination: Path


class _SilentProgress:
    """并发翻译阶段避免多个工作线程互相覆盖控制台光标。"""

    @staticmethod
    def update(_value: int, _message: str) -> None:
        return


class BatchEventJournal:
    """逐事件持久化批次状态，异常退出后仍可定位最后一个视频和错误。"""

    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        self.path = _configured_project_path("batch_journal") / f"batch-{batch_id}.jsonl"
        self._lock = threading.Lock()

    def write(self, event: str, **details: object) -> None:
        payload = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "batch_id": self.batch_id,
            "event": event,
            **details,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            logging.getLogger(__name__).warning("实时批次日志写入失败: %s", exc)


def load_batch_config(path: Path = CONFIG_PATH) -> BatchConfig:
    """读取 shared + batch，并返回批处理频繁使用的语言配置。"""
    try:
        raw = load_mode_settings("batch", mode_path=path)
    except ProjectConfigError as exc:
        raise ConfigError(str(exc)) from exc
    source = str(get_value(raw, "language.video.source", "")).strip()
    target = str(get_value(raw, "language.video.target", "")).strip()
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2,8})?", source):
        raise ConfigError(f"无效的 source_language: {source!r}")
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2,8})?", target):
        raise ConfigError(f"无效的 target_language: {target!r}")

    return BatchConfig(source_language=source.lower(), target_language=_normalise_lang_code(target))


def _normalise_lang_code(code: str) -> str:
    parts = code.split("-", 1)
    return parts[0].lower() if len(parts) == 1 else f"{parts[0].lower()}-{parts[1].title()}"


def discover_videos(path: Path) -> list[Path]:
    """按稳定顺序枚举单个视频或目录下的全部视频。"""
    path = path.expanduser().resolve(strict=False)
    if path.is_file():
        return [path] if path.suffix.lower() in VIDEO_EXTENSIONS else []
    if not path.is_dir():
        raise FileNotFoundError(f"路径不存在: {path}")
    return sorted(
        (item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda item: str(item).casefold(),
    )


def count_srt_cues(path: Path) -> int:
    """统计同时具有合法时间轴和非空正文的 SRT cue 数。"""
    if not path.is_file() or path.stat().st_size == 0:
        return 0
    text = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    count = 0
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines()]
        timeline_index = next((i for i, line in enumerate(lines) if TIMESTAMP_RE.match(line)), None)
        if timeline_index is None:
            continue
        body = [line for line in lines[timeline_index + 1 :] if line]
        if _has_subtitle_text(body):
            count += 1
    return count


def _has_subtitle_text(lines: Iterable[str]) -> bool:
    """正文去除标签后仍有可见字符；保留纯标点和音乐符号等有效字幕。"""
    text = "\n".join(re.sub(r"<[^>]+>", "", line) for line in lines)
    return bool(text.strip())


def _read_complete_srt_entries(path: Path) -> list[tuple[str, list[str]]]:
    """读取并严格校验全部 SRT 块，保留时间轴和非空正文行。"""
    if not path.is_file() or path.stat().st_size == 0:
        raise StageError("validate", "SRT 为空白")
    text = path.read_text(encoding="utf-8-sig", errors="strict").replace("\r\n", "\n")
    if "\ufffd" in text:
        raise StageError("validate", "SRT 包含无效编码字符")
    blocks = re.split(r"\n\s*\n", text.strip())
    entries: list[tuple[str, list[str]]] = []
    for expected_index, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines()]
        if len(lines) < 3 or lines[0] != str(expected_index):
            raise StageError("validate", f"SRT 第 {expected_index} 条序号缺失或不连续")
        if not TIMESTAMP_RE.match(lines[1]):
            raise StageError("validate", f"SRT 第 {expected_index} 条时间轴无效")
        body = [line for line in lines[2:] if line]
        if not body or not _has_subtitle_text(body):
            raise StageError("validate", f"SRT 第 {expected_index} 条正文为空")
        entries.append((lines[1], body))
    return entries


def _read_complete_srt_bodies(path: Path) -> list[list[str]]:
    """读取并严格校验全部 SRT 块，返回每条字幕的非空正文行。"""
    return [body for _timeline, body in _read_complete_srt_entries(path)]


def _timestamp_to_milliseconds(value: str) -> int:
    """将 SRT 时间戳转换为毫秒，兼容省略小时及小数点分隔符。"""
    clock, milliseconds = re.split(r"[,.]", value.strip(), maxsplit=1)
    parts = [int(part) for part in clock.split(":")]
    if len(parts) == 2:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(milliseconds)


def _format_srt_timestamp(milliseconds: int) -> str:
    """使用标准 SRT 格式输出非负毫秒时间。"""
    milliseconds = max(0, milliseconds)
    seconds, millis = divmod(milliseconds, 1000)
    minutes, secs = divmod(seconds, 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def _timeline_bounds_milliseconds(timeline: str, cue_index: int) -> tuple[int, int]:
    """解析单条 SRT 时间轴，供署名间隙定位复用。"""
    match = TIMESTAMP_CAPTURE_RE.match(timeline)
    if match is None:
        raise StageError("validate", f"SRT 第 {cue_index} 条时间轴无法解析")
    return (
        _timestamp_to_milliseconds(match.group("start")),
        _timestamp_to_milliseconds(match.group("end")),
    )


def write_srt_with_intro(source: Path, output: Path) -> tuple[int, bool]:
    """优先在片头加入配置署名；片头被占用时改放到首个合格空隙中央。"""
    entries = _read_complete_srt_entries(source)
    real_entries = [
        (timeline, body)
        for timeline, body in entries
        if not INTRO_SUBTITLE_TEXT
        or "\n".join(body).strip() != INTRO_SUBTITLE_TEXT
    ]
    intro_start_ms: int | None = (
        0
        if INTRO_SUBTITLE_ENABLED
        and INTRO_SUBTITLE_TEXT
        and INTRO_SUBTITLE_DURATION_MS > 0
        else None
    )
    insert_index = 0
    bounds = [
        _timeline_bounds_milliseconds(timeline, index)
        for index, (timeline, _body) in enumerate(real_entries, start=1)
    ]
    if intro_start_ms is not None and bounds and bounds[0][0] < INTRO_SUBTITLE_DURATION_MS:
        intro_start_ms = None
        for index, ((_, previous_end), (next_start, _)) in enumerate(
            zip(bounds, bounds[1:]), start=1
        ):
            gap_duration = next_start - previous_end
            if gap_duration < INTRO_SUBTITLE_DURATION_MS:
                continue
            intro_start_ms = previous_end + (
                gap_duration - INTRO_SUBTITLE_DURATION_MS
            ) // 2
            insert_index = index
            break

    output_entries = list(real_entries)
    if intro_start_ms is not None:
        intro_end_ms = intro_start_ms + INTRO_SUBTITLE_DURATION_MS
        output_entries.insert(
            insert_index,
            (
                f"{_format_srt_timestamp(intro_start_ms)} --> "
                f"{_format_srt_timestamp(intro_end_ms)}",
                [INTRO_SUBTITLE_TEXT],
            ),
        )
    if not output_entries:
        raise StageError("validate", "SRT 没有可发布的字幕")

    content = "\n\n".join(
        f"{index}\n{timeline}\n" + "\n".join(body)
        for index, (timeline, body) in enumerate(output_entries, start=1)
    )
    output.write_text(content + "\n", encoding="utf-8", newline="\n")
    return len(real_entries), intro_start_ms is not None


def validate_complete_srt(path: Path) -> int:
    """严格校验每个 SRT 块，拒绝空白、乱码、断号或混入损坏条目的文件。"""
    return len(_read_complete_srt_bodies(path))


def _normalise_original_lines(lines: list[str]) -> list[str]:
    """复现上游发布前仅移除末尾中文逗号和句号的处理。"""
    normalised = [line.strip() for line in lines if line.strip()]
    if normalised:
        normalised[-1] = re.sub(r"[，。]+$", "", normalised[-1]).strip()
    return normalised


def validate_bilingual_output(source_srt: Path, translated_srt: Path) -> int:
    """以实际双语 SRT 为准，确认每条原文仍在且其上方存在非空译文。"""
    source_bodies = _read_complete_srt_bodies(source_srt)
    translated_bodies = _read_complete_srt_bodies(translated_srt)
    if len(translated_bodies) != len(source_bodies):
        raise StageError(
            "translate",
            f"DeepSeek 翻译前后句数不一致：{len(source_bodies)} → {len(translated_bodies)}",
        )
    missing: list[int] = []
    altered: list[int] = []
    for index, (source_lines, output_lines) in enumerate(
        zip(source_bodies, translated_bodies), start=1
    ):
        original = _normalise_original_lines(source_lines)
        output = [line.strip() for line in output_lines if line.strip()]
        if not original or len(output) < len(original) or output[-len(original) :] != original:
            altered.append(index)
            continue
        translation = output[: -len(original)]
        if not translation or not any(line.strip() for line in translation):
            missing.append(index)
    if altered:
        preview = ",".join(map(str, altered[:10]))
        raise StageError("translate", f"DeepSeek 双语结果中的原文不匹配：第 {preview} 句")
    if missing:
        preview = ",".join(map(str, missing[:10]))
        raise StageError(
            "translate",
            f"DeepSeek 翻译部分失败：{len(missing)}/{len(source_bodies)} 句缺少译文（第 {preview} 句）",
        )
    return len(translated_bodies)


def _publish_srt_atomically(source: Path, destination: Path) -> None:
    """在目标目录内先写临时文件，再原子替换正式 SRT。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def classify_error(stage: str, message: str) -> str:
    """将错误归入稳定、可统计的类别。"""
    text = message.casefold()
    if stage == "translate" or "deepseek" in text:
        if any(token in text for token in ("401", "403", "unauthorized", "forbidden", "api key", "api_key", "鉴权", "密钥")):
            return "deepseek_auth"
        if any(token in text for token in ("429", "rate limit", "quota", "限流", "余额", "insufficient")):
            return "deepseek_rate_limit"
        if any(token in text for token in ("connection", "connecterror", "timeout", "timed out", "proxy", "10061", "网络", "连接")):
            return "deepseek_connection"
        if any(token in text for token in (
            "缺少译文", "原文不匹配", "翻译前后句数不一致", "未生成 srt",
            "missing keys", "empty translations", "response validation failed",
        )):
            return "deepseek_incomplete_output"
        return "deepseek_server"
    if "ffmpeg" in text or "音频转换" in text:
        return "ffmpeg"
    if stage == "transcribe" or any(token in text for token in ("whisper", "转录", "cuda")):
        return "asr"
    if stage == "input":
        return "input"
    if stage == "validate":
        return "invalid_or_blank_srt"
    return "unknown"


def sanitise_error(message: str) -> str:
    """生成适合报告的单行、脱敏错误摘要。"""
    clean = SECRET_RE.sub("***", " ".join(str(message).split()))
    return clean[:500]


def _record_failure(result: VideoResult, exc: Exception, progress: object) -> None:
    """统一记录阶段失败，保证分类、脱敏和进度状态一致。"""
    stage = exc.stage if isinstance(exc, StageError) else "unknown"
    category = classify_error(stage, str(exc))
    result.errors.append(ErrorEvent(category, stage, sanitise_error(str(exc))))
    result.status = "error"
    progress.update(100, f"失败：{category}")


def _empty_translation_keys(response_dict: object, reflect: bool = False) -> list[str]:
    """找出 LLM JSON 响应中存在键但译文为空的条目。"""
    if not isinstance(response_dict, dict):
        return []
    empty: list[str] = []
    for key, value in response_dict.items():
        candidate = value.get("native_translation") if reflect and isinstance(value, dict) else value
        if candidate is None or not str(candidate).strip():
            empty.append(str(key))
    return empty


def read_deepseek_key(path: Path = DEEPSEEK_SECRET_PATH) -> str:
    """读取 GUI 与批处理共用的 DeepSeek 密钥，不记录其内容。"""
    try:
        key = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"缺少共享 DeepSeek Key: {path}") from exc
    if not key:
        raise ConfigError(f"共享 DeepSeek Key 为空: {path}")
    return key


def _display_width(text: str) -> int:
    """保守计算 Windows 终端列宽，避免模糊宽字符触发隐式换行。"""
    character_widths: list[int] = []
    for char in text:
        if char == "\ufe0f":
            if character_widths:
                character_widths[-1] = max(character_widths[-1], 2)
            continue
        if unicodedata.combining(char) or unicodedata.category(char) in {"Cf", "Me", "Mn"}:
            continue
        # CMD/Windows Terminal 会随字体把 East Asian Ambiguous 字符渲染为双列。
        # 按双列保守计算只会提前换行，不会让 ANSI 光标少回退一行。
        character_widths.append(
            2 if unicodedata.east_asian_width(char) in {"A", "F", "W"} else 1
        )
    return sum(character_widths)


def _truncate_display_tail(text: str, max_width: int) -> str:
    """从末尾保留不超过指定终端列宽的文件名。"""
    if _display_width(text) <= max_width:
        return text
    suffix: list[str] = []
    used = 3
    for char in reversed(text):
        char_width = _display_width(char)
        if used + char_width > max_width:
            break
        suffix.append(char)
        used += char_width
    return "..." + "".join(reversed(suffix))


def _wrap_display_text(text: str, max_width: int) -> list[str]:
    """按终端显示列宽分行，保证宽字符和完整路径都不被截断。"""
    max_width = max(1, max_width)
    lines: list[str] = []
    current: list[str] = []
    current_width = 0
    for char in text:
        char_width = _display_width(char)
        if current and current_width + char_width > max_width:
            lines.append("".join(current))
            current = []
            current_width = 0
        current.append(char)
        current_width += char_width
    if current or not lines:
        lines.append("".join(current))
    return lines


def _format_remaining(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _progress_detail_label(message: str) -> str:
    """将上游详情压缩为路径前的阶段或最终状态。"""
    if message.startswith("转录"):
        return "当前转录："
    if message.startswith("翻译"):
        return "当前翻译："
    if message.startswith("已有同名 SRT 已加入署名"):
        count = re.search(r"（(\d+) 句）", message)
        return f"已有 SRT 已加入署名（{count.group(1)} 句）" if count else "已有 SRT 已加入署名"
    if message.startswith(("完成", "失败")):
        return message
    if message == "校验并发布":
        return "当前校验并发布："
    if message == "准备":
        return "准备："
    if message == "GPU 可用":
        return "GPU 可用："
    if message.startswith(("等待 GPU", "确认 GPU 空闲")):
        return f"{message}："
    return f"当前状态：{message}"


def _enable_console_cursor_rewrite() -> bool:
    """为交互终端启用 ANSI 光标控制；重定向输出保持纯文本。"""
    if os.name != "nt":
        return bool(getattr(sys.stdout, "isatty", lambda: False)())
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [ctypes.c_uint32]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetConsoleMode.restype = ctypes.c_int
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetConsoleMode.restype = ctypes.c_int
        handle = kernel32.GetStdHandle(ctypes.c_uint32(-11 & 0xFFFFFFFF))
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def _query_gpu_utilization() -> int | None:
    """读取配置 GPU 的整体核心利用率；不可用时返回 None。"""
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "-i",
                str(GPU_INDEX),
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=GPU_QUERY_TIMEOUT_SECONDS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logging.warning("无法查询 GPU 占用率，将继续处理：%s", exc)
        return None

    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        logging.warning("无法查询 GPU 占用率，将继续处理：%s", detail)
        return None

    first_line = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), "")
    try:
        return max(0, min(100, int(float(first_line))))
    except ValueError:
        logging.warning("无法解析 GPU 占用率，将继续处理：%r", first_line)
        return None


def _wait_for_gpu(
    progress: "ConsoleProgress",
    threshold: int = GPU_BUSY_THRESHOLD_PERCENT,
    poll_interval: float = GPU_POLL_INTERVAL_SECONDS,
) -> None:
    """GPU 过忙时等待；开始等待后需连续两次读数不高于阈值。"""
    if not get_value(EFFECTIVE_CONFIG, "gpu_gate.enabled"):
        return
    has_waited = False
    low_samples = 0
    while True:
        utilization = _query_gpu_utilization()
        if utilization is None:
            return
        if utilization <= threshold:
            if not has_waited:
                return
            low_samples += 1
            if low_samples >= GPU_REQUIRED_LOW_READINGS:
                progress.update(0, "GPU 可用")
                return
            progress.update(0, f"确认 GPU 空闲（{utilization}%）")
        else:
            has_waited = True
            low_samples = 0
            progress.update(0, f"等待 GPU（{utilization}%）")
        time.sleep(poll_interval)


@dataclass
class _ProgressState:
    """保存单个视频的独立显示状态。"""

    index: int
    video: str
    percent: int = 0
    message: str = "准备"
    settled: bool = False
    counts_toward_eta: bool = True
    columns: int = 100
    rendered_lines: list[str] = field(default_factory=list)


class _VideoProgress:
    """把并发阶段的更新固定路由到所属视频。"""

    def __init__(self, owner: "ConsoleProgress", index: int):
        self._owner = owner
        self._index = index

    def update(self, percent: int, message: str) -> None:
        self._owner._update_video(self._index, percent, message)

    def finish_line(self) -> None:
        self._owner._settle_video(self._index)


class ConsoleProgress:
    """在普通 CMD 中为每个视频原位维护固定单行进度区域。"""

    def __init__(self, total: int, eta_videos: set[str] | None = None):
        self.total = max(total, 1)
        self.index = 0
        self.video = ""
        self._rendered_lines = 0
        self._cursor_rewrite = _enable_console_cursor_rewrite()
        self._eta_videos = eta_videos
        self._eta_total = self.total if eta_videos is None else len(eta_videos)
        self._eta_started_at: float | None = None
        self._eta_completed = 0
        self._states: dict[int, _ProgressState] = {}
        self._current: _VideoProgress | None = None
        self._lock = threading.RLock()

    def begin(self, index: int, video: Path) -> _VideoProgress:
        with self._lock:
            self.index = index
            self.video = str(video)
            columns = shutil.get_terminal_size(fallback=(100, 24)).columns
            counts_toward_eta = (
                self._eta_videos is None or self.video in self._eta_videos
            )
            if counts_toward_eta and self._eta_started_at is None:
                self._eta_started_at = time.monotonic()
            self._states[index] = _ProgressState(
                index=index,
                video=self.video,
                counts_toward_eta=counts_toward_eta,
                columns=columns,
            )
            handle = _VideoProgress(self, index)
            self._current = handle
            state = self._states[index]
            state.rendered_lines = self._render_lines(state)
            if self._cursor_rewrite:
                prefix = "" if self._rendered_lines == 0 else "\n\x1b[2K"
                sys.stdout.write(prefix + "\n".join(state.rendered_lines) + "\r")
                sys.stdout.flush()
                self._rendered_lines += len(state.rendered_lines)
            return handle

    def _progress_line(self, state: _ProgressState) -> str:
        percent = state.percent
        active_completed = sum(
            item.percent / 100
            for item in self._states.values()
            if item.counts_toward_eta
        )
        completed = self._eta_completed + active_completed
        elapsed = (
            0.0
            if self._eta_started_at is None
            else time.monotonic() - self._eta_started_at
        )
        if self._eta_total <= 0 or completed <= 0 or elapsed < 3:
            remaining = "--:--"
        else:
            remaining = _format_remaining(
                elapsed * max(0.0, self._eta_total - completed) / completed
            )
        return (
            f"[{state.index:>{len(str(self.total))}}/{self.total}] "
            f"{percent:3d}% | 剩余 {remaining}"
        )

    def _detail_lines(self, state: _ProgressState) -> list[str]:
        """为重定向输出保留状态和完整路径，长路径使用同一缩进续行。"""
        indent = "    "
        usable_width = max(
            1,
            state.columns
            - _display_width(indent)
            - CONSOLE_WRAP_SAFETY_COLUMNS,
        )
        label = _truncate_display_tail(
            _progress_detail_label(state.message).rstrip("："),
            usable_width,
        )
        path_lines = _wrap_display_text(state.video, usable_width)
        return [indent + label, *(indent + line for line in path_lines)]

    def _render_lines(self, state: _ProgressState) -> list[str]:
        progress_line = self._progress_line(state)
        if not self._cursor_rewrite:
            return [progress_line, *self._detail_lines(state)]

        # 并发原位刷新只能依赖实际终端行数；完整长路径和字体相关的
        # Unicode 宽度会让 CMD 隐式折行，使后续刷新覆盖其他视频。
        # 交互终端因此固定为一行，最终汇总和报告仍提供完整路径。
        safe_width = max(1, state.columns - CONSOLE_WRAP_SAFETY_COLUMNS)
        separator = " | "
        label = _progress_detail_label(state.message).rstrip("：")
        fixed = progress_line + separator + label
        path_budget = safe_width - _display_width(fixed + separator)
        if path_budget <= 3:
            return [fixed]
        path = _truncate_display_tail(state.video, path_budget)
        return [fixed + separator + path]

    def _update_video(self, index: int, percent: int, message: str) -> None:
        percent = max(0, min(int(percent), 100))
        with self._lock:
            state = self._states.get(index)
            if state is None:
                return
            if state.percent == percent and state.message == message:
                return
            state.percent = percent
            state.message = message
            self.index = state.index
            self.video = state.video
            rendered_lines = self._render_lines(state)
            if rendered_lines == state.rendered_lines:
                return
            old_line_count = len(state.rendered_lines)
            if len(rendered_lines) != old_line_count:
                raise RuntimeError("视频进度区域高度在运行中发生变化")
            state.rendered_lines = rendered_lines
            if self._cursor_rewrite:
                position = list(self._states).index(index)
                following = list(self._states.values())[position + 1 :]
                distance = sum(len(item.rendered_lines) for item in following)
                up = distance + old_line_count - 1
                output = ["\r"]
                if up:
                    output.append(f"\x1b[{up}A")
                for line_index, line in enumerate(rendered_lines):
                    output.extend(("\x1b[2K", line))
                    if line_index < len(rendered_lines) - 1:
                        output.append("\r\n")
                output.append("\r")
                if distance:
                    output.append(f"\x1b[{distance}B")
                sys.stdout.write("".join(output))
                sys.stdout.flush()

    def update(self, percent: int, message: str) -> None:
        """兼容单视频调用；并发流水线应使用 begin() 返回的句柄。"""
        with self._lock:
            if self._current is None:
                self.begin(self.index, Path(self.video))
            current = self._current
            assert current is not None
        current.update(percent, message)

    def _settle_video(self, index: int) -> None:
        with self._lock:
            state = self._states.get(index)
            if state is None:
                return
            if not self._cursor_rewrite:
                state.settled = True
                # 重定向输出无法原位刷新；缓存后续完成项，始终按输入顺序落盘。
                while self._states and next(iter(self._states.values())).settled:
                    first_key = next(iter(self._states))
                    settled_state = self._states.pop(first_key)
                    print("\n".join(settled_state.rendered_lines))
                    if settled_state.counts_toward_eta:
                        self._eta_completed += 1
            else:
                state.settled = True
                while self._states and next(iter(self._states.values())).settled:
                    settled_state = self._states.pop(next(iter(self._states)))
                    if settled_state.counts_toward_eta:
                        self._eta_completed += 1
                    self._rendered_lines -= len(settled_state.rendered_lines)
                if not self._states:
                    sys.stdout.write("\n")
                    sys.stdout.flush()

    def finish_line(self) -> None:
        """结束当前状态区；正式流水线结束时由 close() 一次性收尾。"""
        with self._lock:
            if self._current is not None:
                self._current.finish_line()
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._cursor_rewrite and self._rendered_lines:
                sys.stdout.write("\n\n")
                sys.stdout.flush()
            self._rendered_lines = 0
            self._states.clear()
            self._current = None


def _target_language(code: str):
    from videocaptioner.core.translate.types import BING_LANG_MAP, TargetLanguage

    matches = [language for language, mapped in BING_LANG_MAP.items() if mapped.casefold() == code.casefold()]
    if not matches:
        valid = ", ".join(sorted(set(BING_LANG_MAP.values()), key=str.casefold))
        raise ConfigError(f"不支持的 target_language {code!r}；可用值: {valid}")
    generic = next((item for item in matches if item.name in {"ENGLISH", "SPANISH", "PORTUGUESE"}), None)
    return generic or matches[0]


def _validate_source_language(code: str) -> None:
    from videocaptioner.core.entities import LANGUAGES

    supported = {value for value in LANGUAGES.values() if value}
    if code not in supported:
        valid = ", ".join(sorted(supported))
        raise ConfigError(f"不支持的 source_language {code!r}；可用值: {valid}")


def _prepare_shared_runtime() -> None:
    """把共用的 FasterWhisper 二进制加入本次批处理 PATH。"""
    from prepare_runtime import ensure_runtime_layout

    try:
        ensure_runtime_layout("batch")
    except ProjectConfigError as exc:
        raise ConfigError(str(exc)) from exc
    executable = FASTER_WHISPER_EXECUTABLE
    if not executable.is_file():
        raise ConfigError(f"缺少共用 FasterWhisper: {executable}")
    if not SHARED_MODEL_DIR.is_dir():
        raise ConfigError(f"缺少共用模型目录: {SHARED_MODEL_DIR}")
    shared_bins = os.pathsep.join((str(SHARED_FFMPEG_DIR), str(FASTER_WHISPER_DIR)))
    os.environ["PATH"] = shared_bins + os.pathsep + os.environ.get("PATH", "")
    local_patch = str(LOCAL_PATCH_DIR)
    if local_patch not in sys.path:
        sys.path.insert(0, local_patch)
    from videocaptioner_logging_fix import install as install_logging_fix
    from videocaptioner_request_logger_fix import install as install_request_logger_fix
    from videocaptioner_translation_resilience_fix import (
        install as install_translation_resilience_fix,
    )

    install_logging_fix()
    install_request_logger_fix()
    install_translation_resilience_fix()
    from videocaptioner.core.utils import cache as cache_utils

    if get_value(EFFECTIVE_CONFIG, "cache.enabled"):
        cache_utils.enable_cache()
    else:
        cache_utils.disable_cache()


def _silence_upstream_console(loggers: Iterable[logging.Logger] | None = None) -> None:
    """隐藏上游控制台日志，保留所有文件日志和脚本自身的进度/错误摘要。"""
    active_loggers = loggers
    if active_loggers is None:
        active_loggers = (
            item
            for item in logging.Logger.manager.loggerDict.values()
            if isinstance(item, logging.Logger)
        )
    for logger in active_loggers:
        for handler in logger.handlers:
            is_console = isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            )
            if is_console:
                handler.setLevel(logging.CRITICAL + 1)


def _run_transcription(
    video: Path,
    output: Path,
    config: BatchConfig,
    progress: Callable[[int, str], None],
) -> None:
    from videocaptioner.core.entities import (
        FasterWhisperModelEnum,
        TranscribeConfig,
        TranscribeModelEnum,
        TranscribeOutputFormatEnum,
        TranscribeTask,
        VadMethodEnum,
    )
    from videocaptioner.ui.thread.transcript_thread import TranscriptThread

    _silence_upstream_console()

    task = TranscribeTask(
        file_path=str(video),
        output_path=str(output),
        transcribe_config=TranscribeConfig(
            transcribe_model=TranscribeModelEnum.FASTER_WHISPER,
            transcribe_language=config.source_language,
            need_word_time_stamp=FIXED_CONFIG["word_timestamps"],
            output_format=TranscribeOutputFormatEnum.SRT,
            faster_whisper_program=str(FASTER_WHISPER_EXECUTABLE),
            faster_whisper_model=FasterWhisperModelEnum(FIXED_CONFIG["asr_model"]),
            faster_whisper_model_dir=str(SHARED_MODEL_DIR),
            faster_whisper_device=FIXED_CONFIG["device"],
            faster_whisper_vad_filter=FIXED_CONFIG["vad_filter"],
            faster_whisper_vad_threshold=FIXED_CONFIG["vad_threshold"],
            faster_whisper_vad_method=VadMethodEnum(FIXED_CONFIG["vad_method"]),
            faster_whisper_ff_mdx_kim2=FIXED_CONFIG["voice_extraction"],
            faster_whisper_one_word=FIXED_CONFIG["one_word"],
            faster_whisper_prompt=FIXED_CONFIG["prompt"],
        ),
        selected_audio_track_index=FIXED_CONFIG["audio_track_index"],
        need_next_task=False,
    )
    error_messages: list[str] = []
    finished: list[bool] = []
    thread = TranscriptThread(task)
    thread.progress.connect(progress)
    thread.error.connect(error_messages.append)
    thread.finished.connect(lambda _task: finished.append(True))
    thread.run()
    if error_messages:
        raise StageError("transcribe", error_messages[-1])
    if not finished or not output.is_file():
        raise StageError("transcribe", "转录未生成 SRT")


def _run_translation(
    video: Path,
    source_srt: Path,
    output_srt: Path,
    config: BatchConfig,
    api_key: str,
    progress: Callable[[int, str], None],
    token_usage: TokenUsage,
) -> None:
    from videocaptioner.core.entities import (
        SubtitleConfig,
        SubtitleLayoutEnum,
        SubtitleTask,
        TranslatorServiceEnum,
    )
    from videocaptioner.ui.thread.subtitle_thread import SubtitleThread
    from videocaptioner_translation_resilience_fix import capture_llm_usage

    _silence_upstream_console()

    subtitle_config = SubtitleConfig(
        base_url=FIXED_CONFIG["api_base"],
        api_key=api_key,
        llm_model=FIXED_CONFIG["model"],
        translator_service=TranslatorServiceEnum.OPENAI,
        need_translate=get_value(EFFECTIVE_CONFIG, "translation.enabled"),
        need_optimize=FIXED_CONFIG["optimize"],
        need_reflect=FIXED_CONFIG["reflect"],
        thread_num=FIXED_CONFIG["thread_num"],
        batch_size=FIXED_CONFIG["batch_size"],
        subtitle_layout=SubtitleLayoutEnum.TRANSLATE_ON_TOP,
        need_split=FIXED_CONFIG["split"],
        target_language=_target_language(config.target_language),
        custom_prompt_text=get_value(EFFECTIVE_CONFIG, "translation.custom_prompt"),
        max_word_count_cjk=get_value(EFFECTIVE_CONFIG, "translation.max_words_cjk"),
        max_word_count_english=get_value(EFFECTIVE_CONFIG, "translation.max_words_english"),
    )
    # 上游 print_config 会显示密钥首尾字符；批处理要求控制台完全不输出密钥片段。
    subtitle_config.print_config = lambda: (  # type: ignore[method-assign]
        "=========== Batch Subtitle Task ===========\n"
        f"API Base: {FIXED_CONFIG['api_base']}\n"
        f"Model: {FIXED_CONFIG['model']}\n"
        f"Target: {config.target_language}\n"
        "API Key: [omitted]\n"
        "=========================================="
    )

    task = SubtitleTask(
        subtitle_path=str(source_srt),
        video_path=str(video),
        output_path=str(output_srt),
        subtitle_config=subtitle_config,
        need_next_task=False,
    )
    error_messages: list[str] = []
    finished: list[bool] = []
    thread = SubtitleThread(task)
    # 上游每个任务会额外发送一次无法取得 usage 的 Hello 探测请求。批处理直接设置
    # 已校验过的配置，由首个真实翻译请求同时承担连接与鉴权验证。
    os.environ["OPENAI_BASE_URL"] = FIXED_CONFIG["api_base"]
    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["VIDEOCAPTIONER_EXECUTION_MODE"] = "batch"
    thread._setup_llm_config = lambda: subtitle_config  # type: ignore[method-assign]
    thread.progress.connect(progress)
    thread.error.connect(error_messages.append)
    thread.finished.connect(lambda _video, _subtitle: finished.append(True))
    with capture_llm_usage(token_usage.add_response):
        thread.run()
    if error_messages:
        raise StageError("translate", error_messages[-1])
    if not finished or not output_srt.is_file():
        raise StageError("translate", "DeepSeek 翻译未生成 SRT")


def _print_failed_videos(failed: list[VideoResult]) -> None:
    """集中显示当前仍失败的视频完整路径和最后错误。"""
    if not failed:
        return
    print(f"\n=== 当前仍失败视频（{len(failed)}）===")
    for item in failed:
        latest = item.errors[-1] if item.errors else None
        category = latest.category if latest else "unknown"
        message = latest.message if latest else "未记录错误详情"
        print(f"[{category}] {item.video}")
        print(f"  原因：{message}")


def _print_round_summary(round_results: list[VideoResult]) -> None:
    """统计本轮实际尝试结果，并列出新增及已更新的 SRT。"""
    statuses = Counter(item.status for item in round_results)
    print("\n=== 本轮统计 ===")
    print(
        f"新增 SRT {statuses['success']} | "
        f"更新已有 SRT {statuses['updated_existing']} | "
        f"失败 {statuses['error']}"
    )
    detail_groups = (
        ("新增 SRT 详情", "success"),
        ("更新已有 SRT 详情", "updated_existing"),
    )
    for title, status in detail_groups:
        items = [item for item in round_results if item.status == status]
        if not items:
            continue
        print(f"{title}：")
        for item in items:
            output = item.output or str(Path(item.video).with_suffix(".srt"))
            print(f"  - {output}（{item.final_sentence_count} 句）")


def _prepare_video(
    video: Path,
    config: BatchConfig,
    progress: object,
    overwrite: bool,
) -> PreparedVideo | VideoResult:
    """只完成单个视频的 GPU 转录，随后把临时源字幕交给下游工位。"""
    started = time.monotonic()
    result = VideoResult(video=str(video), started_at=datetime.now().isoformat(timespec="seconds"))
    destination = video.with_suffix(".srt")
    result.output = str(destination)

    if destination.is_file() and not overwrite:
        try:
            batch_temp_dir = _configured_project_path("temp")
            batch_temp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="videocaptioner-intro-", dir=batch_temp_dir
            ) as temp_name:
                updated_srt = Path(temp_name) / "with-intro.srt"
                result.final_sentence_count, _intro_added = write_srt_with_intro(
                    destination, updated_srt
                )
                validate_complete_srt(updated_srt)
                _publish_srt_atomically(updated_srt, destination)
            result.status = "updated_existing"
            progress.update(
                100,
                f"已有同名 SRT 已加入署名（{result.final_sentence_count} 句）",
            )
        except Exception as exc:
            _record_failure(result, exc, progress)
        return _finish_result(result, started)

    temp_owner = None
    batch_temp_dir = _configured_project_path("temp")
    batch_temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        _wait_for_gpu(progress)
        temp_owner = tempfile.TemporaryDirectory(
            prefix="videocaptioner-batch-", dir=batch_temp_dir
        )
        temp_name = temp_owner.name
        temp_dir = Path(temp_name)
        source_srt = temp_dir / "source.srt"
        translated_srt = temp_dir / "translated.srt"
        _run_transcription(
            video,
            source_srt,
            config,
            lambda value, message: progress.update(
                int(value * 0.55), f"转录：{message}"
            ),
        )
        try:
            raw_sentence_count = validate_source_srt_file(source_srt)
        except ValueError as exc:
            raise StageError("validate", str(exc)) from exc
        result.raw_source_sentence_count = raw_sentence_count
        translated_srt.unlink(missing_ok=True)
        progress.update(
            55,
            f"转录完成（原始 {raw_sentence_count} 句），进入源字幕后处理",
        )
        return PreparedVideo(
            video=video,
            config=config,
            result=result,
            started=started,
            temp_owner=temp_owner,
            source_srt=source_srt,
            translated_srt=translated_srt,
            destination=destination,
        )
    except Exception as exc:
        if temp_owner is not None:
            temp_owner.cleanup()
        _record_failure(result, exc, progress)
        return _finish_result(result, started)


def _process_downstream_video(
    prepared: PreparedVideo,
    api_key: str,
    progress: object,
) -> VideoResult:
    """在下游工位完成源字幕清理、翻译、终检和独立发布。"""
    result = prepared.result
    try:
        if ASR_CLEANUP_ENABLED:
            try:
                cleanup = clean_source_srt_file(prepared.source_srt)
            except ValueError as exc:
                raise StageError("validate", str(exc)) from exc
            result.asr_removed_consecutive_duplicates = (
                cleanup.removed_consecutive_duplicates
            )
            result.asr_removed_invalid_timelines = cleanup.removed_invalid_timelines
            result.asr_removed_invalid_encoding_cues = (
                cleanup.removed_invalid_encoding_cues
            )
        result.source_sentence_count = validate_complete_srt(prepared.source_srt)
        cleanup_text = ""
        if ASR_CLEANUP_ENABLED:
            cleanup_text = (
                f"，删除连续重复 {result.asr_removed_consecutive_duplicates}"
                f"、反向时间轴 {result.asr_removed_invalid_timelines}"
                f"、无效编码 cue {result.asr_removed_invalid_encoding_cues}"
            )
        progress.update(
            55,
            f"源字幕后处理完成（{result.raw_source_sentence_count} → "
            f"{result.source_sentence_count} 句{cleanup_text}）",
        )
        _run_translation(
            prepared.video,
            prepared.source_srt,
            prepared.translated_srt,
            prepared.config,
            api_key,
            lambda value, message: progress.update(
                55 + int(value * 0.4), f"翻译：{message}"
            ),
            result.token_usage,
        )
        result.final_sentence_count = validate_bilingual_output(
            prepared.source_srt, prepared.translated_srt
        )
        progress.update(97, "校验并发布")
        credited_srt = prepared.translated_srt.with_name("credited.srt")
        real_cue_count, intro_added = write_srt_with_intro(
            prepared.translated_srt, credited_srt
        )
        if real_cue_count != result.final_sentence_count:
            raise StageError("validate", "加入片头署名前后真实字幕句数不一致")
        expected_cues = result.final_sentence_count + int(intro_added)
        if validate_complete_srt(credited_srt) != expected_cues:
            raise StageError("validate", "加入片头署名后的 SRT 校验失败")
        _publish_srt_atomically(credited_srt, prepared.destination)
        result.status = "success"
        progress.update(100, f"完成（{result.final_sentence_count} 句）")
    except Exception as exc:
        _record_failure(result, exc, progress)
    finally:
        prepared.temp_owner.cleanup()
    return _finish_result(result, prepared.started)


def process_video(
    video: Path,
    config: BatchConfig,
    api_key: str,
    progress: ConsoleProgress,
    overwrite: bool,
) -> VideoResult:
    """单视频兼容入口；正式批处理由 process_video_group 进行阶段化调度。"""
    prepared = _prepare_video(
        video,
        config,
        progress,
        overwrite,
    )
    if isinstance(prepared, VideoResult):
        return prepared
    return _process_downstream_video(prepared, api_key, progress)


def process_video_group(
    videos: list[Path],
    config: BatchConfig,
    api_key: str,
    progress: ConsoleProgress,
    overwrite: bool,
    *,
    start_index: int = 1,
) -> list[VideoResult]:
    """以滑动在途窗口调度转录到发布的完整视频流水线。"""
    if not videos:
        return []
    resolved: dict[str, VideoResult] = {}
    pipeline_capacity = PIPELINE_MAX_IN_FLIGHT if VIDEO_LEVEL_TRANSLATION else 1
    pipeline_capacity = max(1, min(pipeline_capacity, len(videos)))
    downstream_workers = max(1, min(VIDEO_TRANSLATION_CONCURRENCY, pipeline_capacity))
    future_to_item: dict[Future[VideoResult], tuple[PreparedVideo, _VideoProgress]] = {}

    def collect_finished(futures: Iterable[Future[VideoResult]]) -> None:
        """在转录工位切换视频的间隙回收已完成的下游结果。"""
        for future in futures:
            item, video_progress = future_to_item.pop(future)
            result = future.result()
            resolved[str(item.video)] = result
            video_progress.finish_line()

    print(
        f"整体流水线：在途最多 {pipeline_capacity} 个视频，"
        f"转录 1 路，下游最多 {downstream_workers} 路并发..."
    )
    with ThreadPoolExecutor(
        max_workers=downstream_workers,
        thread_name_prefix="videocaptioner-downstream",
    ) as downstream_executor:
        # 滑动窗口限制整条流水线的在途视频数；一个转录完成即流入
        # 清理/翻译/终检/发布工位，不等待固定分组内的其他视频。
        for offset, video in enumerate(videos):
            while len(future_to_item) >= pipeline_capacity:
                done, _ = wait(future_to_item, return_when=FIRST_COMPLETED)
                collect_finished(done)

            video_progress = progress.begin(start_index + offset, video)
            prepared = _prepare_video(
                video,
                config,
                video_progress,
                overwrite,
            )
            if isinstance(prepared, VideoResult):
                resolved[str(video)] = prepared
                video_progress.finish_line()
            else:
                future = downstream_executor.submit(
                    _process_downstream_video,
                    prepared,
                    api_key,
                    video_progress,
                )
                future_to_item[future] = (prepared, video_progress)

            finished = [future for future in future_to_item if future.done()]
            collect_finished(finished)

        collect_finished(as_completed(tuple(future_to_item)))

    progress.close()
    return [resolved[str(video)] for video in videos]


def _finish_result(result: VideoResult, started: float) -> VideoResult:
    result.finished_at = datetime.now().isoformat(timespec="seconds")
    result.duration_seconds = round(time.monotonic() - started, 3)
    return result


def summarise_token_usage(
    results: Iterable[VideoResult], status: str | None = None
) -> TokenUsage:
    """汇总一批视频在唯一处理轮中产生的真实 API usage，可按最终状态筛选。"""
    total = TokenUsage()
    for item in results:
        if status is not None and item.status != status:
            continue
        total.merge(item.token_usage)
    return total


def _report_summary(results: Iterable[VideoResult]) -> dict[str, object]:
    """生成最终报告与实时批次日志共用的汇总结构。"""
    rows = list(results)
    return {
        "total": len(rows),
        "status_counts": dict(Counter(result.status for result in rows)),
        "error_counts": dict(
            Counter(event.category for result in rows for event in result.errors)
        ),
        "total_raw_source_sentences": sum(
            item.raw_source_sentence_count for item in rows
        ),
        "total_source_sentences": sum(item.source_sentence_count for item in rows),
        "total_final_sentences": sum(item.final_sentence_count for item in rows),
        "total_asr_removed_consecutive_duplicates": sum(
            item.asr_removed_consecutive_duplicates for item in rows
        ),
        "total_asr_removed_invalid_timelines": sum(
            item.asr_removed_invalid_timelines for item in rows
        ),
        "total_asr_removed_invalid_encoding_cues": sum(
            item.asr_removed_invalid_encoding_cues for item in rows
        ),
        "token_usage": asdict(summarise_token_usage(rows)),
        "failed_token_usage": asdict(summarise_token_usage(rows, status="error")),
    }


def write_reports(input_path: Path, results: Iterable[VideoResult]) -> tuple[Path, Path]:
    """在项目输出目录写入 UTF-8 JSON 与 CSV 报告。"""
    rows = list(results)
    report_dir = _configured_project_path("reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = report_dir / f"VideoCaptioner-batch-report-{stamp}.json"
    csv_path = report_dir / f"VideoCaptioner-batch-report-{stamp}.csv"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pricing": {
            "model": FIXED_CONFIG["model"],
            "currency": "CNY",
            "unit": "per_1m_tokens",
            "checked_on": DEEPSEEK_PRICING_CHECKED_ON,
            "source": DEEPSEEK_PRICING_SOURCE,
            "peak_weekdays_utc": sorted(day + 1 for day in DEEPSEEK_PEAK_WEEKDAYS),
            "peak_ranges_utc": DEEPSEEK_PEAK_RANGES,
            "rates": DEEPSEEK_PRICING_CNY_PER_MILLION,
        },
        "summary": _report_summary(rows),
        "videos": [
            {**asdict(item), "errors": [asdict(error) for error in item.errors]}
            for item in rows
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "video", "output", "status", "raw_source_sentence_count",
                "source_sentence_count", "final_sentence_count",
                "asr_removed_consecutive_duplicates",
                "asr_removed_invalid_timelines",
                "asr_removed_invalid_encoding_cues",
                "duration_seconds",
                "api_requests",
                "peak_requests", "off_peak_requests", "input_cache_hit_tokens",
                "input_cache_miss_tokens", "output_tokens", "total_tokens",
                "estimated_cost_cny", "error_categories", "error_messages",
            ],
        )
        writer.writeheader()
        for item in rows:
            writer.writerow(
                {
                    "video": item.video,
                    "output": item.output,
                    "status": item.status,
                    "raw_source_sentence_count": item.raw_source_sentence_count,
                    "source_sentence_count": item.source_sentence_count,
                    "final_sentence_count": item.final_sentence_count,
                    "asr_removed_consecutive_duplicates": (
                        item.asr_removed_consecutive_duplicates
                    ),
                    "asr_removed_invalid_timelines": (
                        item.asr_removed_invalid_timelines
                    ),
                    "asr_removed_invalid_encoding_cues": (
                        item.asr_removed_invalid_encoding_cues
                    ),
                    "duration_seconds": item.duration_seconds,
                    **asdict(item.token_usage),
                    "error_categories": ";".join(error.category for error in item.errors),
                    "error_messages": " | ".join(error.message for error in item.errors),
                }
            )
    return json_path, csv_path


def print_summary(results: list[VideoResult], report_paths: tuple[Path, Path]) -> None:
    summary = _report_summary(results)
    statuses = Counter(summary["status_counts"])
    errors = Counter(summary["error_counts"])
    total_usage = summary["token_usage"]
    failed_usage = summary["failed_token_usage"]
    status_labels = {
        "success": "成功",
        "updated_existing": "更新已有",
        "error": "失败",
    }
    status_order = ["success", "updated_existing", "error"]
    status_text = " | ".join(
        f"{status_labels.get(name, name)} {statuses[name]}"
        for name in status_order
        if statuses[name]
    )
    print("\n=== 批次完成 ===")
    print(f"视频 {len(results)} | {status_text}")
    print(
        f"句子 ASR 原始 {summary['total_raw_source_sentences']:,} | "
        f"清理后 {summary['total_source_sentences']:,} | "
        f"最终 {summary['total_final_sentences']:,}"
    )
    removed_duplicates = summary["total_asr_removed_consecutive_duplicates"]
    removed_invalid_timelines = summary["total_asr_removed_invalid_timelines"]
    removed_invalid_encoding_cues = summary[
        "total_asr_removed_invalid_encoding_cues"
    ]
    if removed_duplicates or removed_invalid_timelines or removed_invalid_encoding_cues:
        print(
            f"ASR 清理 连续重复 {removed_duplicates:,} | "
            f"反向时间轴 {removed_invalid_timelines:,} | "
            f"无效编码 cue {removed_invalid_encoding_cues:,}"
        )
    print(
        f"DeepSeek 请求 {total_usage['api_requests']}（峰 {total_usage['peak_requests']}/"
        f"谷 {total_usage['off_peak_requests']}） | Token {total_usage['total_tokens']:,} | "
        f"预估 RMB {total_usage['estimated_cost_cny']:.6f}"
    )
    print(
        f"Token 明细 缓存命中 {total_usage['input_cache_hit_tokens']:,} | "
        f"输入未命中 {total_usage['input_cache_miss_tokens']:,} | "
        f"输出 {total_usage['output_tokens']:,}"
    )
    if statuses["error"]:
        print(
            f"最终失败消耗 DeepSeek 请求 {failed_usage['api_requests']} | "
            f"Token {failed_usage['total_tokens']:,} | "
            f"预估 RMB {failed_usage['estimated_cost_cny']:.6f}"
        )
    if errors:
        print("错误 " + " | ".join(f"{name} {count}" for name, count in sorted(errors.items())))
        print("错误/修复记录：")
        for item in results:
            if not item.errors:
                continue
            latest = item.errors[-1]
            print(f"  - {Path(item.video).name} [{latest.category}] {latest.message}")
    print("每视频句子数、Token、费用和完整错误见报告：")
    print(f"  JSON {report_paths[0]}")
    print(f"  CSV  {report_paths[1]}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="无界面批量处理目录中的全部视频")
    parser.add_argument("path", nargs="?", help="视频文件或包含视频的目录")
    parser.add_argument("--overwrite", action="store_true", help="覆盖视频旁已有的同名 SRT")
    return parser.parse_args(argv)


def _confirm_batch_start() -> bool:
    """在交互模式完成扫描后确认是否开始处理。"""
    print("[1] 确认开始处理")
    print("[0] 取消")
    while True:
        try:
            choice = input("请输入数字，直接回车默认选择 1: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            return False
        if choice == "1":
            return True
        if choice == "0":
            return False
        print(f"无效选择：{choice}")


def _run_batch(raw_path: str, overwrite: bool, confirm: bool = False) -> int:
    """处理一个路径；交互菜单和命令行入口共用同一批处理实现。"""
    raw_path = raw_path.strip().strip('"')
    if not raw_path:
        print("错误：未输入路径", file=sys.stderr)
        return 2

    try:
        config = load_batch_config()
        _validate_source_language(config.source_language)
        _target_language(config.target_language)
        _prepare_shared_runtime()
        input_path = Path(raw_path).expanduser().resolve(strict=False)
        videos = discover_videos(input_path)
        if not videos:
            raise ConfigError(f"没有找到支持的视频: {input_path}")
    except (ConfigError, FileNotFoundError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(f"找到 {len(videos)} 个视频；源语言 {config.source_language} → 目标语言 {config.target_language}")
    if (
        confirm
        and get_value(EFFECTIVE_CONFIG, "interaction.confirm_after_scan")
        and not _confirm_batch_start()
    ):
        print("已取消，本路径未开始处理。")
        return 0

    api_key = ""
    if overwrite or any(not video.with_suffix(".srt").is_file() for video in videos):
        try:
            api_key = read_deepseek_key()
        except ConfigError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2

    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    journal = BatchEventJournal(batch_id)
    journal.write(
        "batch_started",
        input_path=str(input_path),
        video_count=len(videos),
        source_language=config.source_language,
        target_language=config.target_language,
        overwrite=overwrite,
    )
    for video in videos:
        journal.write(
            "attempt_started",
            round=1,
            attempt=1,
            video=str(video),
        )

    eta_videos = {
        str(video)
        for video in videos
        if overwrite or not video.with_suffix(".srt").is_file()
    }
    progress = ConsoleProgress(len(videos), eta_videos=eta_videos)
    results = process_video_group(
        videos,
        config,
        api_key,
        progress,
        overwrite,
        start_index=1,
    )
    for result in results:
        journal.write(
            "attempt_finished",
            round=1,
            attempt=1,
            video=result.video,
            result=asdict(result),
        )

    statuses = Counter(item.status for item in results)
    failed = [item for item in results if item.status == "error"]
    journal.write(
        "round_finished",
        round=1,
        success_count=statuses["success"],
        success_videos=[item.video for item in results if item.status == "success"],
        updated_existing_count=statuses["updated_existing"],
        updated_existing_videos=[
            item.video for item in results if item.status == "updated_existing"
        ],
        round_failed_count=statuses["error"],
        round_failed_videos=[item.video for item in failed],
        failed_count=len(failed),
        failed_videos=[item.video for item in failed],
    )
    _print_round_summary(results)
    _print_failed_videos(failed)

    report_paths = write_reports(input_path, results)
    exit_code = 1 if any(item.status == "error" for item in results) else 0
    journal.write(
        "batch_finished",
        exit_code=exit_code,
        reports=[str(path) for path in report_paths],
        summary=_report_summary(results),
    )
    print_summary(results, report_paths)
    return exit_code


def _interactive_main(overwrite: bool) -> int:
    """持续显示数字菜单，让一次启动能够依次处理多个路径。"""
    print("VideoCaptioner 无界面批处理")
    first_path = True
    while True:
        if not first_path:
            print("\n[1] 处理视频文件或目录")
            print("[0] 退出")
            try:
                choice = input("请输入数字，直接回车默认选择 1: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已退出。")
                return 0
            if choice == "0":
                print("已退出。")
                return 0
            if choice not in {"", "1"}:
                print(f"无效选择：{choice}")
                continue

        first_path = False
        try:
            raw_path = input("请输入视频文件或目录路径: ")
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0
        exit_code = _run_batch(raw_path, overwrite, confirm=True)
        if exit_code:
            print(f"本次处理结束，退出码：{exit_code}")

        try:
            input("\n按回车返回主菜单")
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.path:
        exit_code = _run_batch(args.path, args.overwrite, confirm=False)
        if get_value(EFFECTIVE_CONFIG, "interaction.pause_after_one_shot") and sys.stdin.isatty():
            try:
                input("\n按回车退出")
            except (EOFError, KeyboardInterrupt):
                pass
        return exit_code
    return _interactive_main(args.overwrite)


if __name__ == "__main__":
    raise SystemExit(main())
