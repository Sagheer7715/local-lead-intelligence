"""Hardened HTTP fetching for untrusted URLs.

Every network read during enrichment goes through `fetch()`. Nothing else in this
project opens a socket.

Threat model: a business listing's `website` field is attacker-controlled. Left
unguarded it points at our own infrastructure — the scraper's unauthenticated API
on localhost, a cloud metadata endpoint at 169.254.169.254, a LAN host. The
reference kit this replaces is exploitable exactly that way, including via a
same-scheme redirect, which urllib follows to any host without rechecking.

Controls, in order of application:
  1. scheme allowlist (http/https only; no file://, ftp://, gopher://)
  2. no credentials in the URL
  3. port allowlist (80/443) so public hosts can't proxy us to odd internal ports
  4. DNS resolved up front; EVERY answer must be publicly routable
  5. the connection is pinned to a validated IP, closing the DNS-rebinding window
     between check and connect, while SNI and cert validation still use the
     real hostname
  6. redirects are followed manually, revalidated per hop, and capped
  7. hard caps on bytes and time; compression refused so there is no
     decompression bomb to absorb
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})
MAX_REDIRECTS = 3
MAX_BYTES = 400_000
TIMEOUT = 10.0
USER_AGENT = "local-lead-intelligence/0.1 (+https://example.invalid/bot)"

_TEXTUAL = ("text/html", "application/xhtml", "text/plain", "application/json")

# Asserted explicitly rather than inherited from ipaddress: the `is_private`
# membership list has shifted across Python versions, and a security control
# should not depend on which interpreter happens to run it.
_DENY_V4 = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",        # "this host"
    "10.0.0.0/8",       # RFC1918
    "100.64.0.0/10",    # CGNAT
    "127.0.0.0/8",      # loopback
    "169.254.0.0/16",   # link-local — cloud instance metadata
    "172.16.0.0/12",    # RFC1918
    "192.0.0.0/24",     # IETF protocol assignments
    "192.168.0.0/16",   # RFC1918
    "198.18.0.0/15",    # benchmarking
    "224.0.0.0/4",      # multicast
    "240.0.0.0/4",      # reserved
))
_DENY_V6 = tuple(ipaddress.ip_network(n) for n in (
    "::/128",           # unspecified
    "::1/128",          # loopback
    "64:ff9b::/96",     # NAT64
    "100::/64",         # discard-only
    "fc00::/7",         # unique local
    "fe80::/10",        # link-local
    "ff00::/8",         # multicast
))


class Blocked(Exception):
    """A URL or redirect hop was refused before any bytes were sent."""


@dataclass(frozen=True)
class Target:
    url: str
    scheme: str
    host: str
    port: int
    ip: str
    path: str


@dataclass(frozen=True)
class Result:
    url: str          # the final URL, after redirects
    status: int
    text: str
    content_type: str = ""
    truncated: bool = False


def _unwrap(ip):
    """Collapse v6 forms that embed a v4 address.

    Without this, ::ffff:169.254.169.254 sails past the v6 checks and reaches
    the metadata service.
    """
    if ip.version != 6:
        return ip
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    return ip


def _assert_ip_allowed(ip, host):
    ip = _unwrap(ip)
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        raise Blocked(f"{host} resolves to non-public address {ip}")
    for net in (_DENY_V4 if ip.version == 4 else _DENY_V6):
        if ip in net:
            raise Blocked(f"{host} resolves into denied range {net} ({ip})")


def validate(url, *, resolve=socket.getaddrinfo) -> Target:
    """Parse, vet and pin a URL. Raises Blocked rather than returning a verdict."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise Blocked(f"scheme {parts.scheme!r} not allowed")
    if parts.username or parts.password:
        raise Blocked("credentials in URL not allowed")
    try:
        host, port = parts.hostname, parts.port
    except ValueError as e:                       # malformed port
        raise Blocked(f"bad authority: {e}") from e
    if not host:
        raise Blocked("no host in URL")
    port = port or (443 if scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise Blocked(f"port {port} not allowed")

    try:
        infos = resolve(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise Blocked(f"cannot resolve {host}: {e}") from e
    if not infos:
        raise Blocked(f"no addresses for {host}")

    chosen = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # Every answer must pass. Accepting the first public one would let an
        # attacker serve a mixed answer set and win the race on reconnect.
        _assert_ip_allowed(ip, host)
        if chosen is None:
            chosen = ip

    path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return Target(url, scheme, host, port, str(chosen), path)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout, context):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        # Connect to the pinned IP, but validate the certificate and send SNI
        # for the hostname the caller actually asked for.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _request(target, *, timeout, user_agent, max_bytes):
    """One hop. Returns (status, location, content_type, text, truncated)."""
    if target.scheme == "https":
        conn = _PinnedHTTPS(target.host, target.ip, target.port, timeout,
                            ssl.create_default_context())
    else:
        conn = _PinnedHTTP(target.host, target.ip, target.port, timeout)
    try:
        conn.request("GET", target.path, headers={
            "Host": target.host,
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",   # refuse compression: no bomb to absorb
            "Connection": "close",
        })
        resp = conn.getresponse()
        ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        if resp.status in (301, 302, 303, 307, 308):
            return resp.status, resp.getheader("Location"), ctype, "", False
        if ctype and not ctype.startswith(_TEXTUAL):
            return resp.status, None, ctype, "", False      # don't read binaries
        raw = resp.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        return (resp.status, None, ctype,
                raw[:max_bytes].decode("utf-8", "replace"), truncated)
    finally:
        conn.close()


def fetch(url, *, max_bytes=MAX_BYTES, timeout=TIMEOUT, max_redirects=MAX_REDIRECTS,
          user_agent=USER_AGENT, resolve=socket.getaddrinfo, request=_request) -> Result:
    """GET a URL that came from untrusted data. Raises Blocked if it is refused.

    `resolve` and `request` are injectable so the guard can be tested offline
    without a bypass flag living in production code.
    """
    for _ in range(max_redirects + 1):
        target = validate(url, resolve=resolve)
        status, location, ctype, text, truncated = request(
            target, timeout=timeout, user_agent=user_agent, max_bytes=max_bytes)
        if location is None:
            return Result(target.url, status, text, ctype, truncated)
        if not location.strip():
            raise Blocked(f"{status} with empty Location")
        url = urljoin(url, location)   # revalidated at the top of the next pass
    raise Blocked(f"more than {max_redirects} redirects")
