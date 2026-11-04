"""音轨转换失败后的首声道回退测试。"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PATCH_DIR = Path(__file__).parents[1] / "local_patch"
sys.path.insert(0, str(PATCH_DIR))
MODULE_PATH = PATCH_DIR / "videocaptioner_audio_conversion_fix.py"
SPEC = importlib.util.spec_from_file_location("audio_conversion_fix_test", MODULE_PATH)
assert SPEC and SPEC.loader
audio_fix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audio_fix)


class AudioConversionFixTests(unittest.TestCase):
    def test_success_uses_upstream_only(self) -> None:
        original = mock.Mock(return_value=True)
        with mock.patch.object(audio_fix.subprocess, "run") as run:
            self.assertTrue(audio_fix.convert_with_channel_fallback(original, "input.mp4", "output.wav", 1))
        run.assert_not_called()

    def test_failed_conversion_uses_selected_track_first_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output = str(Path(temp_name) / "audio.wav")

            def fake_run(command, **_kwargs):
                Path(output).write_bytes(b"wav")
                return SimpleNamespace(
                    returncode=0,
                    stderr="Error submitting packet to decoder: Invalid data found when processing input",
                )

            with mock.patch.object(audio_fix.subprocess, "run", side_effect=fake_run) as run, self.assertLogs(
                audio_fix.logger, level="WARNING"
            ) as logs:
                self.assertTrue(audio_fix.convert_with_channel_fallback(lambda *_args, **_kwargs: False, "input.mp4", output, 2))
            command = run.call_args.args[0]
            self.assertIn("0:a:2", command)
            self.assertIn("pan=mono|c0=c0", command)
            self.assertIn("解码错误 1 次", logs.output[0])

    def test_failed_fallback_is_not_reported_as_success(self) -> None:
        with mock.patch.object(audio_fix.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="bad audio")):
            self.assertFalse(audio_fix.convert_with_channel_fallback(lambda *_args, **_kwargs: False, "input.mp4", "output.wav"))

    def test_damaged_audio_is_replaced_with_equal_length_silence(self) -> None:
        report = {"duration": 300.0, "streams": [{"type": "audio", "ordinal": 0, "bad_ranges": [[140, 150]]}]}
        with mock.patch.object(audio_fix, "check_media", return_value=report), mock.patch.object(
            audio_fix.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")
        ) as run, mock.patch.object(audio_fix.Path, "is_file", return_value=True), mock.patch.object(
            audio_fix.Path, "stat", return_value=SimpleNamespace(st_size=3)
        ), mock.patch.object(
            audio_fix.Path, "mkdir"
        ), self.assertLogs(audio_fix.logger, level="WARNING"):
            self.assertTrue(audio_fix.convert_with_channel_fallback(lambda *_args, **_kwargs: False,
                                                                   "input.mp4", "output.wav", 0))
        command = run.call_args.args[0]
        self.assertIn("anullsrc=r=16000:cl=mono", command)
        self.assertIn("concat=n=3:v=0:a=1[out]", command[command.index("-filter_complex") + 1])
