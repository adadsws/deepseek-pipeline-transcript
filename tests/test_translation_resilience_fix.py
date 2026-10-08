"""DeepSeek 翻译韧性补丁的纯本地回归测试。"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MODULE_PATH = (
    Path(__file__).parents[1]
    / "local_patch"
    / "videocaptioner_translation_resilience_fix.py"
)
SPEC = importlib.util.spec_from_file_location(
    "videocaptioner_translation_resilience_fix_test", MODULE_PATH
)
assert SPEC and SPEC.loader
resilience = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = resilience
SPEC.loader.exec_module(resilience)


def response(content: str | None, finish_reason: str = "stop") -> object:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ]
    )


class TranslationResilienceFixTests(unittest.TestCase):
    def test_batch_request_options_disable_thinking_and_bound_json_output(self) -> None:
        options = resilience.batch_video_request_options()
        self.assertEqual(options["reasoning_effort"], "none")
        self.assertEqual(options["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(options["response_format"], {"type": "json_object"})
        self.assertEqual(options["max_tokens"], 32000)
        self.assertIn("json", resilience.ensure_json_instruction("output dictionary").lower())
        existing = "Output valid JSON"
        self.assertEqual(resilience.ensure_json_instruction(existing), existing)

    def test_sdk_and_business_layers_send_exactly_one_request(self) -> None:
        expected = response('{"1":"译文"}')
        create = mock.Mock(return_value=expected)
        no_retry_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        base_client = SimpleNamespace(
            with_options=mock.Mock(return_value=no_retry_client)
        )
        llm_module = SimpleNamespace(
            get_llm_client=mock.Mock(return_value=base_client),
            log_llm_response=mock.Mock(),
        )

        actual = resilience.call_llm_api_once(
            llm_module,
            [{"role": "user", "content": "test"}],
            "deepseek-flash",
        )

        self.assertIs(actual, expected)
        base_client.with_options.assert_called_once_with(max_retries=0)
        create.assert_called_once()
        llm_module.log_llm_response.assert_called_once_with(expected)

    def test_normal_video_is_prepared_as_one_complete_request(self) -> None:
        items = [
            SimpleNamespace(index=index, original_text=f"字幕{index}")
            for index in range(1, 103)
        ]

        chunks = resilience.prepare_complete_video_subtitles(
            items,
            prompt_text="prompt",
            max_input_tokens=120000,
            max_output_tokens=32000,
            output_reserve_ratio=2.0,
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 102)

    def test_oversized_video_is_rejected_instead_of_split(self) -> None:
        items = [
            SimpleNamespace(index=index, original_text="长字幕" * 20)
            for index in range(1, 9)
        ]

        with self.assertRaisesRegex(ValueError, "完整视频字幕超过"):
            resilience.prepare_complete_video_subtitles(
                items,
                max_input_tokens=900,
                max_output_tokens=400,
                output_reserve_ratio=2.0,
            )

    def test_incomplete_response_is_repaired_once_with_complete_context(self) -> None:
        subtitles = {"1": "原文一", "2": "原文二"}
        call = mock.Mock(
            side_effect=[
                response('{"1":"译文一"}'),
                response('{"1":"译文一","2":"译文二"}'),
            ]
        )

        def validate(actual, expected):
            missing = [key for key in expected if key not in actual]
            return (not missing, f"Missing keys {missing}")

        actual = resilience.request_complete_video_translation(
            call,
            validate,
            system_prompt="Output JSON",
            subtitle_dict=subtitles,
            model="deepseek-flash",
            request_options={},
            allow_repair=True,
        )

        self.assertEqual(actual, {"1": "译文一", "2": "译文二"})
        self.assertEqual(call.call_count, 2)
        expected_payload = '{"1": "原文一", "2": "原文二"}'
        repair_messages = call.call_args_list[1].kwargs["messages"]
        self.assertEqual([item["role"] for item in repair_messages], ["system", "user", "assistant", "user"])
        self.assertEqual(repair_messages[1]["content"], expected_payload)
        self.assertEqual(repair_messages[2]["content"], '{"1":"译文一"}')
        self.assertIn("Missing keys ['2']", repair_messages[3]["content"])
        self.assertIn("complete JSON object again", repair_messages[3]["content"])

    def test_second_incomplete_response_fails_without_third_request(self) -> None:
        subtitles = {"1": "原文一", "2": "原文二"}
        call = mock.Mock(
            side_effect=[
                response('{"1":"译文一"}'),
                response('{"2":"译文二"}'),
            ]
        )

        def validate(actual, expected):
            missing = [key for key in expected if key not in actual]
            return (not missing, f"Missing keys {missing}")

        with self.assertRaisesRegex(ValueError, "after one complete repair request"):
            resilience.request_complete_video_translation(
                call,
                validate,
                system_prompt="Output JSON",
                subtitle_dict=subtitles,
                model="deepseek-flash",
                request_options={},
                allow_repair=True,
            )

        self.assertEqual(call.call_count, 2)

    def test_valid_first_response_does_not_send_repair_request(self) -> None:
        subtitles = {"1": "原文一"}
        call = mock.Mock(return_value=response('{"1":"译文一"}'))

        actual = resilience.request_complete_video_translation(
            call,
            lambda response_dict, _expected: (bool(response_dict), "invalid"),
            system_prompt="Output JSON",
            subtitle_dict=subtitles,
            model="deepseek-flash",
            request_options={},
            allow_repair=True,
        )

        self.assertEqual(actual, {"1": "译文一"})
        call.assert_called_once()

    def test_request_exception_does_not_trigger_repair_request(self) -> None:
        call = mock.Mock(side_effect=ConnectionError("offline"))
        with self.assertRaisesRegex(ConnectionError, "offline"):
            resilience.request_complete_video_translation(
                call,
                lambda _actual, _expected: (False, "invalid"),
                system_prompt="Output JSON",
                subtitle_dict={"1": "原文一"},
                model="deepseek-flash",
                request_options={},
                allow_repair=True,
            )
        call.assert_called_once()

    def test_batch_request_bypasses_upstream_response_cache(self) -> None:
        cached_call = mock.Mock(name="cached_call")
        direct_call = mock.Mock(name="direct_call")

        self.assertIs(
            resilience.select_translation_call(
                batch_active=True,
                cached_call=cached_call,
                direct_call=direct_call,
            ),
            direct_call,
        )
        self.assertIs(
            resilience.select_translation_call(
                batch_active=False,
                cached_call=cached_call,
                direct_call=direct_call,
            ),
            cached_call,
        )

    def test_request_error_is_not_retried(self) -> None:
        call = mock.Mock(side_effect=ValueError("bad request"))
        with self.assertRaisesRegex(ValueError, "bad request"):
            resilience.call_once_with_response_validation(call)
        call.assert_called_once()

    def test_empty_response_is_rejected_without_retry(self) -> None:
        call = mock.Mock(return_value=response("", "length"))
        with self.assertRaisesRegex(resilience.EmptyLLMResponseError, "empty choices or content"):
            resilience.call_once_with_response_validation(call)
        call.assert_called_once()

    def test_nonempty_response_is_returned_after_one_request(self) -> None:
        expected = response('{"1":"译文"}')
        call = mock.Mock(return_value=expected)
        self.assertIs(resilience.call_once_with_response_validation(call), expected)
        call.assert_called_once()

    def test_punctuation_cleanup_never_turns_nonempty_text_into_empty(self) -> None:
        data = SimpleNamespace(
            segments=[
                SimpleNamespace(text="です。", translated_text="。"),
                SimpleNamespace(text="挨拶。", translated_text="你好。"),
            ]
        )

        def upstream_remove(current: object) -> object:
            for segment in current.segments:
                segment.text = re.sub(r"[，。]+$", "", segment.text.strip())
                segment.translated_text = re.sub(
                    r"[，。]+$", "", segment.translated_text.strip()
                )
            return current

        resilience.preserve_nonempty_punctuation(data, upstream_remove)

        self.assertEqual(data.segments[0].translated_text, "。")
        self.assertEqual(data.segments[1].translated_text, "你好")
        self.assertEqual(data.segments[0].text, "です")

if __name__ == "__main__":
    unittest.main()
