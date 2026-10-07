"""Configuration defaults and deliberately strict v1 validation."""
from __future__ import annotations

import copy
import ipaddress
import math
import re
from urllib.parse import urlsplit

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

DEFAULT_CONFIG = {
    "version": 1,
    "settings": {"internet_interval_s": 3, "service_interval_s": 30,
                 "timeout_s": 2, "concurrency": 4, "history_limit": 300,
                 "notifications": False},
    "connectivity": [
        {"id": "net-google", "name": "Google", "url": "https://www.google.com/generate_204", "expected_statuses": [204]},
        {"id": "net-cloudflare", "name": "Cloudflare", "url": "https://www.cloudflare.com/cdn-cgi/trace", "expected_statuses": [200]},
        {"id": "net-yandex", "name": "Яндекс", "url": "https://ya.ru/robots.txt", "expected_statuses": [200]},
    ],
    "targets": [
        {"id": "yandex", "name": "Яндекс", "group": "Сайты", "kind": "http", "address": "https://ya.ru/", "enabled": True, "interval_s": 30, "timeout_s": 2, "expected_statuses": [200]},
        {"id": "google", "name": "Google", "group": "Сайты", "kind": "http", "address": "https://www.google.com/", "enabled": True, "interval_s": 30, "timeout_s": 2, "expected_statuses": [200]},
        {"id": "openai", "name": "OpenAI API", "group": "LLM", "kind": "http", "address": "https://api.openai.com/", "enabled": True, "interval_s": 30, "timeout_s": 2, "expected_statuses": []},
        {"id": "anthropic", "name": "Anthropic API", "group": "LLM", "kind": "http", "address": "https://api.anthropic.com/", "enabled": True, "interval_s": 30, "timeout_s": 2, "expected_statuses": []},
        {"id": "gemini", "name": "Gemini API", "group": "LLM", "kind": "http", "address": "https://generativelanguage.googleapis.com/", "enabled": True, "interval_s": 30, "timeout_s": 2, "expected_statuses": []},
        {"id": "openrouter", "name": "OpenRouter", "group": "LLM", "kind": "http", "address": "https://openrouter.ai/api/v1/models", "enabled": True, "interval_s": 30, "timeout_s": 2, "expected_statuses": []},
    ],
}

class ConfigError(ValueError): pass

def default_config(): return copy.deepcopy(DEFAULT_CONFIG)

def _number(value, name, lo, hi, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < lo or value > hi or (integer and int(value) != value):
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return int(value) if integer else value

def _id(value, label="id"):
    if not isinstance(value, str) or not ID.fullmatch(value): raise ConfigError(f"invalid {label}")

def _text(value, label, limit):
    if not isinstance(value, str) or not value or len(value) > limit: raise ConfigError(f"invalid {label}")

def _statuses(value):
    if not isinstance(value, list) or any(isinstance(x, bool) or not isinstance(x, int) or x < 100 or x > 599 for x in value):
        raise ConfigError("invalid expected_statuses")

def _http_url(value):
    _text(value, "url", 2048)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if parsed.scheme not in ("http", "https") or not parsed.hostname or "@" in parsed.netloc:
            raise ValueError
        if port is not None and not 1 <= port <= 65535: raise ValueError
        if any(char.isspace() or ord(char) < 32 for char in value): raise ValueError
        _host(parsed.hostname)
    except ValueError as exc:
        raise ConfigError("HTTP URL must be http(s) with a valid host/port and no credentials") from exc

def _host(value):
    if not value or any(char.isspace() for char in value): raise ConfigError("invalid host")
    try:
        ipaddress.ip_address(value)
        return
    except ValueError:
        pass
    try: encoded=value.encode("idna").decode("ascii")
    except UnicodeError as exc: raise ConfigError("invalid host") from exc
    if len(encoded)>253 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", encoded):
        raise ConfigError("invalid host")

def _tcp(value):
    _text(value, "address", 2048)
    if value.startswith("["):
        match=re.fullmatch(r"\[([^\]]+)\]:([0-9]+)", value)
        if not match: raise ConfigError("TCP IPv6 address must be [IPv6]:port")
        host, port=match.groups()
        try: ipaddress.IPv6Address(host)
        except ValueError as exc: raise ConfigError("invalid IPv6 host") from exc
    else:
        match=re.fullmatch(r"([^:\s]+):([0-9]+)", value)
        if not match: raise ConfigError("TCP address must be host:port")
        host, port=match.groups()
        _host(host)
    if not 1 <= int(port) <= 65535: raise ConfigError("invalid TCP port")

def validate_config(value):
    if not isinstance(value, dict) or set(value) != {"version", "settings", "connectivity", "targets"} or value["version"] != 1:
        raise ConfigError("unsupported or malformed config")
    settings = value["settings"]
    if not isinstance(settings, dict) or set(settings) != {"internet_interval_s", "service_interval_s", "timeout_s", "concurrency", "history_limit", "notifications"}:
        raise ConfigError("invalid settings")
    _number(settings["internet_interval_s"], "internet_interval_s", 1, 60, True)
    _number(settings["service_interval_s"], "service_interval_s", 3, 3600, True)
    _number(settings["timeout_s"], "timeout_s", .2, 15)
    _number(settings["concurrency"], "concurrency", 1, 16, True)
    _number(settings["history_limit"], "history_limit", 20, 2000, True)
    if not isinstance(settings["notifications"], bool): raise ConfigError("notifications must be boolean")
    refs, targets = value["connectivity"], value["targets"]
    if not isinstance(refs, list) or not 2 <= len(refs) <= 10 or not isinstance(targets, list) or len(targets) > 100: raise ConfigError("invalid number of references or targets")
    ids = set()
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"id", "name", "url", "expected_statuses"}: raise ConfigError("invalid connectivity reference")
        _id(ref["id"]); _text(ref["name"], "name", 100); _http_url(ref["url"]); _statuses(ref["expected_statuses"])
        if urlsplit(ref["url"]).scheme != "https": raise ConfigError("connectivity URLs must use HTTPS")
        if ref["id"] in ids: raise ConfigError("duplicate id")
        ids.add(ref["id"])
    for target in targets:
        required = {"id", "name", "group", "kind", "address", "enabled", "interval_s", "timeout_s", "expected_statuses"}
        if not isinstance(target, dict) or set(target) != required: raise ConfigError("invalid target")
        _id(target["id"]); _text(target["name"], "name", 100)
        if not isinstance(target["group"], str) or len(target["group"]) > 100: raise ConfigError("invalid group")
        if target["id"] in ids: raise ConfigError("duplicate id")
        ids.add(target["id"])
        if target["kind"] not in ("http", "tcp", "icmp") or not isinstance(target["enabled"], bool): raise ConfigError("invalid target type")
        _number(target["interval_s"], "interval_s", 3, 3600, True); _number(target["timeout_s"], "timeout_s", .2, 15); _statuses(target["expected_statuses"])
        if target["kind"] == "http": _http_url(target["address"])
        elif target["kind"] == "tcp": _tcp(target["address"])
        else:
            _text(target["address"], "address", 2048)
            if target["address"].startswith("-") or any(ch.isspace() for ch in target["address"]):
                raise ConfigError("invalid ICMP address")
    return copy.deepcopy(value)
