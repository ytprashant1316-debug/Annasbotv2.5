# domain.py

import re
import threading
from urllib.parse import urlparse

import http_client

SHADOWLIBRARIES_URL = "https://shadowlibraries.github.io/DirectDownloads/AnnasArchive/"
LIBGEN_SHADOWLIBRARIES_URL = "https://shadowlibraries.github.io/DirectDownloads/libgen/"

FALLBACK_DOMAINS = [
    "annas-archive.gl",
    "annas-archive.pk",
    "annas-archive.gd",
    "annas-archive.li",
    "annas-archive.ch",
    "annas-archive.gs",
    "annas-archive.se",
    "annas-archive.org",
]

FALLBACK_LIBGEN_MIRRORS = [
    "libgen.li",
    "libgen.is",
    "libgen.vg",
    "libgen.la",
]

_DOMAIN_RE = re.compile(r"https://(annas-archive\.[a-z]+)/\?r=")
_LIBGEN_MIRROR_RE = re.compile(r"https?://(libgen\.[a-z]+)/", re.IGNORECASE)

_lock = threading.RLock()
_active_domain = None
_cached_order = None
_cached_libgen_mirrors = None


def get_domain_order() -> list[str]:
    """
    Ordered list of candidate domains: the active domain from the
    Shadow Libraries page first, then its mirrors, then static fallbacks.
    """
    global _cached_order

    if _cached_order is not None:
        return _cached_order

    with _lock:
        if _cached_order is not None:
            return _cached_order

        order: list[str] = []
        fetched = _fetch_domains_from_shadowlibraries()
        if fetched:
            order = fetched

        for domain in FALLBACK_DOMAINS:
            if domain not in order:
                order.append(domain)

        _cached_order = order

    return _cached_order


def get_base_url() -> str:
    """
    Return the currently active Anna's Archive base URL.
    Auto-switches to a working domain if none is selected yet.
    """
    global _active_domain

    if _active_domain:
        return f"https://{_active_domain}"

    with _lock:
        if _active_domain:
            return f"https://{_active_domain}"

        _active_domain = _find_working_domain()

    if not _active_domain:
        raise RuntimeError("No reachable Anna's Archive domain found.")

    return f"https://{_active_domain}"


def set_active_domain(domain: str) -> None:
    """Manually override the active domain (e.g. after a retry)."""
    global _active_domain
    with _lock:
        _active_domain = domain


def get_libgen_mirrors() -> list[str]:
    """
    Ordered list of Libgen mirrors: primary from the Shadow Libraries
    page first, then its mirrors, then static fallbacks.
    """
    global _cached_libgen_mirrors

    if _cached_libgen_mirrors is not None:
        return _cached_libgen_mirrors

    with _lock:
        if _cached_libgen_mirrors is not None:
            return _cached_libgen_mirrors

        order: list[str] = []
        fetched = _fetch_libgen_mirrors()
        if fetched:
            order = fetched

        for mirror in FALLBACK_LIBGEN_MIRRORS:
            if mirror not in order:
                order.append(mirror)

        _cached_libgen_mirrors = order

    return _cached_libgen_mirrors


def _fetch_libgen_mirrors() -> list[str] | None:
    """Fetch the Libgen mirror list from Shadow Libraries."""
    try:
        r = http_client.get(LIBGEN_SHADOWLIBRARIES_URL, timeout=15, cookies={})
        if r.status_code != 200:
            return None

        mirrors = _LIBGEN_MIRROR_RE.findall(r.text)

        unique: list[str] = []
        for mirror in mirrors:
            if mirror not in unique:
                unique.append(mirror)

        return unique or None
    except Exception:
        return None


def _fetch_domains_from_shadowlibraries() -> list[str] | None:
    """Fetch the active Anna's Archive domain + mirrors from Shadow Libraries."""
    try:
        r = http_client.get(SHADOWLIBRARIES_URL, timeout=15, cookies={})
        if r.status_code != 200:
            return None

        domains = _DOMAIN_RE.findall(r.text)

        unique: list[str] = []
        for domain in domains:
            if domain not in unique:
                unique.append(domain)

        return unique or None
    except Exception:
        return None


SEARCH_PAGE_MARKER = "js-aarecord-list-outer"


def _find_working_domain() -> str | None:
    query_string = "index=&page=1&display=&q=test&sort=&ext=pdf&ext=epub"
    cookies = http_client.load_cookies()

    for domain in get_domain_order():
        try:
            r = http_client.get(
                f"https://{domain}/search?{query_string}",
                timeout=15,
                cookies=cookies,
                allow_redirects=True,
            )
            if r.status_code == 200 and SEARCH_PAGE_MARKER in r.text:
                return domain
        except Exception:
            continue

    return None
