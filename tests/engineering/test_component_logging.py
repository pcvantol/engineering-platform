from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.engineering import component_logging


class ComponentLoggingTest(unittest.TestCase):
    def test_component_log_is_structured_redacted_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logger = component_logging.component_logger(root, "inbox", level="DEBUG")
            component_logging.log_event(
                logger,
                logging.INFO,
                "job_finished access_token=do-not-persist",
                run_id="inbox-example",
                diagnostic="authorization: secret-value",
            )
            for handler in logger.handlers:
                handler.flush()
            path = root / ".djconnect" / "logs" / "inbox.log"
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["level"], "INFO")
            self.assertEqual(record["component"], "inbox")
            self.assertEqual(record["run_id"], "inbox-example")
            self.assertIn("timestamp", record)
            self.assertIn("[REDACTED]", record["event"])
            self.assertIn("[REDACTED]", record["diagnostic"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_level_fails_closed_to_info_and_rotation_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            component_logging, "MAX_LOG_BYTES", 1
        ):
            root = Path(temporary)
            logger = component_logging.component_logger(root, "dashboard", level="invalid")
            self.assertEqual(logger.level, logging.INFO)
            component_logging.log_event(logger, logging.INFO, "first")
            component_logging.log_event(logger, logging.INFO, "second")
            for handler in logger.handlers:
                handler.flush()
            directory = root / ".djconnect" / "logs"
            self.assertTrue((directory / "dashboard.log").exists())
            self.assertTrue((directory / "dashboard.log.1").exists())
            self.assertEqual((directory / "dashboard.log").stat().st_mode & 0o777, 0o600)
            self.assertEqual((directory / "dashboard.log.1").stat().st_mode & 0o777, 0o600)
