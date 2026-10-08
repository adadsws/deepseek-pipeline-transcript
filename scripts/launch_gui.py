"""按项目配置准备运行目录并启动固定上游 GUI。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PATCH_DIR = PROJECT_ROOT / "local_patch"
if str(LOCAL_PATCH_DIR) not in sys.path:
    sys.path.insert(0, str(LOCAL_PATCH_DIR))

from prepare_runtime import ensure_runtime_layout  # noqa: E402
from videocaptioner_project_config import (  # noqa: E402
    ProjectConfigError,
    get_value,
    load_mode_settings,
)


def main(argv: list[str] | None = None) -> int:
    try:
        paths = ensure_runtime_layout("gui")
        settings = load_mode_settings("gui")
    except (ProjectConfigError, OSError) as exc:
        print(f"GUI 启动准备失败：{exc}", file=sys.stderr)
        return 2

    program = paths["faster_whisper_bin"] / get_value(settings, "transcription.program")
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(LOCAL_PATCH_DIR), env.get("PYTHONPATH", ""))
        if item
    )
    env["PATH"] = os.pathsep.join((str(program.parent), env.get("PATH", "")))
    executable = PROJECT_ROOT / ".venv" / "Scripts" / "videocaptioner-gui.exe"
    if not executable.is_file():
        print(f"缺少 GUI 可执行入口：{executable}", file=sys.stderr)
        return 2
    completed = subprocess.run(
        [str(executable), *(argv or [])],
        cwd=PROJECT_ROOT,
        env=env,
        check=False,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
