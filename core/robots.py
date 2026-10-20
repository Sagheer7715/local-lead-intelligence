"""robots.txt checking, fetched through the SSRF guard like everything else.

`urllib.robotparser` can fetch on its own, and that is exactly what it must not
do here: it would open a socket straight to a host that came out of scraped
data, bypassing `core.safe_fetch`. So we fetch with the guard and hand the
parser only the text.

Politeness is also self-interest. Respecting robots.txt is what keeps a
crawler off blocklists.
"""
from __future__ import annotations

import threading
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from core import safe_fetch


class RobotsCache:
    """One robots.txt per host, fetched once per run, thread-safe."""

    def __init__(self, user_agent, *, enabled=True, timeout=8.0, fetch=safe_fetch.fetch):
        self.user_agent = user_agent
        self.enabled = enabled
        self.timeout = timeout
        self._fetch = fetch
        self._parsers = {}
        self._lock = threading.Lock()

    def _origin(self, url):
        parts = urlsplit(url)
        if not parts.hostname:
            return None
        return f"{parts.scheme}://{parts.netloc}"

    def _parser_for(self, origin):
        with self._lock:
            if origin in self._parsers:
                return self._parsers[origin]
        parser = RobotFileParser()
        try:
            result = self._fetch(f"{origin}/robots.txt", timeout=self.timeout,
                                 user_agent=self.user_agent, max_bytes=256_000)
            if result.status == 200 and result.text:
                parser.parse(result.text.splitlines())
            else:
                # No robots.txt, or an error page. RFC 9309 treats "not found"
                # as permission to crawl, which is the long-standing default.
                parser.allow_all = True
        except Exception:
            # A host we cannot reach for robots.txt is one we probably cannot
            # crawl either; allow, and let the page fetch fail on its own terms
            # rather than silently skipping a lead over an infrastructure blip.
            parser.allow_all = True
        with self._lock:
            self._parsers[origin] = parser
        return parser

    def allowed(self, url) -> bool:
        if not self.enabled:
            return True
        origin = self._origin(url)
        if origin is None:
            return False
        try:
            return self._parser_for(origin).can_fetch(self.user_agent, url)
        except Exception:
            return True

    def crawl_delay(self, url, default=0.0) -> float:
        """Honour an explicit Crawl-delay when the site sets one."""
        if not self.enabled:
            return default
        origin = self._origin(url)
        if origin is None:
            return default
        try:
            value = self._parser_for(origin).crawl_delay(self.user_agent)
            return max(default, float(value)) if value is not None else default
        except Exception:
            return default
