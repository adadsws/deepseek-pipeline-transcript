import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "local_patch"))

from videocaptioner_output_format_fix import FullPipelineOutputPolicy, OutputPolicyRegistry


class FullPipelineOutputPolicyTests(unittest.TestCase):
    def make_policy(self, selected_format: str) -> FullPipelineOutputPolicy:
        return FullPipelineOutputPolicy(
            task_id="task-1",
            video_path=Path("C:/media/demo.mp4"),
            raw_base_path=Path("C:/work/demo/subtitle/original.srt"),
            selected_format=selected_format,
        )

    def test_txt_keeps_only_txt_in_the_video_folder(self) -> None:
        policy = self.make_policy("TXT")

        self.assertFalse(policy.allows_video_export(Path("C:/media/demo.srt")))
        self.assertFalse(policy.allows_video_export(Path("C:/media/demo.ass")))
        self.assertEqual(
            policy.raw_exports_to_publish(),
            [(Path("C:/work/demo/subtitle/original.txt"), Path("C:/media/demo.txt"))],
        )

    def test_srt_and_ass_each_allow_only_their_selected_export(self) -> None:
        srt_policy = self.make_policy("SRT")
        ass_policy = self.make_policy("ASS")

        self.assertTrue(srt_policy.allows_video_export(Path("C:/media/demo.srt")))
        self.assertFalse(srt_policy.allows_video_export(Path("C:/media/demo.ass")))
        self.assertFalse(ass_policy.allows_video_export(Path("C:/media/demo.srt")))
        self.assertTrue(ass_policy.allows_video_export(Path("C:/media/demo.ass")))

    def test_all_keeps_srt_and_ass_and_publishes_txt_and_vtt(self) -> None:
        policy = self.make_policy("All")

        self.assertTrue(policy.allows_video_export(Path("C:/media/demo.srt")))
        self.assertTrue(policy.allows_video_export(Path("C:/media/demo.ass")))
        self.assertEqual(
            policy.raw_exports_to_publish(),
            [
                (Path("C:/work/demo/subtitle/original.txt"), Path("C:/media/demo.txt")),
                (Path("C:/work/demo/subtitle/original.vtt"), Path("C:/media/demo.vtt")),
            ],
        )

    def test_policy_recognises_only_its_own_final_exports(self) -> None:
        policy = FullPipelineOutputPolicy(
            task_id="task-with-dot",
            video_path=Path("C:/media/freedl.org@fc3845406_1.mp4"),
            raw_base_path=Path("C:/work/demo/subtitle/original.srt"),
            selected_format="SRT",
        )

        self.assertEqual(
            policy.final_export_format(Path("C:/media/freedl.org@fc3845406_1.srt")),
            "srt",
        )
        self.assertEqual(
            policy.final_export_format(Path("C:/media/freedl.org@fc3845406_1.ass")),
            "ass",
        )
        self.assertIsNone(
            policy.final_export_format(Path("C:/media/unrelated.srt")),
        )

    def test_registry_transfers_policy_by_raw_subtitle_path(self) -> None:
        policy = FullPipelineOutputPolicy(
            task_id="task-raw-path",
            video_path=Path("C:/media/demo.mp4"),
            raw_base_path=Path("C:/work/demo/subtitle/original.srt"),
            selected_format="ASS",
        )
        registry = OutputPolicyRegistry()
        registry._policies[policy.raw_base_path] = policy

        self.assertIs(
            registry.for_raw_subtitle(Path("C:/work/demo/subtitle/original.srt")),
            policy,
        )


if __name__ == "__main__":
    unittest.main()
