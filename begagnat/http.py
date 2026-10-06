"""A polite HTTP client: one shared session, retries, and per-host rate limiting."""

from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class Fetcher:
    """Fetches pages without hammering anyone.

    ``min_interval`` is enforced per host, so hitting three marketplaces in
    parallel does not serialise them against each other.
    """

    def __init__(
        self,
        *,
        min_interval: float = 1.5,
        timeout: float = 25.0,
        retries: int = 3,
        user_agent: str = DEFAULT_UA,
        cache_dir: Path | None = None,
    ) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self.cache_dir = cache_dir
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            }
        )

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._lock:
            elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
            wait = self.min_interval - elapsed
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.4))
            self._last_hit[host] = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_error: Exception | None = None

        for attempt in range(self.retries):
            self._throttle(url)
            try:
                response = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("%s %s failed (%s), attempt %d/%d", method, url, exc, attempt + 1, self.retries)
            else:
                if response.status_code in (429, 500, 502, 503, 504):
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if (retry_after or "").isdigit() else 2 ** attempt
                    log.warning("%s returned %s, backing off %.0fs", url, response.status_code, delay)
                    last_error = requests.HTTPError(f"{response.status_code} for {url}")
                    time.sleep(min(delay, 30))
                    continue
                self._maybe_cache(url, response)
                return response
            time.sleep(2 ** attempt)

        raise RuntimeError(f"giving up on {url}: {last_error}")

    def _maybe_cache(self, url: str, response: requests.Response) -> None:
        """Dump raw responses to disk so parser bugs can be reproduced offline.

        Auth responses are never written: an OAuth body carries a bearer token,
        and a debugging dump is the last place it should live. There is no
        parser to debug there either.
        """
        if not self.cache_dir or _is_auth_url(url):
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        stem = urlparse(url).netloc.replace(".", "_")
        path = self.cache_dir / f"{stem}-{int(time.time())}.txt"
        path.write_text(response.text, encoding="utf-8", errors="replace")
        log.info("saved raw response to %s", path)


#: Path fragments that mean "this response contains a credential, not content".
_AUTH_FRAGMENTS = ("/oauth", "/oauth2/", "/token", "auth/token", "refresh-token")


def _is_auth_url(url: str) -> bool:
    return any(fragment in url.lower() for fragment in _AUTH_FRAGMENTS)
