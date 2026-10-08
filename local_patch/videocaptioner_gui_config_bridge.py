"""在 GUI 导入 QConfig 前，从三层 TOML 生成唯一的临时兼容设置。"""

from __future__ import annotations

from videocaptioner_project_config import write_gui_runtime_settings


_installed = False


def install() -> None:
    """设置上游 SETTINGS_PATH；重复安装不重复生成或覆盖钩子。"""
    global _installed
    if _installed:
        return
    import videocaptioner.config as app_config

    app_config.SETTINGS_PATH = write_gui_runtime_settings()
    _installed = True
