# Media damage checker

[简体中文](README.md) | [English](README_EN.md)

Read-only checks for video, audio, and still or animated images. Accepts a file or recursively scans a folder. Audio/video decoding reports damaged ranges per stream; images undergo structure verification and full frame loading.

## Setup

Included with the [main project](../../README_EN.md). Use Python 3.12 from the project root and install `python -m pip install Pillow`. Image checks need no GPU, models, or API key. Audio/video checks also require FFmpeg on PATH or the project's downloaded `runtime/VideoCaptioner/bin/Faster-Whisper-XXL/ffmpeg.exe`. FFprobe is used when available, with FFmpeg as the fallback.

## Usage

Double-click `tools/check_media.bat` and paste a path, or run from the project root:

```powershell
.\.venv\Scripts\python.exe -m tools.media_check
.\.venv\Scripts\python.exe -m tools.media_check "media file or folder" --window-seconds 60
```

The BAT prefers the project's `.venv`, falling back to `py -3.12`, and shows a final status before waiting for a key. Without arguments, the module prompts for a path. Pasted paths may contain surrounding quotes, spaces, non-ASCII characters, or `%`. The former root `check_media.bat` and `scripts/media_check.py` have moved into this subtool.

JSON reports default to the project's `~outputs-final/media-check-reports/`. Use `--output "report.json"` to choose a report path that does not exist. `--window-seconds` must be positive and defaults to 60. Shorter windows improve range precision but require more checks. Unsupported files in folders are also reported.

Per-file statuses are `ok`, `corrupt`, `unsupported`, and `check_failed`. Exit codes: 0 for no damage detected, 1 for corruption or a check failure, 2 for invalid input or unsupported files. Format support depends on FFmpeg and Pillow; damaged ranges reflect decoding windows.

The subtitle pipeline's audio conversion fallback still shares the same checking core. This tool checks source files without repairing media or producing subtitles.
