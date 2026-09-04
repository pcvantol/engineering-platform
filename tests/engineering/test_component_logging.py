from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import component_logging


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
            with sqlite3.connect(root / ".engineering" / "engineering.db") as connection:
                payload = connection.execute(
                    "SELECT payload FROM engineering_component_logs WHERE component='inbox'"
                ).fetchone()[0]
            record = json.loads(payload)
            self.assertEqual(record["level"], "INFO")
            self.assertEqual(record["component"], "inbox")
            self.assertEqual(record["run_id"], "inbox-example")
            self.assertIn("timestamp", record)
            self.assertIn("[REDACTED]", record["event"])
            self.assertIn("[REDACTED]", record["diagnostic"])
            self.assertFalse((root / ".engineering" / "logs" / "inbox.log").exists())

    def test_central_logger_never_creates_repository_storage(self) -> None:
        """Installed lifecycle logging must retain the explicit CENTRAL binding."""
        from engineering_platform.server import initialize
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            root.mkdir()
            data_root = Path(temporary) / "data"
            initialize(data_root)
            central = data_root / "engineering.db"
            logger = component_logging.component_logger(
                root, "lifecycle_worker", central_database=central,
            )
            component_logging.log_event(logger, logging.INFO, "central_lifecycle_event")
            self.assertFalse((root / ".engineering" / "engineering.db").exists())
            with sqlite3.connect(central) as connection:
                self.assertEqual(
                    "central_lifecycle_event",
                    json.loads(connection.execute(
                        "SELECT payload FROM engineering_component_logs WHERE component='lifecycle_worker'"
                    ).fetchone()[0])["event"],
                )

    def test_supported_writer_uses_server_central_sink_without_caller_selection(self) -> None:
        """A normal supported component writer cannot fall back to its checkout."""
        from engineering_platform.server import initialize
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            checkout.mkdir()
            data_root = Path(temporary) / "data"
            initialize(data_root)
            with patch.dict("os.environ", {"EP_SERVER_DATA_ROOT": str(data_root)}):
                logger = component_logging.component_logger(checkout, "operations_console")
                component_logging.log_event(logger, logging.INFO, "normal_console_event")
            self.assertFalse((checkout / ".engineering" / "engineering.db").exists())
            with sqlite3.connect(data_root / "engineering.db") as connection:
                component, payload = connection.execute(
                    "SELECT component,payload FROM engineering_component_logs"
                ).fetchone()
            self.assertEqual(component, "operations_console")
            self.assertEqual(json.loads(payload)["event"], "normal_console_event")

    def test_lifecycle_events_include_only_redacted_component_identity(self) -> None:
        from engineering_platform import providers
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            providers.subprocess,
            "run",
            return_value=__import__("subprocess").CompletedProcess((), 0, "abc123def456\n", ""),
        ):
            root = Path(temporary)
            context = component_logging.component_lifecycle_context(
                root,
                version="1.2.3",
                launchd_label="com.example.engineering",
                launch_agent_path=Path("/Users/example/Library/LaunchAgents/com.example.engineering.plist"),
            )
            logger = component_logging.component_logger(root, "dashboard")
            component_logging.log_event(
                logger,
                logging.INFO,
                "component_restart_trigger_received",
                context={**context, "target_component": "inbox_watcher", "secret": "must-not-persist"},
            )
            with sqlite3.connect(root / ".engineering" / "engineering.db") as connection:
                payload = connection.execute(
                    "SELECT payload FROM engineering_component_logs WHERE component='dashboard'"
                ).fetchone()[0]
            record = json.loads(payload)
            self.assertEqual(record["application_version"], "1.2.3")
            self.assertEqual(record["git_commit"], "abc123def456")
            self.assertEqual(record["launchd_label"], "com.example.engineering")
            self.assertEqual(record["target_component"], "inbox_watcher")
            self.assertNotIn("secret", record)

    def test_invalid_level_fails_closed_to_info_and_uses_file_only_when_storage_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            component_logging, "MAX_LOG_BYTES", 1
        ), patch.object(
            component_logging, "open_storage", side_effect=component_logging.EngineeringStorageError("offline")
        ):
            root = Path(temporary)
            logger = component_logging.component_logger(root, "dashboard", level="invalid")
            self.assertEqual(logger.level, logging.INFO)
            component_logging.log_event(logger, logging.INFO, "first")
            component_logging.log_event(logger, logging.INFO, "second")
            for handler in logger.handlers:
                handler.flush()
            directory = root / ".engineering" / "logs"
            self.assertTrue((directory / "dashboard.log").exists())
            self.assertTrue((directory / "dashboard.log.1").exists())
            self.assertEqual((directory / "dashboard.log").stat().st_mode & 0o777, 0o600)
            self.assertEqual((directory / "dashboard.log.1").stat().st_mode & 0o777, 0o600)

    def test_component_log_reads_sqlite_and_clear_removes_only_requested_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inbox = component_logging.component_logger(root, "inbox")
            dashboard = component_logging.component_logger(root, "dashboard")
            component_logging.log_event(inbox, logging.INFO, "inbox_event")
            component_logging.log_event(dashboard, logging.INFO, "dashboard_event")
            self.assertIn(b"inbox_event", component_logging.component_log(root, "inbox"))
            self.assertIn("sqlite:1:", component_logging.component_log_version(root, "inbox"))
            component_logging.clear_component_log(root, "inbox")
            self.assertNotIn(b"inbox_event", component_logging.component_log(root, "inbox"))
            self.assertIn(b"dashboard_event", component_logging.component_log(root, "dashboard"))

    def test_component_log_page_filters_full_history_before_paginating(self) -> None:
        """Historical rows remain discoverable after newer rows fill a page."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with component_logging.open_storage(root) as connection:
                for payload, created_at in (
                    (
                        {
                            "timestamp": "2026-08-26T09:00:00+00:00",
                            "level": "WARNING",
                            "event": "historical_warning",
                            "diagnostic": "needle from yesterday",
                        },
                        "2026-08-26T09:00:00+00:00",
                    ),
                    (
                        {
                            "timestamp": "2026-08-26T10:00:00+00:00",
                            "level": "INFO",
                            "event": "historical_info",
                            "diagnostic": "another yesterday record",
                        },
                        "2026-08-26T10:00:00+00:00",
                    ),
                    (
                        {
                            "timestamp": "2026-08-27T09:00:00+00:00",
                            "level": "INFO",
                            "event": "newest_record",
                            "diagnostic": "today",
                        },
                        "2026-08-27T09:00:00+00:00",
                    ),
                ):
                    connection.execute(
                        "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                        ("inbox", json.dumps(payload), created_at),
                    )
                # These newer records would have hidden the historical
                # warning in the former client-side latest-100 sample.
                for index in range(120):
                    connection.execute(
                        "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                        (
                            "inbox",
                            json.dumps({
                                "timestamp": f"2026-08-27T10:{index // 60:02d}:{index % 60:02d}+00:00",
                                "level": "INFO",
                                "event": "newest_record",
                                "diagnostic": "today",
                            }),
                            f"2026-08-27T10:{index // 60:02d}:{index % 60:02d}+00:00",
                        ),
                    )

            page = component_logging.component_log_page(
                root,
                "inbox",
                page=1,
                page_size=1,
                start_at="2026-08-26T00:00:00+00:00",
                end_at="2026-08-27T00:00:00+00:00",
                search="needle",
                level="WARNING",
                events=("historical_warning",),
            )

            self.assertEqual(page["total"], 1)
            self.assertEqual(page["events"], ["historical_warning"])
            self.assertEqual(page["entries"], [{
                "timestamp": "2026-08-26T09:00:00+00:00",
                "level": "WARNING",
                "event": "historical_warning",
                "diagnostic": "needle from yesterday",
                "line": 1,
            }])

    def test_component_log_page_treats_level_as_a_minimum_severity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with component_logging.open_storage(root) as connection:
                for index, level in enumerate(("DEBUG", "INFO", "WARNING", "ERROR"), start=1):
                    connection.execute(
                        "INSERT INTO engineering_component_logs(component,payload,created_at) VALUES(?,?,?)",
                        ("inbox", json.dumps({"level": level, "event": level.lower()}), f"2026-08-29T07:00:0{index}+00:00"),
                    )

            for minimum, expected in {
                "DEBUG": ["DEBUG", "INFO", "WARNING", "ERROR"],
                "INFO": ["INFO", "WARNING", "ERROR"],
                "WARNING": ["WARNING", "ERROR"],
                "ERROR": ["ERROR"],
            }.items():
                page = component_logging.component_log_page(root, "inbox", level=minimum, direction="asc")
                self.assertEqual([entry["level"] for entry in page["entries"]], expected)
