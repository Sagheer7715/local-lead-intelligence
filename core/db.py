"""SQLite as the system of record.

The kit this replaces wrote loose CSVs into the repo root. That one choice cost
it dedupe (its own skill tells you to dedupe on `place_id`, which it strips from
the output first), resumability, caching, incremental runs, and any way to honour
an opt-out. A single-file database fixes all of them, with no dependency and no
server.

Nothing here is destructive except `purge()` and `delete_business()`, both of
which are explicit.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 2: dedupe_key now includes location, so chain branches are separate leads.
#    Databases written under v1 carry keys that merge them and must be re-keyed.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,              -- 'osm' | 'gmaps'
    source_id      TEXT NOT NULL,              -- 'node/123456' | a Maps place_id
    dedupe_key     TEXT NOT NULL,              -- cross-source identity
    name           TEXT NOT NULL,
    category       TEXT,
    address        TEXT,
    city           TEXT,
    region         TEXT,
    country        TEXT,
    lat            REAL,
    lon            REAL,
    phone_raw      TEXT,
    phone_e164     TEXT,
    website        TEXT,
    website_domain TEXT,
    rating         REAL,                       -- tier 3 only; NULL from OSM
    review_count   INTEGER,                    -- tier 3 only; NULL from OSM
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_biz_dedupe ON businesses (dedupe_key);
CREATE INDEX IF NOT EXISTS idx_biz_domain ON businesses (website_domain);
CREATE INDEX IF NOT EXISTS idx_biz_city   ON businesses (city);

CREATE TABLE IF NOT EXISTS emails (
    id          INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    local_part  TEXT,
    domain      TEXT,
    kind        TEXT,            -- 'role' (info@) | 'personal' (jane@) | 'unknown'
    mx_ok       INTEGER,         -- 1 | 0 | NULL if not yet checked
    confidence  REAL,
    source_url  TEXT,
    found_at    TEXT NOT NULL,
    UNIQUE (business_id, email)
);
CREATE INDEX IF NOT EXISTS idx_emails_biz ON emails (business_id);

CREATE TABLE IF NOT EXISTS socials (
    id          INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses (id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,   -- 'instagram' | 'facebook' | 'linkedin'
    url         TEXT NOT NULL,
    handle      TEXT,
    found_at    TEXT NOT NULL,
    UNIQUE (business_id, platform)
);

-- The free-throughput win: never fetch the same site twice in a TTL window.
CREATE TABLE IF NOT EXISTS fetch_cache (
    url_hash      TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    status        INTEGER,
    etag          TEXT,
    last_modified TEXT,
    body_sha256   TEXT,
    error         TEXT,
    fetched_at    TEXT NOT NULL
);

-- Run history, so degradation is detectable instead of something you squint at.
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY,
    query           TEXT NOT NULL,
    source          TEXT NOT NULL,
    area            TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    rows_found      INTEGER DEFAULT 0,
    rows_new        INTEGER DEFAULT 0,
    blocked_signals INTEGER DEFAULT 0,
    notes           TEXT
);

-- Checked before every export. An opt-out has to be permanent to be real,
-- and GDPR/CCPA require it to survive the next scrape.
CREATE TABLE IF NOT EXISTS suppression (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL,      -- 'email' | 'phone' | 'domain'
    value    TEXT NOT NULL,
    reason   TEXT,
    added_at TEXT NOT NULL,
    UNIQUE (kind, value)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path) -> sqlite3.Connection:
    """Open (creating if needed) and migrate the database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(str(path), timeout=30.0)
    if fresh:
        # The file holds personal data. Lock it down before anything is written.
        os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    _migrate(conn)
    return conn


def rekey_all(conn) -> int:
    """Recompute every dedupe_key with the current rules. Returns rows changed."""
    changed = 0
    for row in conn.execute("SELECT * FROM businesses").fetchall():
        key = dedupe_key(row["name"] or "", row["website"] or "",
                         row["phone_e164"] or "", row["city"] or "",
                         row["address"] or "", row["lat"], row["lon"])
        if key != row["dedupe_key"]:
            conn.execute("UPDATE businesses SET dedupe_key = ? WHERE id = ?",
                         (key, row["id"]))
            changed += 1
    conn.commit()
    return changed


def _migrate(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    version = int(row["value"]) if row else SCHEMA_VERSION
    if version >= SCHEMA_VERSION:
        return
    if version < 2:
        # v1 keyed on domain alone, collapsing every branch of a chain into one
        # lead. Re-key in place; no data is lost, rows simply regroup.
        changed = rekey_all(conn)
        if changed:
            print(f"  (migrated to schema v2: re-keyed {changed} businesses so "
                  "chain branches are separate leads)")
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                 (str(SCHEMA_VERSION),))
    conn.commit()


# ── identity ────────────────────────────────────────────────────────────────

_WS = re.compile(r"\s+")
_NOISE = re.compile(r"[^a-z0-9 ]+")
_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|co|corp|corporation|gmbh|bv|plc|pllc|pc|lp|llp)\b")


