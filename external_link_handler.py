# external_link_handler.py

from libgen_li_handler import get_libgen_li_direct_link


def parse_external_links(md5: str) -> dict:
    """
    Build external download links using MD5 only.

    Rules:
    - Keep:
        • Z-Library (single clearnet)
        • Libgen.li (direct GET if possible, else ads page)
    - Remove:
        • IPFS
        • Tor
        • Torrents
        • Libgen.rs
    """

    md5 = md5.lower()

    # Z-Library (single)
    zlibrary_url = f"https://z-lib.fm/md5/{md5}"

    # Libgen.li
    libgen_ads_url = f"https://libgen.li/ads.php?md5={md5}"
    libgen_direct = get_libgen_li_direct_link(libgen_ads_url)
    libgen_final = libgen_direct if libgen_direct else libgen_ads_url

    return {
        "fast": {
            "Z-Library": zlibrary_url,
            "Libgen.li": libgen_final
        }
    }

