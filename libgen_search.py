import re
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

import http_client
from domain import get_libgen_mirrors

SEARCH_HOSTS = ["libgen.li"]

_MD5_RE = re.compile(r"md5=([a-fA-F0-9]{32})")


def _candidate_hosts():
    hosts = list(SEARCH_HOSTS)
    try:
        for mirror in get_libgen_mirrors():
            if mirror not in hosts:
                hosts.append(mirror)
    except Exception:
        pass
    return hosts


def build_search_url(host, query, page=1, per_page=50):
    return (
        f"https://{host}/index.php"
        f"?req={quote_plus(query)}"
        f"&columns[]=t&columns[]=a"
        f"&objects[]=f&topics[]=l&topics[]=f"
        f"&res={per_page}"
        f"&page={page}"
    )


def parse_results(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="tablelibgen")
    if not table:
        return []

    results = []
    for tr in table.find_all("tr")[1:]:
        try:
            tds = tr.find_all("td")
            if len(tds) < 8:
                continue

            title = None
            title_a = tds[0].find("a", href=True)
            if title_a:
                title = title_a.get_text(" ", strip=True)

            md5 = None
            detail_url = None
            for a in tr.find_all("a", href=True):
                m = _MD5_RE.search(a["href"])
                if m:
                    md5 = m.group(1).lower()
                    if "ads.php" in a["href"]:
                        detail_url = a["href"]
                        break
            if md5 and not detail_url:
                detail_url = f"/ads.php?md5={md5}"
            if not md5:
                continue

            def cell(i):
                return tds[i].get_text(" ", strip=True) if i < len(tds) else None

            ext = cell(7)
            results.append({
                "md5": md5,
                "title": title,
                "author": cell(1),
                "publisher": cell(2),
                "year": cell(3),
                "language": cell(4),
                "pages": cell(5),
                "size": cell(6),
                "format": ext.upper() if ext else None,
                "downloads": None,
                "description": None,
                "detail_url": detail_url,
            })
        except Exception:
            continue

    return results


def _relevance_score(book, query):
    q = query.lower().strip()
    t = (book.get("title") or "").lower()
    if not t or not q:
        return 0
    if t.startswith(q):
        return 100
    if q in t:
        return 80
    q_words = set(re.findall(r"[a-z0-9]+", q))
    t_words = set(re.findall(r"[a-z0-9]+", t))
    if not q_words:
        return 0
    overlap = len(q_words & t_words) / len(q_words)
    return int(overlap * 60)


def search_libgen(query, page=1, formats=None, per_page=50):
    if not query or not query.strip():
        raise ValueError("Query is required")

    last_error = None
    for host in _candidate_hosts():
        url = build_search_url(host, query, page=page, per_page=per_page)
        try:
            r = http_client.get(url, timeout=25, cookies={})
            if r.status_code != 200:
                continue
            results = parse_results(r.text)
            if results:
                results.sort(
                    key=lambda b: _relevance_score(b, query),
                    reverse=True,
                )
                if formats:
                    wanted = {f.lower() for f in formats}
                    results = [
                        b for b in results
                        if (b.get("format") or "").lower() in wanted
                    ]
                return results
        except Exception as e:
            last_error = e
            continue

    if last_error:
        raise last_error
    return []