def normalize_name(name: str) -> str:
    # Apostrophes are deleted rather than spaced out, so "Joe's Coffee" and
    # "Joes Coffee" land on the same key instead of "joe s" vs "joes".
    s = (name or "").lower().replace("'", "").replace("’", "")
    s = _NOISE.sub(" ", s)
    s = _SUFFIX.sub(" ", s)
    return _WS.sub(" ", s).strip()


def normalize_domain(website: str) -> str:
    """Bare registrable-ish host: no scheme, no www, no port, no path."""
    s = (website or "").strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    s = s.split("@")[-1].split(":")[0]
    return s[4:] if s.startswith("www.") else s


def location_cell(lat=None, lon=None, address: str = "", city: str = "") -> str:
    """The narrowest location identifier available for a business.

    Coordinates come first because every source emits them in the same format,
    while address strings vary wildly between OSM and anything else.

    Three decimal places is roughly a 110m cell: finer than the gap between two
    branches of a chain, coarser than the jitter between two mappings of the
    same building (a node and a building outline).
    """
    if lat is not None and lon is not None:
        try:
            return f"g:{float(lat):.3f},{float(lon):.3f}"
        except (TypeError, ValueError):
            pass
    addr = normalize_name(address)
    if addr:
        return "a:" + addr
    return "c:" + normalize_name(city)


def dedupe_key(name: str, website: str = "", phone_e164: str = "", city: str = "",
               address: str = "", lat=None, lon=None) -> str:
    """Stable cross-source identity for one business AT ONE LOCATION.

    Location is part of every key, never a tiebreaker. An earlier version keyed
    on domain alone, which merged 42 Starbucks branches into a single lead and —
    worse — merged six unrelated campus cafés that happened to share their
    landlord's domain. A shared domain is evidence of a shared *operator*, not
    of being the same business.

    Within a location, domain is still the best cross-source match, then phone,
    then the name. Hashed so the key is fixed-width and carries no PII into
    indexes or logs.
    """
    cell = location_cell(lat, lon, address, city)
    domain = normalize_domain(website)
    if domain:
        basis = "d:" + domain + "|" + cell
    elif phone_e164:
        basis = "p:" + re.sub(r"\D", "", phone_e164) + "|" + cell
    else:
        basis = "n:" + normalize_name(name) + "|" + cell
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


# ── writes ──────────────────────────────────────────────────────────────────

_BIZ_FIELDS = ("name", "category", "address", "city", "region", "country",
               "lat", "lon", "phone_raw", "phone_e164", "website",
               "website_domain", "rating", "review_count")


def upsert_business(conn, source: str, source_id: str, **fields) -> tuple:
    """Insert or refresh one business. Returns (id, created).

    An existing row keeps `first_seen` and only takes non-empty incoming values,
    so a Tier 3 ratings pass can add rating/review_count without blanking the
    contact data Tier 2 found.
    """
    if not source or not source_id:
        raise ValueError("source and source_id are required")
    name = (fields.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")

    fields["website_domain"] = normalize_domain(fields.get("website") or "")
    ts = now()

    row = conn.execute(
        "SELECT * FROM businesses WHERE source = ? AND source_id = ?",
        (source, source_id)).fetchone()

    # The key must come from the MERGED record. A later pass that carries only
    # a rating has no coordinates, and recomputing from the incoming fields
    # alone would silently re-key the business to a coarser location.
    merged = dict(row) if row is not None else {}
    for f in _BIZ_FIELDS:
        if fields.get(f) not in (None, ""):
            merged[f] = fields[f]
    merged["name"] = name
    key = dedupe_key(merged.get("name") or "", merged.get("website") or "",
                     merged.get("phone_e164") or "", merged.get("city") or "",
                     merged.get("address") or "", merged.get("lat"), merged.get("lon"))

    if row is None:
        cols = ["source", "source_id", "dedupe_key", "first_seen", "last_seen"]
        vals = [source, source_id, key, ts, ts]
        for f in _BIZ_FIELDS:
            cols.append(f)
            vals.append(fields.get(f))
        placeholders = ", ".join("?" * len(cols))
        cur = conn.execute(
            f"INSERT INTO businesses ({', '.join(cols)}) VALUES ({placeholders})", vals)
        return cur.lastrowid, True

    sets, vals = ["last_seen = ?", "dedupe_key = ?"], [ts, key]
    for f in _BIZ_FIELDS:
        v = fields.get(f)
        if v not in (None, ""):
            sets.append(f"{f} = ?")
            vals.append(v)
    vals.append(row["id"])
    conn.execute(f"UPDATE businesses SET {', '.join(sets)} WHERE id = ?", vals)
    return row["id"], False


def add_email(conn, business_id: int, email: str, **fields) -> bool:
    """Record an email. Returns True if it was new."""
    addr = (email or "").strip().lower()
    if not addr or "@" not in addr:
        return False
    local, _, domain = addr.rpartition("@")
    cur = conn.execute(
        "INSERT OR IGNORE INTO emails (business_id, email, local_part, domain, kind,"
        " mx_ok, confidence, source_url, found_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (business_id, addr, local, domain, fields.get("kind"), fields.get("mx_ok"),
         fields.get("confidence"), fields.get("source_url"), now()))
    return cur.rowcount > 0


