"""统一本地扫描、上游格式枚举和播放器拖入的视频扩展名策略。

适配 reference/upstream/VideoCaptioner 的 core/entities.py、cli/validators.py
及 ui/components/MyVideoWidget.py，保持固定上游源码不变。
"""

from __future__ import annotations

import re
import subprocess
import sys
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import PathFinder
from enum import Enum
from pathlib import Path

from videocaptioner_project_config import get_value, load_mode_settings


AMBIGUOUS_VIDEO_EXTENSIONS = frozenset({
    ".dat", ".mod", ".asx", ".ifo", ".m4b", ".m4p", ".ram", ".rpm", ".wmx", ".wvx",
})
_installed = False


def video_extensions(mode: str = "batch") -> frozenset[str]:
    """读取当前模式的格式列表；配置默认由 shared 提供。"""
    return frozenset(str(item).lower() for item in get_value(load_mode_settings(mode), "input.video_extensions"))


def _is_media_reference(path: Path) -> bool:
    """识别本地播放指针/光盘索引，避免 FFmpeg 跟随其引用而误判文件内容。"""
    with path.open("rb") as stream:
        header = stream.read(4096)
    if header.startswith((b"DVDVIDEO-VMG", b"DVDVIDEO-VTS")):
        return True
    encoding = "utf-16" if header.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    text = header.decode(encoding, errors="replace").lstrip().lower()
    return bool(
        text.startswith(("#extm3u", "[reference]", "[playlist]", "ffconcat version"))
        or re.match(r"(?:<\?xml[^>]*>\s*)?<(?:asx|smil|playlist)\b", text)
        or re.match(r"(?:https?|rtsp|rtmp|pnm|mms|file)://", text)
    )


def is_video_file(path: Path, extensions: set[str] | frozenset[str], ffmpeg: str) -> bool:
    """普通视频按扩展名枚举；歧义类型仅接受本地文件内的真实视频流。"""
    suffix = path.suffix.lower()
    if suffix not in extensions:
        return False
    if suffix not in AMBIGUOUS_VIDEO_EXTENSIONS:
        return True
    from videocaptioner_media_checker import _probe_with_ffmpeg

    try:
        if _is_media_reference(path):
            return False
        probe = _probe_with_ffmpeg(path, ffmpeg, require_duration=False, local_only=True)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return any(
        stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")
        for stream in probe.get("streams", [])
    )


def _drag_enter(self: object, event: object) -> None:
    """让播放器拖入使用与文件选择相同的格式，并继续支持原有字幕。"""
    if event.mimeData().hasUrls() and any(
        Path(url.toLocalFile()).suffix.lower() in self._project_video_suffixes | {".srt", ".ass"}
        for url in event.mimeData().urls()
    ):
        event.acceptProposedAction()
    else:
        event.ignore()


def _drop(self: object, event: object) -> None:
    """保留真实文件路径大小写，只将扩展名用于比较。"""
    for url in event.mimeData().urls():
        suffix = Path(url.toLocalFile()).suffix.lower()
        if suffix in {".srt", ".ass"}:
            self.vlc_player.add_subtitle(url.toLocalFile())
        elif suffix in self._project_video_suffixes:
            self.setVideo(url)
            self.play()
            break


def _patch_player(module: object, extensions: frozenset[str]) -> None:
    player = module.MyVideoWidget
    player._project_video_suffixes = extensions
    player.dragEnterEvent = _drag_enter
    player.dropEvent = _drop


class _PlayerLoader(Loader):
    """只在原生界面真正导入播放器时适配，不提前引入可选 VLC 依赖。"""

    def __init__(self, original: Loader, extensions: frozenset[str]) -> None:
        self.original = original
        self.extensions = extensions

    def create_module(self, spec):
        return self.original.create_module(spec)

    def exec_module(self, module) -> None:
        self.original.exec_module(module)
        _patch_player(module, self.extensions)


class _PlayerFinder(MetaPathFinder):
    module_name = "videocaptioner.ui.components.MyVideoWidget"

    def __init__(self, extensions: frozenset[str]) -> None:
        self.extensions = extensions

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.module_name:
            return None
        spec = PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _PlayerLoader(spec.loader, self.extensions)
        return spec


def install() -> None:
    """在其他 GUI 补丁导入界面前安装；兼容已经导入的格式枚举引用。"""
    global _installed
    if _installed:
        return
    from videocaptioner.core import entities
    from videocaptioner.cli import validators

    extensions = video_extensions("gui")
    original = entities.SupportedVideoFormats
    members = {item.name: item.value for item in original if f".{item.value}" in extensions}
    for suffix in sorted(extensions):
        value = suffix[1:]
        name = re.sub(r"\W", "_", value.upper())
        if name[0].isdigit():
            name = "V_" + name
        members.setdefault(name, value)
    formats = Enum("SupportedVideoFormats", members)
    entities.SupportedVideoFormats = formats
    for name, module in list(sys.modules.items()):
        if name.startswith("videocaptioner.") and getattr(module, "SupportedVideoFormats", None) is original:
            module.SupportedVideoFormats = formats
    validators.VIDEO_EXTENSIONS = frozenset(suffix[1:] for suffix in extensions)

    # 此组件有独立硬编码白名单，延迟至实际导入后补齐事件入口。
    player_module = sys.modules.get(_PlayerFinder.module_name)
    if player_module is not None:
        _patch_player(player_module, extensions)
    else:
        sys.meta_path.insert(0, _PlayerFinder(extensions))
    _installed = True
