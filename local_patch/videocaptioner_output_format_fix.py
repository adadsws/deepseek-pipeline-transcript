"""Project-owned runtime patch for VideoCaptioner's full GUI workflow.

The upstream full workflow always emits both ``<video>.srt`` and
``<video>.ass``, although its transcription settings offer a single output
format.  This module is intentionally outside the isolated upstream checkout.
It is loaded through ``sitecustomize.py`` by ``start.bat``.
"""

from __future__ import annotations

import logging
import shutil
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


def _normalise_path(path: Path | str) -> Path:
    """Return a case-insensitive, absolute comparison key on Windows."""
    return Path(path).resolve(strict=False)


def _replace_extension(path: Path | str, format_name: str) -> Path:
    """Build a sibling export path without treating dots in file names as suffixes."""
    return Path(path).with_suffix(f".{format_name}")


@dataclass(frozen=True)
class FullPipelineOutputPolicy:
    """The user-selected final subtitle format for one full GUI task."""

    task_id: str
    video_path: Path
    raw_base_path: Path
    selected_format: str

    def _selected_format(self) -> str:
        return self.selected_format.lower()

    def final_export_format(self, save_path: Path | str) -> str | None:
        """Return the format when a path is this task's final video-side export."""
        export_path = _normalise_path(save_path)
        video_path = _normalise_path(self.video_path)
        if export_path == _replace_extension(video_path, "srt"):
            return "srt"
        if export_path == _replace_extension(video_path, "ass"):
            return "ass"
        return None

    def allows_video_export(self, save_path: Path | str) -> bool:
        """Whether the full pipeline may write this SRT/ASS beside the video."""
        format_name = self.final_export_format(save_path)
        if format_name == "srt":
            return self._selected_format() in {"srt", "all"}
        if format_name == "ass":
            return self._selected_format() in {"ass", "all"}
        return True

    def raw_exports_to_publish(self) -> list[tuple[Path, Path]]:
        """Return non-SRT/ASS exports that should appear beside the video."""
        selected = self._selected_format()
        formats = ("txt", "vtt") if selected == "all" else (selected,)
        return [
            (
                _replace_extension(self.raw_base_path, format_name),
                _replace_extension(self.video_path, format_name),
            )
            for format_name in formats
            if format_name in {"txt", "vtt"}
        ]


class OutputPolicyRegistry:
    """Transfers policies through the full GUI pipeline via raw subtitle paths."""

    def __init__(self) -> None:
        self._policies: dict[Path, FullPipelineOutputPolicy] = {}

    def register(self, task: object) -> FullPipelineOutputPolicy | None:
        config = getattr(task, "transcribe_config", None)
        file_path = getattr(task, "file_path", None)
        output_path = getattr(task, "output_path", None)
        output_format = getattr(config, "output_format", None)
        format_name = getattr(output_format, "value", output_format)
        task_id = getattr(task, "task_id", None)
        if not all((file_path, output_path, format_name, task_id)):
            return None

        policy = FullPipelineOutputPolicy(
            task_id=str(task_id),
            video_path=Path(file_path),
            raw_base_path=Path(output_path),
            selected_format=str(format_name),
        )
        self._policies[_normalise_path(policy.raw_base_path)] = policy
        return policy

    def for_raw_subtitle(self, raw_subtitle_path: Path | str) -> FullPipelineOutputPolicy | None:
        return self._policies.get(_normalise_path(raw_subtitle_path))

    def release_raw_subtitle(self, raw_subtitle_path: Path | str) -> None:
        self._policies.pop(_normalise_path(raw_subtitle_path), None)


registry = OutputPolicyRegistry()
_installed = False
active_output_policy: ContextVar[FullPipelineOutputPolicy | None] = ContextVar(
    "active_output_policy", default=None
)


def _publish_raw_exports(policy: FullPipelineOutputPolicy) -> None:
    for source, destination in policy.raw_exports_to_publish():
        if not source.is_file():
            logger.warning("Requested subtitle output was not created: %s", source)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        logger.info("Published selected subtitle output: %s", destination)


