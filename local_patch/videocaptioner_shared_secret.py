"""让 GUI 任务与连接测试在运行时读取项目共享的 DeepSeek 密钥。"""

from __future__ import annotations

from videocaptioner_project_config import get_value, load_mode_settings, resolve_project_path


SECRET_PATH = resolve_project_path(
    get_value(load_mode_settings("gui"), "paths.deepseek_secret")
)
_installed = False


def read_deepseek_key() -> str:
    """读取共享密钥；不缓存、不记录内容。"""
    try:
        key = SECRET_PATH.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"缺少共享 DeepSeek Key: {SECRET_PATH}") from exc
    if not key:
        raise RuntimeError(f"共享 DeepSeek Key 为空: {SECRET_PATH}")
    return key


def install() -> None:
    """只在实际调用 DeepSeek 时注入共享密钥，避免写回 GUI settings.json。"""
    global _installed
    if _installed:
        return
    from videocaptioner.core.entities import LLMServiceEnum
    from videocaptioner.ui.common.config import cfg
    from videocaptioner.ui.task_factory import TaskFactory
    from videocaptioner.ui.view.setting_interface import LLMConnectionThread

    original_create_subtitle_task = TaskFactory.create_subtitle_task

    def create_subtitle_task(*args: object, **kwargs: object) -> object:
        task = original_create_subtitle_task(*args, **kwargs)
        if cfg.llm_service.value == LLMServiceEnum.DEEPSEEK:
            config = getattr(task, "subtitle_config", None)
            if config is not None:
                config.api_key = read_deepseek_key()
        return task

    TaskFactory.create_subtitle_task = staticmethod(create_subtitle_task)

    original_connection_init = LLMConnectionThread.__init__

    def connection_init(self: object, api_base: str, api_key: str, model: str) -> None:
        if api_base.rstrip("/") == "https://api.deepseek.com/v1" and not api_key.strip():
            api_key = read_deepseek_key()
        original_connection_init(self, api_base, api_key, model)

    LLMConnectionThread.__init__ = connection_init  # type: ignore[method-assign]
    _installed = True
