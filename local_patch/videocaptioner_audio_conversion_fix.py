"""在上游常规提取失败时，用单声道显式映射处理异常声道布局。"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from videocaptioner_media_checker import check_media, merge_ranges

logger = logging.getLogger(__name__)
_installed = False


def convert_with_channel_fallback(
    original: object, input_file: str, output: str = "", audio_track_index: int = 0
) -> bool:
    """先沿用上游转换；失败时仅提取所选音轨的首声道。"""
    if original(input_file, output=output, audio_track_index=audio_track_index):
        return True
    if not output:
        return False
    report = check_media(input_file, stream_types={"audio"})
    audio = next((item for item in report.get("streams", [])
                  if item["type"] == "audio" and item["ordinal"] == audio_track_index), None)
    if audio and audio["bad_ranges"]:
        duration = report["duration"]
        ranges = merge_ranges(
            [tuple(item) for item in audio["bad_ranges"]], padding=30, duration=duration
        )
        skipped = sum(end - start for start, end in ranges)
        if len(ranges) <= 8 and skipped < duration * 0.35:
            if _convert_with_silent_ranges(input_file, output, audio_track_index, duration, ranges):
                logger.warning("原音轨损坏，已将 %s 区间替换为等长静音；字幕时间轴保留", ranges)
                return True
        else:
            logger.warning("损坏区间过多或过长，不自动静音：%s", ranges)
    command = [
        "ffmpeg", "-i", input_file, "-map", f"0:a:{audio_track_index}",
        "-vn", "-af", "pan=mono|c0=c0", "-ar", "16000", "-y", output,
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        logger.exception("FFmpeg 首声道回退运行失败")
        return False
    if completed.returncode != 0:
        logger.error("FFmpeg 首声道回退失败: %s", completed.stderr[-2000:])
        return False
    if not Path(output).is_file() or Path(output).stat().st_size == 0:
        return False
    decode_errors = completed.stderr.count("Error submitting packet to decoder")
    logger.warning(
        "常规音频转换失败，首声道回退成功；原音轨解码错误 %d 次，损坏处音频可能缺失",
        decode_errors,
    )
    return True


def _convert_with_silent_ranges(
    input_file: str, output: str, audio_track_index: int,
    duration: float, ranges: list[list[float]],
) -> bool:
    """每段完好音频独立解码，坏段用等长静音替代，不读取坏段数据。"""
    parts: list[tuple[str, float, float]] = []
    position = 0.0
    for start, end in ranges:
        if start > position:
            parts.append(("source", position, start))
        parts.append(("silence", start, end))
        position = end
    if position < duration:
        parts.append(("source", position, duration))
    command = ["ffmpeg", "-v", "error"]
    filters = []
    labels = []
    for index, (kind, start, end) in enumerate(parts):
        length = end - start
        if kind == "source":
            command += ["-ss", str(start), "-t", str(length), "-i", input_file]
            filters.append(
                f"[{index}:a:{audio_track_index}]pan=mono|c0=c0,aresample=16000,"
                f"asetpts=PTS-STARTPTS[p{index}]"
            )
        else:
            command += ["-f", "lavfi", "-t", str(length), "-i", "anullsrc=r=16000:cl=mono"]
            filters.append(f"[{index}:a]asetpts=PTS-STARTPTS[p{index}]")
        labels.append(f"[p{index}]")
    filters.append(f"{''.join(labels)}concat=n={len(parts)}:v=0:a=1[out]")
    command += ["-filter_complex", ";".join(filters), "-map", "[out]", "-y", output]
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError):
        logger.exception("损坏区间静音转换运行失败")
        return False
    if result.returncode != 0:
        logger.error("损坏区间静音转换失败：%s", result.stderr[-1000:])
    return result.returncode == 0 and Path(output).is_file() and Path(output).stat().st_size > 0


def install() -> None:
    """仅替换上游线程已导入的音频转换入口。"""
    global _installed
    if _installed:
        return
    from videocaptioner.ui.thread import transcript_thread

    original = transcript_thread.video2audio

    def video2audio(input_file: str, output: str = "", audio_track_index: int = 0) -> bool:
        return convert_with_channel_fallback(original, input_file, output, audio_track_index)

    transcript_thread.video2audio = video2audio
    _installed = True
