"""Run a paid DeepSeek 2x2 batching benchmark without publishing SRT files."""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import json_repair
from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOGS = (
    PROJECT_ROOT / "~outputs-intermediate/VideoCaptioner/logs/llm_requests.jsonl.old",
    PROJECT_ROOT / "~outputs-intermediate/VideoCaptioner/logs/llm_requests.jsonl",
)
DEFAULT_SECRET = PROJECT_ROOT / "secrets/deepseek-api-key.txt"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "~outputs-intermediate/VideoCaptioner/benchmarks"


@dataclass(frozen=True)
class Variant:
    name: str
    thinking: bool
    whole_video: bool


VARIANTS = (
    Variant("thinking_10_sentences", True, False),
    Variant("nonthinking_10_sentences", False, False),
    Variant("thinking_whole_video", True, True),
    Variant("nonthinking_whole_video", False, True),
)


@dataclass
class RequestResult:
    file_name: str
    first_index: int
    last_index: int
    item_count: int
    status: str = "error"
    finish_reason: str = ""
    duration_seconds: float = 0.0
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    visible_output_tokens: int = 0
    response_text: str = ""
    error: str = ""


@dataclass
class VariantResult:
    variant: str
    thinking: bool
    whole_video: bool
    started_at: str
    finished_at: str = ""
    duration_seconds: float = 0.0
    requests: list[RequestResult] = field(default_factory=list)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def load_corpus(logs: tuple[Path, ...], name_filter: str) -> dict[str, dict[str, Any]]:
    """Rebuild latest complete per-video inputs from existing request logs."""
    corpus: dict[str, dict[str, Any]] = {}
    for log_path in logs:
        if not log_path.is_file():
            continue
        with log_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                    file_name = str(entry.get("file_name", ""))
                    if entry.get("stage") != "translate" or name_filter.lower() not in file_name.lower():
                        continue
                    request = entry.get("request") or {}
                    messages = request.get("messages") or []
                    system_prompt = next(
                        (str(item.get("content", "")) for item in messages if item.get("role") == "system"),
                        "",
                    )
                    user_content = next(
                        (str(item.get("content", "")) for item in reversed(messages) if item.get("role") == "user"),
                        "",
                    )
                    subtitles = json.loads(user_content)
                    if not isinstance(subtitles, dict) or not subtitles:
                        continue
                    if not all(str(key).isdigit() for key in subtitles):
                        continue
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue

                video = corpus.setdefault(
                    file_name,
                    {"system_prompt": system_prompt, "model": str(request.get("model", "deepseek-flash")), "subtitles": {}},
                )
                video["system_prompt"] = system_prompt or video["system_prompt"]
                video["model"] = str(request.get("model") or video["model"])
                video["subtitles"].update({str(key): str(value) for key, value in subtitles.items()})

    for file_name, video in corpus.items():
        ordered = dict(sorted(video["subtitles"].items(), key=lambda item: int(item[0])))
        indices = [int(key) for key in ordered]
        if indices != list(range(1, len(indices) + 1)):
            raise ValueError(f"Non-contiguous subtitle indices for {file_name}")
        if not video["system_prompt"]:
            raise ValueError(f"Missing system prompt for {file_name}")
        video["subtitles"] = ordered
    if not corpus:
        raise ValueError(f"No translation inputs matched {name_filter!r}")
    return dict(sorted(corpus.items()))


def split_subtitles(subtitles: dict[str, str], whole_video: bool) -> list[dict[str, str]]:
    items = list(subtitles.items())
    size = len(items) if whole_video else 10
    return [dict(items[index : index + size]) for index in range(0, len(items), size)]


def validate_response(text: str, expected: dict[str, str]) -> None:
    parsed = json_repair.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"Response is {type(parsed).__name__}, expected dict")
    expected_keys = set(expected)
    actual_keys = {str(key) for key in parsed}
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys, key=int)
        extra = sorted(actual_keys - expected_keys)
        raise ValueError(f"Key mismatch: missing={missing[:20]}, extra={extra[:20]}")
    empty = [key for key in expected if not str(parsed.get(key, "")).strip()]
    if empty:
        raise ValueError(f"Empty translations: {empty[:20]}")


