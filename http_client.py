import json
import os

from curl_cffi import requests as _cffi

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.json")

IMPERSONATE = "chrome120"

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_cookies() -> dict:
    if not os.path.exists(COOKIE_FILE):
        return {}
    try:
        with open(COOKIE_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return {}

    jar = {}
    if isinstance(data, list):
        for c in data:
            if isinstance(c, dict) and c.get("name"):
                jar[c["name"]] = c.get("value", "")
    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str):
                jar[k] = v
            elif isinstance(v, dict) and "value" in v:
                jar[k] = v["value"]
    return jar


def has_cookies() -> bool:
    return bool(load_cookies())


def make_session():
    return _cffi.Session(impersonate=IMPERSONATE)


def get(url, *, headers=None, timeout=25, cookies=None, allow_redirects=True, stream=False):
    session = make_session()
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    jar = load_cookies() if cookies is None else cookies
    try:
        return session.get(
            url,
            headers=hdrs,
            cookies=jar,
            timeout=timeout,
            allow_redirects=allow_redirects,
            stream=stream,
        )
    finally:
        session.close()
