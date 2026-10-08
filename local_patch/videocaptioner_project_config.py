"""加载项目三层 TOML 配置，并生成上游 GUI 可读取的临时 JSON。"""

from __future__ import annotations

import json
import os
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = PROJECT_ROOT / "config"
SHARED_CONFIG_PATH = CONFIG_ROOT / "shared" / "settings.toml"
MODE_CONFIG_PATHS = {
    "batch": CONFIG_ROOT / "batch" / "settings.toml",
    "gui": CONFIG_ROOT / "gui" / "settings.toml",
}


class ProjectConfigError(ValueError):
    """三层项目配置缺失、冲突或类型无效。"""


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ProjectConfigError(f"缺少配置文件: {path}") from exc
    if not isinstance(value, dict):
        raise ProjectConfigError(f"配置根节点必须是表: {path}")
    return value


def _leaf_paths(value: dict[str, Any], prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for key, item in value.items():
        current = (*prefix, key)
        if isinstance(item, dict):
            result.update(_leaf_paths(item, current))
        else:
            result.add(current)
    return result


def _merge_without_overlap(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """合并 shared 与模式层；同一叶子只能归属一个层。"""
    result = deepcopy(base)

    def merge(target: dict[str, Any], source: dict[str, Any], prefix: tuple[str, ...]) -> None:
        for key, value in source.items():
            current = (*prefix, key)
            if key not in target:
                target[key] = deepcopy(value)
            elif isinstance(target[key], dict) and isinstance(value, dict):
                merge(target[key], value, current)
            else:
                raise ProjectConfigError(
                    "配置项重复归属 shared 与模式层: " + ".".join(current)
                )

    merge(result, overlay, ())
    return result


def validate_mode_pair(
    batch: dict[str, Any] | None = None,
    gui: dict[str, Any] | None = None,
) -> None:
    """确保 batch/gui 的覆盖项完全成对且类型一致。"""
    batch = batch if batch is not None else _read_toml(MODE_CONFIG_PATHS["batch"])
    gui = gui if gui is not None else _read_toml(MODE_CONFIG_PATHS["gui"])
    batch_paths = _leaf_paths(batch)
    gui_paths = _leaf_paths(gui)
    if batch_paths != gui_paths:
        only_batch = sorted(".".join(item) for item in batch_paths - gui_paths)
        only_gui = sorted(".".join(item) for item in gui_paths - batch_paths)
        raise ProjectConfigError(
            f"batch/gui 配置未成对；仅 batch={only_batch}；仅 gui={only_gui}"
        )
    for path in batch_paths:
        batch_value = get_value(batch, path)
        gui_value = get_value(gui, path)
        if type(batch_value) is not type(gui_value):
            dotted = ".".join(path)
            raise ProjectConfigError(f"batch/gui 配置类型不一致: {dotted}")

    features = get_value(gui, ("project_features",), {})
    if isinstance(features, dict):
        enabled = [name for name, value in features.items() if value is True]
        if enabled:
            raise ProjectConfigError(
                "GUI 中的项目新增功能必须关闭: " + ", ".join(sorted(enabled))
            )


def load_mode_settings(
    mode: str,
    *,
    shared_path: Path | None = None,
    mode_path: Path | None = None,
) -> dict[str, Any]:
    """返回 shared + mode 的有效配置，并严格检查三层归属。"""
    if mode not in MODE_CONFIG_PATHS:
        raise ProjectConfigError(f"不支持的配置模式: {mode}")
    shared = _read_toml(shared_path or SHARED_CONFIG_PATH)
    selected = _read_toml(mode_path or MODE_CONFIG_PATHS[mode])
    if shared_path is None and mode_path is None:
        validate_mode_pair()
    result = _merge_without_overlap(shared, selected)
    _validate_effective_settings(result, mode)
    return result


def get_value(
    data: dict[str, Any],
    path: str | Iterable[str],
    default: Any = None,
) -> Any:
    """按点路径或路径片段读取嵌套配置。"""
    parts = tuple(path.split(".")) if isinstance(path, str) else tuple(path)
    current: Any = data
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def resolve_project_path(value: str) -> Path:
    """将配置路径解析为绝对路径；相对路径固定相对于项目根目录。"""
    expanded = os.path.expandvars(value.strip())
    if expanded == "~" or expanded.startswith(("~/", "~\\")):
        expanded = os.path.expanduser(expanded)
    path = Path(expanded)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(strict=False)


def _validate_effective_settings(settings: dict[str, Any], mode: str) -> None:
    required = (
        "language.video.source",
        "language.video.target",
        "transcription.engine",
        "translation.enabled",
        "paths.upstream_root",
        "paths.faster_whisper_bin",
        "paths.models",
        "paths.cache",
        "paths.logs",
        "paths.gui_runtime_settings",
    )
    missing = [name for name in required if get_value(settings, name) is None]
    if missing:
        raise ProjectConfigError(f"{mode} 有效配置缺少: {', '.join(missing)}")
    for name in ("language.video.source", "language.video.target"):
        value = get_value(settings, name)
        if not isinstance(value, str) or not value.strip():
            raise ProjectConfigError(f"配置项必须是非空字符串: {name}")
    source = get_value(settings, "language.video.source")
    target = get_value(settings, "language.video.target")
    if source not in _SOURCE_LANGUAGE_LABELS:
        raise ProjectConfigError(f"不支持的源语言代码: {source}")
    if target not in _TARGET_LANGUAGE_LABELS:
        raise ProjectConfigError(f"不支持的目标语言代码: {target}")
    if mode == "batch" and get_value(
        settings, "project_features.request_level_auto_retry"
    ):
        raise ProjectConfigError("DeepSeek 请求级自动重试在本项目中固定关闭")
    if mode == "batch" and get_value(
        settings, "project_features.video_level_translation"
    ):
        positive_integer_fields = (
            "translation_batching.max_in_flight",
            "translation_batching.video_concurrency",
            "translation_batching.max_input_tokens",
            "translation_batching.max_output_tokens",
        )
        for name in positive_integer_fields:
            value = get_value(settings, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ProjectConfigError(f"配置项必须是正整数: {name}")
        reserve_ratio = get_value(settings, "translation_batching.output_reserve_ratio")
        if not isinstance(reserve_ratio, (int, float)) or isinstance(
            reserve_ratio, bool
        ) or reserve_ratio < 1:
            raise ProjectConfigError(
                "配置项必须是不小于 1 的数字: translation_batching.output_reserve_ratio"
            )
        if get_value(settings, "translation_batching.reasoning_effort") != "none":
            raise ProjectConfigError("批处理视频级翻译必须关闭 DeepSeek 思考模式")
    managed_paths = (
        "upstream_root",
        "faster_whisper_bin",
        "models",
        "cache",
        "logs",
        "work",
        "temp",
        "gui_runtime_settings",
        "batch_journal",
        "reports",
        "deepseek_secret",
    )
    for name in managed_paths:
        path = resolve_project_path(str(get_value(settings, f"paths.{name}", "")))
        if not path.is_relative_to(PROJECT_ROOT):
            raise ProjectConfigError(f"项目管理路径不得越出项目根目录: paths.{name}")


_SOURCE_LANGUAGE_LABELS = {
    "auto": "自动检测",
    "en": "英语",
    "zh": "中文",
    "ja": "日本語",
    "ko": "韩语",
    "yue": "粤语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "ru": "俄语",
    "pt": "葡萄牙语",
    "tr": "土耳其语",
    "pl": "Polish",
    "ca": "Catalan",
    "nl": "Dutch",
    "ar": "Arabic",
    "sv": "Swedish",
    "it": "Italian",
    "id": "Indonesian",
    "hi": "Hindi",
    "fi": "Finnish",
    "vi": "Vietnamese",
    "he": "Hebrew",
    "uk": "Ukrainian",
    "el": "Greek",
    "ms": "Malay",
    "cs": "Czech",
    "ro": "Romanian",
    "da": "Danish",
    "hu": "Hungarian",
    "ta": "Tamil",
    "no": "Norwegian",
    "th": "Thai",
    "ur": "Urdu",
    "hr": "Croatian",
    "bg": "Bulgarian",
    "lt": "Lithuanian",
    "la": "Latin",
    "mi": "Maori",
    "ml": "Malayalam",
    "cy": "Welsh",
    "sk": "Slovak",
    "te": "Telugu",
    "fa": "Persian",
    "lv": "Latvian",
    "bn": "Bengali",
    "sr": "Serbian",
    "az": "Azerbaijani",
    "sl": "Slovenian",
    "kn": "Kannada",
    "et": "Estonian",
    "mk": "Macedonian",
    "br": "Breton",
    "eu": "Basque",
    "is": "Icelandic",
    "hy": "Armenian",
    "ne": "Nepali",
    "mn": "Mongolian",
    "bs": "Bosnian",
    "kk": "Kazakh",
    "sq": "Albanian",
    "sw": "Swahili",
    "gl": "Galician",
    "mr": "Marathi",
    "pa": "Punjabi",
    "si": "Sinhala",
    "km": "Khmer",
    "sn": "Shona",
    "yo": "Yoruba",
    "so": "Somali",
    "af": "Afrikaans",
    "oc": "Occitan",
    "ka": "Georgian",
    "be": "Belarusian",
    "tg": "Tajik",
    "sd": "Sindhi",
    "gu": "Gujarati",
    "am": "Amharic",
    "yi": "Yiddish",
    "lo": "Lao",
    "uz": "Uzbek",
    "fo": "Faroese",
    "ht": "Haitian Creole",
    "ps": "Pashto",
    "tk": "Turkmen",
    "nn": "Nynorsk",
    "mt": "Maltese",
    "sa": "Sanskrit",
    "lb": "Luxembourgish",
    "my": "Myanmar",
    "bo": "Tibetan",
    "tl": "Tagalog",
    "mg": "Malagasy",
    "as": "Assamese",
    "tt": "Tatar",
    "haw": "Hawaiian",
    "ln": "Lingala",
    "ha": "Hausa",
    "ba": "Bashkir",
    "jw": "Javanese",
    "su": "Sundanese",
}

_TARGET_LANGUAGE_LABELS = {
    "zh-Hans": "简体中文",
    "zh-Hant": "繁体中文",
    "en": "英语",
    "en-US": "英语(美国)",
    "en-GB": "英语(英国)",
    "ja": "日本語",
    "ko": "韩语",
    "yue": "粤语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
    "ms": "马来语",
    "fil": "菲律宾语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "ru": "俄语",
    "pt": "葡萄牙语",
    "pt-BR": "葡萄牙语(巴西)",
    "pt-PT": "葡萄牙语(葡萄牙)",
    "it": "意大利语",
    "nl": "荷兰语",
    "pl": "波兰语",
    "tr": "土耳其语",
    "el": "希腊语",
    "cs": "捷克语",
    "sv": "瑞典语",
    "da": "丹麦语",
    "fi": "芬兰语",
    "nb": "挪威语",
    "hu": "匈牙利语",
    "ro": "罗马尼亚语",
    "bg": "保加利亚语",
    "uk": "乌克兰语",
    "ar": "阿拉伯语",
    "he": "希伯来语",
    "fa": "波斯语",
}

_TRANSCRIBE_ENGINE_LABELS = {
    "bijian": "B 接口",
    "jianying": "J 接口",
    "whisper-api": "Whisper [API] ✨",
    "faster-whisper": "FasterWhisper ✨",
    "whisper-cpp": "WhisperCpp",
}
_TRANSLATOR_LABELS = {
    "llm": "LLM 大模型翻译",
    "deeplx": "DeepLx 翻译",
    "bing": "微软翻译",
    "google": "谷歌翻译",
}
_LLM_LABELS = {
    "openai": "OpenAI 兼容",
    "siliconcloud": "SiliconCloud",
    "deepseek": "DeepSeek",
    "ollama": "Ollama",
    "lm-studio": "LM Studio",
    "gemini": "Gemini",
    "chatglm": "ChatGLM",
}
_LAYOUT_LABELS = {
    "target-above": "译文在上",
    "source-above": "原文在上",
    "source-only": "仅原文",
    "target-only": "仅译文",
}
_QUALITY_LABELS = {
    "ultra-high": "极高质量",
    "high": "高质量",
    "medium": "中等质量",
    "low": "低质量",
}


def build_gui_qconfig(settings: dict[str, Any]) -> dict[str, Any]:
    """把规范配置转换为上游 QConfig 的 JSON 结构。"""
    service_names = {
        "openai": "OpenAI",
        "siliconcloud": "SiliconCloud",
        "deepseek": "DeepSeek",
        "ollama": "Ollama",
        "lm_studio": "LmStudio",
        "gemini": "Gemini",
        "chatglm": "ChatGLM",
    }
    llm: dict[str, Any] = {
        "LLMService": _LLM_LABELS[get_value(settings, "translation.llm_service")],
    }
    for canonical, qname in service_names.items():
        section_name = canonical.replace("_", "-")
        llm[f"{qname}_Model"] = get_value(
            settings, f"translation.services.{section_name}.model", ""
        )
        llm[f"{qname}_API_Base"] = get_value(
            settings, f"translation.services.{section_name}.api_base", ""
        )
        # 临时 JSON 不复制任何密钥；实际调用由独立 secret 桥按需注入。
        llm[f"{qname}_API_Key"] = ""

    selected_service = str(get_value(settings, "translation.llm_service"))
    selected_qname = service_names[selected_service.replace("-", "_")]
    llm[f"{selected_qname}_Model"] = get_value(settings, "translation.model")
    llm[f"{selected_qname}_API_Base"] = get_value(settings, "translation.api_base")

    source = get_value(settings, "language.video.source")
    target = get_value(settings, "language.video.target")
    return {
        "Translate": {
            "BatchSize": get_value(settings, "translation.batch_size"),
            "DeeplxEndpoint": get_value(settings, "translation.deeplx_endpoint"),
            "NeedReflectTranslate": get_value(settings, "translation.reflect"),
            "ThreadNum": get_value(settings, "translation.thread_num"),
            "TranslatorServiceEnum": _TRANSLATOR_LABELS[
                get_value(settings, "translation.translator")
            ],
        },
        "Cache": {"CacheEnabled": get_value(settings, "cache.enabled")},
        "LLM": llm,
        "Update": {
            "CheckUpdateAtStartUp": get_value(settings, "interface.check_updates_at_startup")
        },
        "Subtitle": {
            "CustomPromptText": get_value(settings, "translation.custom_prompt"),
            "MaxWordCountCJK": get_value(settings, "translation.max_words_cjk"),
            "MaxWordCountEnglish": get_value(settings, "translation.max_words_english"),
            "NeedOptimize": get_value(settings, "translation.optimize"),
            "NeedSplit": get_value(settings, "translation.split"),
            "NeedTranslate": get_value(settings, "translation.enabled"),
            "TargetLanguage": _TARGET_LANGUAGE_LABELS[target],
        },
        "MainWindow": {
            "DpiScale": get_value(settings, "interface.dpi_scale"),
            "Language": get_value(settings, "interface.language"),
            "MicaEnabled": get_value(settings, "interface.mica_enabled"),
        },
        "FasterWhisper": {
            "Device": get_value(settings, "transcription.device"),
            "FfMdxKim2": get_value(settings, "transcription.voice_extraction"),
            "Model": get_value(settings, "transcription.model"),
            "ModelDir": str(resolve_project_path(get_value(settings, "paths.models"))),
            "OneWord": get_value(settings, "transcription.one_word"),
            "Program": str(
                resolve_project_path(get_value(settings, "paths.faster_whisper_bin"))
                / get_value(settings, "transcription.program")
            ),
            "Prompt": get_value(settings, "transcription.prompt"),
            "VadFilter": get_value(settings, "transcription.vad_filter"),
            "VadMethod": get_value(settings, "transcription.vad_method"),
            "VadThreshold": get_value(settings, "transcription.vad_threshold"),
        },
        "Video": {
            "NeedVideo": get_value(settings, "output.synthesize_video"),
            "SoftSubtitle": get_value(settings, "output.soft_subtitle"),
            "UseSubtitleStyle": get_value(settings, "output.use_subtitle_style"),
            "VideoQuality": _QUALITY_LABELS[get_value(settings, "output.video_quality")],
        },
        "RoundedBgStyle": {
            "BgColor": get_value(settings, "subtitle_style.rounded.background_color"),
            "CornerRadius": get_value(settings, "subtitle_style.rounded.corner_radius"),
            "FontName": get_value(settings, "subtitle_style.rounded.font_name"),
            "FontSize": get_value(settings, "subtitle_style.rounded.font_size"),
            "LetterSpacing": get_value(settings, "subtitle_style.rounded.letter_spacing"),
            "LineSpacing": get_value(settings, "subtitle_style.rounded.line_spacing"),
            "MarginBottom": get_value(settings, "subtitle_style.rounded.margin_bottom"),
            "PaddingH": get_value(settings, "subtitle_style.rounded.padding_horizontal"),
            "PaddingV": get_value(settings, "subtitle_style.rounded.padding_vertical"),
            "TextColor": get_value(settings, "subtitle_style.rounded.text_color"),
        },
        "SubtitleStyle": {
            "Layout": _LAYOUT_LABELS[get_value(settings, "output.subtitle_layout")],
            "PreviewImage": get_value(settings, "subtitle_style.preview_image"),
            "RenderMode": get_value(settings, "subtitle_style.render_mode"),
            "StyleName": get_value(settings, "subtitle_style.name"),
        },
        "QFluentWidgets": {
            "ThemeColor": get_value(settings, "interface.theme_color"),
            "ThemeMode": get_value(settings, "interface.theme_mode"),
        },
        "Transcribe": {
            "TranscribeLanguage": _SOURCE_LANGUAGE_LABELS.get(source, source),
            "TranscribeModel": _TRANSCRIBE_ENGINE_LABELS[
                get_value(settings, "transcription.engine")
            ],
            "OutputFormat": get_value(settings, "transcription.output_format"),
        },
        "WhisperAPI": {
            "WhisperApiBase": get_value(settings, "transcription.whisper_api.api_base"),
            "WhisperApiKey": "",
            "WhisperApiModel": get_value(settings, "transcription.whisper_api.model"),
            "WhisperApiPrompt": get_value(settings, "transcription.whisper_api.prompt"),
        },
        "Whisper": {"WhisperModel": get_value(settings, "transcription.whisper_model")},
        "Save": {"Work_Dir": str(resolve_project_path(get_value(settings, "paths.work")))},
    }


def write_gui_runtime_settings(settings: dict[str, Any] | None = None) -> Path:
    """原子生成 GUI 临时 QConfig JSON，返回其绝对路径。"""
    settings = settings or load_mode_settings("gui")
    path = resolve_project_path(get_value(settings, "paths.gui_runtime_settings"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(build_gui_qconfig(settings), ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path
