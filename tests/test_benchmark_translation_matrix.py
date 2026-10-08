import json
import tempfile
import unittest
from pathlib import Path

from scripts.benchmark_translation_matrix import (
    load_corpus,
    split_subtitles,
    validate_response,
)


class BenchmarkTranslationMatrixTests(unittest.TestCase):
    def test_load_corpus_merges_latest_chunks_and_orders_indices(self):
        entries = [
            {
                "stage": "translate",
                "file_name": "mdvr00423-test.mp4",
                "request": {
                    "model": "deepseek-flash",
                    "messages": [
                        {"role": "system", "content": "Return JSON"},
                        {"role": "user", "content": '{"2": "b"}'},
                    ],
                },
            },
            {
                "stage": "translate",
                "file_name": "mdvr00423-test.mp4",
                "request": {
                    "model": "deepseek-flash",
                    "messages": [
                        {"role": "system", "content": "Return JSON"},
                        {"role": "user", "content": '{"1": "a"}'},
                    ],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            log_path = Path(temp_name) / "requests.jsonl"
            log_path.write_text(
                "\n".join(json.dumps(entry) for entry in entries), encoding="utf-8"
            )
            corpus = load_corpus((log_path,), "mdvr00423")

        video = corpus["mdvr00423-test.mp4"]
        self.assertEqual(video["subtitles"], {"1": "a", "2": "b"})
        self.assertEqual(video["system_prompt"], "Return JSON")

    def test_split_subtitles_supports_both_matrix_granularities(self):
        subtitles = {str(index): f"line {index}" for index in range(1, 24)}
        self.assertEqual([len(chunk) for chunk in split_subtitles(subtitles, False)], [10, 10, 3])
        self.assertEqual([len(chunk) for chunk in split_subtitles(subtitles, True)], [23])

    def test_validate_response_rejects_missing_or_empty_items(self):
        expected = {"1": "a", "2": "b"}
        validate_response('{"1":"甲","2":"乙"}', expected)
        with self.assertRaisesRegex(ValueError, "Key mismatch"):
            validate_response('{"1":"甲"}', expected)
        with self.assertRaisesRegex(ValueError, "Empty translations"):
            validate_response('{"1":"甲","2":""}', expected)


if __name__ == "__main__":
    unittest.main()
