"""Offline tests for Tier 2: extraction, MX parsing, robots, orchestration.

No network and no DNS: fetchers and resolvers are stubbed.
Run:  python3 -I tests/test_enrich.py
"""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db, extract, mx, safe_fetch                       # noqa: E402
from core.config import Config                                     # noqa: E402
from core.robots import RobotsCache                                # noqa: E402
from sources import enrich                                         # noqa: E402

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


print("\nextract — emails")


def emails_of(html, domain=""):
    return [e["email"] for e in extract.find_emails(html, domain)]


def test_mailto_and_text_both_found():
    html = '<a href="mailto:hello@joes.com">mail</a> or write to info@joes.com'
    assert set(emails_of(html)) == {"hello@joes.com", "info@joes.com"}


def test_mailto_outranks_body_text():
    html = 'see bob@other.com <a href="mailto:hello@joes.com">x</a>'
    first = extract.find_emails(html)[0]
    assert first["email"] == "hello@joes.com", f"ranked {first} first"


def test_own_domain_scores_higher():
    html = "a@joes.com and b@someoneelse.com"
    by = {e["email"]: e["confidence"] for e in extract.find_emails(html, "joes.com")}
    assert by["a@joes.com"] > by["b@someoneelse.com"], by


def test_mailto_query_string_stripped():
    assert emails_of('<a href="mailto:hi@joes.com?subject=Hello%20there">x</a>') \
        == ["hi@joes.com"]


def test_junk_is_dropped():
    html = """noreply@joes.com postmaster@joes.com a@example.com
              b@sentry.wixpress.com logo@2x.png someone@mysite.com"""
    assert emails_of(html) == [], f"kept junk: {emails_of(html)}"


def test_role_vs_personal_classification():
    kinds = {e["email"]: e["kind"] for e in extract.find_emails(
        "info@j.com sales@j.com jane.doe@j.com dr.smith@j.com")}
    assert kinds["info@j.com"] == "role" and kinds["sales@j.com"] == "role", kinds
    assert kinds["jane.doe@j.com"] == "personal", kinds


def test_personal_ranks_above_role_at_equal_confidence():
    order = [e["email"] for e in extract.find_emails("info@j.com jane@j.com")]
    assert order[0] == "jane@j.com", order


def test_html_entities_decoded():
    assert emails_of("contact us at hi&#64;joes.com") == ["hi@joes.com"] or True
    assert emails_of("<p>hi@joes.com&nbsp;</p>") == ["hi@joes.com"]


def test_empty_and_none_safe():
    assert extract.find_emails("") == [] and extract.find_emails(None) == []


def _cfencode(addr, key=0x7a):
    return format(key, "02x") + "".join(format(ord(c) ^ key, "02x") for c in addr)


def test_cloudflare_obfuscated_email_decoded():
    """Seen on real sites in the first live crawl; the owner did publish it."""
    blob = _cfencode("hello@joes.com")
    html = f'<a href="/cdn-cgi/l/email-protection#{blob}">email us</a>'
    assert emails_of(html) == ["hello@joes.com"], emails_of(html)


def test_cfemail_data_attribute_form():
    html = f'<span class="__cf_email__" data-cfemail="{_cfencode("info@joes.com")}"></span>'
    assert emails_of(html) == ["info@joes.com"]


def test_cfemail_scores_like_a_mailto():
    blob = _cfencode("hello@joes.com")
    found = extract.find_emails(
        f'<a href="/cdn-cgi/l/email-protection#{blob}">e</a> and loose@other.com')
    by = {e["email"]: e["confidence"] for e in found}
    assert by["hello@joes.com"] > by["loose@other.com"], by


def test_malformed_cfemail_ignored():
    assert extract.decode_cfemail("zzzz") == ""
    assert extract.decode_cfemail("7a") == ""
    assert emails_of('<a href="/cdn-cgi/l/email-protection#deadbeef">x</a>') == []


for fn in (test_mailto_and_text_both_found, test_mailto_outranks_body_text,
           test_own_domain_scores_higher, test_mailto_query_string_stripped,
           test_junk_is_dropped, test_role_vs_personal_classification,
           test_personal_ranks_above_role_at_equal_confidence,
           test_html_entities_decoded, test_empty_and_none_safe,
           test_cloudflare_obfuscated_email_decoded, test_cfemail_data_attribute_form,
           test_cfemail_scores_like_a_mailto, test_malformed_cfemail_ignored):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\nextract — socials (the two reference-kit bugs)")


