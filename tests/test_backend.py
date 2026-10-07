import copy
import tempfile
import threading
import unittest
from unittest.mock import patch

from services_ping import Monitor, Store
from services_ping.config import ConfigError, default_config, validate_config
from services_ping.probe import classify_http, run_probe


def result(code=200, status="up"):
    return {"status": status, "latency_ms": 1.2, "http_status": code,
            "message": "fixture", "reachable": code is not None,
            "healthy": status == "up"}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.config = default_config()
        self.config["connectivity"] = self.config["connectivity"][:2]
        self.store.save_config(self.config)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_validation_rejects_credentials_duplicate_nan_and_icmp_option(self):
        cases = [
            lambda c: c["connectivity"][0].update(url="https://u:p@example.test/"),
            lambda c: c["connectivity"][1].update(id=c["connectivity"][0]["id"]),
            lambda c: c["settings"].update(timeout_s=float("nan")),
            lambda c: c["targets"][0].update(kind="icmp", address="--help"),
        ]
        for mutate in cases:
            c = copy.deepcopy(self.config); mutate(c)
            with self.assertRaises(ConfigError): validate_config(c)

    def test_http_status_priority_and_deadline(self):
        self.assertEqual(classify_http(401, [401])[0], "blocked")
        self.assertEqual(classify_http(429, [429])[0], "rate_limited")
        self.assertEqual(classify_http(503, [503])[0], "down")
        self.assertEqual(classify_http(204, [200])[0], "down")
        import subprocess
        with patch("services_ping.probe.subprocess.run", side_effect=subprocess.TimeoutExpired("probe", 1)):
            value = run_probe({"kind":"http", "address":"http://fixture/", "expected_statuses":[]}, .2)
        self.assertEqual((value["status"], value["reachable"]), ("down", False))

    def test_online_partial_offline_recovery_and_generation_protection(self):
        monitor = Monitor(self.store, probe=lambda *args, **kwargs: result())
        refs = self.config["connectivity"]
        monitor.apply_connectivity(refs, [result(204), result(200)])
        self.assertEqual(monitor.snapshot()["internet"]["status"], "online")
        epoch = monitor.network_generation
        self.assertTrue(monitor.apply_target("yandex", result(), network_generation=epoch))
        old_time = monitor.snapshot()["targets"][0]["checked_at"]
        monitor.apply_connectivity(refs, [result(302,"down"), result(200)])
        self.assertEqual(monitor.snapshot()["internet"]["status"], "partial")
        monitor.apply_connectivity(refs, [result(None,"down"), result(None,"down")])
        self.assertEqual(monitor.snapshot()["internet"]["status"], "offline")
        self.assertEqual(monitor.snapshot()["targets"][0]["status"], "no_internet")
        monitor.set_paused(True)
        monitor.apply_connectivity(refs, [result(204), result(200)])
        self.assertFalse(monitor.apply_target("yandex", result(500,"down"), network_generation=epoch))
        # Recovery intentionally schedules a fresh real probe; the old-generation
        # completion above must still be rejected rather than overwriting it.
        self.assertNotEqual(monitor.snapshot()["targets"][0]["status"], "down")
        monitor.close()

    def test_target_concurrency_and_retention_are_bounded(self):
        c = copy.deepcopy(self.config); c["settings"]["concurrency"] = 1
        c["targets"].append({"id":"two", "name":"two", "group":"", "kind":"http", "address":"http://two/", "enabled":True, "interval_s":3, "timeout_s":1, "expected_statuses":[]})
        self.store.save_config(c); entered = threading.Event(); release = threading.Event()
        def slow(*args, **kwargs): entered.set(); release.wait(1); return result()
        monitor = Monitor(self.store, probe=slow)
        monitor.apply_connectivity(c["connectivity"], [result(204), result(200)])
        self.assertEqual(monitor.run_targets(), 1)
        self.assertTrue(entered.wait(1)); self.assertEqual(monitor.run_targets(), 0)
        release.set(); monitor.close()
        for n in range(24): self.store.sample("x", {"timestamp":n,"status":"up","latency_ms":n,"http_status":200,"message":"ok"}, 20)
        self.assertEqual(len(self.store.history("x", 100)), 20)


if __name__ == "__main__":
    unittest.main()
