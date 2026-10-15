"""Real handler-stop and failed-cleanup boundaries for the owning HTTP helper."""
from __future__ import annotations

import http.server
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch


_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "qualification"
sys.path.insert(0, str(_TOOLS))
import http_json_postman_contract as helper
sys.path.pop(0)


class PostmanHelperLifecycleTest(unittest.TestCase):
    def test_held_real_handler_prevents_fixture_cleanup(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                entered.set()
                release.wait(5)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        errors = []
        with helper._qualification_directory() as directory:
            fixture = Path(directory) / "database-bytes"
            fixture.write_bytes(b"original fixture bytes")
            listener = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            listener.daemon_threads = False
            worker = threading.Thread(target=listener.serve_forever)
            client = stopper = None
            try:
                worker.start()
                client = socket.create_connection(listener.server_address)
                client.sendall(b"GET / HTTP/1.0\r\n\r\n")
                self.assertTrue(entered.wait(3))

                def stop():
                    try:
                        helper._stop_server(listener, worker)
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        stopped.set()

                stopper = threading.Thread(target=stop)
                stopper.start()
                self.assertFalse(stopped.wait(0.7), "active handler must block server_close")
                self.assertEqual(fixture.read_bytes(), b"original fixture bytes")
            finally:
                release.set()
                try:
                    if stopper is not None and stopper.ident is not None:
                        stopper.join(5)
                        if stopper.is_alive():
                            raise RuntimeError("POSTMAN_TEST_STOP_UNCONFIRMED")
                    if worker.is_alive():
                        helper._stop_server(listener, worker)
                    else:
                        listener.server_close()
                finally:
                    if client is not None:
                        client.close()
            self.assertTrue(stopped.is_set())
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(listener.socket.fileno(), -1)
        self.assertFalse(fixture.parent.exists())

    def test_unknown_stop_retains_original_bytes_and_both_errors(self):
        listener = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        listener.daemon_threads = False
        worker = threading.Thread(target=listener.serve_forever)
        directory = None
        try:
            worker.start()
            with self.assertRaises(BaseExceptionGroup) as observed:
                with helper._qualification_directory() as directory:
                    fixture = Path(directory) / "database-bytes"
                    fixture.write_bytes(b"original fixture bytes")
                    try:
                        raise ValueError("primary request failure")
                    finally:
                        with patch.object(listener, "shutdown", side_effect=RuntimeError("stop unconfirmed")):
                            helper._stop_server(listener, worker)
            self.assertEqual([str(error) for error in observed.exception.exceptions],
                             ["primary request failure", "stop unconfirmed"])
            self.assertTrue(worker.is_alive())
            self.assertEqual(fixture.read_bytes(), b"original fixture bytes")
        finally:
            if worker.is_alive():
                listener.shutdown()
                worker.join(3)
                if worker.is_alive():
                    raise RuntimeError("POSTMAN_TEST_STOP_UNCONFIRMED")
            listener.server_close()
            if directory is not None:
                import shutil
                shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
