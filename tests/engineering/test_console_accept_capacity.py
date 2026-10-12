"""Native pending-connection capacity: real clients, handlers and bodies."""
import concurrent.futures
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from engineering_platform import server
from tests.engineering.dashboard_run_evidence_fixture import (
    IsolatedProviderTransport, OwnedRequestServer, RealTopologyFixture, TrackedRequestHandler,
)


class ConsoleAcceptCapacityTests(unittest.TestCase):
    def pending_admission(self, queue_size):
        """Measure finite initial accept drain before any HTTP handler starts.

        This is a native capacity observation, not a substitute HTTP result.
        Linux may deliver every request after SYN retransmission; that cannot
        erase a finite admission shortfall after the held accept interval.
        The drain is not an atomic kernel queue snapshot: new handshakes may
        arrive while the nonblocking accept calls drain the native listener.
        """
        actual_listen = []
        native_socket = socket.socket
        class ObservedSocket(native_socket):
            def listen(inner, backlog=socket.SOMAXCONN):
                actual_listen.append(backlog)
                return super().listen(backlog)
        class Listener(server._ConsoleHTTPServer):
            request_queue_size = queue_size
        with patch.object(socket, "socket", ObservedSocket):
            listener = Listener(("127.0.0.1", 0), server._HealthHandler)
        self.assertEqual(actual_listen, [queue_size])
        start = threading.Barrier(13)
        connected, accepted, results = [], [], []
        lock = threading.Lock()
        def client(index):
            connection = native_socket()
            connection.settimeout(1.5)
            address = None
            try:
                start.wait()
                connection.connect(listener.server_address)
                address = connection.getsockname()
                connection.sendall(b"native admission observation")
                with lock: connected.append((index, address, time.monotonic()))
                reply = connection.recv(3)
                return index, address, "ACK" if reply == b"ACK" else "INVALID_REPLY"
            except OSError as error:
                return index, address, type(error).__name__
            finally: connection.close()
        def acknowledge(connection):
            try:
                connection.settimeout(1.5)
                self.assertEqual(connection.recv(128), b"native admission observation")
                connection.sendall(b"ACK")
            except OSError:
                # A bounded, genuine transport loss is retained in the client
                # result. It never becomes an invented HTTP success or reset.
                pass
            finally: connection.close()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as clients:
                pending = [clients.submit(client, index) for index in range(12)]
                start.wait()
                time.sleep(.25)
                before_accept = list(connected)
                listener.socket.setblocking(False)
                boundary_connections = []
                while True:
                    try: boundary_connections.append(listener.socket.accept())
                    except BlockingIOError: break
                admitted = [address for _, address in boundary_connections]
                for connection, address in boundary_connections:
                    accepted.append(address); acknowledge(connection)
                listener.socket.settimeout(.15)
                deadline = time.monotonic() + 1.3
                while any(not item.done() for item in pending) and time.monotonic() < deadline:
                    try:
                        connection, address = listener.socket.accept()
                        accepted.append(address); acknowledge(connection)
                    except TimeoutError: pass
                results = [item.result(timeout=3) for item in pending]
        finally: listener.server_close()
        self.assertEqual({index for index, _, _ in results}, set(range(12)))
        acknowledged = {address for _, address, outcome in results if outcome == "ACK"}
        self.assertTrue(acknowledged <= set(accepted))
        observation = {"actual_socket_listen_arguments": actual_listen,
                       "connections_before_first_accept": before_accept,
                       "initial_accept_drain_after_hold": admitted,
                       "eventual_native_outcomes": results}
        print(json.dumps({"native_pending_admission": observation}), flush=True)
        return observation

    def burst(self, queue_size):
        fixture = RealTopologyFixture()
        transport = IsolatedProviderTransport(quota_mode="invalid")
        transport.start()
        entered, release = threading.Event(), threading.Event()
        accepted, requested, responded = [], [], []
        lock = threading.Lock()
        original_database = fixture.database.read_bytes()

        class Listener(OwnedRequestServer):
            daemon_threads = False
            request_queue_size = queue_size
            def get_request(self):
                if not entered.is_set():
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("accept barrier was not released")
                connection, address = super().get_request()
                with lock:
                    accepted.append(address)
                return connection, address

        class Handler(TrackedRequestHandler):
            def do_GET(self):
                with lock:
                    requested.append(self.client_address)
                return super().do_GET()
            def send_response(self, code, message=None):
                result = super().send_response(code, message)
                with lock:
                    responded.append((self.client_address, code))
                return result

        listener = Listener(("127.0.0.1", 0), Handler)
        listener.data_root = fixture.data
        thread = threading.Thread(target=listener.serve_forever)
        thread.start()
        start = threading.Barrier(13)
        def client(index):
            connection = socket.socket()
            connection.settimeout(1.5)
            address = None
            try:
                start.wait()
                connection.connect(listener.server_address)
                address = connection.getsockname()
                connection.sendall(f"GET /api/github-rate-limit?probe={index} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
                body = b""
                while chunk := connection.recv(65536):
                    body += chunk
                headers, payload = body.split(b"\r\n\r\n", 1)
                self.assertTrue(headers.startswith(b"HTTP/1.0 200 "))
                self.assertEqual(json.loads(payload), {"limited": False})
                return address, "HTTP200"
            except OSError as error:
                return address, type(error).__name__
            finally:
                connection.close()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as clients:
                pending = [clients.submit(client, index) for index in range(12)]
                start.wait()
                self.assertTrue(entered.wait(1))
                time.sleep(.25)
                release.set()
                results = [client.result(timeout=3) for client in pending]
            self.assertEqual(fixture.database.read_bytes(), original_database)
        finally:
            release.set()
            receipt = listener.quiesce()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(receipt["ordinary_quiescent"])
            self.assertEqual(receipt["streams"], 0)
            self.assertEqual(receipt["handler_errors"], [])
            transport.close()
            self.assertTrue(all(process.poll() is not None for process in transport.processes))
            self.assertEqual(transport.unexpected, [])
            fixture.close()
        completed = {address for address, outcome in results if outcome == "HTTP200"}
        self.assertEqual(completed, set(accepted))
        self.assertEqual(completed, set(requested))
        self.assertEqual(completed, {address for address, code in responded if code == 200})
        self.assertEqual(len(completed), transport.github_quota_requests)
        return results

    def test_owned_fixture_uses_production_bounded_capacity(self):
        self.assertEqual(OwnedRequestServer.request_queue_size, server._ConsoleHTTPServer.request_queue_size)
        results = self.burst(server._ConsoleHTTPServer.request_queue_size)
        self.assertEqual(len(results), 12)
        self.assertTrue(all(outcome == "HTTP200" for _, outcome in results))
        observation = self.pending_admission(server._ConsoleHTTPServer.request_queue_size)
        self.assertEqual(len(observation["initial_accept_drain_after_hold"]), 12)
        self.assertTrue(all(outcome == "ACK" for _, _, outcome in observation["eventual_native_outcomes"]))

    def test_original_five_pending_connections_refault_is_not_hidden(self):
        results = self.burst(5)
        self.assertEqual(len(results), 12)
        # Preserve actual HTTP outcomes (including Darwin's observed resets).
        # Linux can deliver all twelve after release, so refault is the actual
        # narrow kernel queue at the same preaccept boundary, not a forced RST.
        print(json.dumps({"original_five_eventual_http_outcomes": results}), flush=True)
        observation = self.pending_admission(5)
        self.assertLess(len(observation["initial_accept_drain_after_hold"]), 12)
