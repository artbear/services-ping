"""Real local API with deterministic network probes for browser acceptance."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services_ping import Monitor, Store, create_server
from services_ping.config import default_config


def main():
    mode = {"value": "online"}
    with tempfile.TemporaryDirectory(prefix="services-ping-browser-") as directory:
        store = Store(directory)
        config = default_config()
        config["settings"].update(internet_interval_s=1, timeout_s=0.2)
        config["targets"] = [{"id": "fixture-site", "name": "Контрольный сайт", "group": "Сайты", "kind": "http",
                              "address": "https://fixture.example/health", "enabled": True,
                              "interval_s": 3, "timeout_s": 0.2, "expected_statuses": [200]}]
        store.save_config(config)

        def fake_probe(item, timeout, connectivity=False):
            failed = connectivity and (mode["value"] == "offline" or
                                        (mode["value"] == "partial" and item["id"] == config["connectivity"][0]["id"]))
            return {"timestamp": time.time(), "status": "down" if failed else "up", "latency_ms": None if failed else 7.4,
                    "http_status": None if failed else (item["expected_statuses"][0] if item["expected_statuses"] else 200),
                    "message": "Fixture: соединение недоступно" if failed else "HTTP: fixture success",
                    "reachable": not failed, "healthy": not failed}

        monitor = Monitor(store, probe=fake_probe)
        server = create_server(monitor, port=0)
        clients = []
        accept = server.get_request

        def track_client():
            client, address = accept()
            clients.append(client)
            return client, address

        server.get_request = track_client
        stopped = threading.Event()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monitor.start()

        class Control(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                selected = json.loads(self.rfile.read(int(self.headers["Content-Length"]))).get("mode")
                if selected in ("online", "partial", "offline"):
                    mode["value"] = selected
                    monitor.check_now()
                elif selected == "stop_backend":
                    server.shutdown()
                    server.server_close()
                    # shutdown() closes the listener but keep-alive HTTP handlers
                    # survive in this same test process. Simulate process exit too.
                    for client in clients:
                        try:
                            client.shutdown(socket.SHUT_RDWR)
                            client.close()
                        except OSError:
                            pass
                    stopped.set()
                else:
                    self.send_error(400)
                    return
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
        threading.Thread(target=control.serve_forever, daemon=True).start()
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}",
                          "control": f"http://127.0.0.1:{control.server_address[1]}"}), flush=True)
        try:
            sys.stdin.buffer.read()
        finally:
            control.shutdown()
            control.server_close()
            if not stopped.is_set(): server.shutdown()
            server.server_close()
            monitor.close()
            store.close()


if __name__ == "__main__": main()
