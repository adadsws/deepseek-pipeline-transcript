"""VAD 关闭时连续重复 ASR 文本清理的纯本地回归测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "local_patch"))

from videocaptioner_asr_cleanup import (  # noqa: E402
    SourceCue,
    clean_source_cues,
    clean_source_srt_file,
    normalise_repetition_text,
    validate_source_srt_file,
)


class ASRCleanupTests(unittest.TestCase):
    def test_consecutive_duplicates_keep_first_regardless_of_time_gap(self) -> None:
        cues = [
            SourceCue(0, 1_000, "同じ字幕。"),
            SourceCue(60_000, 61_000, "同じ字幕。"),
            SourceCue(240_000, 241_000, "同じ字幕。"),
        ]
        cleaned, stats = clean_source_cues(cues)
        self.assertEqual([(cue.start_ms, cue.end_ms) for cue in cleaned], [(0, 1_000)])
        self.assertEqual(stats.removed_consecutive_duplicates, 2)

    def test_intervening_text_starts_a_new_duplicate_group(self) -> None:
        cues = [
            SourceCue(0, 1_000, "繰り返しです。"),
            SourceCue(30_000, 31_000, "繰り返しです。"),
            SourceCue(60_000, 61_000, "別の発話。"),
            SourceCue(90_000, 91_000, "繰り返しです。"),
        ]
        cleaned, stats = clean_source_cues(cues)
        self.assertEqual([cue.start_ms for cue in cleaned], [0, 60_000, 90_000])
        self.assertEqual(stats.removed_consecutive_duplicates, 1)

    def test_normalisation_groups_spacing_and_punctuation_variants(self) -> None:
        self.assertEqual(
            normalise_repetition_text("少 し 休 憩してください..."),
            normalise_repetition_text("少し休憩してください。"),
        )
        cues = [
            SourceCue(0, 1_000, "少 し 休 憩してください..."),
            SourceCue(120_000, 121_000, "少し休憩してください。"),
        ]
        cleaned, stats = clean_source_cues(cues)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(stats.removed_consecutive_duplicates, 1)

    def test_nonduplicate_short_cues_and_punctuation_are_preserved(self) -> None:
        cues = [
            SourceCue(0, 100, "はい。"),
            SourceCue(100, 200, "先生。"),
            SourceCue(200, 300, "……"),
            SourceCue(300, 400, "……"),
        ]
        cleaned, stats = clean_source_cues(cues)
        self.assertEqual([cue.text for cue in cleaned], ["はい。", "先生。", "……", "……"])
        self.assertEqual(stats.removed_consecutive_duplicates, 0)
        self.assertEqual(stats.removed_invalid_timelines, 0)

    def test_reversed_timeline_is_removed_and_remaining_cues_are_reindexed(self) -> None:
        source = (
            "1\n00:00:01,000 --> 00:00:02,000\n最初の字幕。\n\n"
            "2\n00:00:04,000 --> 00:00:03,000\n壊れた時間軸。\n\n"
            "3\n00:00:05,000 --> 00:00:05,000\nゼロ秒は保持。\n"
        )
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.srt"
            path.write_text(source, encoding="utf-8")
            stats = clean_source_srt_file(path)
            output = path.read_text(encoding="utf-8")

        self.assertEqual(stats.original_cues, 3)
        self.assertEqual(stats.final_cues, 2)
        self.assertEqual(stats.removed_invalid_timelines, 1)
        self.assertNotIn("壊れた時間軸。", output)
        self.assertIn("2\n00:00:05,000 --> 00:00:05,000\nゼロ秒は保持。", output)

    def test_replacement_character_cue_is_removed_and_remaining_cues_are_reindexed(
        self,
    ) -> None:
        source = (
            "1\n00:00:01,000 --> 00:00:02,000\n最初の字幕。\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n허\ufffd hair\n\n"
            "3\n00:00:05,000 --> 00:00:06,000\n最後の字幕。\n"
        )
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.srt"
            path.write_text(source, encoding="utf-8")
            self.assertEqual(validate_source_srt_file(path), 3)
            stats = clean_source_srt_file(path)
            output = path.read_text(encoding="utf-8")

        self.assertEqual(stats.original_cues, 3)
        self.assertEqual(stats.final_cues, 2)
        self.assertEqual(stats.removed_invalid_encoding_cues, 1)
        self.assertNotIn("\ufffd", output)
        self.assertNotIn("허", output)
        self.assertIn("2\n00:00:05,000 --> 00:00:06,000\n最後の字幕。", output)

    def test_only_replacement_character_cues_cannot_produce_an_empty_srt(self) -> None:
        source = "1\n00:00:00,000 --> 00:00:01,000\n壊れた\ufffd字幕\n"
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.srt"
            path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ASR 清理后没有剩余字幕"):
                clean_source_srt_file(path)

    def test_only_reversed_timelines_cannot_produce_an_empty_srt(self) -> None:
        source = "1\n00:00:02,000 --> 00:00:01,000\n壊れた時間軸。\n"
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.srt"
            path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ASR 清理后没有剩余字幕"):
                clean_source_srt_file(path)

    def test_file_cleanup_reindexes_without_extending_first_timeline(self) -> None:
        source = (
            "1\n00:00:00,000 --> 00:00:01,000\n同じ字幕。\n\n"
            "2\n00:01:00,000 --> 00:01:01,000\n同じ字幕。\n\n"
            "3\n00:02:00,000 --> 00:02:01,000\n別の発話。\n"
        )
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "source.srt"
            path.write_text(source, encoding="utf-8")
            stats = clean_source_srt_file(path)
            output = path.read_text(encoding="utf-8")
        self.assertEqual(stats.final_cues, 2)
        self.assertIn("1\n00:00:00,000 --> 00:00:01,000\n同じ字幕。", output)
        self.assertIn("2\n00:02:00,000 --> 00:02:01,000\n別の発話。", output)
        self.assertNotIn("00:01:00,000", output)


if __name__ == "__main__":
    unittest.main()
