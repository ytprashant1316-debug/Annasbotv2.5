# book_info.py

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import http_client
from domain import get_libgen_mirrors


def _candidate_ads_urls(md5: str):
    hosts = ["libgen.li"]
    try:
        for mirror in get_libgen_mirrors():
            if mirror not in hosts:
                hosts.append(mirror)
    except Exception:
        pass
    return [f"https://{host}/ads.php?md5={md5}" for host in hosts]


def _referer_for(url: str) -> str:
    """ads.php renders its content only when a Referer header is sent."""
    host = urlparse(url).hostname or "libgen.li"
    return f"https://{host}/index.php"


def fetch_book_info(md5: str):
    """
    Fetch and parse book info from a Libgen ads page (no Anna's Archive).
    """
    if not md5:
        raise ValueError("MD5 is required")
    md5 = md5.lower()

    last_error = None
    for url in _candidate_ads_urls(md5):
        try:
            r = http_client.get(
                url,
                timeout=25,
                cookies={},
                headers={"Referer": _referer_for(url)},
            )
            if r.status_code != 200:
                continue
            info = parse_book_info(r.text, url)
            if info.get("book_name"):
                return info
        except Exception as e:
            last_error = e
            continue

    if last_error:
        raise last_error

    return {
        "book_name": None,
        "author": None,
        "description": None,
        "url": f"https://libgen.li/ads.php?md5={md5}",
    }


def parse_book_info(html: str, book_url: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    def field(label: str):
        m = re.search(rf"{re.escape(label)}:[ \t]*(.+)", text)
        return m.group(1).strip() if m else None

    title = field("Title")
    author = field("Author(s)")
    series = field("Series")
    publisher = field("Publisher")
    year = field("Year")
    isbn = field("ISBN")

    get_url = None
    for h2 in soup.find_all("h2"):
        if h2.get_text(strip=True).upper() == "GET":
            a = h2.find_parent("a", href=True)
            if a:
                get_url = urljoin(book_url, a["href"])
                break

    desc_parts = []
    if series:
        desc_parts.append(f"Series: {series}")
    if publisher:
        desc_parts.append(f"Publisher: {publisher}")
    if year:
        desc_parts.append(f"Year: {year}")
    if isbn:
        desc_parts.append(f"ISBN: {isbn}")

    return {
        "book_name": title,
        "author": author,
        "description": "\n".join(desc_parts) if desc_parts else None,
        "url": book_url,
        "download_url": get_url,
        "publisher": publisher,
        "year": year,
    }


def user_book_menu(book_info: dict, md5: str | None = None):
    """
    Final user menu:
    1) Get book info
    2) Get download link
    """
    print("\nChoose an option:\n")
    print("1) Get book info")
    print("2) Get download link")
    print("0) Exit")

    while True:
        choice = input("\nEnter choice number: ").strip()

        if choice == "0":
            return

        if choice == "1":
            print("\n--- Book Info ---")
            print(f"Book Name : {book_info.get('book_name')}")
            print(f"Author    : {book_info.get('author')}")
            print(f"URL       : {book_info.get('url')}")
            print("\nDetails:\n")
            print(book_info.get("description"))
            return

        if choice == "2":
            if not md5:
                print("\nMD5 is required to fetch download links.")
                return

            from download_links import fetch_download_links, user_download_menu

            print("\nFetching download links... ⏳\n")
            links = fetch_download_links(md5)
            user_download_menu(links, md5=md5)
            return

        print("Invalid choice. Please try again.")


# -------------------------
# Standalone test
# -------------------------
if __name__ == "__main__":
    md5 = input("Enter MD5 hash: ").strip()
    info = fetch_book_info(md5)
    user_book_menu(info)
