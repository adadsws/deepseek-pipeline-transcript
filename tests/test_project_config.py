"""三层 TOML 配置及 GUI 兼容转换回归测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "local_patch"))

import videocaptioner_project_config as project_config


class ProjectConfigTests(unittest.TestCase):
    def test_batch_and_gui_overrides_are_exact_pairs(self) -> None:
        project_config.validate_mode_pair()

    def test_single_side_unknown_option_is_rejected(self) -> None:
        with self.assertRaisesRegex(project_config.ProjectConfigError, "仅 batch"):
            project_config.validate_mode_pair(
                {"section": {"known": True, "unknown": True}},
                {"section": {"known": False}},
            )

    def test_local_override_replaces_existing_value(self) -> None:
        merged = project_config._merge_local_override(
            {"intro_subtitle": {"enabled": False, "duration_seconds": 3.0}},
            {"intro_subtitle": {"enabled": True}},
        )
        self.assertTrue(merged["intro_subtitle"]["enabled"])
        self.assertEqual(merged["intro_subtitle"]["duration_seconds"], 3.0)

    def test_local_override_rejects_unknown_value(self) -> None:
        with self.assertRaisesRegex(project_config.ProjectConfigError, "未知项"):
            project_config._merge_local_override(
                {"intro_subtitle": {"enabled": False}},
                {"intro_subtitle": {"unknown": True}},
            )

    def test_local_override_rejects_type_change(self) -> None:
        with self.assertRaisesRegex(project_config.ProjectConfigError, "类型不一致"):
            project_config._merge_local_override(
                {"intro_subtitle": {"duration_seconds": 3.0}},
                {"intro_subtitle": {"duration_seconds": "3"}},
            )

    def test_languages_are_first_business_section(self) -> None:
        for mode in ("batch", "gui"):
            path = project_config.MODE_CONFIG_PATHS[mode]
            first_table = next(
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("[")
            )
            self.assertEqual(first_table, "[language.video]")

    def test_gui_project_features_are_all_disabled(self) -> None:
        gui = project_config.load_mode_settings("gui")
        self.assertTrue(gui["project_features"])
        self.assertFalse(any(gui["project_features"].values()))

    def test_batch_keeps_vad_and_request_retry_disabled(self) -> None:
        batch = project_config.load_mode_settings("batch")
        self.assertEqual(batch["transcription"]["model"], "medium")
        self.assertFalse(batch["transcription"]["vad_filter"])
        self.assertFalse(batch["project_features"]["request_level_auto_retry"])
        self.assertTrue(batch["project_features"]["video_level_translation"])
        self.assertTrue(
            batch["project_features"]["asr_consecutive_duplicate_cleanup"]
        )
        self.assertEqual(batch["translation_batching"]["reasoning_effort"], "none")
        self.assertEqual(batch["translation_batching"]["max_in_flight"], 5)
        self.assertNotIn("incomplete_response_attempts", batch["translation_batching"])
        self.assertNotIn("round_end_retry_menu", batch["interaction"])
        self.assertNotIn("default_retry_choice", batch["interaction"])
        self.assertNotIn("transcription_cleanup", batch)
        intro = batch["intro_subtitle"]
        self.assertIsInstance(intro["enabled"], bool)
        self.assertIsInstance(intro["text"], str)
        self.assertEqual(intro["duration_seconds"], 3.0)
        if intro["enabled"]:
            self.assertTrue(intro["text"].strip())

    def test_tracked_batch_intro_is_disabled_by_default(self) -> None:
        batch = project_config._read_toml(project_config.MODE_CONFIG_PATHS["batch"])
        self.assertEqual(
            batch["intro_subtitle"],
            {"enabled": False, "text": "", "duration_seconds": 3.0},
        )

    def test_literal_tilde_category_stays_inside_project(self) -> None:
        path = project_config.resolve_project_path("~temp/VideoCaptioner/cache")
        self.assertEqual(path, PROJECT_ROOT / "~temp" / "VideoCaptioner" / "cache")

    def test_managed_path_outside_project_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            shared_text = project_config.SHARED_CONFIG_PATH.read_text(encoding="utf-8")
            shared_text = shared_text.replace(
                'cache = "~temp/VideoCaptioner/cache"', 'cache = "C:/Windows/Temp"'
            )
            shared_path = Path(temp_name) / "shared.toml"
            shared_path.write_text(shared_text, encoding="utf-8")
            with self.assertRaisesRegex(project_config.ProjectConfigError, "不得越出"):
                project_config.load_mode_settings("batch", shared_path=shared_path)

    def test_gui_qconfig_uses_upstream_defaults(self) -> None:
        gui = project_config.load_mode_settings("gui")
        qconfig = project_config.build_gui_qconfig(gui)
        self.assertEqual(qconfig["Transcribe"]["TranscribeLanguage"], "自动检测")
        self.assertEqual(qconfig["Transcribe"]["TranscribeModel"], "B 接口")
        self.assertEqual(qconfig["Subtitle"]["TargetLanguage"], "简体中文")
        self.assertFalse(qconfig["Subtitle"]["NeedTranslate"])
        self.assertEqual(qconfig["Translate"]["TranslatorServiceEnum"], "微软翻译")
        self.assertTrue(qconfig["FasterWhisper"]["VadFilter"])
        self.assertEqual(qconfig["FasterWhisper"]["Model"], "tiny")
        self.assertTrue(qconfig["FasterWhisper"]["OneWord"])
        self.assertEqual(qconfig["LLM"]["LLMService"], "OpenAI 兼容")
        self.assertEqual(qconfig["LLM"]["DeepSeek_Model"], "deepseek-chat")
        self.assertEqual(qconfig["LLM"]["DeepSeek_API_Base"], "https://api.deepseek.com/v1")
        self.assertEqual(qconfig["LLM"]["DeepSeek_API_Key"], "")
        self.assertTrue(qconfig["Video"]["NeedVideo"])
        self.assertFalse(qconfig["Video"]["SoftSubtitle"])
        self.assertEqual(qconfig["Video"]["VideoQuality"], "中等质量")

    def test_config_directory_contains_only_toml_text(self) -> None:
        files = [item for item in (PROJECT_ROOT / "config").rglob("*") if item.is_file()]
        self.assertTrue(files)
        self.assertTrue(all(item.suffix == ".toml" for item in files))


if __name__ == "__main__":
    unittest.main()
