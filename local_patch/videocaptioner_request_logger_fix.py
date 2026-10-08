"""修复 VideoCaptioner v1.4.2 并发 LLM 请求日志的错误配对。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any

import httpx


# 上游来源：https://github.com/WEIFENG2333/VideoCaptioner/tree/d753521d57cf2311df96bfee96fe39c6306a8e09
# v1.4.2 的 log_llm_response 会从全局队列取“第一个已完成请求”，并发时可能把
# 一个响应写到另一个请求上。本补丁改为在 httpx 响应钩子中用 response.request
# 精确关联，并在请求发出时保存任务上下文。
_pending_lock = threading.Lock()
_pending_requests: dict[int, dict[str, Any]] = {}
_installed = False
_task_context = threading.local()


def _install_thread_local_task_context() -> None:
    """上游使用进程级单例；多视频并发时改为每个工作线程独立上下文。"""
    from videocaptioner.core.llm import context

    def set_task_context(task_id: str, file_name: str, stage: str) -> None:
        _task_context.value = context.TaskContext(task_id, file_name, stage)

    def get_task_context():
        return getattr(_task_context, "value", None)

    def update_stage(stage: str) -> None:
        current = get_task_context()
        if current is not None:
            _task_context.value = context.TaskContext(
                current.task_id, current.file_name, stage
            )

    def clear_task_context() -> None:
        if hasattr(_task_context, "value"):
            del _task_context.value

    context.set_task_context = set_task_context
    context.get_task_context = get_task_context
    context.update_stage = update_stage
    context.clear_task_context = clear_task_context


def _on_request(request: httpx.Request) -> None:
    if "/chat/completions" not in str(request.url):
        return

    from videocaptioner.core.llm.context import get_task_context

    try:
        request_body = json.loads(request.content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        request_body = {"raw": request.content.decode("utf-8", errors="replace")}
    context = get_task_context()
    pending = {
        "start_time": time.time(),
        "url": str(request.url),
        "request": request_body,
        "task_id": context.task_id if context else "",
        "file_name": context.file_name if context else "",
        "stage": context.stage if context else "",
    }
    with _pending_lock:
        _pending_requests[id(request)] = pending


def _on_response(response: httpx.Response) -> None:
    request = response.request
    with _pending_lock:
        pending = _pending_requests.pop(id(request), None)
    if pending is None:
        return

    from videocaptioner.core.llm import request_logger

    # httpx 的响应钩子在 SDK 解析前运行；读取失败时记录并原样抛出网络异常，
    # 禁止访问尚未完整读取的 response.text 而覆盖真正原因。
    try:
        response.read()
    except httpx.HTTPError as exc:
        request_logger._write_log(
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "task_id": pending["task_id"],
                "file_name": pending["file_name"],
                "stage": pending["stage"],
                "url": pending["url"],
                "status": response.status_code,
                "duration_ms": int((time.time() - pending["start_time"]) * 1000),
                "request": pending["request"],
                "response": {
                    "read_error_type": type(exc).__name__,
                    "read_error": str(exc),
                },
            }
        )
        raise

    try:
        response_data = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        response_data = {"raw": response.text}

    request_logger._write_log(
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "task_id": pending["task_id"],
            "file_name": pending["file_name"],
            "stage": pending["stage"],
            "url": pending["url"],
            "status": response.status_code,
            "duration_ms": int((time.time() - pending["start_time"]) * 1000),
            "request": pending["request"],
            "response": response_data,
        }
    )


def create_logging_http_client() -> httpx.Client:
    """创建能够严格关联并发请求和响应的同步 HTTP 客户端。"""
    return httpx.Client(event_hooks={"request": [_on_request], "response": [_on_response]})


def _response_already_logged(_response: Any) -> None:
    """响应已由精确关联的 httpx 钩子记录，避免 SDK 解析后重复写入。"""


def install() -> None:
    """在全局 OpenAI 客户端创建前替换上游请求日志入口。"""
    global _installed
    if _installed:
        return

    from videocaptioner.core.llm import client, request_logger

    _install_thread_local_task_context()
    request_logger.create_logging_http_client = create_logging_http_client
    request_logger.log_llm_response = _response_already_logged
    client.create_logging_http_client = create_logging_http_client
    client.log_llm_response = _response_already_logged
    _installed = True
