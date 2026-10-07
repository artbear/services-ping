"""Loopback-only v1 HTTP API."""
from __future__ import annotations
import json, secrets, socket, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from .config import ConfigError, validate_config

MAX_BODY=128*1024
MAX_REQUEST_WORKERS=16
REQUEST_TIMEOUT_S=2

class BoundedHTTPServer(ThreadingHTTPServer):
    """Reject excess connections and expire stalled loopback clients."""
    def __init__(self, *args, **kwargs):
        self.worker_slots=threading.BoundedSemaphore(MAX_REQUEST_WORKERS)
        self.worker_count=0
        self.worker_lock=threading.Lock()
        super().__init__(*args, **kwargs)

    def get_request(self):
        client, address=super().get_request()
        client.settimeout(REQUEST_TIMEOUT_S)
        return client, address

    def process_request(self, request, client_address):
        if not self.worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self.worker_lock: self.worker_count += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self.worker_lock: self.worker_count -= 1
            self.worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.worker_lock: self.worker_count -= 1
            self.worker_slots.release()

    def handle_error(self, request, client_address):
        # Expected client disconnects must not print tracebacks during shutdown.
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)
class APIError(Exception):
    def __init__(self, code,message): self.code,self.message=code,message

def create_server(monitor, host="127.0.0.1", port=0, static_dir=None):
    if host != "127.0.0.1": raise ValueError("server is loopback IPv4 only")
    static=Path(static_dir or Path(__file__).with_name("static")); csrf=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        protocol_version="HTTP/1.1"; server_version="ServicesPing/1"
        def log_message(self,*args): pass
        def handle(self):
            try: super().handle()
            except (BrokenPipeError, ConnectionResetError, TimeoutError): pass
        def parse_request(self):
            if len(self.raw_requestline)>8192 or not super().parse_request(): return False
            if sum(len(k)+len(v)+4 for k,v in self.headers.items())>32768: self.send_error(431); return False
            return True
        def _host(self):
            hosts=self.headers.get_all("Host", [])
            return len(hosts) == 1 and hosts[0] in {f"127.0.0.1:{self.server.server_address[1]}",f"localhost:{self.server.server_address[1]}"}
        def _json(self,status,value,extra=None):
            data=json.dumps(value,ensure_ascii=False,separators=(",",":")).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Content-Security-Policy","default-src 'self'; frame-ancestors 'none'");
            if extra:
                for k,v in extra.items(): self.send_header(k,v)
            self.end_headers(); self.wfile.write(data)
        def _error(self,code,msg):
            # Rejecting a malformed/oversized request without draining it must not
            # let its remaining bytes be parsed as a second request.
            self.close_connection=True
            self._json(code,{"error":msg})
        def _guard(self, mutate=False):
            if not self._host(): raise APIError(403,"invalid Host")
            if mutate:
                origin=self.headers.get("Origin")
                if origin is not None and origin not in {f"http://127.0.0.1:{self.server.server_address[1]}",f"http://localhost:{self.server.server_address[1]}"}: raise APIError(403,"invalid Origin")
                if self.headers.get("X-CSRF-Token") != csrf: raise APIError(403,"invalid CSRF token")
        def _body(self):
            try: n=int(self.headers.get("Content-Length","-1"))
            except ValueError: raise APIError(400,"invalid Content-Length")
            if n<0: raise APIError(400,"Content-Length required")
            if n>MAX_BODY: raise APIError(413,"request body too large")
            try: return json.loads(self.rfile.read(n).decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (UnicodeDecodeError,ValueError): raise APIError(400,"invalid JSON")
        def do_GET(self):
            try:
                self._guard(); path=urlsplit(self.path).path; query=parse_qs(urlsplit(self.path).query)
                if path=="/api/state": return self._json(200,monitor.snapshot())
                if path=="/api/config": return self._json(200,monitor.config)
                if path=="/api/session": return self._json(200,{"csrf_token":csrf})
                if path=="/api/events": return self._json(200,{"events":monitor.store.events(_limit(query,50,1,1000))})
                if path=="/api/history":
                    target=query.get("target_id",[None])[0]
                    if not target or target not in {t["id"] for t in monitor.config["targets"]}: raise APIError(404,"unknown target")
                    return self._json(200,{"samples":monitor.store.history(target,_limit(query,60,1,2000))})
                if path=="/api/export": return self._json(200,monitor.config,{"Content-Disposition":"attachment; filename=services-ping-config.json"})
                if path in ("/","/index.html","/app.js","/style.css"):
                    name={"/":"index.html","/index.html":"index.html","/app.js":"app.js","/style.css":"style.css"}[path]; file=static/name
                    if not file.is_file(): raise APIError(404,"not found")
                    data=file.read_bytes(); ctype="text/html; charset=utf-8" if name.endswith("html") else "text/javascript; charset=utf-8" if name.endswith("js") else "text/css; charset=utf-8"
                    self.send_response(200); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(len(data))); self.send_header("Content-Security-Policy","default-src 'self'; frame-ancestors 'none'"); self.send_header("X-Content-Type-Options","nosniff"); self.end_headers(); self.wfile.write(data); return
                raise APIError(404,"not found")
            except APIError as exc: self._error(exc.code,exc.message)
            except Exception: self._error(500,"internal server error")
        def do_PUT(self):
            try:
                if urlsplit(self.path).path!="/api/config": raise APIError(404,"not found")
                self._guard(True); config=validate_config(self._body()); monitor.replace_config(config); self._json(200,config)
            except ConfigError as exc: self._error(400,str(exc))
            except APIError as exc: self._error(exc.code,exc.message)
            except RuntimeError as exc: self._error(500,str(exc))
            except Exception: self._error(500,"internal server error")
        def do_POST(self):
            try:
                path=urlsplit(self.path).path
                if path not in ("/api/check","/api/pause"): raise APIError(404,"not found")
                self._guard(True); body=self._body()
                if not isinstance(body, dict): raise APIError(400,"JSON body must be an object")
                if path=="/api/check":
                    if body != {}: raise APIError(400,"check body must be empty")
                    monitor.check_now(); return self._json(200,{"ok":True})
                if set(body)!={"paused"} or not isinstance(body["paused"],bool): raise APIError(400,"paused must be boolean")
                monitor.set_paused(body["paused"]); self._json(200,{"paused":body["paused"]})
            except APIError as exc: self._error(exc.code,exc.message)
            except Exception: self._error(500,"internal server error")
        def do_DELETE(self): self._error(405,"method not allowed")
    return BoundedHTTPServer((host,port),Handler)

def _limit(query,default,lo,hi):
    try: value=int(query.get("limit",[default])[0])
    except ValueError: raise APIError(400,"invalid limit")
    if not lo<=value<=hi: raise APIError(400,"invalid limit")
    return value