def make_request(
    client: OpenAI,
    file_name: str,
    model: str,
    system_prompt: str,
    chunk: dict[str, str],
    thinking: bool,
) -> RequestResult:
    indices = [int(key) for key in chunk]
    result = RequestResult(file_name, min(indices), max(indices), len(indices))
    started = time.monotonic()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(chunk, ensure_ascii=False)},
            ],
            temperature=1,
            reasoning_effort="high" if thinking else "none",
            extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
        )
        choice = response.choices[0]
        result.finish_reason = str(choice.finish_reason or "")
        result.response_text = str(choice.message.content or "")
        if result.finish_reason != "stop":
            raise ValueError(f"Unexpected finish_reason={result.finish_reason!r}")
        if not result.response_text.strip():
            raise ValueError("Empty response content")
        validate_response(result.response_text, chunk)

        usage = response.usage
        result.prompt_tokens = _as_int(getattr(usage, "prompt_tokens", 0))
        result.cache_hit_tokens = _as_int(getattr(usage, "prompt_cache_hit_tokens", 0))
        result.cache_miss_tokens = _as_int(getattr(usage, "prompt_cache_miss_tokens", 0))
        result.completion_tokens = _as_int(getattr(usage, "completion_tokens", 0))
        details = getattr(usage, "completion_tokens_details", None)
        result.reasoning_tokens = _as_int(getattr(details, "reasoning_tokens", 0))
        result.visible_output_tokens = result.completion_tokens - result.reasoning_tokens
        result.status = "success"
    except Exception as exc:  # No retry: record exactly one failed request.
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.duration_seconds = round(time.monotonic() - started, 3)
    return result


def run_variant(client: OpenAI, corpus: dict[str, dict[str, Any]], variant: Variant) -> VariantResult:
    started = time.monotonic()
    result = VariantResult(variant.name, variant.thinking, variant.whole_video, datetime.now().isoformat(timespec="seconds"))

    if variant.whole_video:
        with ThreadPoolExecutor(max_workers=min(4, len(corpus))) as executor:
            jobs = [
                executor.submit(
                    make_request,
                    client,
                    file_name,
                    video["model"],
                    video["system_prompt"],
                    split_subtitles(video["subtitles"], True)[0],
                    variant.thinking,
                )
                for file_name, video in corpus.items()
            ]
            result.requests.extend(future.result() for future in as_completed(jobs))
    else:
        for file_name, video in corpus.items():
            chunks = split_subtitles(video["subtitles"], False)
            with ThreadPoolExecutor(max_workers=min(10, len(chunks))) as executor:
                jobs = [
                    executor.submit(make_request, client, file_name, video["model"], video["system_prompt"], chunk, variant.thinking)
                    for chunk in chunks
                ]
                result.requests.extend(future.result() for future in as_completed(jobs))

    result.requests.sort(key=lambda item: (item.file_name, item.first_index))
    result.finished_at = datetime.now().isoformat(timespec="seconds")
    result.duration_seconds = round(time.monotonic() - started, 3)
    return result


def summarise(result: VariantResult) -> dict[str, Any]:
    requests = result.requests
    return {
        "variant": result.variant,
        "thinking": result.thinking,
        "whole_video": result.whole_video,
        "status": "success" if requests and all(item.status == "success" for item in requests) else "error",
        "request_count": len(requests),
        "successful_requests": sum(item.status == "success" for item in requests),
        "subtitle_count": sum(item.item_count for item in requests),
        "duration_seconds": result.duration_seconds,
        "prompt_tokens": sum(item.prompt_tokens for item in requests),
        "cache_hit_tokens": sum(item.cache_hit_tokens for item in requests),
        "cache_miss_tokens": sum(item.cache_miss_tokens for item in requests),
        "completion_tokens": sum(item.completion_tokens for item in requests),
        "reasoning_tokens": sum(item.reasoning_tokens for item in requests),
        "visible_output_tokens": sum(item.visible_output_tokens for item in requests),
        "total_tokens": sum(item.prompt_tokens + item.completion_tokens for item in requests),
        "errors": [item.error for item in requests if item.error],
    }


def save_report(output_path: Path, corpus: dict[str, dict[str, Any]], results: list[VariantResult]) -> None:
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "corpus": {
            file_name: {"subtitle_count": len(video["subtitles"]), "model": video["model"]}
            for file_name, video in corpus.items()
        },
        "summaries": [summarise(result) for result in results],
        "variants": [asdict(result) for result in results],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filter", default="mdvr00423")
    parser.add_argument("--secret", type=Path, default=DEFAULT_SECRET)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    corpus = load_corpus(DEFAULT_LOGS, args.filter)
    api_key = args.secret.read_text(encoding="utf-8").strip()
    if not api_key:
        raise ValueError(f"Empty API key file: {args.secret}")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1", max_retries=0, timeout=900)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = args.output or DEFAULT_OUTPUT_ROOT / f"translation-matrix-{stamp}.json"

    results: list[VariantResult] = []
    print("Corpus: " + ", ".join(f"{name}={len(video['subtitles'])} cues" for name, video in corpus.items()), flush=True)
    for variant in VARIANTS:
        print(f"Starting {variant.name}...", flush=True)
        result = run_variant(client, corpus, variant)
        results.append(result)
        save_report(output_path, corpus, results)
        print(json.dumps(summarise(result), ensure_ascii=False), flush=True)

    print(f"Report: {output_path}", flush=True)
    return 0 if all(summarise(result)["status"] == "success" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
