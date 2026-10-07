"""Compatibility imports for the integration fixture; implementation is split by concern."""
from .config import DEFAULT_CONFIG as DEFAULT, ConfigError, default_config, validate_config
from .monitor import Monitor
from .probe import classify_http, run_probe as isolated_probe
from .store import Store
from .server import create_server

probe = isolated_probe

def make_server(monitor, host="127.0.0.1", port=0, static_dir=None):
    return create_server(monitor, host=host, port=port, static_dir=static_dir)

__all__ = ["DEFAULT", "ConfigError", "Monitor", "Store", "classify_http", "probe", "isolated_probe", "validate_config", "make_server"]
