"""Tier 3: backfill ratings and review counts from Google Maps. OPT-IN, OFF.

OpenStreetMap has no ratings, and for qualifying a lead they matter. This is
the only part of the project that touches Google, and it is deliberately the
smallest possible surface:

  * it never discovers anything — Tiers 1 and 2 build the list, this adds two
    numbers to rows that already exist;
  * it runs one job per category rather than one per business, so a city is a
    handful of requests instead of hundreds;
  * it is gated twice, by `LLI_TIER3_ENABLED` and by an explicit flag on the
    command, because scraping Maps violates Google's Terms of Service and
    puts the operator's IP at risk, not ours.

The engine is `gosom/google-maps-scraper` running locally behind a compose
profile. Its API is async: create a job, poll until Status is "ok", download
a CSV.
"""
from __future__ import annotations

import csv
import io
import json
import math
import time
from dataclasses import dataclass, field

from core import db, http

POLL_SECONDS = 8
MAX_POLLS = 120


class Tier3Error(Exception):
    pass


class Tier3Disabled(Tier3Error):
    """Refused because the operator has not switched Tier 3 on."""


@dataclass
class RatingsResult:
    scraped: int = 0
    matched: int = 0
    updated: int = 0
    unmatched: list = field(default_factory=list)
    job_id: str = ""
    seconds: float = 0.0


def _headers(cfg):
    # The engine ships no authentication; the key only exists for setups that
    # put an authenticating proxy in front of it.
    return {"X-API-Key": cfg.scraper_api_key} if cfg.scraper_api_key else {}


def health(cfg, *, client=http) -> bool:
    try:
        client.request(f"{cfg.scraper_base_url}/api/v1/jobs", user_agent=cfg.user_agent,
                       headers=_headers(cfg), timeout=10.0, retries=0)
        return True
    except http.HttpError:
        return False


def create_job(cfg, keyword, lat, lon, *, depth=5, max_time=600, client=http) -> str:
    body = {
        "name": "lli-tier3",
        "keywords": [keyword],
        "lang": "en",
        "zoom": 15,
        # The API 422s unless lat/lon are STRINGS and max_time is SECONDS.
        "lat": str(lat),
        "lon": str(lon),
        "fast_mode": False,
        "radius": 10000,
        "depth": depth,
        "email": False,            # Tier 2 already did this, politely and cached
        "max_time": max_time,
    }
    response = client.request(
        f"{cfg.scraper_base_url}/api/v1/jobs", method="POST",
        data=json.dumps(body),
        headers={**_headers(cfg), "Content-Type": "application/json"},
        user_agent=cfg.user_agent, timeout=30.0, retries=1)
    job_id = (response.json() or {}).get("id")
    if not job_id:
        raise Tier3Error(f"engine returned no job id: {response.text[:200]}")
    return job_id


def wait_for(cfg, job_id, *, client=http, sleep=time.sleep, max_polls=MAX_POLLS) -> None:
    for _ in range(max_polls):
        response = client.request(
            f"{cfg.scraper_base_url}/api/v1/jobs/{job_id}", headers=_headers(cfg),
            user_agent=cfg.user_agent, timeout=30.0, retries=1)
        status = (response.json() or {}).get("Status")     # capital S, per the API
        if status == "ok":
            return
        if status == "failed":
            raise Tier3Error(
                "the engine reported the job failed. Repeated failures usually mean "
                "Google is rate-limiting this IP — wait, lower the depth, or add "
                "proxies to the job body.")
        sleep(POLL_SECONDS)
    raise Tier3Error(f"job {job_id} did not finish in "
                     f"{max_polls * POLL_SECONDS}s")


def download(cfg, job_id, *, client=http) -> list:
    response = client.request(
        f"{cfg.scraper_base_url}/api/v1/jobs/{job_id}/download",
        headers=_headers(cfg), user_agent=cfg.user_agent, timeout=120.0, retries=1)
    text = response.body.decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(text)))


