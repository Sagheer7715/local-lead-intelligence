#!/usr/bin/env python3
"""local-lead-intelligence — command line entry point.

    python3 cli.py discover "dentists" --in "Austin, Texas, USA"
    python3 cli.py export --out leads.xlsx
    python3 cli.py stats
    python3 cli.py categories
    python3 cli.py suppress email someone@example.com --reason "unsubscribed"
    python3 cli.py purge --days 90
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import db, export                                        # noqa: E402
from core.config import ConfigError, load                          # noqa: E402
from sources import categories, enrich, gmaps, overpass            # noqa: E402
from sources.geocode import GeocodeError, Geocoder                  # noqa: E402


def _die(message, hint=""):
    print(f"✗ {message}", file=sys.stderr)
    if hint:
        print(f"  {hint}", file=sys.stderr)
    raise SystemExit(1)


# ── discover ────────────────────────────────────────────────────────────────

def cmd_discover(args, cfg):
    terms = [t.strip() for t in args.terms if t.strip()]
    if args.terms_file:
        text = Path(args.terms_file).read_text(encoding="utf-8")
        terms += [ln.strip() for ln in text.splitlines()
                  if ln.strip() and not ln.startswith("#")]
    if not terms:
        _die("no business types given", 'e.g. discover "dentists" --in "Austin, TX, USA"')

    geocoder = Geocoder(cfg)
    try:
        place = geocoder.lookup(args.place)
    except GeocodeError as e:
        _die(str(e))
    print(f"▸ {place}")
    if not place.has_area:
        print(f"  no boundary polygon for this place — using a {args.radius}m radius")

    conn = db.connect(cfg.db_path)
    grand = {"new": 0, "updated": 0, "emails": 0, "socials": 0}

    for i, term in enumerate(terms, 1):
        canonical, tag_sets = categories.resolve(term)
        if not tag_sets:
            print(f"\n[{i}/{len(terms)}] {term!r} — not a known category; "
                  "falling back to a name search (weaker coverage)")
        else:
            print(f"\n[{i}/{len(terms)}] {term!r} → {canonical}")

        run = conn.execute(
            "INSERT INTO runs (query, source, area, started_at) VALUES (?,?,?,?)",
            (term, "osm", place.name, db.now()))
        run_id = run.lastrowid
        conn.commit()

        try:
            result = overpass.discover(cfg, term, place, radius=args.radius)
        except overpass.OverpassError as e:
            conn.execute("UPDATE runs SET finished_at=?, notes=? WHERE id=?",
                         (db.now(), str(e)[:500], run_id))
            conn.commit()
            print(f"  ✗ {e}", file=sys.stderr)
            continue

        counts = overpass.store(conn, result)
        for k in grand:
            grand[k] += counts[k]
        conn.execute(
            "UPDATE runs SET finished_at=?, rows_found=?, rows_new=? WHERE id=?",
            (db.now(), len(result.businesses), counts["new"], run_id))
        conn.commit()

        print(f"  {len(result.businesses)} businesses "
              f"({result.elements} elements, {result.unnamed} unnamed and skipped) "
              f"in {result.seconds:.1f}s")
        print(f"  phone {result.with_phone} · website {result.with_website} "
              f"· email {result.with_email}  →  "
              f"{counts['new']} new, {counts['updated']} refreshed")

    print(f"\n✓ {grand['new']} new, {grand['updated']} refreshed, "
          f"{grand['emails']} emails, {grand['socials']} socials")
    totals = db.counts(conn)
    print(f"  database: {totals['businesses']} rows, {totals['unique']} unique businesses")
    if totals["with_website"]:
        print(f"  {totals['with_website']} have a website — Phase 3 will crawl these "
              "for emails and socials")
    print(f"\nNext: python3 cli.py export --out leads.xlsx")


# ── export ──────────────────────────────────────────────────────────────────

def cmd_enrich(args, cfg):
    conn = db.connect(cfg.db_path)
    todo = enrich.pending(conn, limit=args.limit)
    if not todo:
        print("  nothing to enrich — every business with a website already has an "
              "email, or none have websites yet. Run `discover` first.")
        return
    print(f"▸ enriching {len(todo)} business websites "
          f"({cfg.max_workers} workers, {cfg.per_host_delay}s per host, "
          f"robots={'on' if cfg.respect_robots else 'OFF'})")
    print(f"  identifying as {cfg.user_agent}")

    result = enrich.run(cfg, conn, limit=args.limit, workers=args.workers,
                        check_mx=not args.no_mx)

    print(f"\n✓ {result.emails_found} emails, {result.socials_found} socials "
          f"in {result.seconds:.0f}s")
    print(f"  fetched {result.fetched} · cached {result.from_cache} · "
          f"failed {result.failed}")
    if result.dead_domains:
        print(f"  {result.dead_domains} website(s) no longer resolve — dead links in OSM")
    if result.blocked_by_robots:
        print(f"  {result.blocked_by_robots} skipped by robots.txt")
    if result.mx_rejected:
        print(f"  {result.mx_rejected} addresses dropped — domain has no mail server")
    if result.refused_by_guard:
        print(f"  ⚠ {result.refused_by_guard} URL(s) refused by the SSRF guard:")
        for note in result.guard_blocks:
            print(f"      {note}")
    totals = db.counts(conn)
    print(f"\n  database: {totals['businesses']} businesses, "
          f"{totals['emails']} emails, {totals['socials']} socials")


def cmd_ratings(args, cfg):
    """Tier 3: the only command that touches Google. Opt-in, twice."""
    geocoder = Geocoder(cfg)
    try:
        place = geocoder.lookup(args.place)
    except GeocodeError as e:
        _die(str(e))
    conn = db.connect(cfg.db_path)
    if not args.yes:
        print("⚠ This scrapes Google Maps, which violates their Terms of Service and")
        print("  can get your IP temporarily rate-limited. It adds ratings and review")
        print("  counts to businesses you already have; it discovers nothing new.")
        print(f"\n  Would run: {args.keyword!r} @ {place.name[:60]} depth={args.depth}")
        print("  Re-run with --yes to proceed.")
        return
    try:
        result = gmaps.run(cfg, conn, args.keyword, place, depth=args.depth,
                           confirmed=True)
    except gmaps.Tier3Disabled as e:
        _die(str(e))
    except gmaps.Tier3Error as e:
        _die(str(e))
    print(f"✓ {result.updated} businesses given ratings "
          f"({result.scraped} scraped, {result.matched} matched) in {result.seconds:.0f}s")
    if result.unmatched:
        print(f"  {len(result.unmatched)} scraped rows matched nothing we hold "
              f"(e.g. {', '.join(result.unmatched[:3])})")


def cmd_export(args, cfg):
    conn = db.connect(cfg.db_path)
    rows = db.export_rows(conn, city=args.city)
    if not rows:
        _die("nothing to export", "run `discover` first, or relax --city")

    fields = list(db.LEAD_FIELDS)
    if args.socials:
        fields += ["instagram", "facebook", "linkedin"]
    if args.fields:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]

    path = export.write(rows, fields, args.out)
    print(f"✓ {len(rows)} leads → {path} (mode 600)")
    if path.suffix == ".csv":
        print("  note: CSV neutralizes formulas by prefixing an apostrophe, which is")
        print("  visible on phone numbers. Use --out leads.xlsx for clean typed cells.")
    for row in rows[:5]:
        print(f"  • {row.get('title','')} | {row.get('phone') or '—'} "
              f"| {row.get('emails') or '—'}")
    if len(rows) > 5:
        print(f"  … and {len(rows) - 5} more")


# ── small commands ──────────────────────────────────────────────────────────

def cmd_stats(args, cfg):
    conn = db.connect(cfg.db_path)
    for key, value in db.counts(conn).items():
        print(f"  {key.replace('_', ' '):16} {value}")
    recent = conn.execute(
        "SELECT query, area, rows_found, rows_new, started_at, notes FROM runs"
        " ORDER BY id DESC LIMIT 8").fetchall()
    if recent:
        print("\n  recent runs")
        for r in recent:
            flag = "  ✗" if r["notes"] else ""
            print(f"    {r['started_at'][:16]}  {r['query'][:28]:28} "
                  f"{r['rows_found'] or 0:>5} found  {r['rows_new'] or 0:>5} new{flag}")


def cmd_categories(args, cfg):
    known = categories.known()
    print(f"{len(known)} categories (plus many aliases — plurals and synonyms work):\n")
    for i in range(0, len(known), 3):
        print("  " + "".join(f"{c:26}" for c in known[i:i + 3]).rstrip())
    print("\nAnything else falls back to a name search, which finds fewer results.")


def cmd_suppress(args, cfg):
    conn = db.connect(cfg.db_path)
    if args.list:
        rows = conn.execute(
            "SELECT kind, value, reason, added_at FROM suppression ORDER BY added_at"
        ).fetchall()
        if not rows:
            print("  (no suppression entries)")
        for r in rows:
            print(f"  {r['kind']:7} {r['value']:40} {r['reason'] or ''}")
        return
    if not (args.kind and args.value):
        _die("give a kind and a value", "suppress email a@b.com --reason unsubscribed")
    db.suppress(conn, args.kind, args.value, args.reason or "")
    print(f"✓ {args.kind} {args.value} will be excluded from every future export")


def cmd_purge(args, cfg):
    days = args.days or cfg.retention_days
    conn = db.connect(cfg.db_path)
    before = db.counts(conn)
    if not args.yes:
        stale = conn.execute(
            "SELECT COUNT(*) FROM businesses WHERE last_seen <"
            " datetime('now', ?)", (f"-{days} days",)).fetchone()[0]
        print(f"This deletes {stale} businesses not seen in {days} days "
              f"(of {before['businesses']}), and their emails and socials.")
        print("Suppression entries are kept. Re-run with --yes to proceed.")
        return
    out = db.purge(conn, days)
    print(f"✓ deleted {out['businesses_deleted']} businesses and "
          f"{out['cache_deleted']} cache rows older than {out['cutoff'][:10]}")


# ── wiring ──────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cli.py", description="Free, local business lead lists from OpenStreetMap.")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="find businesses in a place (tier 1, free)")
    d.add_argument("terms", nargs="*", help='business types, e.g. "dentists" "orthodontists"')
    d.add_argument("--in", "--city", dest="place", required=True,
                   help='place to search, e.g. "Austin, Texas, USA"')
    d.add_argument("--terms-file", help="file of business types, one per line")
    d.add_argument("--radius", type=int, default=15000,
                   help="metres, used only if the place has no boundary (default 15000)")
    d.set_defaults(func=cmd_discover)

    n = sub.add_parser("enrich", help="crawl business websites for emails and socials")
    n.add_argument("--limit", type=int, help="only this many businesses")
    n.add_argument("--workers", type=int, help="override LLI_MAX_WORKERS")
    n.add_argument("--no-mx", action="store_true",
                   help="skip DNS validation of email domains (faster, lower quality)")
    n.set_defaults(func=cmd_enrich)

    t = sub.add_parser("ratings",
                       help="OPT-IN: add Google ratings to businesses you already have")
    t.add_argument("keyword", help='the same term you discovered with, e.g. "dentists"')
    t.add_argument("--in", "--city", dest="place", required=True, help="place to search")
    t.add_argument("--depth", type=int, default=5, help="scroll depth (start low)")
    t.add_argument("--yes", action="store_true", help="confirm the Google ToS tradeoff")
    t.set_defaults(func=cmd_ratings)

    e = sub.add_parser("export", help="write the lead list to a file")
    e.add_argument("--out", default="leads.xlsx",
                   help="output path; .xlsx (recommended), .csv or .json")
    e.add_argument("--city", help="only this city")
    e.add_argument("--socials", action="store_true", help="include social columns")
    e.add_argument("--fields", help="comma-separated column list (overrides the default)")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("stats", help="what is in the database")
    s.set_defaults(func=cmd_stats)

    c = sub.add_parser("categories", help="list the business types that map to OSM tags")
    c.set_defaults(func=cmd_categories)

    p = sub.add_parser("suppress", help="never contact this again (survives re-scraping)")
    p.add_argument("kind", nargs="?", choices=["email", "phone", "domain"])
    p.add_argument("value", nargs="?")
    p.add_argument("--reason", help="why, for your own records")
    p.add_argument("--list", action="store_true", help="show current entries")
    p.set_defaults(func=cmd_suppress)

    g = sub.add_parser("purge", help="delete records past the retention window")
    g.add_argument("--days", type=int, help="override LLI_RETENTION_DAYS")
    g.add_argument("--yes", action="store_true", help="actually delete")
    g.set_defaults(func=cmd_purge)

    args = ap.parse_args(argv)
    try:
        cfg = load()
    except ConfigError as e:
        _die(str(e))
    try:
        return args.func(args, cfg) or 0
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except ValueError as e:
        # Mostly the missing-contact guard in core.http, which is a setup
        # problem the user can fix — not something to show a traceback for.
        _die(str(e))
    except ConfigError as e:
        _die(str(e))


if __name__ == "__main__":
    raise SystemExit(main())
