# local-lead-intelligence

Local business lead lists — free to run, built on open data, hardened against the
URLs it ingests.

Name a business type and a place; get back a deduplicated, contactable lead list
with phone, email, website and socials. Everything runs on your machine. No API
keys, no SaaS, no proxy bill.

> **Status: Phase 2 of 4.** Discovery works end to end — a live run returned 119
> Austin dentists (112 after dedupe) in 6 seconds, free. 143 tests. The
> enrichment crawl lands in Phase 3 — see [Roadmap](#roadmap).

## Why it is built this way

Most tools in this space make **Google Maps the primary source**, and every
problem follows from that one choice: your home IP is the consumable, blocks are
silent and look like "fewer results today", the data can't be legally retained,
and the only real fix — proxies — costs money. You cannot engineer around that
from the inside.

So this inverts it:

| | Source | Cost | Legal posture | Rate limits |
|---|---|---|---|---|
| **Tier 1 — discovery** | OpenStreetMap via Overpass | free | open data (ODbL) | polite limits; none at all with a local extract |
| **Tier 2 — enrichment** | each business's own website | free | public first-party data | self-imposed |
| **Tier 3 — ratings** *(opt-in, off)* | Google Maps engine | free | **against Google's ToS** | real, and yours to carry |

Tiers 1 and 2 produce a complete lead list on their own. Tier 3 exists only
because OSM has no ratings or review counts; it runs as a small pass over an
already-built list rather than as the thing that builds it.

**The honest trade:** OSM's coverage of small US businesses trails Google —
it's strongest in Europe and in well-mapped categories. You give up some recall
for free, legal, unlimited and un-blockable, and Tier 3 backfills where it
matters.

## Security

The threat model is not hypothetical: a business's `website` field is
attacker-controlled, and this project fetches it.

- **One network chokepoint.** Every untrusted fetch goes through
  [`core/safe_fetch.py`](core/safe_fetch.py); nothing else opens a socket.
  Scheme and port allowlists, no URL credentials, every DNS answer vetted as
  publicly routable, the connection **pinned to a validated IP** so the
  DNS-rebinding window is closed while SNI and certificates still use the real
  hostname, redirects followed manually and **revalidated per hop**, hard byte
  and time caps, compression refused.
- **Exports can't carry formulas.** XLSX writes typed text cells, which are
  structurally incapable of being formulas. CSV neutralizes the `= + - @`
  prefixes. Every output file is `0600`.
- **Personal data is treated as such.** SQLite is `0600`, a `suppression` table
  is consulted before every export and survives re-scraping, and `purge`
  enforces a retention window.
- **No free proxy lists, ever.** They exist to intercept your traffic.
- The Tier 3 container is digest-pinned, non-privileged, capability-stripped,
  resource-capped, network-isolated and bound to `127.0.0.1`.

Tests for all of this run offline, with no Docker and no network:

```bash
make test
```

## Quick start

Python 3.9+. No dependencies.

```bash
cp .env.example .env && chmod 600 .env   # config.py refuses a readable .env
```

Set `LLI_CONTACT_URL` in `.env` to something that reaches you. Overpass and
Nominatim are donation-funded shared infrastructure and their policies require
it; the HTTP client refuses to run with the placeholder still in place.

```bash
python3 cli.py discover "dentists" --in "Austin, Texas, USA"
python3 cli.py export --out leads.xlsx
```

```
▸ Austin, Travis County, Texas, United States (area 3600113314)

[1/1] 'dentists' → dentist
  119 businesses (121 elements, 2 unnamed and skipped) in 6.1s
  phone 54 · website 54 · email 16  →  119 new, 0 refreshed

✓ 119 new, 0 refreshed, 16 emails, 8 socials
  database: 119 rows, 112 unique businesses
```

Several verticals in one pass, and the other commands:

```bash
python3 cli.py discover "dentists" "orthodontists" --in "Denver, Colorado, USA"
python3 cli.py categories                      # what maps to OSM tags
python3 cli.py stats                           # what is in the database
python3 cli.py suppress email a@b.com --reason "unsubscribed"
python3 cli.py purge --days 90 --yes
make test                                      # 143 offline tests
```

A place with an administrative boundary is searched by its **exact city
limits**, not a radius circle — `area(3600113314)` above is Austin's own OSM
relation. Only a place with no polygon falls back to `--radius`.

Docker is needed **only** for Tier 3, and only when you ask for it:

```bash
docker compose --profile tier3 up -d
```

## Layout

```
cli.py            discover / export / stats / categories / suppress / purge
core/
  safe_fetch.py   the single hardened chokepoint for UNTRUSTED urls (SSRF guard)
  http.py         client for CONFIGURED endpoints: POST, retries, backoff
  sanitize.py     spreadsheet formula neutralization
  config.py       .env loader + typed settings
  db.py           SQLite schema, identity/dedupe, suppression, retention
  export.py       CSV / XLSX / JSON writers, all 0600
  phone.py        best-effort E.164, stdlib only
sources/
  categories.py   plain language -> OSM tags (66 categories, 162 aliases)
  geocode.py      Nominatim place -> exact administrative area
  overpass.py     query building, parsing, storage
tests/            143 offline tests
docker-compose.yml  tier 3 only, behind a profile
```

## Roadmap

- [x] **Phase 1 — spine.** SSRF guard, output safety, SQLite schema, config, exporters, CI.
- [x] **Phase 2 — Tier 1 discovery.** Overpass queries, exact-boundary geocoding, category mapping, phone normalization, CLI.
- [ ] **Phase 3 — Tier 2 enrichment.** Cached crawl, email + social extraction, MX validation, quality scoring, robots.txt.
- [ ] **Phase 4 — surface.** CLI, `purge`/`suppress` commands, Claude Code skill, optional Tier 3 pass.

## Responsible use

Tiers 1 and 2 use open data and public first-party pages. Tier 3 scrapes Google
Maps, which violates Google's Terms of Service — it is off by default and gated
behind an explicit flag and a compose profile.

Phone numbers and email addresses are **personal data** wherever they came from.
Comply with GDPR, CCPA and CAN-SPAM: have a lawful basis, honour opt-outs (use
the `suppression` table — that's what it's for), and treat output as leads to
verify rather than a dataset to resell.

OSM data is ODbL. Internal use is unencumbered; redistributing a derived
database carries attribution and share-alike obligations.

## Credits

Tier 1 data © OpenStreetMap contributors, ODbL. Tier 3, if enabled, runs
[`gosom/google-maps-scraper`](https://github.com/gosom/google-maps-scraper)
(MIT, © 2023 Georgios Komninos) as its official image, unmodified.

MIT licensed — see [LICENSE](LICENSE).
