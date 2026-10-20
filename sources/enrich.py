"""Tier 2: turn a directory listing into a contactable lead list.

Visits each business's own website — public, first-party data — and extracts
emails and social profiles, then validates the email domains over DNS.

Three properties make this sustainable rather than a one-shot scrape:

  * every fetch goes through `core.safe_fetch`, because these URLs came out of
    scraped data and are exactly the attacker-controlled input its SSRF guard
    exists for;
  * every fetch is cached in SQLite, so a second run costs almost nothing —
    which is what makes "free" hold up over time;
  * one host is never hit by two workers at once, and never faster than the
    configured delay.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from core import db, extract, mx, safe_fetch
from core.robots import RobotsCache


@dataclass
class EnrichResult:
    attempted: int = 0
    fetched: int = 0
    from_cache: int = 0
    blocked_by_robots: int = 0
    refused_by_guard: int = 0
    dead_domains: int = 0
    failed: int = 0
    emails_found: int = 0
    socials_found: int = 0
    mx_checked: int = 0
    mx_rejected: int = 0
    seconds: float = 0.0
    guard_blocks: list = field(default_factory=list)


class HostThrottle:
    """Serialises each host and spaces its requests."""

    def __init__(self, delay=1.0):
        self.delay = delay
        self._locks = defaultdict(threading.Lock)
        self._last = {}
        self._guard = threading.Lock()

    def _lock_for(self, host):
        with self._guard:
            return self._locks[host]

    def wait(self, host, delay=None):
        gap = self.delay if delay is None else max(self.delay, delay)
        lock = self._lock_for(host)
        lock.acquire()
        last = self._last.get(host)
        if last is not None:
            remaining = gap - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        return lock

    def done(self, host, lock):
        self._last[host] = time.monotonic()
        lock.release()


def pending(conn, *, limit=None, recheck_days=None):
    """Businesses with a website that have not been successfully enriched.

    Skips anything that already produced an email, so a re-run is incremental
    by default rather than a full re-crawl.
    """
    sql = """
        SELECT b.id, b.name, b.website, b.website_domain
        FROM businesses b
        WHERE b.website IS NOT NULL AND b.website <> ''
          AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.business_id = b.id)
    """
    params = []
    if recheck_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=recheck_days)
                  ).isoformat(timespec="seconds")
        sql += """ AND NOT EXISTS (
                     SELECT 1 FROM fetch_cache c
                     WHERE c.url = b.website AND c.fetched_at > ?)"""
        params.append(cutoff)
    sql += " ORDER BY b.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params)]


def _cache_get(conn, url, ttl_days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)
              ).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT status, error FROM fetch_cache WHERE url_hash = ? AND fetched_at > ?",
        (db.url_hash(url), cutoff)).fetchone()


def _cache_put(conn, url, status, error=""):
    conn.execute(
        "INSERT INTO fetch_cache (url_hash, url, status, error, fetched_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(url_hash) DO UPDATE SET status=excluded.status,"
        " error=excluded.error, fetched_at=excluded.fetched_at",
        (db.url_hash(url), url, status, error, db.now()))


def _enrich_one(biz, *, cfg, robots, throttle, fetcher, result, lock):
    """Fetch one business's site (plus up to two contact pages) and extract."""
    url = biz["website"]
    host = urlsplit(url).hostname or ""
    pages, harvest_emails, harvest_socials = [url], [], {}

    for page in pages:
        if not robots.allowed(page):
            with lock:
                result.blocked_by_robots += 1
            continue
        host_lock = throttle.wait(host, robots.crawl_delay(page))
        response = None
        try:
            response = fetcher(page, timeout=cfg.http_timeout,
                               max_bytes=cfg.http_max_bytes,
                               user_agent=cfg.user_agent)
        except safe_fetch.Unresolvable:
            # A dead domain, not a threat. Counted as an ordinary failure so
            # the guard's warnings stay meaningful.
            with lock:
                result.dead_domains += 1
        except safe_fetch.Blocked as e:
            with lock:
                result.refused_by_guard += 1
                if len(result.guard_blocks) < 10:
                    result.guard_blocks.append(f"{host}: {e}")
        except Exception:                            # noqa: BLE001 — network is messy
            with lock:
                result.failed += 1
        finally:
            # One release, on every path. Releasing in each branch instead is
            # how a lock ends up double-released or held forever.
            throttle.done(host, host_lock)
        if response is None:
            continue

        with lock:
            result.fetched += 1
        if response.status != 200 or not response.text:
            continue

        harvest_emails += extract.find_emails(response.text, biz["website_domain"] or "")
        for platform, info in extract.find_socials(response.text).items():
            harvest_socials.setdefault(platform, info)

        # Only chase a contact page if the homepage gave us nothing.
        if page is url and not harvest_emails:
            pages += extract.find_contact_links(response.text, url, limit=2)

    return harvest_emails, harvest_socials


def run(cfg, conn, *, limit=None, workers=None, check_mx=True,
        fetcher=safe_fetch.fetch) -> EnrichResult:
    candidates = pending(conn, limit=limit)
    result = EnrichResult(attempted=len(candidates))
    if not candidates:
        return result

    # Cache filtering happens HERE, on the main thread. A sqlite3 connection
    # belongs to the thread that made it and raises if used from another, so
    # the worker must never touch `conn`.
    targets = []
    for biz in candidates:
        if _cache_get(conn, biz["website"], cfg.cache_ttl_days) is not None:
            result.from_cache += 1
        else:
            targets.append(biz)
    if not targets:
        return result

    started = time.monotonic()
    robots = RobotsCache(cfg.user_agent, enabled=cfg.respect_robots, fetch=fetcher)
    throttle = HostThrottle(cfg.per_host_delay)
    lock = threading.Lock()
    mx_cache = {}

    from concurrent.futures import ThreadPoolExecutor

    def work(biz):
        """Runs on a pool thread: network and parsing only, never the database."""
        emails, socials = _enrich_one(
            biz, cfg=cfg, robots=robots, throttle=throttle,
            fetcher=fetcher, result=result, lock=lock)
        return biz, emails, socials

    with ThreadPoolExecutor(max_workers=workers or cfg.max_workers) as pool:
        done = 0
        for biz, emails, socials in pool.map(work, targets):
            done += 1
            if sys.stdout.isatty():          # progress is for a human, not a pipe
                print(f"\r  {done}/{len(targets)} sites · "
                      f"{result.emails_found} emails · {result.socials_found} socials",
                      end="", flush=True)

            kept = []
            for item in emails:
                domain = item["email"].partition("@")[2]
                if check_mx:
                    if domain not in mx_cache:
                        mx_cache[domain] = mx.has_mx(domain)
                        result.mx_checked += 1
                    ok = mx_cache[domain]
                    if ok is False:
                        result.mx_rejected += 1
                        continue
                    item["mx_ok"] = 1 if ok else None
                kept.append(item)

            for item in kept[:5]:                    # a lead needs contacts, not a list
                if db.add_email(conn, biz["id"], item["email"], kind=item["kind"],
                                mx_ok=item.get("mx_ok"), confidence=item["confidence"],
                                source_url=biz["website"]):
                    result.emails_found += 1
            for platform, info in socials.items():
                if db.add_social(conn, biz["id"], platform, info["url"], info["handle"]):
                    result.socials_found += 1
            _cache_put(conn, biz["website"], 200 if kept or socials else 204)
            conn.commit()

    if sys.stdout.isatty():
        print()
    result.seconds = time.monotonic() - started
    return result
