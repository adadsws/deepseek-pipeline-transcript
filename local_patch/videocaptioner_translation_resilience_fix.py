"""增强 VideoCaptioner v1.4.2 的响应校验、缓存校验和标点后处理。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from contextlib import contextmanager
from typing import Callable

import json_repair

from videocaptioner_project_config import get_value, load_mode_settings

logger = logging.getLogger(__name__)
_installed = False
_CACHE_TTL_SECONDS = int(
    get_value(load_mode_settings("batch"), "cache.translation_ttl_seconds")
)
_BATCH_SETTINGS = load_mode_settings("batch")
_VIDEO_LEVEL_TRANSLATION = bool(
    get_value(_BATCH_SETTINGS, "project_features.video_level_translation")
)
_MAX_INPUT_TOKENS = int(
    get_value(_BATCH_SETTINGS, "translation_batching.max_input_tokens")
)
_MAX_OUTPUT_TOKENS = int(
    get_value(_BATCH_SETTINGS, "translation_batching.max_output_tokens")
)
_OUTPUT_RESERVE_RATIO = float(
    get_value(_BATCH_SETTINGS, "translation_batching.output_reserve_ratio")
)
_REASONING_EFFORT = str(
    get_value(_BATCH_SETTINGS, "translation_batching.reasoning_effort")
)
_VIDEO_BATCH_CACHE_VERSION = "full-video-v2"
_usage_context = threading.local()


class EmptyLLMResponseError(ValueError):
    """HTTP 请求成功但模型没有返回可用正文。"""


def _batch_video_translation_active() -> bool:
    return _VIDEO_LEVEL_TRANSLATION and os.environ.get(
        "VIDEOCAPTIONER_EXECUTION_MODE"
    ) == "batch"


def batch_video_request_options() -> dict[str, object]:
    """返回路线 A 的显式 DeepSeek 请求参数。"""
    return {
        "reasoning_effort": _REASONING_EFFORT,
        "extra_body": {"thinking": {"type": "disabled"}},
        "response_format": {"type": "json_object"},
        "max_tokens": _MAX_OUTPUT_TOKENS,
    }


def ensure_json_instruction(prompt: str) -> str:
    """DeepSeek JSON mode requires the literal word `json` in the prompt."""
    if "json" in prompt.lower():
        return prompt
    return prompt.rstrip() + "\n\nOutput ONLY a valid JSON object."


@contextmanager
def capture_llm_usage(callback: Callable[[object], None]):
    """把一次同步翻译调用的真实响应 usage 交还给当前批处理视频。"""
    previous = getattr(_usage_context, "callback", None)
    _usage_context.callback = callback
    try:
        yield
    finally:
        _usage_context.callback = previous


def _notify_usage(response: object) -> None:
    callback = getattr(_usage_context, "callback", None)
    if callback is not None:
        callback(response)


def _estimated_text_tokens(text: str) -> int:
    """对中日字幕采用保守字符估算，避免完整请求触碰服务端上限。"""
    return max(1, len(text))


def prepare_complete_video_subtitles(
    items: list[object],
    *,
    prompt_text: str = "",
    max_input_tokens: int = _MAX_INPUT_TOKENS,
    max_output_tokens: int = _MAX_OUTPUT_TOKENS,
    output_reserve_ratio: float = _OUTPUT_RESERVE_RATIO,
) -> list[list[object]]:
    """完整视频只生成一个请求；超过安全预算时明确失败，绝不拆分。"""
    if not items:
        return []
    fixed_input = _estimated_text_tokens(prompt_text) + 512
    source_budget = min(
        max_input_tokens - fixed_input,
        math.floor(max_output_tokens / output_reserve_ratio),
    )
    if source_budget <= 0:
        raise ValueError("视频级翻译 token 预算不足以容纳系统提示词")

    total_tokens = 2
    for item in items:
        index = getattr(item, "index", "")
        text = str(getattr(item, "original_text", "") or "")
        item_tokens = _estimated_text_tokens(
            json.dumps({str(index): text}, ensure_ascii=False)
        )
        total_tokens += item_tokens
    if total_tokens > source_budget:
        raise ValueError(
            "完整视频字幕超过单次 DeepSeek 请求的安全 token 预算；"
            "为保证完整视频一次发送，已拒绝拆分"
        )
    return [items]


def request_complete_video_translation(
    call: Callable[..., object],
    validate: Callable[[object, dict[str, str]], tuple[bool, str]],
    *,
    system_prompt: str,
    subtitle_dict: dict[str, str],
    model: str,
    request_options: dict[str, object],
    allow_repair: bool,
) -> dict[str, str]:
    """首次响应校验失败时，携带完整输入输出进行至多一次修复。"""
    source_payload = json.dumps(subtitle_dict, ensure_ascii=False)
    initial_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": source_payload},
    ]
    response = call(
        messages=initial_messages,
        model=model,
        **request_options,
    )

    def parse_and_validate(candidate: object) -> tuple[dict[str, str] | None, str, str]:
        try:
            content = str(candidate.choices[0].message.content or "").strip()
            response_dict = json_repair.loads(content)
            valid, validation_message = validate(response_dict, subtitle_dict)
            if valid and isinstance(response_dict, dict):
                return response_dict, content, ""
            if valid:
                validation_message = (
                    f"Response is {type(response_dict).__name__}, expected dict"
                )
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            content = str(
                getattr(
                    getattr((getattr(candidate, "choices", None) or [None])[0], "message", None),
                    "content",
                    "",
                )
                or ""
            ).strip()
            validation_message = str(exc)
        return None, content, validation_message

    response_dict, first_content, first_error = parse_and_validate(response)
    if response_dict is not None:
        return response_dict
    if not allow_repair:
        raise ValueError(f"DeepSeek response validation failed: {first_error}")

    repair_prompt = (
        "The previous JSON response failed strict validation:\n"
        f"{first_error}\n\n"
        "Repair the response using the complete original subtitle input above. "
        "Return the complete JSON object again, not a patch. Every original numeric "
        "key must appear exactly once, with no missing, extra, renamed, merged, or "
        "empty entries. Output ONLY the repaired valid JSON object."
    )
    repaired_response = call(
        messages=initial_messages
        + [
            {"role": "assistant", "content": first_content},
            {"role": "user", "content": repair_prompt},
        ],
        model=model,
        **request_options,
    )
    repaired_dict, _repaired_content, repair_error = parse_and_validate(
        repaired_response
    )
    if repaired_dict is not None:
        return repaired_dict
    raise ValueError(
        "DeepSeek response validation failed after one complete repair request: "
        f"first response: {first_error}; repair response: {repair_error}"
    )


def select_translation_call(
    *,
    batch_active: bool,
    cached_call: Callable[..., object],
    direct_call: Callable[..., object],
) -> Callable[..., object]:
    """批处理的实际请求绕过上游响应缓存，确保结果来自本次处理。"""
    return direct_call if batch_active else cached_call


def _response_has_content(response: object) -> bool:
    choices = getattr(response, "choices", None)
    if not choices:
        return False
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    return bool(str(content or "").strip())


def call_once_with_response_validation(
    call: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> object:
    """每个 DeepSeek 请求只发送一次，同时拒绝无正文的成功响应。"""
    response = call(*args, **kwargs)
    if not _response_has_content(response):
        choices = getattr(response, "choices", None) or []
        finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
        raise EmptyLLMResponseError(
            "Invalid OpenAI API response: empty choices or content "
            f"(finish_reason={finish_reason or 'unknown'})"
        )
    return response


def call_llm_api_once(
    llm_client: object,
    messages: list[dict],
    model: str,
    temperature: float = 1,
    **kwargs: object,
) -> object:
    """单次调用 OpenAI 兼容接口，显式关闭 SDK 内建重试。"""
    api_client = llm_client.get_llm_client().with_options(max_retries=0)
    response = api_client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        **kwargs,
    )
    llm_client.log_llm_response(response)
    validated = call_once_with_response_validation(lambda: response)
    _notify_usage(validated)
    return validated


def preserve_nonempty_punctuation(
    asr_data: object,
    original_remove: Callable[[object], object],
) -> object:
    """沿用上游句尾清理，但禁止把原本非空的原文或译文清成空串。"""
    segments = list(getattr(asr_data, "segments", []))
    originals = [
        (
            str(getattr(segment, "text", "") or ""),
            str(getattr(segment, "translated_text", "") or ""),
        )
        for segment in segments
    ]
    result = original_remove(asr_data)
    for segment, (original_text, translated_text) in zip(segments, originals):
        if original_text.strip() and not str(getattr(segment, "text", "") or "").strip():
            segment.text = original_text.strip()
        if translated_text.strip() and not str(
            getattr(segment, "translated_text", "") or ""
        ).strip():
            segment.translated_text = translated_text.strip()
    return result


def _translation_is_complete(items: object, expected: list[object] | None = None) -> bool:
    if not items or not isinstance(items, list):
        return False
    if expected is not None:
        cached_indices = {getattr(item, "index", None) for item in items}
        expected_indices = {getattr(item, "index", None) for item in expected}
        if cached_indices != expected_indices:
            return False
    return all(
        str(getattr(item, "translated_text", "") or "").strip() for item in items
    )


def _read_cached_chunk(translator: object, chunk: list[object]) -> tuple[str, object]:
    cache_key = translator._get_cache_key(chunk)
    try:
        cached = translator._cache.get(cache_key, default=None)
    except Exception:
        cached = None
        translator._cache.delete(cache_key)
    return cache_key, cached


def install() -> None:
    """为 GUI 与无界面批处理安装一次翻译韧性补丁。"""
    global _installed
    if _installed:
        return

    from videocaptioner.core.asr.asr_data import ASRData
    from videocaptioner.core.llm import client as llm_client
    from videocaptioner.core.translate.base import BaseTranslator
    from videocaptioner.core.translate import llm_translator as llm_translator_module
    from videocaptioner.core.translate.llm_translator import LLMTranslator

    def validated_call_llm_api(
        messages: list[dict],
        model: str,
        temperature: float = 1,
        **kwargs: object,
    ) -> object:
        """绕过上游 tenacity，并关闭 OpenAI SDK 自带的两次重试。"""
        return call_llm_api_once(
            llm_client,
            messages,
            model,
            temperature,
            **kwargs,
        )

    llm_client._call_llm_api = validated_call_llm_api

    original_remove_punctuation = ASRData.remove_punctuation

    def safe_remove_punctuation(self: object) -> object:
        return preserve_nonempty_punctuation(self, original_remove_punctuation)

    ASRData.remove_punctuation = safe_remove_punctuation

    original_validate_response = LLMTranslator._validate_llm_response

    def validate_nonempty_response(
        self: object,
        response_dict: object,
        subtitle_dict: dict[str, str],
    ) -> tuple[bool, str]:
        valid, message = original_validate_response(self, response_dict, subtitle_dict)
        if not valid or not isinstance(response_dict, dict):
            return valid, message
        empty = []
        for key, value in response_dict.items():
            candidate = (
                value.get("native_translation")
                if getattr(self, "is_reflect", False) and isinstance(value, dict)
                else value
            )
            if candidate is None or not str(candidate).strip():
                empty.append(str(key))
        if empty:
            return False, f"Empty translations for keys {empty}"
        return True, ""

    LLMTranslator._validate_llm_response = validate_nonempty_response

    def translate_once(
        self: object,
        system_prompt: str,
        subtitle_dict: dict[str, str],
    ) -> dict[str, str]:
        """完整视频首次响应不合格时，批处理允许一次完整上下文修复。"""
        request_options: dict[str, object] = {}
        effective_prompt = system_prompt
        batch_active = _batch_video_translation_active()
        if batch_active:
            request_options = batch_video_request_options()
            effective_prompt = ensure_json_instruction(system_prompt)
        request_call = select_translation_call(
            batch_active=batch_active,
            cached_call=llm_translator_module.call_llm,
            direct_call=llm_client._call_llm_api,
        )
        return request_complete_video_translation(
            request_call,
            self._validate_llm_response,
            system_prompt=effective_prompt,
            subtitle_dict=subtitle_dict,
            model=getattr(self, "model"),
            request_options=request_options,
            allow_repair=batch_active,
        )

    LLMTranslator._agent_loop = translate_once

    original_split_chunks = LLMTranslator._split_chunks

    def prepare_video_request(
        self: object, translate_data_list: list[object]
    ) -> list[list[object]]:
        if not _batch_video_translation_active():
            return original_split_chunks(self, translate_data_list)
        prompt_path = "translate/reflect" if getattr(self, "is_reflect", False) else "translate/standard"
        prompt = llm_translator_module.get_prompt(
            prompt_path,
            target_language=getattr(self, "target_language"),
            custom_prompt=getattr(self, "custom_prompt", ""),
        )
        return prepare_complete_video_subtitles(
            translate_data_list,
            prompt_text=prompt,
        )

    LLMTranslator._split_chunks = prepare_video_request

    original_parallel_translate = BaseTranslator._parallel_translate

    def translate_video_chunks_in_calling_thread(
        self: object, chunks: list[list[object]]
    ) -> list[object]:
        if not (_batch_video_translation_active() and isinstance(self, LLMTranslator)):
            return original_parallel_translate(self, chunks)
        translated: list[object] = []
        for chunk in chunks:
            translated.extend(self._safe_translate_chunk(chunk))
        return translated

    BaseTranslator._parallel_translate = translate_video_chunks_in_calling_thread

    original_llm_cache_key = LLMTranslator._get_cache_key

    def versioned_video_cache_key(self: object, chunk: list[object]) -> str:
        key = original_llm_cache_key(self, chunk)
        if not _batch_video_translation_active():
            return key
        prompt_fingerprint = hashlib.sha256(
            (
                f"reflect={bool(getattr(self, 'is_reflect', False))}\n"
                f"{getattr(self, 'custom_prompt', '')}"
            ).encode("utf-8")
        ).hexdigest()[:16]
        return (
            f"{key}:{_VIDEO_BATCH_CACHE_VERSION}:{_REASONING_EFFORT}:"
            f"{_MAX_INPUT_TOKENS}:{_MAX_OUTPUT_TOKENS}:{_OUTPUT_RESERVE_RATIO:g}:"
            "one-complete-repair:"
            f"{prompt_fingerprint}"
        )

    LLMTranslator._get_cache_key = versioned_video_cache_key

    original_safe_translate_chunk = BaseTranslator._safe_translate_chunk

    def reject_incomplete_cached_chunk(self: object, chunk: list[object]) -> list[object]:
        cache_key, cached = _read_cached_chunk(self, chunk)
        if cached is not None and not _translation_is_complete(cached, chunk):
            self._cache.delete(cache_key)
        result = original_safe_translate_chunk(self, chunk)
        # 上游固定为 7 天；项目配置作为唯一 TTL 来源并覆盖该写入。
        self._cache.set(cache_key, result, expire=_CACHE_TTL_SECONDS)
        return result

    BaseTranslator._safe_translate_chunk = reject_incomplete_cached_chunk

    _installed = True
    logger.info("Installed project translation-resilience fix")