def install() -> None:
    """Install the patch once after the upstream package is importable."""
    global _installed
    if _installed:
        return

    from videocaptioner.core.asr.asr_data import ASRData
    from videocaptioner.core.entities import SubtitleLayoutEnum
    from videocaptioner.ui.task_factory import TaskFactory
    from videocaptioner.ui.thread.subtitle_thread import SubtitleThread
    from videocaptioner.ui.thread.transcript_thread import TranscriptThread

    original_create_transcribe_task = TaskFactory.create_transcribe_task

    def create_transcribe_task(*args: object, **kwargs: object) -> object:
        task = original_create_transcribe_task(*args, **kwargs)
        if getattr(task, "need_next_task", False):
            registry.register(task)
        return task

    TaskFactory.create_transcribe_task = staticmethod(create_transcribe_task)

    original_perform_transcription = TranscriptThread._perform_transcription

    def perform_transcription(self: object) -> None:
        original_perform_transcription(self)
        task = getattr(self, "task")
        policy = registry.for_raw_subtitle(getattr(task, "output_path"))
        if policy:
            _publish_raw_exports(policy)

    TranscriptThread._perform_transcription = perform_transcription

    original_create_subtitle_task = TaskFactory.create_subtitle_task

    def create_subtitle_task(*args: object, **kwargs: object) -> object:
        task = original_create_subtitle_task(*args, **kwargs)
        raw_subtitle_path = kwargs.get("file_path") or (args[0] if args else None)
        if getattr(task, "need_next_task", False) and raw_subtitle_path:
            policy = registry.for_raw_subtitle(str(raw_subtitle_path))
            if policy:
                setattr(task, "_videocaptioner_output_policy", policy)
        return task

    TaskFactory.create_subtitle_task = staticmethod(create_subtitle_task)

    original_subtitle_run = SubtitleThread.run

    def subtitle_run(self: object) -> object:
        task = getattr(self, "task")
        policy = getattr(task, "_videocaptioner_output_policy", None)
        token = active_output_policy.set(policy)
        try:
            return original_subtitle_run(self)
        finally:
            active_output_policy.reset(token)
            if policy:
                registry.release_raw_subtitle(policy.raw_base_path)

    SubtitleThread.run = subtitle_run

    original_save = ASRData.save

    def save(
        self: ASRData,
        save_path: str,
        ass_style: str | None = None,
        layout: object = None,
    ) -> None:
        if str(save_path).lower().endswith(".vtt"):
            path = Path(save_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.to_vtt(save_path=str(path))
            return
        if layout is None:
            return original_save(self, save_path, ass_style=ass_style)
        return original_save(self, save_path, ass_style=ass_style, layout=layout)

    ASRData.save = save

    original_to_srt = ASRData.to_srt

    def to_srt(
        self: ASRData,
        layout: object = SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        save_path: str | None = None,
    ) -> str:
        policy = active_output_policy.get()
        if (
            policy
            and save_path
            and policy.final_export_format(save_path) == "srt"
            and not policy.allows_video_export(save_path)
        ):
            return original_to_srt(self, layout=layout, save_path=None)
        return original_to_srt(self, layout=layout, save_path=save_path)

    ASRData.to_srt = to_srt

    original_to_ass = ASRData.to_ass

    def to_ass(
        self: ASRData,
        style_str: str | None = None,
        layout: object = SubtitleLayoutEnum.ORIGINAL_ON_TOP,
        save_path: str | None = None,
        video_width: int = 1280,
        video_height: int = 720,
    ) -> str:
        policy = active_output_policy.get()
        if (
            policy
            and save_path
            and policy.final_export_format(save_path) == "ass"
            and not policy.allows_video_export(save_path)
        ):
            return original_to_ass(
                self,
                style_str=style_str,
                layout=layout,
                save_path=None,
                video_width=video_width,
                video_height=video_height,
            )
        return original_to_ass(
            self,
            style_str=style_str,
            layout=layout,
            save_path=save_path,
            video_width=video_width,
            video_height=video_height,
        )

    ASRData.to_ass = to_ass

    _installed = True
    logger.info("Installed project output-format fix for full GUI tasks")