def add_social(conn, business_id: int, platform: str, url: str, handle=None) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO socials (business_id, platform, url, handle, found_at)"
        " VALUES (?,?,?,?,?)", (business_id, platform, url, handle, now()))
    return cur.rowcount > 0


def suppress(conn, kind: str, value: str, reason: str = "") -> None:
    """Add a permanent do-not-contact entry."""
    if kind not in ("email", "phone", "domain"):
        raise ValueError(f"kind must be email, phone or domain, got {kind!r}")
    norm = value.strip().lower()
    if kind == "phone":
        norm = re.sub(r"\D", "", norm)
    elif kind == "domain":
        norm = normalize_domain(norm)
    conn.execute(
        "INSERT OR IGNORE INTO suppression (kind, value, reason, added_at)"
        " VALUES (?,?,?,?)", (kind, norm, reason, now()))
    conn.commit()


# ── reads ───────────────────────────────────────────────────────────────────

# One row per business, deduped across sources, suppressed entries removed.
# The emails are aggregated so a lead is one line in a spreadsheet.
EXPORT_SQL = """
WITH ranked AS (
    SELECT b.*,
           ROW_NUMBER() OVER (
               PARTITION BY b.dedupe_key
               ORDER BY (b.phone_e164 IS NOT NULL) DESC,
                        (b.website IS NOT NULL) DESC,
                        b.last_seen DESC
           ) AS rn
    FROM businesses b
    WHERE NOT EXISTS (
            SELECT 1 FROM suppression s
            WHERE (s.kind = 'domain' AND s.value = b.website_domain)
               OR (s.kind = 'phone'  AND s.value = REPLACE(REPLACE(REPLACE(
                        REPLACE(COALESCE(b.phone_e164, ''), '+', ''),
                        '-', ''), ' ', ''), '.', ''))
          )
)
SELECT r.id, r.name AS title, r.category, r.address, r.city,
       r.phone_e164 AS phone, r.website, r.website_domain,
       r.rating AS review_rating, r.review_count,
       (SELECT GROUP_CONCAT(e.email, '; ') FROM emails e
         WHERE e.business_id = r.id
           AND NOT EXISTS (SELECT 1 FROM suppression s
                           WHERE s.kind = 'email' AND s.value = e.email)
       ) AS emails,
       (SELECT s.url FROM socials s WHERE s.business_id = r.id AND s.platform='instagram') AS instagram,
       (SELECT s.url FROM socials s WHERE s.business_id = r.id AND s.platform='facebook')  AS facebook,
       (SELECT s.url FROM socials s WHERE s.business_id = r.id AND s.platform='linkedin')  AS linkedin,
       r.source, r.last_seen
FROM ranked r
WHERE r.rn = 1
ORDER BY r.name
"""

LEAD_FIELDS = ("title", "phone", "emails", "website", "category", "address",
               "review_rating", "review_count")


def export_rows(conn, city=None):
    rows = [dict(r) for r in conn.execute(EXPORT_SQL)]
    if city:
        want = normalize_name(city)
        rows = [r for r in rows if normalize_name(r.get("city") or "") == want]
    return rows


def counts(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "businesses": q("SELECT COUNT(*) FROM businesses"),
        "unique": q("SELECT COUNT(DISTINCT dedupe_key) FROM businesses"),
        "with_phone": q("SELECT COUNT(*) FROM businesses WHERE phone_e164 IS NOT NULL"),
        "with_website": q("SELECT COUNT(*) FROM businesses WHERE website IS NOT NULL"),
        "emails": q("SELECT COUNT(*) FROM emails"),
        "socials": q("SELECT COUNT(*) FROM socials"),
        "suppressed": q("SELECT COUNT(*) FROM suppression"),
        "cached_fetches": q("SELECT COUNT(*) FROM fetch_cache"),
    }


# ── retention ───────────────────────────────────────────────────────────────

def purge(conn, older_than_days: int) -> dict:
    """Delete businesses not seen in N days, plus stale cache rows.

    Destructive and explicit. Suppression entries are never purged — that is
    the point of them.
    """
    if older_than_days < 1:
        raise ValueError("older_than_days must be >= 1")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)
              ).isoformat(timespec="seconds")
    biz = conn.execute("DELETE FROM businesses WHERE last_seen < ?", (cutoff,)).rowcount
    cache = conn.execute("DELETE FROM fetch_cache WHERE fetched_at < ?", (cutoff,)).rowcount
    conn.commit()
    conn.execute("VACUUM")
    return {"businesses_deleted": biz, "cache_deleted": cache, "cutoff": cutoff}
