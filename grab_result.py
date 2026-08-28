# grab_result.py

from bs4 import BeautifulSoup
from urllib.parse import urljoin

from domain import get_base_url


def parse_search_results(html):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    records = soup.select("div.js-aarecord-list-outer > div.flex")

    for record in records:
        try:
            title_link = record.select_one("a.js-vim-focus")
            if not title_link:
                continue

            detail_path = title_link.get("href", "")
            md5 = detail_path.replace("/md5/", "")
            detail_url = urljoin(get_base_url(), detail_path)

            title = title_link.get_text(strip=True)

            author_tag = record.select_one("a[href^='/search?q=']")
            author = author_tag.get_text(strip=True) if author_tag else None

            publisher = None
            pub_tags = record.select("a.text-sm")
            if len(pub_tags) >= 2:
                publisher = pub_tags[1].get_text(strip=True)

            desc_tag = record.select_one("div.text-gray-600")
            description = desc_tag.get_text(" ", strip=True) if desc_tag else None

            language = file_format = size = year = None
            meta = record.select_one("div.font-semibold.text-sm")

            if meta:
                parts = [p.strip() for p in meta.get_text(" ", strip=True).split("·")]

                if len(parts) >= 3:
                    language = parts[0]
                    file_format = parts[1]
                    size = parts[2]

                for p in parts:
                    if p.isdigit() and len(p) == 4:
                        year = p

            downloads = None
            downloads_tag = record.select_one("span[title='Downloads']")
            if downloads_tag:
                downloads = downloads_tag.get_text(strip=True)

            results.append({
                "md5": md5,
                "title": title,
                "author": author,
                "publisher": publisher,
                "description": description,
                "language": language,
                "format": file_format,
                "size": size,
                "year": year,
                "downloads": downloads,
                "detail_url": detail_url
            })

        except Exception:
            continue

    return results

