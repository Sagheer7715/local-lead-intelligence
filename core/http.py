"""HTTP client for endpoints the operator configured.

Deliberately NOT `core.safe_fetch`. That module guards URLs arriving inside
scraped data, where the entire point is to refuse anything that is not publicly
routable. The endpoints here — Overpass, Nominatim, a Tier 3 engine — come from
config, and a self-hosted Overpass at `http://127.0.0.1:12345` is a supported
setup rather than an attack. Two clients means neither has to compromise:
`safe_fetch` stays strict, and this one can POST, retry and reach localhost.

Nothing here should ever be handed a URL that came from scraped data.
"""
from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

MAX_BYTES = 64 * 1024 * 1024        # Overpass answers for a big city get large
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class HttpError(Exception):
    def __init__(self, status, message, body=""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


class RateLimited(HttpError):
    """The server asked us to slow down, and we ran out of retries."""


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict

    def json(self):
        return json.loads(self.body.decode("utf-8", "replace"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _sleep_for(attempt: int, retry_after=None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 120.0)
        except (TypeError, ValueError):
            pass
    # Exponential with jitter: 2s, 4s, 8s … jitter so parallel callers don't
    # retry in lockstep and look like a burst.
    return min(2.0 ** (attempt + 1), 60.0) * (0.5 + random.random() / 2)


def request(url, *, method="GET", data=None, headers=None, user_agent,
            timeout=90.0, retries=3, max_bytes=MAX_BYTES, sleep=time.sleep) -> Response:
    """One request, with backoff on the statuses that mean "later".

    `user_agent` is required, not defaulted. Overpass and Nominatim are free,
    donation-funded, shared infrastructure, and both ask that clients identify
    themselves with a way to make contact. A tool that scrapes politely but
    hammers them anonymously has missed the point.
    """
    if not user_agent or "example.invalid" in user_agent:
        raise ValueError(
            "a real contact User-Agent is required — set LLI_CONTACT_URL in .env")

    body = data
    if isinstance(body, dict):
        body = urllib.parse.urlencode(body).encode()
    elif isinstance(body, str):
        body = body.encode()

    hdrs = {
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    if body is not None and "Content-Type" not in (headers or {}):
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    hdrs.update(headers or {})

    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise HttpError(resp.status, f"response exceeded {max_bytes} bytes")
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return Response(resp.status, raw, dict(resp.headers))
        except urllib.error.HTTPError as e:
            detail = e.read(2000).decode("utf-8", "replace")
            last = (e.code, e.reason, detail, e.headers.get("Retry-After"))
            if e.code not in RETRY_STATUSES or attempt == retries:
                cls = RateLimited if e.code == 429 else HttpError
                raise cls(e.code, e.reason, detail) from e
        except (urllib.error.URLError, TimeoutError) as e:
            last = (0, str(e), "", None)
            if attempt == retries:
                raise HttpError(0, f"{url} unreachable: {e}") from e
        sleep(_sleep_for(attempt, last[3] if last else None))

    status, reason, detail, _ = last            # pragma: no cover — loop always returns
    raise HttpError(status, reason, detail)


def get_json(url, params=None, **kw):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return request(url, **kw).json()


def post_json(url, data, **kw):
    return request(url, method="POST", data=data, **kw).json()
