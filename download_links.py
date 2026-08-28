# download_links.py

import os

from external_link_handler import parse_external_links
from libgen_li_handler import download_libgen_li_book

DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")


def fetch_download_links(md5: str) -> dict:
    """
    Build download links from an MD5. No Anna's Archive fetch is needed;
    the links are derived from the MD5 and Libgen mirrors directly.
    """
    if not md5:
        raise ValueError("MD5 is required")
    return parse_external_links(md5)


def user_download_menu(links: dict, md5: str | None = None):
    """
    CLI menu for download links. Optionally downloads via Libgen.li.
    """
    from database import init_database, check_book_exists, save_book, update_download_path

    fast = links.get("fast", {})

    while True:
        print("\nAvailable Download Options:\n")

        for i, (name, url) in enumerate(fast.items(), start=1):
            print(f"{i}. {name}")
            print(f"   {url}\n")

        print(f"{len(fast) + 1}. Download via Libgen.li")
        print("0) Exit")

        choice = input("\nEnter choice number: ").strip()

        if choice == "0":
            return

        if choice == str(len(fast) + 1):
            if not md5:
                print("\nMD5 is required to download.")
                return

            init_database()
            cached = check_book_exists(md5)

            if cached and cached.get("file_path") and os.path.exists(cached["file_path"]):
                print(f"\nAlready downloaded: {cached['file_path']}")
                return

            print("\nDownloading from Libgen.li... ⏳\n")
            try:
                filepath = download_libgen_li_book(md5, DEFAULT_DOWNLOAD_DIR)
            except Exception as e:
                print(f"\nDownload failed: {e}")
                return

            if cached:
                update_download_path(md5, filepath)
            else:
                save_book(md5, md5, file_path=filepath)

            print(f"Downloaded to: {filepath}")
            return

        if choice.isdigit() and 1 <= int(choice) <= len(fast):
            name = list(fast.keys())[int(choice) - 1]
            url = fast[name]
            print(f"\n{name}: {url}")
            continue

        print("Invalid choice. Please try again.")
