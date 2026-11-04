"""独立媒体错误检查入口：单文件或目录递归扫描，逐文件保存 JSON 进度。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
bundled_ffmpeg = ROOT / "runtime" / "VideoCaptioner" / "bin" / "Faster-Whisper-XXL" / "ffmpeg.exe"
if bundled_ffmpeg.is_file():
    os.environ["PATH"] = str(bundled_ffmpeg.parent) + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, str(ROOT / "local_patch"))
from videocaptioner_media_checker import check_media  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量检查视频、音频和图片数据错误")
    parser.add_argument("path", nargs="?", type=Path, help="媒体文件或目录")
    parser.add_argument("--prompt", action="store_true", help="从终端读取媒体路径")
    parser.add_argument("--output", type=Path, help="JSON 报告路径；默认写入项目输出目录")
    parser.add_argument("--window-seconds", type=int, default=60, help="音视频检查窗口秒数，默认 60")
    args = parser.parse_args(argv)
    if args.window_seconds < 1:
        parser.error("--window-seconds 必须大于 0")
    if args.prompt and args.path is not None:
        parser.error("--prompt 不能与路径参数同时使用")
    if args.prompt or args.path is None:
        try:
            entered = input("媒体文件或目录路径: ").strip()
        except EOFError:
            entered = ""
        if len(entered) >= 2 and entered[0] == entered[-1] and entered[0] in {'"', "'"}:
            entered = entered[1:-1]
        if not entered:
            parser.error("未输入媒体路径")
        source = Path(entered).resolve()
    elif args.path is not None:
        source = args.path.resolve()
    else:
        parser.error("需要媒体文件或目录路径")
    if not source.exists():
        parser.error(f"路径不存在：{source}")
    files = [source] if source.is_file() else sorted((p for p in source.rglob("*") if p.is_file()), key=lambda p: str(p).casefold())
    output = args.output or ROOT / "~outputs-final" / "media-check-reports" / f"media-check-{datetime.now():%Y%m%d-%H%M%S}.json"
    if output.exists():
        parser.error(f"报告已存在：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"source": str(source), "window_seconds": args.window_seconds, "files": []}
    for index, path in enumerate(files, 1):
        last_percent: dict[str, int] = {}

        def show_progress(stream: str, done: int, total: int) -> None:
            percent = done * 100 // total
            if percent // 10 > last_percent.get(stream, -1) or done == total:
                last_percent[stream] = percent // 10
                print(f"[{index}/{len(files)}] {path.name} {stream} {done}/{total}", flush=True)

        result = check_media(path, window_seconds=args.window_seconds, progress=show_progress)
        report["files"].append(result)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{index}/{len(files)}] {result['status']} {path}", flush=True)
    print(f"报告：{output}")
    if any(item["status"] in {"corrupt", "check_failed"} for item in report["files"]):
        return 1
    if any(item["status"] == "unsupported" for item in report["files"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