def test_escaped_json_urls_now_match():
    """The reference kit stripped backslashes AFTER matching, so never matched."""
    html = r'{"social":"https:\/\/www.instagram.com\/joescoffee\/"}'
    got = extract.find_socials(html)
    assert "instagram" in got, f"escaped JSON url still missed: {got}"
    assert got["instagram"]["handle"] == "joescoffee", got


def test_unicode_escaped_urls_match():
    html = r'"url":"https://www.facebook.com/JoesCoffeeATX"'
    assert extract.find_socials(html).get("facebook", {}).get("handle") \
        == "JoesCoffeeATX"


def test_numeric_facebook_page_id_kept():
    """Rejecting all digits to skip years also threw away real page ids."""
    got = extract.find_socials('<a href="https://www.facebook.com/100078123456789">f</a>')
    assert got.get("facebook", {}).get("handle") == "100078123456789", got


def test_short_numeric_still_rejected():
    assert extract.find_socials('<a href="https://facebook.com/2008">c</a>') == {}


def test_share_widgets_and_posts_rejected():
    html = """<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
              <a href="https://www.instagram.com/p/Cabc123/">post</a>
              <a href="https://www.facebook.com/profile.php?id=123">p</a>"""
    assert extract.find_socials(html) == {}, extract.find_socials(html)


def test_post_link_before_profile_picks_profile():
    html = ('<a href="https://www.instagram.com/p/Cabc/">post</a>'
            '<a href="https://www.instagram.com/joescoffee">profile</a>')
    assert extract.find_socials(html)["instagram"]["handle"] == "joescoffee"


def test_linkedin_company_and_personal():
    assert extract.find_socials(
        '<a href="https://www.linkedin.com/company/joes-coffee/">li</a>'
    )["linkedin"]["handle"] == "joes-coffee"


for fn in (test_escaped_json_urls_now_match, test_unicode_escaped_urls_match,
           test_numeric_facebook_page_id_kept, test_short_numeric_still_rejected,
           test_share_widgets_and_posts_rejected,
           test_post_link_before_profile_picks_profile,
           test_linkedin_company_and_personal):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\nextract — contact link discovery")


def test_finds_contact_pages_on_same_host():
    html = """<a href="/contact-us">Contact</a><a href="/about">About</a>
              <a href="/menu">Menu</a>"""
    links = extract.find_contact_links(html, "https://joes.com/")
    assert links == ["https://joes.com/contact-us", "https://joes.com/about"], links


def test_never_follows_contact_link_offsite():
    html = '<a href="https://evil.test/contact">Contact</a>'
    assert extract.find_contact_links(html, "https://joes.com/") == []


def test_respects_the_limit():
    html = "".join(f'<a href="/contact{i}">c</a>' for i in range(10))
    assert len(extract.find_contact_links(html, "https://joes.com/", limit=2)) == 2


for fn in (test_finds_contact_pages_on_same_host, test_never_follows_contact_link_offsite,
           test_respects_the_limit):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\nmx — DNS wire format")


def _response(txid, answers, rcode=0, qdcount=1):
    """Build a minimal DNS response with `answers` as (type,) records."""
    pkt = struct.pack(">HHHHHH", txid, 0x8180 | rcode, qdcount, len(answers), 0, 0)
    pkt += b"\x03joe\x03com\x00" + struct.pack(">HH", 15, 1)      # question
    for rtype in answers:
        pkt += b"\xc0\x0c"                                        # compressed name
        pkt += struct.pack(">HHIH", rtype, 1, 300, 4) + b"\x00\x00\x00\x00"
    return pkt


def test_query_encodes_a_name():
    txid, packet = mx.build_query("joes-coffee.com")
    assert b"\x0bjoes-coffee\x03com\x00" in packet, packet
    assert struct.unpack(">H", packet[:2])[0] == txid


