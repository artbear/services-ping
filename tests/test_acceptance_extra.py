"""Acceptance checks using the real loopback API and deterministic network probes."""
import copy
import http.client
import json
import math
import queue
import subprocess
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from services_ping import Monitor, Store, create_server
from services_ping.config import ConfigError, default_config, validate_config
from services_ping.probe import _one, run_probe
from services_ping.server import MAX_REQUEST_WORKERS

NO_BODY = object()

class LocalAPIAcceptance(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.monitor = Monitor(self.store, probe=lambda item, timeout, connectivity=False: {
            "status": "up", "latency_ms": 1.0, "http_status": item["expected_statuses"][0] if item["expected_statuses"] else 200,
            "message": "fixture", "reachable": True, "healthy": True,
        })
        try:
            self.server = create_server(self.monitor, port=0)
        except PermissionError:
            self.monitor.close()
            self.store.close()
            self.directory.cleanup()
            self.skipTest("execution sandbox does not permit loopback sockets")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.token = self.request("GET", "/api/session")[1]["csrf_token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.monitor.close()
        self.store.close()
        self.directory.cleanup()

    def request(self, method, path, body=NO_BODY, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        data = None if body is NO_BODY else json.dumps(body).encode()
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=data, headers=request_headers)
        response = connection.getresponse()
        result = response.read()
        connection.close()
        return response.status, json.loads(result) if result else None

    def mutation_headers(self):
        return {"Origin": self.origin, "X-CSRF-Token": self.token}

    def test_session_refuses_rebinding_host(self):
        code, _ = self.request("GET", "/api/session", headers={"Host": f"attacker.example:{self.port}"})
        self.assertEqual(code, 403)

    def test_duplicate_host_is_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.putrequest("GET", "/api/session", skip_host=True)
        connection.putheader("Host", f"127.0.0.1:{self.port}")
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 403)

    def test_mutations_require_csrf_and_acceptable_origin(self):
        self.assertEqual(self.request("POST", "/api/check", {})[0], 403)
        for origin in ("null", "https://attacker.example", self.origin + ".attacker.example"):
            code, _ = self.request("POST", "/api/check", {}, {"Origin": origin, "X-CSRF-Token": self.token})
            self.assertEqual(code, 403)
        self.assertEqual(self.request("POST", "/api/check", {}, self.mutation_headers())[0], 200)

    def test_invalid_import_preserves_saved_and_runtime_config(self):
        before = self.monitor.snapshot()["config_revision"]
        config = self.request("GET", "/api/config")[1]
        invalid = copy.deepcopy(config)
        invalid["version"] = 99
        self.assertEqual(self.request("PUT", "/api/config", invalid, self.mutation_headers())[0], 400)
        self.assertEqual(self.monitor.snapshot()["config_revision"], before)
        self.assertEqual(self.store.config(), config)

    def test_config_persists_and_export_excludes_runtime(self):
        config = self.request("GET", "/api/config")[1]
        config["settings"]["history_limit"] = 25
        self.assertEqual(self.request("PUT", "/api/config", config, self.mutation_headers())[0], 200)
        exported = self.request("GET", "/api/export")[1]
        self.assertEqual(exported, config)
        self.assertEqual(self.store.config(), config)
        self.assertNotIn("csrf_token", exported)
        self.assertNotIn("internet", exported)

    def test_save_failure_preserves_runtime_and_saved_config(self):
        before = self.request("GET", "/api/config")[1]
        revision = self.monitor.snapshot()["config_revision"]
        changed = copy.deepcopy(before)
        changed["settings"]["history_limit"] = 40
        with patch.object(self.store, "save_config", side_effect=RuntimeError("fixture: database locked")):
            code, _ = self.request("PUT", "/api/config", changed, self.mutation_headers())
        self.assertEqual(code, 500)
        self.assertEqual(self.monitor.snapshot()["config_revision"], revision)
        self.assertEqual(self.request("GET", "/api/config")[1], before)
        self.assertEqual(self.store.config(), before)

    def test_cleanup_failure_rolls_back_config_and_history(self):
        before = self.store.config()
        revision = self.monitor.snapshot()["config_revision"]
        sample = {"timestamp": 1, "status": "up", "latency_ms": 2, "http_status": 200, "message": "kept"}
        self.store.sample("yandex", sample, 300)
        changed = copy.deepcopy(before)
        changed["targets"] = []
        cleanup = self.store.delete_targets_not_in

        def broken_cleanup(ids):
            cleanup(ids)
            raise RuntimeError("fixture failure after SQL cleanup")

        with patch.object(self.store, "delete_targets_not_in", side_effect=broken_cleanup):
            self.assertEqual(self.request("PUT", "/api/config", changed, self.mutation_headers())[0], 500)
        self.assertEqual(self.store.config(), before)
        self.assertEqual(self.monitor.snapshot()["config_revision"], revision)
        self.assertEqual(self.store.history("yandex", 10), [sample])

    def test_reducing_retention_prunes_disabled_target_immediately(self):
        config = self.store.config()
        config["targets"][0]["enabled"] = False
        for index in range(30):
            self.store.sample("yandex", {"timestamp": index, "status": "up", "latency_ms": 2, "http_status": 200, "message": "fixture"}, 300)
        config["settings"]["history_limit"] = 20
        self.assertEqual(self.request("PUT", "/api/config", config, self.mutation_headers())[0], 200)
        self.assertEqual(len(self.store.history("yandex", 100)), 20)

    def test_retention_failure_rolls_back_config_cleanup_and_samples(self):
        before = self.store.config()
        snapshot = self.monitor.snapshot()
        for index in range(30):
            self.store.sample("yandex", {"timestamp": index, "status": "up", "latency_ms": 2, "http_status": 200, "message": "fixture"}, 300)
        self.store.sample("orphan", {"timestamp": 0, "status": "up", "message": "fixture"}, 300)
        history = self.store.history("yandex", 100)
        orphan = self.store.history("orphan", 100)
        changed = copy.deepcopy(before)
        changed["settings"]["history_limit"] = 20
        connection = self.store.db

        class FailAfterRetention:
            def __getattr__(self, name): return getattr(connection, name)
            def __enter__(self): return connection.__enter__()
            def __exit__(self, *args): return connection.__exit__(*args)
            def execute(self, statement, parameters=()):
                cursor = connection.execute(statement, parameters)
                if statement.startswith("DELETE FROM samples WHERE target_id=?"):
                    raise sqlite3.OperationalError("fixture: failure after retention delete")
                return cursor

        with patch.object(self.store, "db", FailAfterRetention()):
            self.assertEqual(self.request("PUT", "/api/config", changed, self.mutation_headers())[0], 500)
        self.assertEqual(self.store.config(), before)
        self.assertEqual(self.store.history("yandex", 100), history)
        self.assertEqual(self.store.history("orphan", 100), orphan)
        after = self.monitor.snapshot()
        for key in ("config_revision", "paused", "internet", "targets", "settings"):
            self.assertEqual(after[key], snapshot[key])

    def test_stalled_http_clients_cannot_exceed_worker_cap(self):
        clients = []
        try:
            for _ in range(MAX_REQUEST_WORKERS + 8):
                client = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                client.sendall(b"GET /api/state HTTP/1.1\r")
                clients.append(client)
            time.sleep(0.1)
            self.assertLessEqual(self.server.worker_count, MAX_REQUEST_WORKERS)
            deadline = time.monotonic() + 4
            completed = False
            while time.monotonic() < deadline:
                try:
                    completed = self.request("GET", "/api/state")[0] == 200
                    if completed: break
                except (OSError, http.client.HTTPException):
                    time.sleep(0.1)
            self.assertTrue(completed, "stalled clients expire and API becomes available")
        finally:
            for client in clients: client.close()

    def test_oversized_mutation_is_rejected(self):
        code, _ = self.request("POST", "/api/check", {"data": "x" * (128 * 1024)}, self.mutation_headers())
        self.assertEqual(code, 413)

    def test_null_and_nonobject_pause_payload_is_bad_request(self):
        for value in (None, [1], "paused", 1, False):
            code, _ = self.request("POST", "/api/pause", value, self.mutation_headers())
            self.assertEqual(code, 400)

    def test_traversal_cannot_read_source_or_database(self):
        for path in ("/../config.py", "/%2e%2e/config.py", "/static/../../services-ping.sqlite3", "/%2f..%2fconfig.py"):
            self.assertEqual(self.request("GET", path)[0], 404)


class ConfigurationAcceptance(unittest.TestCase):
    def test_invalid_http_and_tcp_addresses_cannot_be_imported(self):
        for kind, address in (("http", "https://example.test:notaport/"), ("http", "https://example.test:0/"),
                              ("tcp", "[]:80"), ("tcp", "host name:80"), ("tcp", "::1:80")):
            config = default_config()
            config["targets"][0].update(kind=kind, address=address)
            with self.subTest(address=address), self.assertRaises(ConfigError):
                validate_config(config)
    def test_nonfinite_numbers_are_invalid(self):
        for value in (math.nan, math.inf, -math.inf):
            config = default_config()
            config["settings"]["timeout_s"] = value
            with self.assertRaises(ConfigError):
                validate_config(config)

    def test_option_like_icmp_address_is_invalid(self):
        config = default_config()
        config["targets"] = [{
            "id": "bad-ping", "name": "bad ping", "group": "", "kind": "icmp", "address": "--help",
            "enabled": True, "interval_s": 3, "timeout_s": 1, "expected_statuses": [],
        }]
        with self.assertRaises(ConfigError):
            validate_config(config)


class ProbeAcceptance(unittest.TestCase):
    def setUp(self):
        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/slow":
                    time.sleep(1)
                    code = 200
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/status/200")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                else:
                    code = int(self.path.rsplit("/", 1)[-1])
                try:
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        except PermissionError:
            self.skipTest("execution sandbox does not permit loopback sockets")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def item(self, path):
        return {"kind": "http", "address": self.url + path, "expected_statuses": [200]}

    def test_real_http_helper_statuses_and_reachability(self):
        for code, expected in ((200, "up"), (204, "down"), (401, "blocked"),
                               (403, "blocked"), (404, "down"), (429, "rate_limited"), (503, "down")):
            with self.subTest(code=code):
                result = run_probe(self.item(f"/status/{code}"), 2)
                self.assertEqual(result["status"], expected)
                self.assertTrue(result["reachable"])
                self.assertEqual(result["http_status"], code)

    def test_connectivity_does_not_follow_redirect(self):
        self.assertEqual(run_probe(self.item("/redirect"), 2)["status"], "up")
        value = run_probe(self.item("/redirect"), 2, connectivity=True)
        self.assertEqual(value["http_status"], 302)
        self.assertTrue(value["reachable"])

    def test_tcp_success_and_http_total_deadline(self):
        tcp = run_probe({"kind": "tcp", "address": f"127.0.0.1:{self.server.server_address[1]}"}, 2)
        self.assertEqual(tcp["status"], "up")
        started = time.monotonic()
        value = run_probe(self.item("/slow"), 0.2)
        self.assertEqual(value["status"], "down")
        self.assertLess(time.monotonic() - started, 2)

    def test_helper_deadline_terminates_blocking_resolver_equivalent(self):
        # Exercise the real process timeout instead of merely raising TimeoutExpired.
        # A sleeping helper represents an uninterruptible system resolver inside the helper.
        real_run = subprocess.run

        def blocked_helper(*args, **kwargs):
            return real_run([sys.executable, "-c", "import time; time.sleep(10)"],
                            timeout=kwargs["timeout"], capture_output=True, text=True)

        started = time.monotonic()
        with patch("services_ping.probe.subprocess.run", side_effect=blocked_helper):
            value = run_probe(self.item("/status/200"), 0.2)
        self.assertEqual(value["status"], "down")
        self.assertFalse(value["reachable"])
        self.assertLess(time.monotonic() - started, 2)

    def test_icmp_platform_timeout_and_unknown_rtt(self):
        item = {"kind": "icmp", "address": "127.0.0.1"}
        for platform, flag, budget in (("linux", "-W", "2"), ("darwin", "-W", "2000"), ("win32", "-w", "2000")):
            with self.subTest(platform=platform), patch("services_ping.probe.sys.platform", platform), \
                 patch("services_ping.probe.shutil.which", return_value="ping"), \
                 patch("services_ping.probe.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "Reply: time=1,25 ms")) as run:
                value = _one(item, 2)
                args = run.call_args.args[0]
                self.assertEqual(args[args.index(flag) + 1], budget)
                self.assertEqual(value["latency_ms"], 1.25)
        with patch("services_ping.probe.shutil.which", return_value="ping"), \
             patch("services_ping.probe.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "Unrecognized successful reply")):
            value = _one(item, 2)
        self.assertEqual(value["status"], "unsupported")
        self.assertTrue(value["reachable"])
        self.assertIsNone(value["healthy"])


class SchedulerAcceptance(unittest.TestCase):
    def test_automatic_work_rechecks_pause_at_submission_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            monitor = Monitor(store, probe=lambda *args, **kwargs: self.fail("paused automatic probe"))
            try:
                monitor.set_paused(True)
                monitor.internet["status"] = "online"
                self.assertEqual(monitor.run_targets(due_only=True), 0)
                self.assertFalse(monitor.run_connectivity(automatic=True))
                # The scheduler may have read 'not paused' before set_paused.
                # A completed connectivity cycle must also respect the new pause.
                monitor.internet["status"] = "offline"
                refs = monitor.config["connectivity"]
                values = [{"status": "up", "http_status": ref["expected_statuses"][0]} for ref in refs]
                monitor.apply_connectivity(refs, values)
                self.assertEqual(monitor.inflight, set())
                self.assertTrue(monitor.paused)
            finally:
                monitor.close()
                store.close()

    def test_notification_transitions_and_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            config = default_config()
            config["settings"]["notifications"] = True
            store.save_config(config)
            notifications = queue.Queue()
            monitor = Monitor(store, notifier=lambda status, message: notifications.put(status))
            monitor.set_paused(True)
            refs = monitor.config["connectivity"]
            def transition(status, timestamp):
                codes = [ref["expected_statuses"][0] for ref in refs]
                if status == "offline": codes = [None] * len(refs)
                if status == "partial": codes[0] = 302
                with patch("services_ping.monitor.time.time", return_value=timestamp):
                    monitor.apply_connectivity(refs, [{"http_status": code} for code in codes])
            try:
                transition("online", 100)
                self.assertTrue(notifications.empty(), "initial online is not recovery")
                transition("offline", 101)
                self.assertEqual(notifications.get(timeout=1), "offline")
                transition("partial", 102)
                self.assertTrue(notifications.empty(), "cooldown suppresses rapid repeat")
                transition("online", 115)
                self.assertTrue(notifications.empty(), "partial to online is not recovery")
                transition("offline", 116)
                self.assertEqual(notifications.get(timeout=1), "offline")
                transition("partial", 127)
                self.assertEqual(notifications.get(timeout=1), "partial")
                transition("offline", 138)
                self.assertEqual(notifications.get(timeout=1), "offline")
                transition("online", 149)
                self.assertEqual(notifications.get(timeout=1), "online")
            finally:
                monitor.close()
                store.close()

    def test_manual_cycle_in_pause_reaches_every_target_with_one_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            config = default_config()
            config["settings"]["concurrency"] = 1
            config["targets"] = config["targets"][:3]
            store.save_config(config)
            visited = set()

            def fake(item, timeout, connectivity=False):
                if not connectivity:
                    visited.add(item["id"])
                return {"status": "up", "latency_ms": 1, "http_status": item["expected_statuses"][0] if item["expected_statuses"] else 200,
                        "message": "fixture", "reachable": True, "healthy": True}

            monitor = Monitor(store, probe=fake)
            try:
                monitor.set_paused(True)
                monitor.check_now()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and len(visited) < len(config["targets"]):
                    time.sleep(0.02)
                self.assertEqual(visited, {target["id"] for target in config["targets"]})
                self.assertTrue(monitor.snapshot()["paused"])
            finally:
                monitor.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
