"""校验项目运行资源，并为固定上游目录准备兼容 Junction。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PATCH_DIR = PROJECT_ROOT / "local_patch"
if str(LOCAL_PATCH_DIR) not in sys.path:
    sys.path.insert(0, str(LOCAL_PATCH_DIR))

from videocaptioner_project_config import (  # noqa: E402
    ProjectConfigError,
    get_value,
    load_mode_settings,
    resolve_project_path,
)


def _resolved_link_target(path: Path) -> Path | None:
    is_link = path.is_symlink() or getattr(path, "is_junction", lambda: False)()
    if not path.exists() and not is_link:
        return None
    try:
        return path.resolve(strict=False)
    except OSError:
        return None


def _ensure_junction(path: Path, target: Path) -> None:
    """只创建缺失 Junction；任何已有非预期对象都拒绝覆盖。"""
    target.mkdir(parents=True, exist_ok=True)
    is_link = path.is_symlink() or getattr(path, "is_junction", lambda: False)()
    if path.exists() or is_link:
        resolved = _resolved_link_target(path)
        if resolved == target.resolve(strict=False):
            return
        raise ProjectConfigError(f"兼容路径已存在且目标不符: {path} -> {resolved}")
    path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(path), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ProjectConfigError(f"无法创建 Junction {path} -> {target}: {detail}")


def ensure_runtime_layout(mode: str = "batch", *, require_resources: bool = True) -> dict[str, Path]:
    """准备缓存/日志目录与上游兼容链接，并可验证大型资源。"""
    settings = load_mode_settings(mode)
    paths = {
        name: resolve_project_path(get_value(settings, f"paths.{name}"))
        for name in (
            "upstream_root",
            "faster_whisper_bin",
            "models",
            "cache",
            "logs",
            "work",
            "temp",
            "batch_journal",
            "reports",
        )
    }
    for name in ("cache", "logs", "work", "temp", "batch_journal", "reports"):
        paths[name].mkdir(parents=True, exist_ok=True)

    upstream = paths["upstream_root"]
    app_data = upstream / "AppData"
    app_data_is_link = app_data.is_symlink() or getattr(
        app_data, "is_junction", lambda: False
    )()
    if app_data.exists() and app_data.is_dir() and not app_data_is_link:
        pass
    elif app_data.exists() or app_data_is_link:
        raise ProjectConfigError(f"AppData 必须是普通目录: {app_data}")
    else:
        app_data.mkdir(parents=True)

    _ensure_junction(app_data / "models", paths["models"])
    _ensure_junction(app_data / "cache", paths["cache"])
    _ensure_junction(app_data / "logs", paths["logs"])
    _ensure_junction(upstream / "resource" / "bin", paths["faster_whisper_bin"])

    if require_resources:
        executable = paths["faster_whisper_bin"] / "Faster-Whisper-XXL" / "faster-whisper-xxl.exe"
        if not executable.is_file():
            raise ProjectConfigError(f"缺少 FasterWhisper 可执行文件: {executable}")
        if not paths["models"].is_dir() or not any(paths["models"].iterdir()):
            raise ProjectConfigError(f"缺少模型资源: {paths['models']}")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备 VideoCaptioner 运行目录")
    parser.add_argument("--mode", choices=("batch", "gui"), default="batch")
    parser.add_argument("--allow-missing-resources", action="store_true")
    args = parser.parse_args(argv)
    try:
        paths = ensure_runtime_layout(
            args.mode, require_resources=not args.allow_missing_resources
        )
    except (ProjectConfigError, OSError) as exc:
        print(f"运行目录准备失败：{exc}", file=sys.stderr)
        return 2
    print(f"运行目录已就绪：{paths['upstream_root']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
