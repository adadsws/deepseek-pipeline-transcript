# [Whisper Transcription + DeepSeek Translation] Subtitle Pipeline

[简体中文](README.md) | [English](README_EN.md)

Transcribe and translate video speech, then save `.srt` subtitles next to the source video. Process one video or an entire folder at a time.

## First-time setup

Requires Windows, an NVIDIA GPU, Python 3.12, and a DeepSeek API key.

1. Open PowerShell and run:

```powershell
git clone --recurse-submodules https://github.com/adadsws/deepseek-pipeline-transcript.git
cd deepseek-pipeline-transcript
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .\reference\upstream\VideoCaptioner
.\runtime\VideoCaptioner\download-bin.ps1
.\runtime\VideoCaptioner\download-models.ps1
.\.venv\Scripts\python.exe .\scripts\prepare_runtime.py
```

2. Copy `secrets/deepseek-api-key.example` to `secrets/deepseek-api-key.txt`, then enter your DeepSeek API key in the copy.

Do not publish or commit your DeepSeek API key in plaintext.

## Usage

1. Double-click `batch_videos.bat`.
2. Enter a video file or folder path.
3. Confirm the scan and wait for processing to finish.

Folders are scanned recursively. On success, each video gets a matching `.srt` file beside it.

Video extensions are case-insensitive and include MP4, MTS, M2TS, M2T, MOV, MXF, 3GP and OGV. The complete candidate list is `input.video_extensions` in `config/shared/settings.toml`. DAT, MOD, M4B, M4P, RAM, RPM, IFO, ASX, WMX and WVX are probed for an actual video stream in the file. Audio-only files, cover art, playlists and DVD indexes are excluded; network links are not followed. Decoding and transcription depend on the file contents and FFmpeg.

To view scan counts without starting processing, run in PowerShell:

```powershell
.\batch_videos.bat "path to video or folder" --scan-only
```

## Tools

| Tool | Purpose | Entry point | Requirements |
|---|---|---|---|
| [Media damage checker](tools/media_check/README_EN.md) | Recursively check video, audio and images; report damaged ranges. | `python -m tools.media_check` or `tools/check_media.bat` | Python 3.12, Pillow; FFmpeg for audio/video |

## Change languages

Set the source and subtitle languages in `config/batch/settings.toml`:

```toml
[language.video]
source = "ja"
target = "zh-Hans"
```

`ja` means Japanese; `zh-Hans` means Simplified Chinese.
