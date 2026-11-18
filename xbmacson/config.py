"""~/.xbmacson.json, same format as earlier versions."""

import json
import os

SCREENSHOT_DIR = os.path.expanduser("~/Desktop/xbox_screenshots")


def config_path():
    return os.environ.get("XBMACSON_CONFIG") or os.path.expanduser("~/.xbmacson.json")


DEFAULTS = {
    "default_ip": "",
    "auto_reconnect": True,
    "default_logging": False,
    "log_dir": "",
    "screenshot_dir": SCREENSHOT_DIR,
    "last_connected": [],
}


def load_config(path=None):
    cfg = dict(DEFAULTS)
    cfg["last_connected"] = []
    try:
        with open(path or config_path(), "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg, path=None):
    path = path or config_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def remember(cfg, host, name="", keep=5):
    """Put ``host`` at the top of last_connected."""
    last = cfg.get("last_connected", [])
    last = [e for e in last if (e[0] if isinstance(e, list) else e) != host]
    last.insert(0, [host, name])
    cfg["last_connected"] = last[:keep]
    return cfg
