"""视频格式评测：来源覆盖、真实导入函数及只扫描入口，不启动字幕任务。"""

from __future__ import annotations

import importlib
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "local_patch"))
import videocaptioner_video_formats as formats
import videocaptioner_project_config as config


class VideoFormatTests(unittest.TestCase):
    def test_complete_installed_potplayer_lists_are_covered(self):
        # 独立输入：从 DLL 完整默认列表及语言资源 1386 提取，禁止手挑子集。
        snapshot = json.loads((ROOT / "data/video-formats/potplayer-20261118.json").read_text(encoding="utf-8"))
        expected = set(snapshot["video_extensions"]) | set(snapshot["legacy_video_association_extensions"])
        self.assertEqual(len(snapshot["video_extensions"]), 52)
        self.assertEqual(len(snapshot["legacy_video_association_extensions"]), 34)
        self.assertFalse(expected - formats.video_extensions())

    def test_actual_camera_formats_and_potplayer_video_associations(self):
        # 来源：本机 PotPlayerMini64.* 视频关联；不含音频、播放列表及光盘索引。
        expected = {
            ".3g2", ".3gp", ".3gp2", ".3gpp", ".amv", ".asf", ".avi", ".divx",
            ".dmskm", ".dpg", ".dvr-ms", ".evo", ".f4v", ".flv", ".k3g", ".lmp4",
            ".m1v", ".m2t", ".m2ts", ".m2v", ".m4v", ".mkv", ".mov", ".mp2v",
            ".mp4", ".mpe", ".mpeg", ".mpg", ".mpv2", ".mqv", ".mts", ".mxf",
            ".nsr", ".nsv", ".ogm", ".ogv", ".qt", ".rm", ".rmvb", ".skm", ".swf",
            ".tp", ".tpr", ".trp", ".ts", ".vob", ".webm", ".wm", ".wmp", ".wmv", ".wtv",
        }
        self.assertFalse(expected - formats.video_extensions())
        self.assertEqual(formats.video_extensions(), formats.video_extensions("gui"))

    def test_nonvideo_and_incomplete_download_extensions_are_excluded(self):
        excluded = {".jpg", ".gif", ".srt", ".mp3", ".torrent", ".url", ".m3u8", ".bc!", ".!qb"}
        self.assertFalse(excluded & formats.video_extensions())

    def test_all_extensions_case_insensitive_except_ambiguous_need_probe(self):
        extensions = formats.video_extensions()
        for suffix in extensions - formats.AMBIGUOUS_VIDEO_EXTENSIONS:
            with self.subTest(suffix=suffix):
                self.assertTrue(formats.is_video_file(Path("中文 % sample" + suffix.upper()), extensions, "unused"))

    def test_ambiguous_extensions_require_video_stream(self):
        probe_name = "videocaptioner_media_checker._probe_with_ffmpeg"
        extensions = formats.video_extensions()
        for suffix in formats.AMBIGUOUS_VIDEO_EXTENSIONS:
            for kind, expected in (("video", True), ("audio", False)):
                with self.subTest(suffix=suffix, kind=kind), mock.patch.object(formats, "_is_media_reference", return_value=False), mock.patch(
                    probe_name, return_value={"streams": [{"codec_type": kind}]}
                ) as probe:
                    self.assertEqual(formats.is_video_file(Path("sample" + suffix), extensions, "ffmpeg"), expected)
                    probe.assert_called_once_with(Path("sample" + suffix), "ffmpeg", require_duration=False, local_only=True)
            with mock.patch.object(formats, "_is_media_reference", return_value=False), mock.patch(probe_name, side_effect=ValueError("not media")):
                self.assertFalse(formats.is_video_file(Path("sample" + suffix), extensions, "ffmpeg"))

    def test_playlists_and_dvd_indexes_are_excluded_before_probing(self):
        extensions = formats.video_extensions()
        references = [b'DVDVIDEO-VMG\0', b'DVDVIDEO-VTS\0', b'#EXTM3U\nvideo.mp4',
                      b'<ASX><ENTRY><REF HREF="video.mp4"/></ENTRY></ASX>',
                      b'<?xml version="1.0"?>\n<smil/>', b'file://video.mp4',
                      b'https://example.invalid/video.mp4', b'[Reference]\nRef1=video.mp4',
                      '#EXTM3U\nvideo.mp4'.encode('utf-16')]
        for header in references:
            with self.subTest(header=header), mock.patch.object(Path, "open", mock.mock_open(read_data=header)), \
                 mock.patch("videocaptioner_media_checker._probe_with_ffmpeg") as probe:
                self.assertFalse(formats.is_video_file(Path("sample.ram"), extensions, "unused"))
                probe.assert_not_called()

    def test_audio_cover_is_not_a_video_and_local_probe_blocks_network_protocols(self):
        from videocaptioner_media_checker import _probe_with_ffmpeg

        output = "Duration: 00:01:00.00\n Stream #0:0: Audio: aac\n Stream #0:1: Video: mjpeg, 64x64 (attached pic)\n"
        with mock.patch.object(formats, "_is_media_reference", return_value=False), \
             mock.patch("videocaptioner_media_checker.subprocess.run", return_value=SimpleNamespace(stderr=output)) as run:
            probe = _probe_with_ffmpeg(Path("sample.m4b"), "ffmpeg", local_only=True)
            self.assertEqual(probe["streams"][1]["disposition"]["attached_pic"], 1)
            self.assertIn("-protocol_whitelist", run.call_args.args[0])
            self.assertIn("file", run.call_args.args[0])
            self.assertFalse(formats.is_video_file(Path("sample.m4b"), formats.video_extensions(), "ffmpeg"))

    def test_config_rejects_bad_extension_list(self):
        for value in ([], "mp4", ["mp4"], [".mp4 "]):
            settings = config.load_mode_settings("batch")
            settings["input"]["video_extensions"] = value
            with self.subTest(value=value), self.assertRaisesRegex(config.ProjectConfigError, "video_extensions"):
                config._validate_effective_settings(settings, "batch")

    def test_video_stream_without_duration_is_still_identified(self):
        from videocaptioner_media_checker import _probe_with_ffmpeg

        result = SimpleNamespace(stderr="Duration: N/A\n Stream #0:0: Video: mpeg2video, 64x64\n")
        with mock.patch("videocaptioner_media_checker.subprocess.run", return_value=result):
            probe = _probe_with_ffmpeg(Path("sample.dat"), "ffmpeg", require_duration=False)
            self.assertEqual(probe["streams"][0]["codec_type"], "video")
            with self.assertRaisesRegex(ValueError, "时长"):
                _probe_with_ffmpeg(Path("sample.dat"), "ffmpeg")

    def test_install_is_idempotent_and_updates_cli_and_existing_ui_references(self):
        from videocaptioner.core import entities
        from videocaptioner.cli import validators

        prior = entities.SupportedVideoFormats
        consumer = ModuleType("videocaptioner.test_existing_format_consumer")
        consumer.SupportedVideoFormats = prior
        with mock.patch.dict(sys.modules, {consumer.__name__: consumer}):
            formats.install()
            first = entities.SupportedVideoFormats
            formats.install()
            self.assertIs(first, entities.SupportedVideoFormats)
            self.assertIs(first, consumer.SupportedVideoFormats)
        self.assertEqual({"." + item.value for item in first}, formats.video_extensions("gui"))
        for suffix in ("m2t", "3gp", "ogv", "dvr-ms"):
            self.assertIn(suffix, validators.VIDEO_EXTENSIONS)
            self.assertIsNone(validators.validate_video_input(Path("sample." + suffix)))
        with mock.patch.object(Path, "is_file", return_value=True):
            self.assertIsNone(validators.validate_media_input(Path("sample.M2T")))

    def test_lazy_player_loader_and_drag_drop_preserve_original_path(self):
        player_class = type("Player", (), {})
        module = SimpleNamespace(MyVideoWidget=player_class)
        loader = mock.Mock()
        patched_loader = formats._PlayerLoader(loader, formats.video_extensions("gui"))
        patched_loader.exec_module(module)
        loader.exec_module.assert_called_once_with(module)
        player = player_class()
        player.vlc_player = mock.Mock()
        player.setVideo = mock.Mock()
        player.play = mock.Mock()
        subtitle_url = mock.Mock()
        subtitle_url.toLocalFile.return_value = "C:/中文 %/Subtitle.ASS"
        video_url = mock.Mock()
        video_url.toLocalFile.return_value = "C:/中文 %/Camera.M2T"
        event = mock.Mock()
        event.mimeData.return_value.urls.return_value = [subtitle_url, video_url]
        player.dragEnterEvent(event)
        event.acceptProposedAction.assert_called_once()
        player.dropEvent(event)
        player.setVideo.assert_called_once_with(video_url)
        player.vlc_player.add_subtitle.assert_called_once_with("C:/中文 %/Subtitle.ASS")
        player.play.assert_called_once()

    def test_scan_only_never_starts_processing(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        batch = importlib.import_module("headless_batch")
        output = io.StringIO()
        with mock.patch.object(batch, "discover_videos", return_value=[Path("a.M2T"), Path("b.m2t")]), \
             mock.patch.object(batch, "_run_batch") as run, redirect_stdout(output):
            self.assertEqual(batch.main(["中文 % path", "--scan-only"]), 0)
        run.assert_not_called()
        self.assertIn(".m2t: 2", output.getvalue())
        with redirect_stdout(io.StringIO()), mock.patch.object(batch, "discover_videos", return_value=[]):
            self.assertEqual(batch.main(["empty", "--scan-only"]), 2)


if __name__ == "__main__":
    unittest.main()
