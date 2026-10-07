"""Isolated probes: parent timeout kills DNS/TLS/redirect work, not just sockets."""
from __future__ import annotations
import json, math, os, re, shutil, socket, subprocess, sys, time
from urllib.error import HTTPError, URLError
from urllib.request import BaseHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

def classify_http(code, expected):
    if code in (401,403): return "blocked", True, False, f"HTTP {code}"
    if code == 429: return "rate_limited", True, False, "HTTP 429"
    if code >= 500: return "down", True, False, f"HTTP {code}"
    if expected: return ("up",True,True,f"HTTP {code}") if code in expected else ("down",True,False,f"unexpected HTTP {code}")
    return "reachable", True, None, f"HTTP {code}"

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args): return None

class LimitedRedirect(HTTPRedirectHandler):
    max_redirections = 5

def _one(item, timeout, connectivity=False):
    started=time.monotonic(); kind=item.get("kind", "http"); address=item.get("address", item.get("url")); expected=item.get("expected_statuses", [])
    try:
        if kind == "http":
            handlers=[ProxyHandler({}), NoRedirect() if connectivity else LimitedRedirect()]
            opener=build_opener(*handlers)
            try:
                response=opener.open(Request(address, method="GET", headers={"User-Agent":"Services-Ping/1"}), timeout=timeout)
                code=response.status; response.close()
            except HTTPError as exc: code=exc.code; exc.close()
            status, reachable, healthy, message=classify_http(code,expected)
            return {"status":status,"latency_ms":round((time.monotonic()-started)*1000,1),"http_status":code,"message":message,"reachable":reachable,"healthy":healthy}
        if kind == "tcp":
            host, _, port=address.rpartition(":"); host=host.strip("[]")
            with socket.create_connection((host,int(port)), timeout=timeout): pass
            return {"status":"up","latency_ms":round((time.monotonic()-started)*1000,1),"http_status":None,"message":"TCP connection succeeded","reachable":True,"healthy":True}
        if kind == "icmp":
            ping=shutil.which("ping")
            if not ping: return {"status":"unsupported","latency_ms":None,"http_status":None,"message":"ping is unavailable","reachable":False,"healthy":None}
            if sys.platform == "win32":
                args=[ping,"-n","1","-w",str(math.ceil(timeout*1000)),address]
            elif sys.platform == "darwin":
                args=[ping,"-n","-c","1","-W",str(math.ceil(timeout*1000)),address]
            else:
                args=[ping,"-n","-c","1","-W",str(max(1,math.ceil(timeout))),address]
            env=dict(os.environ, LC_ALL="C", LANG="C")
            done=subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", env=env, timeout=timeout, check=False)
            match=re.search(r"(?:time|время|temps|zeit)\s*[=<]\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:ms|мс)",done.stdout,re.I)
            if done.returncode == 0:
                if match: return {"status":"up","latency_ms":float(match.group(1).replace(",", ".")),"http_status":None,"message":"ICMP reply","reachable":True,"healthy":True}
                return {"status":"unsupported","latency_ms":None,"http_status":None,"message":"ICMP reply received; RTT format unsupported","reachable":True,"healthy":None}
            return {"status":"down","latency_ms":None,"http_status":None,"message":"no ICMP reply","reachable":False,"healthy":False}
        return {"status":"unsupported","latency_ms":None,"http_status":None,"message":"unsupported probe","reachable":False,"healthy":None}
    except Exception as exc:
        return {"status":"down","latency_ms":None,"http_status":None,"message":str(exc)[:300],"reachable":False,"healthy":False}

def run_probe(item, timeout, connectivity=False):
    """Run one helper; deadline includes process, DNS, TLS and redirects."""
    payload=json.dumps({"item":item,"timeout":timeout,"connectivity":connectivity})
    try:
        p=subprocess.run([sys.executable,"-m","services_ping.probe_helper"],input=payload,text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=timeout+.25,env={k:v for k,v in os.environ.items() if k.lower() not in {"http_proxy","https_proxy","all_proxy","no_proxy"}})
        if p.returncode == 0: return json.loads(p.stdout)
    except subprocess.TimeoutExpired: pass
    return {"status":"down","latency_ms":None,"http_status":None,"message":"probe deadline exceeded","reachable":False,"healthy":False}
