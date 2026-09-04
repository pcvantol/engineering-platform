from __future__ import annotations

import json
import io
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engineering_platform import component_logging


class ComponentLoggingTest(unittest.TestCase):
    def test_component_log_is_structured_redacted_and_private(self) -> None:
        from engineering_platform.server import initialize
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "central"
            initialize(data_root)
            logger = component_logging.component_logger(
                root, "file_inbox_ingress", level="DEBUG", central_database=data_root / "engineering.db",
            )
            component_logging.log_event(
                logger,
                logging.INFO,
                "job_finished access_token=do-not-persist",
                run_id="inbox-example",
                diagnostic="authorization: secret-value",
            )
            with sqlite3.connect(data_root / "engineering.db") as connection:
                payload = connection.execute(
                    "SELECT payload FROM engineering_component_logs WHERE component='file_inbox_ingress'"
                ).fetchone()[0]
            record = json.loads(payload)
            self.assertEqual(record["level"], "INFO")
            self.assertEqual(record["component"], "file_inbox_ingress")
            self.assertEqual(record["run_id"], "inbox-example")
            self.assertIn("timestamp", record)
            self.assertIn("[REDACTED]", record["event"])
            self.assertIn("[REDACTED]", record["diagnostic"])
            self.assertFalse((root / ".engineering").exists())

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
        from engineering_platform import providers, server
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            providers.subprocess,
            "run",
            return_value=__import__("subprocess").CompletedProcess((), 0, "abc123def456\n", ""),
        ):
            root = Path(temporary)
            data_root = root / "central"
            server.initialize(data_root)
            context = component_logging.component_lifecycle_context(
                root,
                version="1.2.3",
                launchd_label="com.example.engineering",
                launch_agent_path=Path("/Users/example/Library/LaunchAgents/com.example.engineering.plist"),
            )
            logger = component_logging.component_logger(
                root, "operations_console", central_database=data_root / "engineering.db",
            )
            component_logging.log_event(
                logger,
                logging.INFO,
                "component_restart_trigger_received",
                context={**context, "target_component": "inbox_watcher", "secret": "must-not-persist"},
            )
            with sqlite3.connect(data_root / "engineering.db") as connection:
                payload = connection.execute(
                    "SELECT payload FROM engineering_component_logs WHERE component='operations_console'"
                ).fetchone()[0]
            record = json.loads(payload)
            self.assertEqual(record["application_version"], "1.2.3")
            self.assertEqual(record["git_commit"], "abc123def456")
            self.assertEqual(record["launchd_label"], "com.example.engineering")
            self.assertEqual(record["target_component"], "inbox_watcher")
            self.assertNotIn("secret", record)

    def test_invalid_level_fails_closed_without_creating_a_local_log_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(component_logging.sys, "stderr", io.StringIO()):
            root = Path(temporary)
            logger = component_logging.component_logger(root, "operations_console", level="invalid")
            self.assertEqual(logger.level, logging.INFO)
            component_logging.log_event(logger, logging.INFO, "first")
            self.assertFalse((root / ".engineering").exists())

    def test_read_and_clear_never_use_a_repository_local_component_log_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_log = root / ".engineering" / "logs" / "inbox.log"
            legacy_log.parent.mkdir(parents=True)
            legacy_log.write_text("legacy local authority", encoding="utf-8")
            unavailable = component_logging.EngineeringStorageError("CENTRAL unavailable")
            with patch.object(component_logging, "open_storage", side_effect=unavailable):
                self.assertEqual(
                    component_logging.component_log(root, "inbox"),
                    b"CENTRAL componentlog is tijdelijk niet beschikbaar.",
                )
                self.assertEqual(component_logging.component_log_version(root, "inbox"), "central-unavailable")
                with self.assertRaisesRegex(OSError, "CENTRAL componentlog kon niet worden gewist"):
                    component_logging.clear_component_log(root, "inbox")
            self.assertEqual(legacy_log.read_text(encoding="utf-8"), "legacy local authority")

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
