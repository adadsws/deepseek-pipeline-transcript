"""修复 VideoCaptioner v1.4.2 在 Windows 上共享日志文件的轮转冲突。"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
from collections.abc import Iterable

from videocaptioner_project_config import get_value, load_mode_settings


# 上游来源：https://github.com/WEIFENG2333/VideoCaptioner/tree/d753521d57cf2311df96bfee96fe39c6306a8e09
# 上游 setup_logger 会让每个命名 logger 分别打开同一个 app.log。Windows 不允许在
# 其他处理器仍持有文件时重命名该文件，因此达到轮转阈值后会反复触发 WinError 32。
_install_lock = threading.RLock()
_shared_handlers: dict[str, WindowsSafeRotatingFileHandler] = {}
_original_setup_logger = None
_installed = False
_settings = load_mode_settings("gui")


class WindowsSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """共享冲突时延迟轮转，并继续把当前记录写入原日志文件。"""

    retry_interval_seconds = float(get_value(_settings, "logging.rotation_retry_seconds"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rollover_retry_after = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        if time.monotonic() < self._rollover_retry_after:
            return False
        return super().shouldRollover(record)

    def doRollover(self) -> None:  # noqa: N802
        try:
            super().doRollover()
        except OSError as exc:
            if not _is_windows_sharing_violation(exc):
                raise
            self._rollover_retry_after = time.monotonic() + self.retry_interval_seconds
            if self.stream is None and not self.delay:
                self.stream = self._open()


def _is_windows_sharing_violation(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {32, 33}


def _handler_key(handler: logging.handlers.RotatingFileHandler) -> str:
    return os.path.normcase(os.path.abspath(handler.baseFilename))


def _make_shared_handler(
    source: logging.handlers.RotatingFileHandler,
) -> WindowsSafeRotatingFileHandler:
    handler = WindowsSafeRotatingFileHandler(
        source.baseFilename,
        mode=source.mode,
        maxBytes=int(get_value(_settings, "logging.rotate_max_bytes")),
        backupCount=int(get_value(_settings, "logging.rotate_backup_count")),
        encoding=source.encoding,
        delay=source.delay,
        errors=getattr(source, "errors", None),
    )
    configured_level = str(get_value(_settings, "logging.level")).upper()
    handler.setLevel(getattr(logging, configured_level, source.level))
    handler.setFormatter(source.formatter)
    for item in source.filters:
        handler.addFilter(item)
    return handler


def coalesce_rotating_handlers(loggers: Iterable[logging.Logger]) -> None:
    """让指向同一文件的多个 logger 共用一个安全轮转处理器。"""
    with _install_lock:
        for logger in loggers:
            for handler in tuple(logger.handlers):
                if not isinstance(handler, logging.handlers.RotatingFileHandler):
                    continue
                key = _handler_key(handler)
                shared = _shared_handlers.get(key)
                if shared is None:
                    if isinstance(handler, WindowsSafeRotatingFileHandler):
                        shared = handler
                    else:
                        logger.removeHandler(handler)
                        handler.close()
                        shared = _make_shared_handler(handler)
                        logger.addHandler(shared)
                    _shared_handlers[key] = shared
                elif handler is not shared:
                    logger.removeHandler(handler)
                    handler.close()
                    logger.addHandler(shared)


def _configured_loggers() -> list[logging.Logger]:
    rows = [logging.getLogger()]
    rows.extend(
        item
        for item in logging.Logger.manager.loggerDict.values()
        if isinstance(item, logging.Logger)
    )
    return rows


def install() -> None:
    """复用上游配置逻辑，只替换其每 logger 独占文件句柄的行为。"""
    global _installed, _original_setup_logger
    with _install_lock:
        if _installed:
            return

        from videocaptioner.core.utils import logger as upstream_logger

        _original_setup_logger = upstream_logger.setup_logger

        def setup_logger(*args, **kwargs):
            configured = _original_setup_logger(*args, **kwargs)
            coalesce_rotating_handlers((configured,))
            return configured

        upstream_logger.setup_logger = setup_logger
        coalesce_rotating_handlers(_configured_loggers())
        _installed = True
