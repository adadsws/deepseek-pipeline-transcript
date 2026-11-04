"""独立媒体检查器的图片与时间区间回归测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "local_patch"))
from videocaptioner_media_checker import check_media, merge_ranges


class MediaCheckerTests(unittest.TestCase):
    def test_image_full_decode_detects_truncation(self) -> None:
        fixtures = Path(__file__).parent / "fixtures"
        self.assertEqual(check_media(fixtures / "media-check-clean.png")["status"], "ok")
        self.assertEqual(check_media(fixtures / "media-check-truncated.png")["status"], "corrupt")

    def test_merge_ranges_adds_padding_without_shifting_timeline(self) -> None:
        self.assertEqual(
            merge_ranges([(74, 75), (75, 76), (94, 95)], padding=1, duration=100),
            [[73, 77], [93, 96]],
        )
