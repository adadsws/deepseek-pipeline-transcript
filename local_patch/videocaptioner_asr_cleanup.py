"""在 VAD 关闭时把字幕序列中连续相同的 ASR 文本保留第一条。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


_TIMELINE_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})$"
)


@dataclass
class SourceCue:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class CleanupStats:
    original_cues: int
    final_cues: int
    removed_consecutive_duplicates: int
    removed_invalid_timelines: int


def _timestamp_ms(parts: tuple[str, ...]) -> int:
    hours, minutes, seconds, milliseconds = map(int, parts)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _format_timestamp(milliseconds: int) -> str:
    total_seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def parse_source_srt(text: str) -> list[SourceCue]:
    """解析结构和正文已经过批处理校验的单语源 SRT。"""
    cues: list[SourceCue] = []
    for expected_index, block in enumerate(
        re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()), start=1
    ):
        lines = [line.strip() for line in block.splitlines()]
        if len(lines) < 3 or lines[0] != str(expected_index):
            raise ValueError(f"源 SRT 第 {expected_index} 条结构无效")
        match = _TIMELINE_RE.match(lines[1])
        if match is None:
            raise ValueError(f"源 SRT 第 {expected_index} 条时间轴无效")
        start_ms = _timestamp_ms(match.groups()[:4])
        end_ms = _timestamp_ms(match.groups()[4:])
        body = "\n".join(line for line in lines[2:] if line)
        if not body:
            raise ValueError(f"源 SRT 第 {expected_index} 条正文无效")
        cues.append(SourceCue(start_ms, end_ms, body))
    return cues


def normalise_repetition_text(text: str) -> str:
    """忽略空格、标点、全半角差异，但保留字母与数字本身。"""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character).startswith(("L", "N"))
    )


def clean_source_cues(cues: list[SourceCue]) -> tuple[list[SourceCue], CleanupStats]:
    """删除反向时间轴，并把连续相同文本只保留第一条。"""
    kept: list[SourceCue] = []
    removed_duplicates = 0
    removed_invalid_timelines = 0
    for cue in cues:
        # 上游 optimize_timing 在片段严重重叠时可能产生结束早于开始的 cue。
        # 正确边界已不可恢复，删除该 cue 比交换或猜测时间戳更保守。
        if cue.end_ms < cue.start_ms:
            removed_invalid_timelines += 1
            continue
        key = normalise_repetition_text(cue.text)
        if kept and key and key == normalise_repetition_text(kept[-1].text):
            removed_duplicates += 1
            continue
        kept.append(SourceCue(cue.start_ms, cue.end_ms, cue.text))

    stats = CleanupStats(
        original_cues=len(cues),
        final_cues=len(kept),
        removed_consecutive_duplicates=removed_duplicates,
        removed_invalid_timelines=removed_invalid_timelines,
    )
    return kept, stats


def render_source_srt(cues: list[SourceCue]) -> str:
    return "\n".join(
        f"{index}\n{_format_timestamp(cue.start_ms)} --> "
        f"{_format_timestamp(cue.end_ms)}\n{cue.text}\n"
        for index, cue in enumerate(cues, start=1)
    )


def clean_source_srt_file(path: Path) -> CleanupStats:
    """清理临时源 SRT 并原子写回；最终视频目录仍不接触中间结果。"""
    cues = parse_source_srt(path.read_text(encoding="utf-8-sig", errors="strict"))
    cleaned, stats = clean_source_cues(cues)
    if not cleaned:
        raise ValueError("ASR 清理后没有剩余字幕")
    temporary = path.with_name(path.name + ".cleanup.tmp")
    try:
        temporary.write_text(render_source_srt(cleaned), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return stats
