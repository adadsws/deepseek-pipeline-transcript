"""无界面批处理的纯本地测试。"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "headless_batch.py"
BATCH_PATH = Path(__file__).parents[1] / "batch_videos.bat"
SPEC = importlib.util.spec_from_file_location("headless_batch", MODULE_PATH)
assert SPEC and SPEC.loader
batch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = batch
SPEC.loader.exec_module(batch)
REAL_WAIT_FOR_GPU = batch._wait_for_gpu


class HeadlessBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gpu_wait_patcher = mock.patch.object(batch, "_wait_for_gpu")
        self.gpu_wait = self.gpu_wait_patcher.start()
        self.addCleanup(self.gpu_wait_patcher.stop)
        self.intro_patchers = [
            mock.patch.object(batch, "INTRO_SUBTITLE_ENABLED", True),
            mock.patch.object(
                batch, "INTRO_SUBTITLE_TEXT", "字幕制作：github.com/adadsws"
            ),
            mock.patch.object(batch, "INTRO_SUBTITLE_DURATION_MS", 3_000),
        ]
        for patcher in self.intro_patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_query_gpu_utilization_parses_nvidia_smi_output(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="98\n", stderr="")
        with mock.patch.object(batch.subprocess, "run", return_value=completed) as run:
            self.assertEqual(batch._query_gpu_utilization(), 98)
        self.assertIn("--query-gpu=utilization.gpu", run.call_args.args[0])

    def test_gpu_wait_returns_immediately_when_initial_reading_is_not_busy(self) -> None:
        progress = mock.Mock()
        with mock.patch.object(batch, "_query_gpu_utilization", return_value=60), mock.patch.object(
            batch.time, "sleep"
        ) as sleep:
            REAL_WAIT_FOR_GPU(progress)
        sleep.assert_not_called()
        progress.update.assert_not_called()

    def test_gpu_wait_requires_two_low_readings_after_busy_period(self) -> None:
        progress = mock.Mock()
        with mock.patch.object(
            batch, "_query_gpu_utilization", side_effect=[95, 60, 59]
        ), mock.patch.object(batch.time, "sleep") as sleep:
            REAL_WAIT_FOR_GPU(progress)
        self.assertEqual(sleep.call_count, 2)
        progress.update.assert_has_calls(
            [
                mock.call(0, "等待 GPU（95%）"),
                mock.call(0, "确认 GPU 空闲（60%）"),
                mock.call(0, "GPU 可用"),
            ]
        )

    def test_gpu_wait_continues_when_query_becomes_unavailable(self) -> None:
        progress = mock.Mock()
        with mock.patch.object(
            batch, "_query_gpu_utilization", side_effect=[95, None]
        ), mock.patch.object(batch.time, "sleep") as sleep:
            REAL_WAIT_FOR_GPU(progress)
        sleep.assert_called_once_with(batch.GPU_POLL_INTERVAL_SECONDS)

    def test_batch_event_journal_flushes_each_event_as_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name, mock.patch.object(
            batch, "PROJECT_ROOT", Path(temp_name)
        ):
            journal = batch.BatchEventJournal("run-1")
            journal.write("attempt_finished", video="sample.mp4", status="error")

            lines = journal.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["batch_id"], "run-1")
            self.assertEqual(payload["event"], "attempt_finished")
            self.assertEqual(payload["video"], "sample.mp4")

    def test_upstream_logs_only_go_to_file_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            logger = logging.Logger("upstream_test", level=logging.DEBUG)
            console_stream = io.StringIO()
            console_handler = logging.StreamHandler(console_stream)
            log_path = Path(temp_name) / "app.log"
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            logger.addHandler(console_handler)
            logger.addHandler(file_handler)
            try:
                batch._silence_upstream_console([logger])
                logger.info("重复阶段信息")
                logger.warning("非致命警告")
                logger.error("详细错误")
                file_handler.flush()
            finally:
                file_handler.close()
            self.assertEqual(console_stream.getvalue(), "")
            file_text = log_path.read_text(encoding="utf-8")
            self.assertIn("重复阶段信息", file_text)
            self.assertIn("非致命警告", file_text)
            self.assertIn("详细错误", file_text)

    def test_console_progress_uses_short_action_and_deduplicates(self) -> None:
        output = io.StringIO()
        with mock.patch.object(batch.sys, "stdout", output), mock.patch.object(
            batch, "_enable_console_cursor_rewrite", return_value=True
        ):
            progress = batch.ConsoleProgress(12)
            progress.begin(2, Path("very-long-video-file-name-for-test.mp4"))
            progress.update(50, "转录")
            progress.update(50, "转录")
        text = output.getvalue()
        self.assertIn("[ 2/12]", text)
        self.assertIn("[#######-------]  50%", text)
        self.assertEqual(text.count("[#######-------]  50%"), 1)
        self.assertNotIn("| 当前：", text)
        self.assertIn("当前转录", text)
        self.assertIn("very-long-video-file-name-for-test.mp4", text)

    def test_console_progress_replaces_current_stage_with_completion(self) -> None:
        output = io.StringIO()
        with mock.patch.object(batch.sys, "stdout", output), mock.patch.object(
            batch, "_enable_console_cursor_rewrite", return_value=True
        ):
            progress = batch.ConsoleProgress(1)
            progress.begin(1, Path(r"V:\folder\video1.mp4"))
            progress.update(55, "转录：处理中")
            progress.update(100, "完成（42 句）")
        text = output.getvalue()
        self.assertIn("当前转录", text)
        self.assertIn("完成（42 句）", text)
        self.assertIn(r"V:\folder\video1.mp4", text)

    def test_console_progress_estimates_remaining_time(self) -> None:
        with mock.patch.object(batch.time, "monotonic", return_value=0), mock.patch.object(
            batch, "_enable_console_cursor_rewrite", return_value=True
        ):
            progress = batch.ConsoleProgress(10)
        progress.index = 2
        progress.video = "测试视频.mp4"
        output = io.StringIO()
        with mock.patch.object(batch.time, "monotonic", return_value=10), mock.patch.object(
            batch.sys, "stdout", output
        ):
            progress.update(50, "转录")
        self.assertIn("剩余 00:57", output.getvalue())

    def test_noninteractive_progress_prints_only_final_snapshot(self) -> None:
        output = io.StringIO()
        with mock.patch.object(batch.sys, "stdout", output), mock.patch.object(
            batch, "_enable_console_cursor_rewrite", return_value=False
        ):
            progress = batch.ConsoleProgress(405)
            progress.begin(11, Path(r"V:\folder\video.mp4"))
            progress.update(11, "转录：处理中")
            progress.update(55, "翻译：处理中")
            progress.update(100, "完成（42 句）")
            progress.finish_line()
        text = output.getvalue()
        self.assertEqual(text.count("[ 11/405]"), 1)
        self.assertIn("完成（42 句）", text)
        self.assertIn(r"V:\folder\video.mp4", text)
        self.assertNotIn("当前转录", text)
        self.assertNotIn("当前翻译", text)

    def test_concurrent_progress_updates_stay_with_their_own_video(self) -> None:
        output = io.StringIO()
        with mock.patch.object(batch.sys, "stdout", output), mock.patch.object(
            batch, "_enable_console_cursor_rewrite", return_value=True
        ):
            progress = batch.ConsoleProgress(3)
            first = progress.begin(1, Path("first.mp4"))
            second = progress.begin(2, Path("second.mp4"))
            first.update(100, "完成（10 句）")
            second.update(73, "翻译：处理中")

            self.assertEqual(progress._states[1].video, "first.mp4")
            self.assertEqual(progress._states[1].message, "完成（10 句）")
            self.assertEqual(progress._states[2].video, "second.mp4")
            self.assertEqual(progress._states[2].message, "翻译：处理中")

            first.finish_line()
            self.assertNotIn(1, progress._states)
            progress.begin(3, Path("third.mp4"))
            self.assertIn(2, progress._states)

        text = output.getvalue()
        self.assertIn("完成（10 句）", text)
        self.assertIn("first.mp4", text)
        self.assertIn("当前翻译", text)
        self.assertIn("second.mp4", text)

    def test_adding_or_updating_video_does_not_rewrite_other_progress_lines(self) -> None:
        output = io.StringIO()
        with mock.patch.object(batch.sys, "stdout", output), mock.patch.object(
            batch, "_enable_console_cursor_rewrite", return_value=True
        ):
            progress = batch.ConsoleProgress(2)
            first = progress.begin(1, Path("first.mp4"))
            before_second = len(output.getvalue())
            second = progress.begin(2, Path("second.mp4"))
            added = output.getvalue()[before_second:]
            self.assertNotIn("first.mp4", added)

            before_update = len(output.getvalue())
            first.update(55, "转录：处理中")
            updated = output.getvalue()[before_update:]
            self.assertIn("first.mp4", updated)
            self.assertNotIn("second.mp4", updated)
            second.update(55, "翻译：处理中")

    def test_noninteractive_results_wait_for_input_order(self) -> None:
        output = io.StringIO()
        with mock.patch.object(batch.sys, "stdout", output), mock.patch.object(
            batch, "_enable_console_cursor_rewrite", return_value=False
        ):
            progress = batch.ConsoleProgress(3)
            first = progress.begin(1, Path("first.mp4"))
            second = progress.begin(2, Path("second.mp4"))
            third = progress.begin(3, Path("third.mp4"))
            first.update(100, "完成（1 句）")
            second.update(100, "完成（2 句）")
            third.update(100, "完成（3 句）")

            second.finish_line()
            self.assertEqual(output.getvalue(), "")
            first.finish_line()
            self.assertLess(output.getvalue().index("first.mp4"), output.getvalue().index("second.mp4"))
            third.finish_line()

        text = output.getvalue()
        self.assertLess(text.index("second.mp4"), text.index("third.mp4"))

    def test_concurrent_progress_thread_stress_keeps_video_ownership(self) -> None:
        output = io.StringIO()
        with mock.patch.object(batch.sys, "stdout", output), mock.patch.object(
            batch, "_enable_console_cursor_rewrite", return_value=True
        ):
            progress = batch.ConsoleProgress(5)
            handles = [
                progress.begin(index, Path(f"video-{index}.mp4"))
                for index in range(1, 6)
            ]
            barrier = threading.Barrier(len(handles))

            def update_video(index, handle):
                barrier.wait(timeout=2)
                for percent in range(0, 101, 5):
                    handle.update(percent, f"视频 {index}：{percent}%")

            workers = [
                threading.Thread(target=update_video, args=(index, handle))
                for index, handle in enumerate(handles, start=1)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5)

            self.assertTrue(all(not worker.is_alive() for worker in workers))
            for index in range(1, 6):
                state = progress._states[index]
                self.assertEqual(state.video, f"video-{index}.mp4")
                self.assertEqual(state.percent, 100)
                self.assertEqual(state.message, f"视频 {index}：100%")

    def test_console_progress_uses_terminal_width_for_wide_characters(self) -> None:
        self.assertEqual(batch._display_width("中文A"), 5)
        shortened = batch._truncate_display_tail("很长的文件名-video.mp4", 12)
        self.assertTrue(shortened.startswith("..."))
        self.assertLessEqual(batch._display_width(shortened), 12)

    def test_summary_is_compact_and_only_lists_error_files(self) -> None:
        success = batch.VideoResult(video="ok.mp4", status="success", final_sentence_count=3)
        failed = batch.VideoResult(video="bad.mp4", status="error")
        failed.errors.append(batch.ErrorEvent("asr", "transcribe", "CUDA failed"))
        failed.token_usage = batch.TokenUsage(
            api_requests=2,
            total_tokens=345,
            estimated_cost_cny=0.001234,
        )
        output = io.StringIO()
        with mock.patch.object(batch.sys, "stdout", output):
            batch.print_summary([success, failed], (Path("report.json"), Path("report.csv")))
        text = output.getvalue()
        self.assertIn("视频 2 | 成功 1 | 失败 1", text)
        self.assertIn("错误/修复记录：", text)
        self.assertIn("bad.mp4 [asr] CUDA failed", text)
        self.assertNotIn("ok.mp4", text)
        self.assertIn("每视频句子数、Token、费用", text)
        self.assertIn("RMB", text)
        self.assertIn("最终失败消耗 DeepSeek 请求 2 | Token 345 | 预估 RMB 0.001234", text)

    def test_batch_entry_uses_windows_line_endings(self) -> None:
        content = BATCH_PATH.read_bytes()
        self.assertNotIn(b"\xef\xbb\xbf", content[:3])
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))

    def test_interactive_menu_can_process_multiple_paths(self) -> None:
        responses = iter([r"D:\first", "", "1", r"D:\second", "", "0"])
        prompts: list[str] = []

        def answer(prompt: str = "") -> str:
            prompts.append(prompt)
            return next(responses)

        with mock.patch("builtins.input", side_effect=answer), mock.patch.object(
            batch, "_run_batch", return_value=0
        ) as run_batch:
            self.assertEqual(batch._interactive_main(overwrite=False), 0)
        self.assertEqual(prompts[0], "请输入视频文件或目录路径: ")
        self.assertEqual(
            run_batch.call_args_list,
            [
                mock.call(r"D:\first", False, confirm=True),
                mock.call(r"D:\second", False, confirm=True),
            ],
        )

    def test_scan_confirmation_defaults_to_start_and_can_cancel(self) -> None:
        with mock.patch("builtins.input", return_value=""):
            self.assertTrue(batch._confirm_batch_start())
        with mock.patch("builtins.input", return_value="0"):
            self.assertFalse(batch._confirm_batch_start())

    def test_cancel_after_scan_does_not_read_api_key(self) -> None:
        video = Path(r"D:\videos\sample.mp4")
        config = batch.BatchConfig("ja", "zh-Hans")
        with mock.patch.object(batch, "load_batch_config", return_value=config), mock.patch.object(
            batch, "_validate_source_language"
        ), mock.patch.object(batch, "_target_language"), mock.patch.object(
            batch, "_prepare_shared_runtime"
        ), mock.patch.object(batch, "discover_videos", return_value=[video]), mock.patch.object(
            batch, "_confirm_batch_start", return_value=False
        ), mock.patch.object(batch, "read_deepseek_key") as read_key:
            self.assertEqual(batch._run_batch(r"D:\videos", False, confirm=True), 0)
        read_key.assert_not_called()

    def test_existing_srt_only_batch_does_not_read_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            video = root / "sample.mp4"
            video.write_bytes(b"video")
            video.with_suffix(".srt").write_text(
                "1\n00:00:05,000 --> 00:00:06,000\n旧字幕\n",
                encoding="utf-8",
            )
            result = batch.VideoResult(
                video=str(video),
                output=str(video.with_suffix(".srt")),
                status="updated_existing",
                final_sentence_count=1,
            )
            captured_keys: list[str] = []

            def fake_group(_videos, _config, key, _progress, _overwrite, **_kwargs):
                captured_keys.append(key)
                return [result]

            with mock.patch.object(
                batch, "load_batch_config", return_value=batch.BatchConfig("ja", "zh-Hans")
            ), mock.patch.object(batch, "_validate_source_language"), mock.patch.object(
                batch, "_target_language"
            ), mock.patch.object(batch, "_prepare_shared_runtime"), mock.patch.object(
                batch, "discover_videos", return_value=[video]
            ), mock.patch.object(batch, "read_deepseek_key") as read_key, mock.patch.object(
                batch, "process_video_group", side_effect=fake_group
            ), mock.patch.object(
                batch, "write_reports", return_value=(Path("report.json"), Path("report.csv"))
            ), mock.patch.object(batch, "print_summary"), mock.patch.object(
                batch, "_print_round_summary"
            ), mock.patch.object(batch, "BatchEventJournal", return_value=mock.Mock()):
                self.assertEqual(batch._run_batch(str(root), overwrite=False), 0)

            read_key.assert_not_called()
            self.assertEqual(captured_keys, [""])

    def test_path_argument_keeps_one_shot_mode(self) -> None:
        with mock.patch.object(batch, "_run_batch", return_value=7) as run_batch:
            self.assertEqual(batch.main([r"D:\videos", "--overwrite"]), 7)
        run_batch.assert_called_once_with(r"D:\videos", True, confirm=False)

    def test_count_srt_cues_rejects_blank_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "blank.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:01,000\n\n", encoding="utf-8")
            self.assertEqual(batch.count_srt_cues(path), 0)

    def test_count_srt_cues_counts_only_valid_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "valid.srt"
            path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n\n"
                "2\ninvalid\nignored\n\n"
                "3\n00:00:02,000 --> 00:00:03,000\n你好\n",
                encoding="utf-8",
            )
            self.assertEqual(batch.count_srt_cues(path), 2)
            with self.assertRaisesRegex(batch.StageError, "SRT 第 2 条"):
                batch.validate_complete_srt(path)

    def test_punctuation_only_cues_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.srt"
            path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n-\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\n。\n\n"
                "3\n00:00:02,000 --> 00:00:03,000\nうん。\n",
                encoding="utf-8",
            )

            self.assertEqual(batch.validate_complete_srt(path), 3)
            self.assertEqual(batch.count_srt_cues(path), 3)

    def test_existing_same_name_srt_is_updated_without_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            video = root / "sample.mp4"
            destination = root / "sample.srt"
            video.write_bytes(b"video")
            destination.write_text(
                "1\n00:00:05,000 --> 00:00:06,000\n旧字幕\n",
                encoding="utf-8",
            )
            config = batch.BatchConfig("ja", "zh-Hans")
            progress = batch.ConsoleProgress(1)
            progress.begin(1, video)
            with mock.patch.object(batch, "_run_transcription") as transcribe:
                result = batch.process_video(
                    video, config, "secret", progress, overwrite=False
                )
            progress.finish_line()
            self.assertEqual(result.status, "updated_existing")
            self.assertEqual(result.final_sentence_count, 1)
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "1\n00:00:00,000 --> 00:00:03,000\n"
                "字幕制作：github.com/adadsws\n\n"
                "2\n00:00:05,000 --> 00:00:06,000\n旧字幕\n",
            )
            transcribe.assert_not_called()
            self.gpu_wait.assert_not_called()

    def test_intro_is_disabled_when_public_config_defaults_are_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.srt"
            output = root / "output.srt"
            source.write_text(
                "1\n00:00:05,000 --> 00:00:06,000\n真字幕\n",
                encoding="utf-8",
            )

            with mock.patch.object(batch, "INTRO_SUBTITLE_ENABLED", False), mock.patch.object(
                batch, "INTRO_SUBTITLE_TEXT", ""
            ):
                real_count, intro_added = batch.write_srt_with_intro(source, output)

            self.assertEqual((real_count, intro_added), (1, False))
            self.assertEqual(source.read_text(encoding="utf-8"), output.read_text(encoding="utf-8"))

    def test_intro_moves_to_center_of_first_three_second_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.srt"
            output = root / "output.srt"
            repeated_output = root / "repeated.srt"
            source.write_text(
                "1\n00:00:01,250 --> 00:00:02,000\n第一条\n\n"
                "2\n00:00:10,000 --> 00:00:11,000\n第二条\n",
                encoding="utf-8",
            )

            real_count, intro_added = batch.write_srt_with_intro(source, output)
            repeated_count, repeated_added = batch.write_srt_with_intro(
                output, repeated_output
            )

            self.assertEqual(real_count, 2)
            self.assertTrue(intro_added)
            self.assertEqual((repeated_count, repeated_added), (2, True))
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "1\n00:00:01,250 --> 00:00:02,000\n第一条\n\n"
                "2\n00:00:04,500 --> 00:00:07,500\n"
                "字幕制作：github.com/adadsws\n\n"
                "3\n00:00:10,000 --> 00:00:11,000\n第二条\n",
            )
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                repeated_output.read_text(encoding="utf-8"),
            )

    def test_intro_is_omitted_when_early_subtitles_have_no_three_second_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.srt"
            first_output = root / "first.srt"
            second_output = root / "second.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n第一条\n\n"
                "2\n00:00:04,000 --> 00:00:05,000\n第二条\n",
                encoding="utf-8",
            )

            real_count, intro_added = batch.write_srt_with_intro(source, first_output)
            second_count, second_added = batch.write_srt_with_intro(
                first_output, second_output
            )

            self.assertEqual((real_count, intro_added), (2, False))
            self.assertEqual((second_count, second_added), (2, False))
            self.assertEqual(first_output.read_text(encoding="utf-8"), second_output.read_text(encoding="utf-8"))
            self.assertNotIn(batch.INTRO_SUBTITLE_TEXT, second_output.read_text(encoding="utf-8"))

    def test_intro_is_not_duplicated_on_repeated_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.srt"
            first_output = root / "first.srt"
            second_output = root / "second.srt"
            source.write_text(
                "1\n00:00:05,000 --> 00:00:06,000\n真字幕\n",
                encoding="utf-8",
            )

            batch.write_srt_with_intro(source, first_output)
            batch.write_srt_with_intro(first_output, second_output)

            text = second_output.read_text(encoding="utf-8")
            self.assertEqual(text.count(batch.INTRO_SUBTITLE_TEXT), 1)
            self.assertEqual(first_output.read_text(encoding="utf-8"), text)

    def test_discover_videos_is_recursive_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "b.MKV").write_bytes(b"x")
            nested = root / "nested"
            nested.mkdir()
            (nested / "a.mp4").write_bytes(b"x")
            (nested / "ignore.txt").write_text("x", encoding="utf-8")
            self.assertEqual([item.name for item in batch.discover_videos(root)], ["b.MKV", "a.mp4"])

    def test_classify_error(self) -> None:
        cases = [
            ("translate", "APIConnectionError WinError 10061", "deepseek_connection"),
            ("translate", "401 Unauthorized", "deepseek_auth"),
            ("translate", "429 rate limit", "deepseek_rate_limit"),
            ("translate", "500 internal server error", "deepseek_server"),
            ("translate", "1/2 句缺少译文", "deepseek_incomplete_output"),
            ("translate", "Missing keys ['2']", "deepseek_incomplete_output"),
            ("transcribe", "CUDA failed", "asr"),
            ("validate", "blank", "invalid_or_blank_srt"),
        ]
        for stage, message, expected in cases:
            with self.subTest(stage=stage, message=message):
                self.assertEqual(batch.classify_error(stage, message), expected)

    def test_empty_translation_keys_rejects_blank_values(self) -> None:
        self.assertEqual(
            batch._empty_translation_keys({"1": "译文", "2": "", "3": "   ", "4": None}),
            ["2", "3", "4"],
        )
        self.assertEqual(
            batch._empty_translation_keys(
                {
                    "1": {"native_translation": "译文"},
                    "2": {"native_translation": ""},
                },
                reflect=True,
            ),
            ["2"],
        )

    def test_bilingual_output_rejects_missing_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.srt"
            translated = root / "translated.srt"
            source.write_text("1\n00:00:00,000 --> 00:00:01,000\n原文。\n", encoding="utf-8")
            translated.write_text("1\n00:00:00,000 --> 00:00:01,000\n原文\n", encoding="utf-8")
            with self.assertRaisesRegex(batch.StageError, "1/1 句缺少译文"):
                batch.validate_bilingual_output(source, translated)

    def test_bilingual_output_accepts_complete_file_without_signal_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.srt"
            translated = root / "translated.srt"
            source.write_text("1\n00:00:00,000 --> 00:00:01,000\n原文。\n", encoding="utf-8")
            translated.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n译文\n原文\n",
                encoding="utf-8",
            )
            self.assertEqual(batch.validate_bilingual_output(source, translated), 1)

    def test_token_usage_uses_peak_and_off_peak_prices(self) -> None:
        response = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=3000,
                prompt_cache_hit_tokens=1000,
                prompt_cache_miss_tokens=2000,
                completion_tokens=3000,
                total_tokens=6000,
            )
        )
        usage = batch.TokenUsage()
        usage.add_response(response, datetime(2026, 9, 21, 2, tzinfo=timezone.utc))
        usage.add_response(response, datetime(2026, 9, 20, 2, tzinfo=timezone.utc))
        self.assertEqual(usage.peak_requests, 1)
        self.assertEqual(usage.off_peak_requests, 1)
        self.assertEqual(usage.total_tokens, 12000)
        self.assertAlmostEqual(usage.estimated_cost_cny, 0.04206, places=9)

    def test_load_batch_config_rejects_missing_language(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            text = batch.CONFIG_PATH.read_text(encoding="utf-8").replace(
                'target = "zh-Hans"', ''
            )
            path = Path(temp_name) / "settings.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(batch.ConfigError, "language.video.target"):
                batch.load_batch_config(path)

    def test_batch_vad_is_permanently_disabled(self) -> None:
        self.assertFalse(batch.FIXED_CONFIG["vad_filter"])
        config_text = batch.CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn("vad_filter = false", config_text)

    def test_sanitise_error_masks_key(self) -> None:
        self.assertNotIn("sk-secretvalue", batch.sanitise_error("api_key=sk-secretvalue"))

    def test_read_deepseek_key_rejects_blank_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "deepseek-api-key.txt"
            path.write_text("  \n", encoding="utf-8")
            with self.assertRaisesRegex(batch.ConfigError, "为空"):
                batch.read_deepseek_key(path)

    def test_process_video_publishes_valid_srt_and_counts_sentences(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            video = Path(temp_name) / "sample.mp4"
            video.write_bytes(b"video")
            config = batch.BatchConfig("ja", "zh-Hans")
            progress = batch.ConsoleProgress(1)
            progress.begin(1, video)

            def fake_transcribe(_video, output, _config, _progress):
                output.write_text("1\n00:00:05,000 --> 00:00:06,000\n原文\n", encoding="utf-8")

            def fake_translate(_video, _source, output, _config, _key, _progress, _usage):
                output.write_text("1\n00:00:05,000 --> 00:00:06,000\n译文\n原文\n", encoding="utf-8")

            with mock.patch.object(
                batch, "_run_transcription", side_effect=fake_transcribe
            ), mock.patch.object(batch, "_run_translation", side_effect=fake_translate):
                result = batch.process_video(video, config, "secret", progress, overwrite=False)
            progress.finish_line()
            self.assertEqual(result.status, "success")
            self.assertEqual(result.raw_source_sentence_count, 1)
            self.assertEqual(result.source_sentence_count, 1)
            self.assertEqual(result.final_sentence_count, 1)
            self.assertTrue(Path(result.output).is_file())
            self.assertEqual(Path(result.output).name, "sample.srt")
            published = Path(result.output).read_text(encoding="utf-8")
            self.assertIn("字幕制作：github.com/adadsws", published)
            self.assertIn("00:00:00,000 --> 00:00:03,000", published)

    def test_video_group_pipelines_direct_single_transcription_with_translation(self) -> None:
        videos = [Path(f"video-{index}.mp4") for index in range(1, 4)]
        config = batch.BatchConfig("ja", "zh-Hans")
        progress = mock.Mock()
        video_progress = [mock.Mock(name=f"progress-{index}") for index in range(3)]
        progress.begin.side_effect = video_progress
        events: list[str] = []
        prepare_threads: set[str] = set()
        translate_threads: set[str] = set()
        prepare_progress: dict[str, object] = {}
        translate_progress: dict[str, object] = {}
        active_prepares = 0
        maximum_active_prepares = 0
        activity_lock = threading.Lock()
        first_translation_started = threading.Event()
        second_preparation_started = threading.Event()

        def fake_prepare(video, current_config, _progress, _overwrite):
            nonlocal active_prepares, maximum_active_prepares
            with activity_lock:
                active_prepares += 1
                maximum_active_prepares = max(maximum_active_prepares, active_prepares)
            try:
                prepare_threads.add(threading.current_thread().name)
                prepare_progress[video.name] = _progress
                events.append(f"prepare:{video.name}")
                if video == videos[1]:
                    self.assertTrue(first_translation_started.wait(timeout=1))
                    second_preparation_started.set()
                result = batch.VideoResult(video=str(video), status="error")
                return batch.PreparedVideo(
                    video=video,
                    config=current_config,
                    result=result,
                    started=0.0,
                    temp_owner=mock.Mock(),
                    source_srt=Path("source.srt"),
                    translated_srt=Path("translated.srt"),
                    destination=video.with_suffix(".srt"),
                )
            finally:
                with activity_lock:
                    active_prepares -= 1

        def fake_translate(prepared, _key, _progress):
            translate_threads.add(threading.current_thread().name)
            translate_progress[prepared.video.name] = _progress
            events.append(f"translate:{prepared.video.name}")
            if prepared.video == videos[0]:
                first_translation_started.set()
                self.assertTrue(second_preparation_started.wait(timeout=1))
            prepared.result.status = "success"
            return prepared.result

        with mock.patch.object(batch, "_prepare_video", side_effect=fake_prepare), mock.patch.object(
            batch, "_process_downstream_video", side_effect=fake_translate
        ):
            results = batch.process_video_group(
                videos, config, "secret", progress, overwrite=False
            )

        self.assertLess(
            events.index("translate:video-1.mp4"),
            events.index("prepare:video-2.mp4"),
        )
        self.assertEqual(
            [event for event in events if event.startswith("prepare:")],
            [f"prepare:{video.name}" for video in videos],
        )
        self.assertEqual(maximum_active_prepares, 1)
        self.assertEqual(prepare_threads, {threading.current_thread().name})
        for index, video in enumerate(videos):
            self.assertIs(prepare_progress[video.name], video_progress[index])
            self.assertIs(translate_progress[video.name], video_progress[index])
        self.assertTrue(
            all(name.startswith("videocaptioner-downstream") for name in translate_threads)
        )
        self.assertEqual([result.status for result in results], ["success"] * 3)

    def test_pipeline_overlaps_source_cleanup_with_next_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            videos = [root / "first.mp4", root / "second.mp4"]
            for video in videos:
                video.write_bytes(b"video")
            config = batch.BatchConfig("ja", "zh-Hans")
            progress = mock.Mock()
            cleanup_started = threading.Event()
            second_transcription_started = threading.Event()
            cleanup_threads: list[str] = []
            cleanup_calls = 0
            cleanup_lock = threading.Lock()
            real_cleanup = batch.clean_source_srt_file

            def fake_transcribe(video, output, _config, _progress):
                if video == videos[1]:
                    self.assertTrue(cleanup_started.wait(timeout=1))
                    second_transcription_started.set()
                output.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\n原文\n",
                    encoding="utf-8",
                )

            def blocking_cleanup(path):
                nonlocal cleanup_calls
                with cleanup_lock:
                    cleanup_calls += 1
                    current_call = cleanup_calls
                cleanup_threads.append(threading.current_thread().name)
                if current_call == 1:
                    cleanup_started.set()
                    self.assertTrue(second_transcription_started.wait(timeout=1))
                return real_cleanup(path)

            def fake_translate(
                _video, _source, output, _config, _key, _progress, _usage
            ):
                output.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\n译文\n原文\n",
                    encoding="utf-8",
                )

            with mock.patch.object(
                batch, "_run_transcription", side_effect=fake_transcribe
            ), mock.patch.object(
                batch, "clean_source_srt_file", side_effect=blocking_cleanup
            ), mock.patch.object(
                batch, "_run_translation", side_effect=fake_translate
            ):
                results = batch.process_video_group(
                    videos, config, "secret", progress, overwrite=False
                )

            self.assertTrue(second_transcription_started.is_set())
            self.assertEqual(cleanup_calls, 2)
            self.assertTrue(
                all(name.startswith("videocaptioner-downstream") for name in cleanup_threads)
            )
            self.assertEqual([result.status for result in results], ["success", "success"])

    def test_run_batch_uses_one_sliding_pipeline_for_the_whole_round(self) -> None:
        videos = [Path(f"video-{index}.mp4") for index in range(1, 7)]
        pipeline_calls: list[list[Path]] = []

        def fake_group(
            current_videos, _config, _key, _progress, _overwrite, **_kwargs
        ):
            pipeline_calls.append(list(current_videos))
            return [
                batch.VideoResult(video=str(video), status="success")
                for video in current_videos
            ]

        with mock.patch.object(
            batch, "load_batch_config", return_value=batch.BatchConfig("ja", "zh-Hans")
        ), mock.patch.object(batch, "_prepare_shared_runtime"), mock.patch.object(
            batch, "discover_videos", return_value=videos
        ), mock.patch.object(
            batch, "read_deepseek_key", return_value="secret"
        ), mock.patch.object(
            batch, "process_video_group", side_effect=fake_group
        ), mock.patch.object(
            batch, "write_reports", return_value=(Path("report.json"), Path("report.csv"))
        ), mock.patch.object(batch, "print_summary"), mock.patch.object(
            batch, "BatchEventJournal", return_value=mock.Mock()
        ):
            self.assertEqual(batch._run_batch("videos", overwrite=False), 0)

        self.assertEqual(pipeline_calls, [videos])

    def test_process_video_reports_publish_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            video = Path(temp_name) / "sample.mp4"
            video.write_bytes(b"video")
            config = batch.BatchConfig("ja", "zh-Hans")
            progress = batch.ConsoleProgress(1)
            progress.begin(1, video)

            def fake_transcribe(_video, output, _config, _progress):
                output.write_text("1\n00:00:00,000 --> 00:00:01,000\n原文\n", encoding="utf-8")

            def fake_translate(_video, _source, output, _config, _key, _progress, _usage):
                output.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\n译文\n原文\n",
                    encoding="utf-8",
                )

            with mock.patch.object(
                batch, "_run_transcription", side_effect=fake_transcribe
            ), mock.patch.object(
                batch, "_run_translation", side_effect=fake_translate
            ), mock.patch.object(
                batch, "_publish_srt_atomically", side_effect=OSError("denied")
            ):
                result = batch.process_video(
                    video, config, "secret", progress, overwrite=False
                )

            progress.finish_line()
            self.assertEqual(result.status, "error")

    def test_overwrite_failure_keeps_existing_same_name_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            video = root / "sample.mp4"
            destination = root / "sample.srt"
            video.write_bytes(b"video")
            destination.write_text("无法验证的旧字幕", encoding="utf-8")
            config = batch.BatchConfig("ja", "zh-Hans")
            progress = batch.ConsoleProgress(1)
            progress.begin(1, video)

            with mock.patch.object(
                batch, "_run_transcription", side_effect=RuntimeError("ASR failed")
            ):
                result = batch.process_video(video, config, "secret", progress, overwrite=True)
            progress.finish_line()
            self.assertEqual(result.status, "error")
            self.assertEqual(destination.read_text(encoding="utf-8"), "无法验证的旧字幕")

    def test_write_reports_stays_inside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            project_root = Path(temp_name) / "project"
            input_root = Path(temp_name) / "external-videos"
            input_root.mkdir()
            result = batch.VideoResult(
                video=str(input_root / "sample.mp4"),
                status="success",
                raw_source_sentence_count=10,
                source_sentence_count=7,
                final_sentence_count=7,
                asr_removed_consecutive_duplicates=3,
                asr_removed_invalid_timelines=2,
            )
            result.token_usage = batch.TokenUsage(
                api_requests=1,
                input_cache_miss_tokens=20,
                output_tokens=30,
                total_tokens=50,
                estimated_cost_cny=0.00026,
            )

            with mock.patch.object(batch, "PROJECT_ROOT", project_root):
                json_path, csv_path = batch.write_reports(input_root, [result])

            expected_dir = project_root / "~outputs-final" / "batch-reports"
            self.assertEqual(json_path.parent, expected_dir)
            self.assertEqual(csv_path.parent, expected_dir)
            self.assertTrue(json_path.is_file())
            self.assertTrue(csv_path.is_file())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["token_usage"]["total_tokens"], 50)
            self.assertEqual(payload["summary"]["total_raw_source_sentences"], 10)
            self.assertEqual(payload["summary"]["total_source_sentences"], 7)
            self.assertEqual(
                payload["summary"]["total_asr_removed_consecutive_duplicates"],
                3,
            )
            self.assertEqual(
                payload["summary"]["total_asr_removed_invalid_timelines"],
                2,
            )
            self.assertIn("raw_source_sentence_count", csv_path.read_text(encoding="utf-8-sig"))
            self.assertIn(
                "asr_removed_invalid_timelines",
                csv_path.read_text(encoding="utf-8-sig"),
            )
            self.assertEqual(payload["pricing"]["model"], "deepseek-flash")
            self.assertEqual(payload["pricing"]["currency"], "CNY")
            self.assertEqual(payload["pricing"]["rates"]["peak"]["output"], 8.0)

    def test_reports_include_failed_token_and_cost_subtotal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            project_root = Path(temp_name) / "project"
            failed = batch.VideoResult(video="failed.mp4", status="error")
            failed.token_usage = batch.TokenUsage(
                api_requests=3,
                input_cache_hit_tokens=100,
                input_cache_miss_tokens=200,
                output_tokens=50,
                total_tokens=350,
                estimated_cost_cny=0.0025,
            )
            success = batch.VideoResult(video="success.mp4", status="success")
            success.token_usage = batch.TokenUsage(
                api_requests=1,
                total_tokens=25,
                estimated_cost_cny=0.0001,
            )

            with mock.patch.object(batch, "PROJECT_ROOT", project_root):
                json_path, _ = batch.write_reports(Path(temp_name), [failed, success])

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["token_usage"]["total_tokens"], 375)
            self.assertEqual(payload["summary"]["failed_token_usage"]["api_requests"], 3)
            self.assertEqual(payload["summary"]["failed_token_usage"]["total_tokens"], 350)
            self.assertEqual(
                payload["summary"]["failed_token_usage"]["estimated_cost_cny"],
                0.0025,
            )

    def test_process_video_does_not_publish_blank_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            video = Path(temp_name) / "sample.mp4"
            video.write_bytes(b"video")
            config = batch.BatchConfig("ja", "zh-Hans")
            progress = batch.ConsoleProgress(1)
            progress.begin(1, video)

            def fake_transcribe(_video, output, _config, _progress):
                output.write_text("1\n00:00:00,000 --> 00:00:01,000\n原文\n", encoding="utf-8")

            def fake_translate(_video, _source, output, _config, _key, _progress, _usage):
                output.write_text("1\n00:00:00,000 --> 00:00:01,000\n\n", encoding="utf-8")

            with mock.patch.object(batch, "_run_transcription", side_effect=fake_transcribe), mock.patch.object(
                batch, "_run_translation", side_effect=fake_translate
            ):
                result = batch.process_video(video, config, "secret", progress, overwrite=False)
            progress.finish_line()
            self.assertEqual(result.status, "error")
            self.assertEqual(result.errors[-1].category, "invalid_or_blank_srt")
            self.assertFalse(Path(result.output).exists())

    def test_process_video_does_not_retry_blank_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            video = Path(temp_name) / "quiet.mp4"
            video.write_bytes(b"video")
            config = batch.BatchConfig("ja", "zh-Hans")
            progress = batch.ConsoleProgress(1)
            progress.begin(1, video)
            transcribe_calls: list[Path] = []

            def fake_transcribe(_video, output, _config, _progress):
                transcribe_calls.append(output)
                output.write_text("", encoding="utf-8")

            with mock.patch.object(
                batch, "_run_transcription", side_effect=fake_transcribe
            ), mock.patch.object(
                batch, "_run_translation"
            ) as translate:
                result = batch.process_video(
                    video, config, "secret", progress, overwrite=False
                )

            progress.finish_line()
            self.assertEqual(len(transcribe_calls), 1)
            translate.assert_not_called()
            self.assertEqual(result.status, "error")
            self.assertEqual(result.errors[-1].category, "invalid_or_blank_srt")

    def test_process_video_classifies_all_reversed_timelines_as_invalid_srt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            video = Path(temp_name) / "reversed.mp4"
            video.write_bytes(b"video")
            config = batch.BatchConfig("ja", "zh-Hans")
            progress = batch.ConsoleProgress(1)
            progress.begin(1, video)

            def fake_transcribe(_video, output, _config, _progress):
                output.write_text(
                    "1\n00:00:02,000 --> 00:00:01,000\n原文\n",
                    encoding="utf-8",
                )

            with mock.patch.object(
                batch, "_run_transcription", side_effect=fake_transcribe
            ), mock.patch.object(batch, "_run_translation") as translate:
                result = batch.process_video(
                    video, config, "secret", progress, overwrite=False
                )

            progress.finish_line()
            translate.assert_not_called()
            self.assertEqual(result.status, "error")
            self.assertEqual(result.errors[-1].category, "invalid_or_blank_srt")

    def test_deepseek_error_is_deferred_to_round_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            video = Path(temp_name) / "sample.mp4"
            video.write_bytes(b"video")
            config = batch.BatchConfig("ja", "zh-Hans")
            progress = batch.ConsoleProgress(1)
            progress.begin(1, video)

            def fake_transcribe(_video, output, _config, _progress):
                output.write_text("1\n00:00:00,000 --> 00:00:01,000\n原文\n", encoding="utf-8")

            with mock.patch.object(batch, "_run_transcription", side_effect=fake_transcribe), mock.patch.object(
                batch, "_run_translation", side_effect=batch.StageError("translate", "429 rate limit")
            ), mock.patch("builtins.input") as user_input:
                result = batch.process_video(video, config, "secret", progress, overwrite=False)
            user_input.assert_not_called()
            self.assertEqual(result.status, "error")
            self.assertEqual(result.errors[-1].category, "deepseek_rate_limit")

    def test_incomplete_deepseek_output_is_deferred_to_round_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            video = Path(temp_name) / "sample.mp4"
            video.write_bytes(b"video")
            config = batch.BatchConfig("ja", "zh-Hans")
            progress = batch.ConsoleProgress(1)
            progress.begin(1, video)

            def fake_transcribe(_video, output, _config, _progress):
                output.write_text("1\n00:00:00,000 --> 00:00:01,000\n原文。\n", encoding="utf-8")

            def fake_translate(_video, _source, output, _config, _key, _progress, _usage):
                output.write_text("1\n00:00:00,000 --> 00:00:01,000\n原文\n", encoding="utf-8")

            with mock.patch.object(batch, "_run_transcription", side_effect=fake_transcribe), mock.patch.object(
                batch, "_run_translation", side_effect=fake_translate
            ), mock.patch("builtins.input") as user_input:
                result = batch.process_video(video, config, "secret", progress, overwrite=False)
            user_input.assert_not_called()
            self.assertEqual(result.status, "error")
            self.assertEqual(result.errors[-1].category, "deepseek_incomplete_output")

    def test_round_failure_list_shows_full_paths_and_reasons(self) -> None:
        deepseek = batch.VideoResult(video=r"V:\folder\deepseek.mp4", status="error")
        deepseek.errors.append(
            batch.ErrorEvent("deepseek_server", "translate", "empty choices or content")
        )
        blank = batch.VideoResult(video=r"V:\folder\blank.mp4", status="error")
        blank.errors.append(
            batch.ErrorEvent("invalid_or_blank_srt", "validate", "SRT 为空白")
        )
        output = io.StringIO()
        with mock.patch.object(batch.sys, "stdout", output):
            batch._print_failed_videos([deepseek, blank])
        text = output.getvalue()
        self.assertIn("当前仍失败视频（2）", text)
        self.assertIn(r"[deepseek_server] V:\folder\deepseek.mp4", text)
        self.assertIn("原因：empty choices or content", text)
        self.assertIn(r"[invalid_or_blank_srt] V:\folder\blank.mp4", text)
        self.assertIn("原因：SRT 为空白", text)

    def test_round_summary_counts_created_skipped_and_failed_results(self) -> None:
        results = [
            batch.VideoResult(
                video=r"V:\folder\created-1.mp4",
                output=r"V:\folder\created-1.srt",
                status="success",
                final_sentence_count=12,
            ),
            batch.VideoResult(
                video=r"V:\folder\created-2.mp4",
                output=r"V:\folder\created-2.srt",
                status="success",
                final_sentence_count=8,
            ),
            batch.VideoResult(
                video=r"V:\folder\existing.mp4",
                output=r"V:\folder\existing.srt",
                status="updated_existing",
                final_sentence_count=6,
            ),
            batch.VideoResult(video="failed.mp4", status="error"),
        ]
        output = io.StringIO()

        with mock.patch.object(batch.sys, "stdout", output):
            batch._print_round_summary(results)

        text = output.getvalue()
        self.assertIn("=== 本轮统计 ===", text)
        self.assertIn("新增 SRT 2 | 更新已有 SRT 1 | 失败 1", text)
        self.assertIn("新增 SRT 详情：", text)
        self.assertIn(r"V:\folder\created-1.srt（12 句）", text)
        self.assertIn(r"V:\folder\created-2.srt（8 句）", text)
        self.assertIn("更新已有 SRT 详情：", text)
        self.assertIn(r"V:\folder\existing.srt（6 句）", text)
        self.assertNotIn("failed.srt", text)

    def test_progress_uses_a_fixed_multiline_region_per_video(self) -> None:
        progress = batch.ConsoleProgress(1)
        state = batch._ProgressState(
            index=1,
            video=r"V:\很长的目录名称\第二层很长的目录名称\video1.mp4",
            percent=55,
            message="转录：处理中",
            columns=32,
        )
        lines = progress._render_lines(state)

        self.assertGreater(len(lines), 3)
        self.assertIn("55%", lines[0])
        self.assertEqual(lines[1], "    当前转录")
        self.assertTrue(all(line.startswith("    ") for line in lines[1:]))
        self.assertTrue(all(batch._display_width(line) <= 32 for line in lines))
        self.assertEqual("".join(line[4:] for line in lines[2:]), state.video)

    def test_run_batch_processes_each_video_exactly_once(self) -> None:
        first = Path("first.mp4")
        second = Path("second.mp4")
        events: list[str] = []

        def fake_group(
            videos, current_config, key, progress, current_overwrite, **kwargs
        ):
            self.assertEqual(kwargs, {"start_index": 1})
            events.extend(video.name for video in videos)
            failed = batch.VideoResult(video=str(first), status="error")
            failed.errors.append(
                batch.ErrorEvent("invalid_or_blank_srt", "validate", "blank")
            )
            return [failed, batch.VideoResult(video=str(second), status="success")]

        captured: list[batch.VideoResult] = []
        journal = mock.Mock()
        with mock.patch.object(batch, "load_batch_config", return_value=batch.BatchConfig("ja", "zh-Hans")), mock.patch.object(
            batch, "_prepare_shared_runtime"
        ), mock.patch.object(batch, "discover_videos", return_value=[first, second]), mock.patch.object(
            batch, "read_deepseek_key", return_value="secret"
        ), mock.patch.object(batch, "process_video_group", side_effect=fake_group), mock.patch.object(
            batch, "write_reports", return_value=(Path("report.json"), Path("report.csv"))
        ), mock.patch.object(
            batch, "print_summary", side_effect=lambda results, _paths: captured.extend(results)
        ), mock.patch.object(
            batch, "BatchEventJournal", return_value=journal
        ):
            exit_code = batch._run_batch("videos", overwrite=False)

        self.assertEqual(exit_code, 1)
        self.assertEqual(events, ["first.mp4", "second.mp4"])
        self.assertEqual([item.status for item in captured], ["error", "success"])
        self.assertEqual(captured[0].errors[-1].category, "invalid_or_blank_srt")
        round_events = [
            call.kwargs
            for call in journal.write.call_args_list
            if call.args and call.args[0] == "round_finished"
        ]
        self.assertEqual(
            [
                (
                    event["success_count"],
                    event["updated_existing_count"],
                    event["round_failed_count"],
                )
                for event in round_events
            ],
            [(1, 0, 1)],
        )
        self.assertEqual(round_events[0]["success_videos"], [str(second)])
        self.assertEqual(round_events[0]["round_failed_videos"], [str(first)])


if __name__ == "__main__":
    unittest.main()
