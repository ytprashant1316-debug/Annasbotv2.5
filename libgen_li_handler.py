# libgen_li_handler.py

import os
import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse

from domain import get_libgen_mirrors

LIBGEN_BASE = "https://libgen.li"


def get_libgen_li_direct_link(ads_url: str) -> str | None:
    """
    Extract direct GET download link from a libgen ads page.
    Tries the given URL first, then falls back across mirror hosts.
    Works with structure: <a><h2>GET</h2></a>
    """

    scraper = cloudscraper.create_scraper()

    for url in _candidate_ads_urls(ads_url):
        try:
            r = scraper.get(url, timeout=30)
            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, "html.parser")

            # Find <h2>GET</h2>
            for h2 in soup.find_all("h2"):
                if h2.get_text(strip=True).upper() == "GET":
                    a = h2.find_parent("a", href=True)
                    if a:
                        return urljoin(url, a["href"])

            # Fallback: find any <a> with href containing get.php and text containing GET
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "get.php" in href.lower():
                    text = a.get_text(strip=True).upper()
                    if "GET" in text:
                        return urljoin(url, href)

        except Exception:
            continue

    return None


def get_libgen_li_download_url(md5: str) -> str:
    """
    Resolve the final download URL for an MD5.
    Prefers a direct GET link, falls back to the ads page.
    """
    md5 = md5.lower()

    for mirror in get_libgen_mirrors():
        ads_url = f"https://{mirror}/ads.php?md5={md5}"
        direct = get_libgen_li_direct_link(ads_url)
        if direct:
            return direct

    return f"https://{get_libgen_mirrors()[0]}/ads.php?md5={md5}"


def download_libgen_li_book(
    md5: str,
    dest_dir: str = "downloads",
    timeout: int = 120,
) -> str:
    """
    Download a book from Libgen by MD5 to a local file.

    Tries each mirror's direct link (falling back to its ads page)
    until one succeeds. Returns the local file path, or raises.
    """
    md5 = md5.lower()

    os.makedirs(dest_dir, exist_ok=True)

    last_error: Exception | None = None

    for url in _candidate_download_urls(md5):
        try:
            return _download_from_url(url, md5, dest_dir, timeout)
        except Exception as e:
            last_error = e
            continue

    raise last_error if last_error else RuntimeError("Libgen download failed")


def _download_from_url(url: str, md5: str, dest_dir: str, timeout: int) -> str:
    scraper = cloudscraper.create_scraper()
    response = scraper.get(url, stream=True, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"Libgen download failed with status {response.status_code}")

    ext = _guess_extension(
        response.headers.get("Content-Type", ""),
        response.headers.get("Content-Disposition", ""),
        response.url,
    )
    filepath = os.path.join(dest_dir, f"{md5}{ext}")

    with open(filepath, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    return filepath


def _candidate_download_urls(md5: str):
    for mirror in get_libgen_mirrors():
        ads_url = f"https://{mirror}/ads.php?md5={md5}"
        direct = get_libgen_li_direct_link(ads_url)
        yield direct if direct else ads_url


def _candidate_ads_urls(ads_url: str):
    """
    Yield the given ads URL (rewritten to each mirror host).
    The original host is tried first, then the mirror list.
    """
    parts = urlparse(ads_url)
    original_host = parts.hostname

    hosts = []
    if original_host:
        hosts.append(original_host)
    for mirror in get_libgen_mirrors():
        if mirror not in hosts:
            hosts.append(mirror)

    for host in hosts:
        netloc = host
        if parts.port:
            netloc = f"{host}:{parts.port}"
        yield urlunparse(parts._replace(scheme="https", netloc=netloc))


def _guess_extension(content_type: str, content_disposition: str, url: str) -> str:
    import re

    match = re.search(r'filename="?([^";]+)"?', content_disposition or "")
    if match:
        filename = match.group(1).strip().lower()
        path_ext = os.path.splitext(filename)[1]
        if path_ext and path_ext != ".tmp":
            return path_ext

    ctype = (content_type or "").lower()
    for name, ext in (("epub", ".epub"), ("pdf", ".pdf"), ("mobi", ".mobi"),
                      ("djvu", ".djvu"), ("azw3", ".azw3")):
        if name in ctype:
            return ext

    path = urlparse(url).path.lower()
    for ext in (".epub", ".pdf", ".mobi", ".djvu", ".azw3", ".cbz"):
        if path.endswith(ext):
            return ext

    return ".bin"
