# bot_config.py

import json
import os
import threading

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "bot_config.json")

DEFAULT_CONFIG = {
    "owner_username": "prasadonly",
    "pm_enabled": True,
    "search_mode": "all",
    "authorized_groups": [],
    "auto_clean": True,
}

_lock = threading.RLock()
_config = None


def init_config():
    """Load config from disk and ensure it exists."""
    global _config
    with _lock:
        if _config is None:
            load_config()
        save_config()


def load_config():
    global _config
    if _config is not None:
        return _config
    with _lock:
        if _config is not None:
            return _config
        data = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        _config = {**DEFAULT_CONFIG, **data}
    return _config


def save_config():
    global _config
    with _lock:
        if _config is None:
            return
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(_config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print("Failed to save config:", e)


def get(key, default=None):
    return load_config().get(key, default)


def set(key, value):
    cfg = load_config()
    with _lock:
        cfg[key] = value
        save_config()


def add_group(group_id):
    cfg = load_config()
    with _lock:
        groups = cfg.get("authorized_groups", [])
        if group_id in groups:
            return False
        cfg["authorized_groups"] = groups + [group_id]
        save_config()
        return True


def remove_group(group_id):
    cfg = load_config()
    with _lock:
        groups = cfg.get("authorized_groups", [])
        if group_id not in groups:
            return False
        cfg["authorized_groups"] = [g for g in groups if g != group_id]
        save_config()
        return True


def is_group_authorized(group_id):
    return group_id in get("authorized_groups", [])


def is_pm_enabled():
    return bool(get("pm_enabled", True))
