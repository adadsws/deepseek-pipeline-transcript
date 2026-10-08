"""并发 LLM 请求日志配对补丁的纯本地测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx


MODULE_PATH = Path(__file__).parents[1] / "local_patch" / "videocaptioner_request_logger_fix.py"
SPEC = importlib.util.spec_from_file_location("videocaptioner_request_logger_fix_test", MODULE_PATH)
assert SPEC and SPEC.loader
request_fix = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = request_fix
SPEC.loader.exec_module(request_fix)


class RequestLoggerFixTests(unittest.TestCase):
    def tearDown(self) -> None:
        request_fix._pending_requests.clear()

    def test_task_context_is_isolated_between_video_threads(self) -> None:
        from videocaptioner.core.llm import context

        request_fix._install_thread_local_task_context()

        def worker(name: str) -> tuple[str, str]:
            context.set_task_context(name, f"{name}.mp4", "translate")
            current = context.get_task_context()
            return current.task_id, current.file_name

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, ("first", "second")))

        self.assertCountEqual(
            results,
            [("first", "first.mp4"), ("second", "second.mp4")],
        )

    def test_out_of_order_responses_remain_paired_with_their_requests(self) -> None:
        first = httpx.Request(
            "POST",
            "https://api.deepseek.com/v1/chat/completions",
            content=json.dumps({"messages": [{"role": "user", "content": "first"}]}),
        )
        second = httpx.Request(
            "POST",
            "https://api.deepseek.com/v1/chat/completions",
            content=json.dumps({"messages": [{"role": "user", "content": "second"}]}),
        )
        entries: list[dict] = []
        context = SimpleNamespace(task_id="task", file_name="sample.mp4", stage="translate")

        with mock.patch(
            "videocaptioner.core.llm.context.get_task_context", return_value=context
        ), mock.patch(
            "videocaptioner.core.llm.request_logger._write_log",
            side_effect=entries.append,
        ):
            request_fix._on_request(first)
            request_fix._on_request(second)
            request_fix._on_response(
                httpx.Response(200, request=second, json={"id": "response-second"})
            )
            request_fix._on_response(
                httpx.Response(200, request=first, json={"id": "response-first"})
            )

        self.assertEqual(entries[0]["request"]["messages"][0]["content"], "second")
        self.assertEqual(entries[0]["response"]["id"], "response-second")
        self.assertEqual(entries[1]["request"]["messages"][0]["content"], "first")
        self.assertEqual(entries[1]["response"]["id"], "response-first")
        self.assertEqual(request_fix._pending_requests, {})

    def test_incomplete_response_logs_and_preserves_original_transport_error(self) -> None:
        class BrokenStream(httpx.SyncByteStream):
            def __iter__(self):
                raise httpx.RemoteProtocolError("incomplete chunked read")

        request = httpx.Request(
            "POST",
            "https://api.deepseek.com/v1/chat/completions",
            content=json.dumps({"messages": [{"role": "user", "content": "test"}]}),
        )
        response = httpx.Response(200, request=request, stream=BrokenStream())
        entries: list[dict] = []
        context = SimpleNamespace(task_id="task", file_name="sample.mp4", stage="translate")

        with mock.patch(
            "videocaptioner.core.llm.context.get_task_context", return_value=context
        ), mock.patch(
            "videocaptioner.core.llm.request_logger._write_log",
            side_effect=entries.append,
        ):
            request_fix._on_request(request)
            with self.assertRaisesRegex(httpx.RemoteProtocolError, "incomplete chunked read"):
                request_fix._on_response(response)

        self.assertEqual(entries[0]["response"]["read_error_type"], "RemoteProtocolError")
        self.assertIn("incomplete chunked read", entries[0]["response"]["read_error"])
        self.assertEqual(request_fix._pending_requests, {})


if __name__ == "__main__":
    unittest.main()
