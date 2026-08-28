# search.py

from urllib.parse import urlencode, quote_plus

from domain import get_base_url


def build_base_url() -> str:
    return f"{get_base_url()}/search"


def build_search_url(
    query: str,
    page: int = 1,
    formats: list | None = None,
    language: str | None = None,
    year: int | None = None,
    sort: str | None = None,
    src: str = "lgli",
):
    """
    Build Anna's Archive search URL using user-defined filters.

    `src` filters results by source (e.g. "lgli" = Libgen.li only).
    """

    if not query or not query.strip():
        raise ValueError("Query is required")

    if formats is None:
        formats = ["pdf", "epub"]

    params = [
        ("index", ""),
        ("page", page),
    ]

    if sort:
        params.append(("sort", sort))
    else:
        params.append(("sort", ""))

    # File extensions
    for fmt in formats:
        params.append(("ext", fmt))

    # Source filter (Libgen.li only)
    params.append(("src", src))

    params.append(("display", ""))
    params.append(("q", query))

    # Optional filters (kept future-safe)
    if language:
        params.append(("lang", language))

    if year:
        params.append(("year", year))

    query_string = urlencode(params, doseq=True, quote_via=quote_plus)
    return f"{build_base_url()}?{query_string}"

