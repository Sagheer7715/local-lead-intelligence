"""Offline tests for config loading, the database layer and the exporters.

No network, no Docker, no pytest.  Run:  python3 -I tests/test_core.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db, export                                       # noqa: E402
from core.config import ConfigError, load_env                     # noqa: E402

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


print(f"\nsqlite {sqlite3.sqlite_version} / python {sys.version.split()[0]}")

print("\nconfig — .env is actually loaded")


def test_env_loads():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text('# comment\n\nexport LLI_T_A=1\nLLI_T_B="two words"\nLLI_T_C=\n')
        p.chmod(0o600)
        os.environ.pop("LLI_T_A", None)
        os.environ.pop("LLI_T_B", None)
        loaded = load_env(p)
        assert os.environ.get("LLI_T_A") == "1", loaded
        assert os.environ.get("LLI_T_B") == "two words", "quotes not stripped"
        assert "LLI_T_C" in loaded and loaded["LLI_T_C"] == "", "empty value dropped"


def test_env_does_not_clobber_real_environment():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text("LLI_T_D=from_file\n")
        p.chmod(0o600)
        os.environ["LLI_T_D"] = "from_shell"
        load_env(p)
        assert os.environ["LLI_T_D"] == "from_shell", "file overrode the shell"
        load_env(p, override=True)
        assert os.environ["LLI_T_D"] == "from_file", "override=True ignored"


def test_env_refuses_world_readable():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text("SCRAPER_API_KEY=secret\n")
        p.chmod(0o644)
        try:
            load_env(p)
        except ConfigError as e:
            assert "chmod" in str(e), f"unhelpful error: {e}"
            return
        raise AssertionError("loaded a world-readable .env holding a key")


def test_env_rejects_malformed_line():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text("THIS_IS_NOT_A_PAIR\n")
        p.chmod(0o600)
        try:
            load_env(p)
        except ConfigError:
            return
        raise AssertionError("accepted a malformed line")


def test_missing_env_is_fine():
    assert load_env(Path(tempfile.gettempdir()) / "definitely-absent.env") == {}


for fn in (test_env_loads, test_env_does_not_clobber_real_environment,
           test_env_refuses_world_readable, test_env_rejects_malformed_line,
           test_missing_env_is_fine):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

def _assert_eq(got, want):
    assert got == want, f"got {got!r}, wanted {want!r}"


def _assert_ne(a, b):
    assert a != b, f"expected different, both were {a!r}"


print("\ndb — identity and normalization")
check("domain strips scheme/www/path/port", lambda: _assert_eq(
    [db.normalize_domain(x) for x in
     ("https://WWW.Example.com/contact?x=1", "example.com:8080/a", "Example.com", "")],
    ["example.com", "example.com", "example.com", ""]))
check("company suffixes ignored in name key", lambda: _assert_eq(
    db.normalize_name("Joe's Coffee, LLC"), db.normalize_name("Joes Coffee")))
check("same domain -> same dedupe key", lambda: _assert_eq(
    db.dedupe_key("Joe's Coffee", "http://joescoffee.com/"),
    db.dedupe_key("Joe Coffee Inc", "https://www.joescoffee.com/about")))
check("different domains -> different keys", lambda: _assert_ne(
    db.dedupe_key("A", "a.com"), db.dedupe_key("A", "b.com")))
check("phone used when no website", lambda: _assert_eq(
    db.dedupe_key("X", "", "+1 512-555-0101"), db.dedupe_key("X", "", "+15125550101")))


def test_chain_branches_are_separate_leads():
    """42 Starbucks on one domain are 42 leads, not one. Regression test."""
    a = db.dedupe_key("Starbucks", "https://starbucks.com", lat=30.2672, lon=-97.7431)
    b = db.dedupe_key("Starbucks", "https://starbucks.com", lat=30.3100, lon=-97.7000)
    assert a != b, "two branches of a chain collapsed into one lead"


def test_different_businesses_sharing_a_landlord_domain_stay_separate():
    """The campus case: six cafés on housing.utexas.edu are six businesses."""
    keys = {db.dedupe_key(name, "https://housing.utexas.edu", lat=lat, lon=-97.73)
            for name, lat in [("Jester Java", 30.283), ("Prufrock's", 30.285),
                              ("Union Coffee House", 30.287), ("Up and Atom", 30.289)]}
    assert len(keys) == 4, f"distinct businesses merged: {len(keys)} keys for 4 cafés"


def test_same_building_mapped_twice_still_merges():
    """A node and a building outline for one shop are metres apart, not blocks."""
    a = db.dedupe_key("Medici Roasting", "https://mediciroasting.com",
                      lat=30.29341, lon=-97.74102)
    b = db.dedupe_key("Medici Roasting", "https://mediciroasting.com",
                      lat=30.29349, lon=-97.74108)
    assert a == b, "one business mapped two ways produced two leads"


def test_cross_source_match_at_one_location_still_merges():
    a = db.dedupe_key("Joe's Coffee", "http://joescoffee.com/", lat=30.2672, lon=-97.7431)
    b = db.dedupe_key("Joe Coffee Inc", "https://www.joescoffee.com/about",
                      lat=30.2672, lon=-97.7431)
    assert a == b, "same business from two sources did not merge"


def test_location_falls_back_when_coordinates_are_missing():
    assert db.location_cell(None, None, "100 Congress Ave").startswith("a:")
    assert db.location_cell(None, None, "", "Austin").startswith("c:")
    assert db.location_cell(30.2672, -97.7431).startswith("g:")
    assert db.location_cell("bad", "coords", "", "Austin").startswith("c:"), \
        "non-numeric coordinates were not survived"


for fn in (test_chain_branches_are_separate_leads,
           test_different_businesses_sharing_a_landlord_domain_stay_separate,
           test_same_building_mapped_twice_still_merges,
           test_cross_source_match_at_one_location_still_merges,
           test_location_falls_back_when_coordinates_are_missing):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\ndb — upsert, dedupe, suppression, purge")


def _fresh():
    d = tempfile.mkdtemp()
    return db.connect(Path(d) / "t.db")


def test_schema_and_permissions():
    d = tempfile.mkdtemp()
    p = Path(d) / "t.db"
    db.connect(p).close()
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600, f"db is mode {oct(mode)}, wanted 0600"


def test_insert_then_update_preserves_first_seen():
    conn = _fresh()
    bid, created = db.upsert_business(conn, "osm", "node/1", name="Joe's Coffee",
                                      website="http://joescoffee.com", city="Austin")
    assert created, "first insert not reported as new"
    first = conn.execute("SELECT first_seen FROM businesses WHERE id=?", (bid,)).fetchone()[0]
    bid2, created2 = db.upsert_business(conn, "osm", "node/1", name="Joe's Coffee",
                                        phone_e164="+15125550101")
    assert bid2 == bid and not created2, "upsert created a duplicate row"
    row = conn.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
    assert row["first_seen"] == first, "first_seen was overwritten"
    assert row["phone_e164"] == "+15125550101", "new field not merged"
    assert row["website"] == "http://joescoffee.com", "existing field was blanked"


def test_tier3_pass_adds_rating_without_blanking_contacts():
    conn = _fresh()
    bid, _ = db.upsert_business(conn, "osm", "node/2", name="Cafe", website="http://c.com",
                                phone_e164="+15125550102", lat=30.26, lon=-97.74)
    before = conn.execute("SELECT dedupe_key FROM businesses WHERE id=?", (bid,)).fetchone()[0]
    # A ratings-only pass carries no coordinates.
    db.upsert_business(conn, "osm", "node/2", name="Cafe", rating=4.7, review_count=812)
    row = conn.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
    assert row["rating"] == 4.7 and row["review_count"] == 812, "tier 3 data not stored"
    assert row["phone_e164"] == "+15125550102", "tier 3 pass wiped the phone"
    assert row["dedupe_key"] == before, \
        "a partial update re-keyed the business — identity must come from the merged row"


def test_export_dedupes_across_sources():
    conn = _fresh()
    db.upsert_business(conn, "osm", "node/3", name="Joe's Coffee", website="http://joescoffee.com")
    db.upsert_business(conn, "gmaps", "ChIJabc", name="Joe Coffee LLC",
                       website="https://www.joescoffee.com", phone_e164="+15125550103")
    conn.commit()
    rows = db.export_rows(conn)
    assert len(rows) == 1, f"same business exported {len(rows)} times"
    assert rows[0]["phone"] == "+15125550103", "lost the row that had the phone"


def test_email_dedupe_and_aggregation():
    conn = _fresh()
    bid, _ = db.upsert_business(conn, "osm", "node/4", name="Cafe", website="http://c4.com")
    assert db.add_email(conn, bid, "Hello@C4.com") is True
    assert db.add_email(conn, bid, "hello@c4.com") is False, "case-duplicate accepted"
    db.add_email(conn, bid, "info@c4.com")
    conn.commit()
    emails = db.export_rows(conn)[0]["emails"]
    assert "hello@c4.com" in emails and "info@c4.com" in emails, emails


def test_suppression_removes_from_export():
    conn = _fresh()
    bid, _ = db.upsert_business(conn, "osm", "node/5", name="Cafe", website="http://c5.com",
                                phone_e164="+15125550105")
    db.add_email(conn, bid, "hello@c5.com")
    conn.commit()
    db.suppress(conn, "email", "hello@c5.com", "unsubscribed")
    assert not db.export_rows(conn)[0]["emails"], "suppressed email still exported"
    db.suppress(conn, "phone", "+1 512 555 0105", "do not call")
    assert db.export_rows(conn) == [], "suppressed phone still exported"


def test_suppressed_domain_survives_a_rescrape():
    conn = _fresh()
    db.upsert_business(conn, "osm", "node/6", name="Cafe", website="http://c6.com")
    conn.commit()
    db.suppress(conn, "domain", "https://www.c6.com/contact", "opted out")
    db.upsert_business(conn, "osm", "node/6", name="Cafe", website="http://c6.com")
    conn.commit()
    assert db.export_rows(conn) == [], "re-scraping resurrected a suppressed business"


def test_suppress_rejects_bad_kind():
    conn = _fresh()
    try:
        db.suppress(conn, "fax", "123")
    except ValueError:
        return
    raise AssertionError("accepted an unknown suppression kind")


def test_purge_keeps_suppression():
    conn = _fresh()
    bid, _ = db.upsert_business(conn, "osm", "node/7", name="Old", website="http://old.com")
    db.suppress(conn, "email", "x@old.com")
    conn.execute("UPDATE businesses SET last_seen='2000-01-01T00:00:00+00:00' WHERE id=?", (bid,))
    conn.commit()
    out = db.purge(conn, 30)
    assert out["businesses_deleted"] == 1, out
    assert conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 1, \
        "purge deleted a suppression entry"


def test_purge_rejects_zero():
    conn = _fresh()
    try:
        db.purge(conn, 0)
    except ValueError:
        return
    raise AssertionError("purge accepted 0 days")


def test_cascade_delete_removes_children():
    conn = _fresh()
    bid, _ = db.upsert_business(conn, "osm", "node/8", name="Cafe", website="http://c8.com")
    db.add_email(conn, bid, "a@c8.com")
    db.add_social(conn, bid, "instagram", "https://instagram.com/c8")
    conn.commit()
    conn.execute("DELETE FROM businesses WHERE id=?", (bid,))
    conn.commit()
    left = (conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM socials").fetchone()[0])
    assert left == 0, f"{left} orphaned child rows"


def test_v1_database_is_rekeyed_on_open():
    """A database written before the location fix must regroup, not stay broken."""
    d = tempfile.mkdtemp()
    path = Path(d) / "old.db"
    conn = db.connect(path)
    for i, (lat, lon) in enumerate([(30.26, -97.74), (30.31, -97.70), (30.35, -97.68)]):
        db.upsert_business(conn, "osm", f"node/{i}", name="Starbucks",
                           website="https://starbucks.com", lat=lat, lon=lon)
    # Force the v1 state: one key for every branch, schema marked as old.
    conn.execute("UPDATE businesses SET dedupe_key = 'legacy-domain-only'")
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    reopened = db.connect(path)
    keys = {r[0] for r in reopened.execute("SELECT dedupe_key FROM businesses")}
    assert len(keys) == 3, f"migration left {len(keys)} keys for 3 branches"
    assert "legacy-domain-only" not in keys, "stale key survived the migration"
    version = reopened.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert version == str(db.SCHEMA_VERSION), f"version not bumped: {version}"
    assert len(db.export_rows(reopened)) == 3, "export still collapses the branches"


def test_migration_is_idempotent():
    d = tempfile.mkdtemp()
    path = Path(d) / "x.db"
    conn = db.connect(path)
    db.upsert_business(conn, "osm", "node/1", name="Cafe", lat=30.1, lon=-97.1)
    conn.commit()
    conn.close()
    db.connect(path).close()
    assert db.rekey_all(db.connect(path)) == 0, "re-keying a current database changed rows"


def test_name_is_required():
    conn = _fresh()
    try:
        db.upsert_business(conn, "osm", "node/9", name="   ")
    except ValueError:
        return
    raise AssertionError("accepted a business with no name")


for fn in (test_schema_and_permissions, test_insert_then_update_preserves_first_seen,
           test_tier3_pass_adds_rating_without_blanking_contacts,
           test_export_dedupes_across_sources, test_email_dedupe_and_aggregation,
           test_suppression_removes_from_export, test_suppressed_domain_survives_a_rescrape,
           test_suppress_rejects_bad_kind, test_purge_keeps_suppression,
           test_purge_rejects_zero, test_cascade_delete_removes_children,
           test_v1_database_is_rekeyed_on_open, test_migration_is_idempotent,
           test_name_is_required):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\nexporters")
ROWS = [
    {"title": '=HYPERLINK("http://evil.test","x")', "phone": "+15125550101",
     "emails": "a@b.com", "website": "http://b.com", "category": "Cafe",
     "address": "1 Main St", "review_rating": "4.7", "review_count": "812"},
    {"title": "Ünïcode & <Tags>", "phone": "", "emails": "", "website": "",
     "category": "", "address": "", "review_rating": None, "review_count": None},
]
FIELDS = db.LEAD_FIELDS


def test_csv_neutralizes_and_is_private():
    import csv as _csv
    with tempfile.TemporaryDirectory() as d:
        p = export.write_csv(ROWS, FIELDS, Path(d) / "out.csv")
        # Assert on the parsed cell, not the raw text: the payload contains a
        # comma and quotes, so csv quotes the field around the apostrophe.
        with open(p, newline="", encoding="utf-8") as f:
            parsed = list(_csv.DictReader(f))
        assert parsed[0]["title"].startswith("'="), \
            f"formula not neutralized: {parsed[0]['title']!r}"
        assert parsed[0]["phone"].startswith("'+"), "E.164 phone not neutralized"
        assert parsed[1]["title"] == "Ünïcode & <Tags>", "unicode mangled"
        assert (p.stat().st_mode & 0o777) == 0o600, f"csv is {oct(p.stat().st_mode & 0o777)}"


def test_xlsx_is_a_valid_package():
    with tempfile.TemporaryDirectory() as d:
        p = export.write_xlsx(ROWS, FIELDS, Path(d) / "out.xlsx")
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
            for required in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                             "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet1.xml",
                             "xl/styles.xml"):
                assert required in names, f"missing part {required}"
            for name in names:                       # every part must parse
                ElementTree.fromstring(z.read(name))
        assert (p.stat().st_mode & 0o777) == 0o600, "xlsx not private"


def test_xlsx_text_cannot_be_a_formula():
    with tempfile.TemporaryDirectory() as d:
        p = export.write_xlsx(ROWS, FIELDS, Path(d) / "out.xlsx")
        with zipfile.ZipFile(p) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        # A formula requires an <f> element; we never emit one, so the
        # payload rides along as inert typed text with no apostrophe.
        assert "<f>" not in sheet, "xlsx contains a formula element"
        assert 't="inlineStr"' in sheet, "text not written as an inline string"
        assert "HYPERLINK" in sheet and "'=HYPERLINK" not in sheet, \
            "xlsx should not need the apostrophe mitigation"


def test_xlsx_numbers_stay_numbers():
    with tempfile.TemporaryDirectory() as d:
        p = export.write_xlsx(ROWS, FIELDS, Path(d) / "out.xlsx")
        with zipfile.ZipFile(p) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        assert "<v>4.7</v>" in sheet, "rating not written as a number"
        assert "<v>812.0</v>" in sheet or "<v>812</v>" in sheet, "review count not numeric"


def test_xlsx_strips_illegal_control_characters():
    rows = [{f: "" for f in FIELDS}]
    rows[0]["title"] = "Caf\x07e\x00 Null"           # BEL and NUL
    with tempfile.TemporaryDirectory() as d:
        p = export.write_xlsx(rows, FIELDS, Path(d) / "out.xlsx")
        with zipfile.ZipFile(p) as z:
            ElementTree.fromstring(z.read("xl/worksheets/sheet1.xml"))  # must parse
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        assert "\x07" not in sheet and "\x00" not in sheet, "control chars survived"


def test_write_dispatches_on_extension():
    with tempfile.TemporaryDirectory() as d:
        assert export.write(ROWS, FIELDS, Path(d) / "a.csv").suffix == ".csv"
        assert export.write(ROWS, FIELDS, Path(d) / "a.xlsx").suffix == ".xlsx"
        assert export.write(ROWS, FIELDS, Path(d) / "a.json").suffix == ".json"
        try:
            export.write(ROWS, FIELDS, Path(d) / "a.pdf")
        except ValueError:
            return
        raise AssertionError("accepted an unsupported extension")


for fn in (test_csv_neutralizes_and_is_private, test_xlsx_is_a_valid_package,
           test_xlsx_text_cannot_be_a_formula, test_xlsx_numbers_stay_numbers,
           test_xlsx_strips_illegal_control_characters, test_write_dispatches_on_extension):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print(f"\n{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
