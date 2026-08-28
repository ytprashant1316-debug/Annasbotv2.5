import time
from urllib.parse import urlparse, urlunparse

import http_client
from domain import get_base_url, set_active_domain, get_domain_order

CRAWL_DELAY = 10  # seconds


class SafeFetcher:
    def __init__(self):
        self.session = http_client.make_session()
        self.cookies = http_client.load_cookies()
        self.last_request_time = 0

    def _respect_crawl_delay(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < CRAWL_DELAY:
            time.sleep(CRAWL_DELAY - elapsed)

    def get(self, url, timeout=30, check=None):
        self._respect_crawl_delay()
        return self._get_with_domain_switch(url, timeout, check)

    def _get_with_domain_switch(self, url, timeout, check):
        """
        Fetch a URL, retrying across Anna's Archive domains when the
        current one is unreachable or returns unexpected content.
        The URL's host is rewritten to each candidate domain before retrying.
        `check` is an optional callable that receives the response text and
        returns True if the content is acceptable.
        """
        attempts = []

        active = get_base_url()
        parsed_active = urlparse(active)
        attempts.append(parsed_active.hostname)

        for domain in get_domain_order():
            if domain not in attempts:
                attempts.append(domain)

        last_error = None

        for domain in attempts:
            rewritten = _rewrite_host(url, domain)

            try:
                response = self.session.get(
                    rewritten,
                    headers=http_client.DEFAULT_HEADERS,
                    cookies=self.cookies,
                    timeout=timeout,
                )
                self.last_request_time = time.time()
                response.raise_for_status()
                text = response.text
                if check is not None and not check(text):
                    raise ValueError("content validation failed")
                set_active_domain(domain)
                return text
            except Exception as e:
                last_error = e
                continue

        raise last_error


def _rewrite_host(url: str, host: str) -> str:
    parts = urlparse(url)
    if not parts.hostname:
        return url
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    return urlunparse(parts._replace(netloc=netloc))
