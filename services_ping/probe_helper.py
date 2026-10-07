from __future__ import annotations
import json, sys
from .probe import _one
try:
    value=json.load(sys.stdin); print(json.dumps(_one(value["item"], value["timeout"], value.get("connectivity",False))))
except Exception as exc: print(json.dumps({"status":"down","latency_ms":None,"http_status":None,"message":str(exc)[:300],"reachable":False,"healthy":False}))