def delete_job(cfg, job_id, *, client=http) -> None:
    try:
        client.request(f"{cfg.scraper_base_url}/api/v1/jobs/{job_id}", method="DELETE",
                       headers=_headers(cfg), user_agent=cfg.user_agent,
                       timeout=30.0, retries=0)
    except http.HttpError:
        pass                        # disk housekeeping, not worth failing a run over


# ── matching ────────────────────────────────────────────────────────────────

def _metres(lat1, lon1, lat2, lon2) -> float:
    """Haversine. Good to well under a metre at these distances."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def match_business(candidates, name, lat, lon, *, max_metres=250):
    """Find the existing business a scraped row refers to.

    Deliberately NOT a dedupe_key comparison. The key buckets coordinates into
    a ~110m grid, and Google and OSM routinely place the same shop tens of
    metres apart — enough to straddle a cell boundary and miss. Matching on
    normalised name plus real distance tolerates that.
    """
    if lat is None or lon is None:
        return None
    target = db.normalize_name(name)
    if not target:
        return None
    best, best_distance = None, max_metres
    for row in candidates:
        if row["lat"] is None or row["lon"] is None:
            continue
        if db.normalize_name(row["name"]) != target:
            continue
        distance = _metres(lat, lon, row["lat"], row["lon"])
        if distance <= best_distance:
            best, best_distance = row, distance
    return best


def _number(value, cast):
    try:
        text = (value or "").strip().replace(",", "")
        return cast(text) if text else None
    except (TypeError, ValueError):
        return None


def apply_ratings(conn, rows, *, max_metres=250) -> RatingsResult:
    """Attach rating and review_count to businesses we already have."""
    result = RatingsResult(scraped=len(rows))
    candidates = [dict(r) for r in conn.execute(
        "SELECT id, name, lat, lon, source, source_id FROM businesses")]

    for row in rows:
        name = (row.get("title") or "").strip()
        rating = _number(row.get("review_rating"), float)
        reviews = _number(row.get("review_count"), int)
        if not name or (rating is None and reviews is None):
            continue
        lat = _number(row.get("latitude"), float)
        lon = _number(row.get("longitude"), float)
        hit = match_business(candidates, name, lat, lon, max_metres=max_metres)
        if hit is None:
            if len(result.unmatched) < 20:
                result.unmatched.append(name)
            continue
        result.matched += 1
        # Only the two fields Tier 3 exists for. upsert_business merges rather
        # than replaces, so nothing Tier 1 or 2 found gets overwritten.
        db.upsert_business(conn, hit["source"], hit["source_id"], name=hit["name"],
                           rating=rating, review_count=reviews)
        result.updated += 1
    conn.commit()
    return result


def run(cfg, conn, keyword, place, *, depth=5, confirmed=False, client=http,
        sleep=time.sleep) -> RatingsResult:
    """One Tier 3 pass. Refuses unless explicitly enabled AND confirmed."""
    if not cfg.tier3_enabled:
        raise Tier3Disabled(
            "Tier 3 is off. It scrapes Google Maps, which violates their Terms of "
            "Service and risks your IP being rate-limited. Set LLI_TIER3_ENABLED=true "
            "in .env if you want it.")
    if not confirmed:
        raise Tier3Disabled("Tier 3 needs --yes on the command as well as the "
                            "setting — two gates, deliberately.")
    if not health(cfg, client=client):
        raise Tier3Error(
            f"no engine at {cfg.scraper_base_url}. Start it with: "
            "docker compose --profile tier3 up -d")

    started = time.monotonic()
    job_id = create_job(cfg, keyword, place.lat, place.lon, depth=depth, client=client)
    try:
        wait_for(cfg, job_id, client=client, sleep=sleep)
        rows = download(cfg, job_id, client=client)
    finally:
        delete_job(cfg, job_id, client=client)
    result = apply_ratings(conn, rows)
    result.job_id = job_id
    result.seconds = time.monotonic() - started
    return result