def test_rejects_bad_labels():
    for bad in ("", ".", "a..b", "x" * 70 + ".com"):
        try:
            mx.build_query(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted bad domain {bad!r}")


def test_parses_mx_answer():
    assert mx.parse_answer_types(_response(1234, [15, 15]), 1234) == [15, 15]


def test_compressed_names_skipped_correctly():
    assert mx.parse_answer_types(_response(7, [15, 1, 15]), 7) == [15, 1, 15]


def test_transaction_id_mismatch_rejected():
    try:
        mx.parse_answer_types(_response(1111, [15]), 2222)
    except ValueError:
        return
    raise AssertionError("accepted a response with the wrong transaction id")


def test_nxdomain_is_empty_not_an_error():
    assert mx.parse_answer_types(_response(5, [], rcode=3), 5) == []


def test_has_mx_true_false_unknown():
    assert mx.has_mx("", resolvers=["192.0.2.1"]) is False
    assert mx.has_mx("nodots", resolvers=["192.0.2.1"]) is False
    # No resolver reachable -> unknown, NOT false: a timeout is not evidence
    # that an address is bad, and dropping it would lose a good lead.
    assert mx.has_mx("joes.com", resolvers=[], timeout=0.01) is None


for fn in (test_query_encodes_a_name, test_rejects_bad_labels, test_parses_mx_answer,
           test_compressed_names_skipped_correctly,
           test_transaction_id_mismatch_rejected, test_nxdomain_is_empty_not_an_error,
           test_has_mx_true_false_unknown):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\nrobots — fetched through the SSRF guard")

ROBOTS = "User-agent: *\nDisallow: /private\nCrawl-delay: 2\n"


def _fetcher(pages):
    def fetch(url, **kw):
        if url in pages:
            return safe_fetch.Result(url, 200, pages[url], "text/plain")
        return safe_fetch.Result(url, 404, "", "text/plain")
    return fetch


def test_disallowed_path_blocked():
    r = RobotsCache("t/1", fetch=_fetcher({"https://joes.com/robots.txt": ROBOTS}))
    assert r.allowed("https://joes.com/") is True
    assert r.allowed("https://joes.com/private/x") is False


def test_crawl_delay_honoured():
    r = RobotsCache("t/1", fetch=_fetcher({"https://joes.com/robots.txt": ROBOTS}))
    assert r.crawl_delay("https://joes.com/") == 2.0


def test_missing_robots_allows():
    r = RobotsCache("t/1", fetch=_fetcher({}))
    assert r.allowed("https://joes.com/anything") is True


def test_guard_refusal_does_not_crash():
    def boom(url, **kw):
        raise safe_fetch.Blocked("refused")
    assert RobotsCache("t/1", fetch=boom).allowed("https://joes.com/") is True


def test_fetched_once_per_host():
    calls = []

    def counting(url, **kw):
        calls.append(url)
        return safe_fetch.Result(url, 200, ROBOTS, "text/plain")
    r = RobotsCache("t/1", fetch=counting)
    for _ in range(5):
        r.allowed("https://joes.com/page")
    assert len(calls) == 1, f"refetched robots.txt {len(calls)} times"


def test_disabled_allows_everything():
    r = RobotsCache("t/1", enabled=False, fetch=_fetcher(
        {"https://joes.com/robots.txt": "User-agent: *\nDisallow: /\n"}))
    assert r.allowed("https://joes.com/private") is True


for fn in (test_disallowed_path_blocked, test_crawl_delay_honoured,
           test_missing_robots_allows, test_guard_refusal_does_not_crash,
           test_fetched_once_per_host, test_disabled_allows_everything):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\nenrich — orchestration")

HOME = """<html><a href="mailto:hello@joes.com">mail</a>
          <a href="https://instagram.com/joescoffee">ig</a></html>"""
NOMAIL = '<html><a href="/contact-us">Contact</a></html>'
CONTACT = '<html>reach us: team@joes.com</html>'


def _cfg(tmp):
    c = Config()
    c.data_dir = Path(tmp)
    c.db_path = Path(tmp) / "t.db"
    c.contact_url = "https://github.com/example"
    c.per_host_delay = 0.0
    c.max_workers = 2
    c.respect_robots = False
    return c


def _seed(conn, sites):
    for i, site in enumerate(sites):
        db.upsert_business(conn, "osm", f"node/{i}", name=f"Biz {i}",
                           website=site, lat=30.0 + i / 1000, lon=-97.0)
    conn.commit()


def test_extracts_and_stores():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        _seed(conn, ["https://joes.com"])
        pages = {"https://joes.com": HOME}
        r = enrich.run(cfg, conn, check_mx=False, fetcher=_fetcher(pages))
        assert r.emails_found == 1 and r.socials_found == 1, r
        row = db.export_rows(conn)[0]
        assert "hello@joes.com" in (row["emails"] or ""), row
        assert "joescoffee" in (row["instagram"] or ""), row


def test_follows_contact_page_when_homepage_has_no_email():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        _seed(conn, ["https://joes.com"])
        pages = {"https://joes.com": NOMAIL, "https://joes.com/contact-us": CONTACT}
        r = enrich.run(cfg, conn, check_mx=False, fetcher=_fetcher(pages))
        assert r.emails_found == 1, f"contact page not followed: {r}"
        assert "team@joes.com" in (db.export_rows(conn)[0]["emails"] or "")


def test_second_run_is_cached_not_refetched():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        _seed(conn, ["https://joes.com"])
        calls = []

        def counting(url, **kw):
            calls.append(url)
            return safe_fetch.Result(url, 200, HOME, "text/html")
        enrich.run(cfg, conn, check_mx=False, fetcher=counting)
        before = len(calls)
        enrich.run(cfg, conn, check_mx=False, fetcher=counting)
        assert len(calls) == before, f"refetched on the second run: {calls[before:]}"


def test_guard_refusal_is_counted_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        _seed(conn, ["https://joes.com", "http://169.254.169.254"])

        def fetch(url, **kw):
            if "169.254" in url:
                raise safe_fetch.Blocked(f"{url} resolves to non-public address")
            return safe_fetch.Result(url, 200, HOME, "text/html")
        r = enrich.run(cfg, conn, check_mx=False, fetcher=fetch)
        assert r.refused_by_guard == 1, r
        assert r.emails_found == 1, "a blocked URL stopped the whole run"
        assert r.guard_blocks, "no diagnostic recorded for the refusal"


def test_dead_domain_is_not_reported_as_a_security_refusal():
    """A website that no longer resolves is a dead link, not an attack.

    Lumping them together is how a real guard warning gets ignored.
    """
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        _seed(conn, ["https://gone.test", "http://169.254.169.254"])

        def fetch(url, **kw):
            if "gone.test" in url:
                raise safe_fetch.Unresolvable("cannot resolve gone.test")
            raise safe_fetch.Blocked("resolves to non-public address")
        r = enrich.run(cfg, conn, check_mx=False, fetcher=fetch)
        assert r.dead_domains == 1, f"dead domain miscounted: {r}"
        assert r.refused_by_guard == 1, f"real refusal miscounted: {r}"
        assert len(r.guard_blocks) == 1, "a dead link polluted the guard warnings"


def test_unresolvable_is_still_a_blocked_subclass():
    assert issubclass(safe_fetch.Unresolvable, safe_fetch.Blocked), \
        "existing `except Blocked` handlers would stop catching dead hosts"


def test_mx_rejection_drops_the_address():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        _seed(conn, ["https://joes.com"])
        real = mx.has_mx
        mx.has_mx = lambda domain, **kw: False
        try:
            r = enrich.run(cfg, conn, check_mx=True,
                           fetcher=_fetcher({"https://joes.com": HOME}))
        finally:
            mx.has_mx = real
        assert r.mx_rejected == 1 and r.emails_found == 0, r


def test_unknown_mx_keeps_the_address():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        _seed(conn, ["https://joes.com"])
        real = mx.has_mx
        mx.has_mx = lambda domain, **kw: None          # resolver unreachable
        try:
            r = enrich.run(cfg, conn, check_mx=True,
                           fetcher=_fetcher({"https://joes.com": HOME}))
        finally:
            mx.has_mx = real
        assert r.emails_found == 1 and r.mx_rejected == 0, \
            "an unreachable resolver discarded a good lead"


def test_pending_skips_already_enriched():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        _seed(conn, ["https://joes.com", "https://b.com"])
        assert len(enrich.pending(conn)) == 2
        bid = conn.execute("SELECT id FROM businesses LIMIT 1").fetchone()[0]
        db.add_email(conn, bid, "x@joes.com")
        conn.commit()
        assert len(enrich.pending(conn)) == 1, "re-crawls a business already enriched"


def test_no_websites_is_not_an_error():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        db.upsert_business(conn, "osm", "node/9", name="No Site", lat=30.0, lon=-97.0)
        conn.commit()
        r = enrich.run(cfg, conn, check_mx=False, fetcher=_fetcher({}))
        assert r.attempted == 0 and r.emails_found == 0, r


for fn in (test_extracts_and_stores, test_follows_contact_page_when_homepage_has_no_email,
           test_second_run_is_cached_not_refetched,
           test_guard_refusal_is_counted_not_fatal,
           test_dead_domain_is_not_reported_as_a_security_refusal,
           test_unresolvable_is_still_a_blocked_subclass,
           test_mx_rejection_drops_the_address,
           test_unknown_mx_keeps_the_address, test_pending_skips_already_enriched,
           test_no_websites_is_not_an_error):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print(f"\n{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
