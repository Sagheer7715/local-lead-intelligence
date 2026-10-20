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
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

# On Python 3.9 `socket.timeout` is a plain OSError subclass, NOT TimeoutError —
# they were only unified in 3.10. Catching just TimeoutError lets a read
# timeout escape as a raw traceback on exactly the interpreter macOS ships.
#
# ssl.SSLError is listed separately because it is not a URLError. urlopen wraps
# the ones raised while connecting, but one raised later — during the response
# read — reaches the caller bare.
NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError)

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


def _is_handshake_failure(exc) -> bool:
    text = str(exc)
    return "HANDSHAKE_FAILURE" in text or "UNSUPPORTED_PROTOCOL" in text


def _curl_argv(url, *, method, body, headers, timeout, max_bytes, header_file):
    """Build the curl command. Factored out so the tests can inspect it.

    The request body goes in on STDIN (`--data-binary @-`), never in argv:
    argv is world-readable through `ps`, and a query is not something to
    broadcast to every process on the machine.
    """
    argv = ["curl", "-sS", "--proto", "=http,https", "--proto-redir", "=http,https",
            "--max-time", str(int(timeout)), "--max-filesize", str(max_bytes),
            "-D", header_file, "-X", method, url]
    for key, value in headers.items():
        if key.lower() == "accept-encoding":
            continue                     # let curl and us agree on identity
        argv += ["-H", f"{key}: {value}"]
    if body is not None:
        argv += ["--data-binary", "@-"]
    return argv


def _curl_request(url, *, method, body, headers, timeout, max_bytes) -> Response:
    """Fallback transport for endpoints our TLS stack cannot reach.

    macOS ships Python linked against LibreSSL 2.8.3, which cannot do TLS 1.3,
    and overpass-api.de now requires it. The system curl (SecureTransport +
    LibreSSL 3.3.6) can, and ships on every Mac.

    Scope is deliberately narrow: this is only for endpoints named in config,
    and only after a handshake failure. `core.safe_fetch` never comes here —
    its SSRF guard depends on pinning the connection to a validated IP, and
    re-implementing that through a subprocess would mean two versions of the
    security-critical path.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(mode="r+", suffix=".hdr") as hdr:
        argv = _curl_argv(url, method=method, body=body, headers=headers,
                          timeout=timeout, max_bytes=max_bytes, header_file=hdr.name)
        try:
            proc = subprocess.run(                       # noqa: S603 — list argv, no shell
                argv, input=body, capture_output=True,
                timeout=timeout + 10, shell=False)
        except FileNotFoundError as e:
            raise HttpError(0, "curl not found, and this Python cannot do TLS 1.3") from e
        except subprocess.TimeoutExpired as e:
            raise HttpError(0, f"{url} timed out via curl") from e
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
            raise HttpError(0, f"{url} unreachable via curl: {detail}")
        hdr.seek(0)
        head = hdr.read()

    status, parsed = 0, {}
    for line in head.splitlines():
        if line.startswith("HTTP/"):
            parts = line.split()
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            parsed = {}                                  # reset across redirects
        elif ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()
    if status in RETRY_STATUSES or status >= 400:
        detail = proc.stdout[:2000].decode("utf-8", "replace")
        raise HttpError(status, _first_meaningful_line(detail) or "request failed", detail)
    return Response(status, proc.stdout, parsed)


def _first_meaningful_line(body: str) -> str:
    """Pull the server's own explanation out of an error page.

    Overpass answers a 502 with a readable markdown page saying it is at
    capacity. Reporting a bare "HTTP 502: request failed" while holding that
    text is throwing away the only useful part of the response.
    """
    for raw in (body or "").splitlines():
        line = raw.strip().lstrip("#").strip()
        if len(line) > 3 and not line.startswith("<"):
            return line[:160]
    return ""


def _explain(exc) -> str:
    """Turn an opaque network error into something actionable.

    A bare `SSLV3_ALERT_HANDSHAKE_FAILURE` tells you nothing. In practice it
    means one thing here: macOS ships a Python linked against LibreSSL 2.8.3,
    which cannot do TLS 1.3, and the main Overpass instance now requires it.
    """
    text = str(exc)
    if "HANDSHAKE_FAILURE" in text or "UNSUPPORTED_PROTOCOL" in text:
        _ssl = ssl
        return (f"TLS handshake refused ({_ssl.OPENSSL_VERSION}, "
                f"TLS1.3={_ssl.HAS_TLSv1_3}). This endpoint likely requires "
                "TLS 1.3, which macOS's system Python cannot negotiate. Either "
                "set LLI_OVERPASS_URL to a mirror that accepts TLS 1.2, or run "
                "on a Python built against OpenSSL 1.1.1+.")
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in text:
        return (f"{text} — the endpoint accepted the connection but did not "
                "answer in time. Public Overpass mirrors queue under load; "
                "retry, lower the scope, or try another LLI_OVERPASS_URL.")
    return text


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
            timeout=90.0, retries=3, max_bytes=MAX_BYTES, sleep=time.sleep,
            allow_curl_fallback=True) -> Response:
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
    use_curl = False
    attempt = 0
    # A while loop, not `for attempt in range(...)`: switching transport after a
    # handshake failure must not consume a retry slot, or with retries=0 the
    # fallback would never actually run.
    while attempt <= retries:
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            if use_curl:
                # Inside the loop on purpose. Where TLS forces the curl path it
                # becomes the ONLY path, so it has to get the same backoff —
                # a 502 from a public Overpass instance is routine.
                return _curl_request(url, method=method, body=body, headers=hdrs,
                                     timeout=timeout, max_bytes=max_bytes)
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
        except HttpError as e:
            # Raised by the curl transport. Same retry policy as urllib's.
            last = (e.status, str(e), e.body, None)
            if e.status not in RETRY_STATUSES or attempt == retries:
                raise
        except NETWORK_ERRORS as e:
            # A handshake failure will not improve on retry — the TLS stack
            # simply cannot speak to this endpoint. Switch transport and go
            # again immediately, without burning an attempt on a sleep.
            if allow_curl_fallback and not use_curl and _is_handshake_failure(e):
                use_curl = True
                continue
            last = (0, str(e), "", None)
            if attempt == retries:
                raise HttpError(0, f"{url} unreachable: {_explain(e)}") from e
        sleep(_sleep_for(attempt, last[3] if last else None))
        attempt += 1

    if last is None:                            # pragma: no cover — defensive
        raise HttpError(0, f"{url}: exhausted retries with no recorded error")
    status, reason, detail, _ = last
    raise HttpError(status, reason, detail)


def get_json(url, params=None, **kw):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return request(url, **kw).json()


def post_json(url, data, **kw):
    return request(url, method="POST", data=data, **kw).json()
