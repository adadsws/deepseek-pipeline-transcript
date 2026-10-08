"""Windows 共享日志文件轮转补丁的纯本地测试。"""

from __future__ import annotations

import importlib.util
import logging
import logging.handlers
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "local_patch" / "videocaptioner_logging_fix.py"
SPEC = importlib.util.spec_from_file_location("videocaptioner_logging_fix_test", MODULE_PATH)
assert SPEC and SPEC.loader
logging_fix = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = logging_fix
SPEC.loader.exec_module(logging_fix)


class LoggingFixTests(unittest.TestCase):
    def tearDown(self) -> None:
        for handler in logging_fix._shared_handlers.values():
            handler.close()
        logging_fix._shared_handlers.clear()

    def test_loggers_for_same_file_share_one_rotating_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "app.log"
            first = logging.Logger("first")
            second = logging.Logger("second")
            first.addHandler(
                logging.handlers.RotatingFileHandler(path, maxBytes=1024, backupCount=2)
            )
            second.addHandler(
                logging.handlers.RotatingFileHandler(path, maxBytes=1024, backupCount=2)
            )

            logging_fix.coalesce_rotating_handlers((first, second))

            self.assertIs(first.handlers[0], second.handlers[0])
            self.assertIsInstance(
                first.handlers[0], logging_fix.WindowsSafeRotatingFileHandler
            )
            first.handlers.clear()
            second.handlers.clear()
            for handler in logging_fix._shared_handlers.values():
                handler.close()
            logging_fix._shared_handlers.clear()

    def test_sharing_violation_defers_rollover_and_still_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "app.log"
            handler = logging_fix.WindowsSafeRotatingFileHandler(
                path, maxBytes=1, backupCount=1, encoding="utf-8"
            )
            record = logging.LogRecord("test", logging.INFO, __file__, 1, "继续写入", (), None)
            blocked = PermissionError(13, "文件被占用")
            blocked.winerror = 32

            with mock.patch.object(
                logging.handlers.RotatingFileHandler,
                "doRollover",
                side_effect=blocked,
            ):
                handler.doRollover()
            handler.emit(record)
            handler.flush()
            handler.close()

            self.assertIn("继续写入", path.read_text(encoding="utf-8"))
            self.assertGreater(handler._rollover_retry_after, 0)


if __name__ == "__main__":
    unittest.main()
