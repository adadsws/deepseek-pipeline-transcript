"""只读检查音视频流与图片帧，供独立工具及批处理回退共用。"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable

from PIL import Image, UnidentifiedImageError

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic"}


def merge_ranges(ranges: list[tuple[float, float]], *, padding: float = 0, duration: float = 0) -> list[list[float]]:
    """合并相邻坏区间并留安全边界，始终保持原媒体时间坐标。"""
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        start = max(0, start - padding)
        end = min(duration, end + padding) if duration else end + padding
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _probe(path: Path, ffprobe: str) -> dict:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,codec_name", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip()[-500:] or "ffprobe 无法识别媒体")
    return json.loads(result.stdout)


def _probe_with_ffmpeg(path: Path, ffmpeg: str) -> dict:
    """项目运行时只有 ffmpeg.exe 时，复用其输入信息识别常见音视频。"""
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=30)
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not duration_match:
        raise ValueError("ffmpeg 未提供可用媒体时长")
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    streams = [
        {"index": int(index), "codec_type": kind.lower(), "codec_name": codec}
        for index, kind, codec in re.findall(
            r"Stream #0:(\d+)(?:\[[^]]*\])?(?:\([^)]*\))?:\s*(Video|Audio):\s*([^,\s]+)", result.stderr
        )
    ]
    return {"format": {"duration": duration}, "streams": streams}


def _check_image(path: Path) -> dict:
    with Image.open(path) as image:
        fmt = image.format
        image.verify()
    with Image.open(path) as image:
        frames = getattr(image, "n_frames", 1)
        for index in range(frames):
            image.seek(index)
            image.load()
    return {"path": str(path), "type": "image", "status": "ok", "format": fmt, "frames": frames, "streams": []}


def check_media(
    path: Path | str, *, stream_types: set[str] | None = None,
    window_seconds: int = 60, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
    progress: Callable[[str, int, int], None] | None = None,
) -> dict:
    """逐帧/逐时间窗解码；失败明确报告，不把容器可打开当作媒体完好。"""
    path = Path(path)
    if not path.is_file():
        return {"path": str(path), "type": "unknown", "status": "check_failed", "error": "文件不存在", "streams": []}
    if path.suffix.lower() in IMAGE_SUFFIXES:
        try:
            return _check_image(path)
        except (OSError, ValueError, SyntaxError) as exc:
            return {"path": str(path), "type": "image", "status": "corrupt", "error": str(exc), "streams": []}
    try:
        try:
            probe = _probe(path, ffprobe)
        except FileNotFoundError:
            probe = _probe_with_ffmpeg(path, ffmpeg)
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        try:
            return _check_image(path)
        except (OSError, ValueError, SyntaxError, UnidentifiedImageError):
            return {"path": str(path), "type": "unknown", "status": "unsupported", "error": str(exc), "streams": []}
    duration = float(probe.get("format", {}).get("duration") or 0)
    streams = [stream for stream in probe.get("streams", []) if stream.get("codec_type") in {"audio", "video"}]
    if not streams or duration <= 0:
        return {"path": str(path), "type": "unknown", "status": "unsupported", "error": "没有可检查的定时音视频流", "streams": []}
    media_type = "video" if any(stream["codec_type"] == "video" for stream in streams) else "audio"
    checked = []
    selected = [stream for stream in streams if stream_types is None or stream["codec_type"] in stream_types]
    if not selected:
        return {"path": str(path), "type": media_type, "status": "unsupported",
                "error": "没有请求检查的音视频流", "streams": []}
    for stream in streams:
        kind = stream["codec_type"]
        if stream_types is not None and kind not in stream_types:
            continue
        stream_index = sum(1 for prior in streams if prior["codec_type"] == kind and prior["index"] < stream["index"])
        bad: list[tuple[float, float]] = []
        error_count = 0
        examples: list[str] = []
        starts = range(0, int(duration) + 1, window_seconds)
        total_windows = sum(start < duration for start in starts)
        for window_index, start in enumerate(starts, 1):
            length = min(window_seconds, duration - start)
            if length <= 0:
                break
            command = [ffmpeg, "-v", "error", "-ss", str(start), "-i", str(path), "-t", str(length),
                       "-map", f"0:{'a' if kind == 'audio' else 'v'}:{stream_index}"]
            if kind == "audio":
                command += ["-af", "pan=mono|c0=c0"]
            command += ["-f", "null", "-"]
            try:
                result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace", timeout=180)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"path": str(path), "type": media_type, "status": "check_failed", "error": str(exc), "streams": checked}
            lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
            if lines or result.returncode != 0:
                bad.append((float(start), float(start + length)))
                error_count += max(1, len(lines))
                for line in lines:
                    if line not in examples and len(examples) < 3:
                        examples.append(line[:240])
            if progress is not None:
                progress(f"{kind}:{stream_index}", window_index, total_windows)
        checked.append({"index": stream["index"], "ordinal": stream_index, "type": kind, "codec": stream.get("codec_name"),
                        "error_count": error_count, "bad_ranges": merge_ranges(bad, duration=duration), "examples": examples})
    return {"path": str(path), "type": media_type,
            "status": "corrupt" if any(item["bad_ranges"] for item in checked) else "ok",
            "duration": duration, "streams": checked}
