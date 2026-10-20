"""Offline tests for Tier 3: gating, the engine protocol, and matching.

The engine is stubbed — these never start Docker or touch Google.
Run:  python3 -I tests/test_tier3.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db, http                                          # noqa: E402
from core.config import Config                                     # noqa: E402
from sources import gmaps                                          # noqa: E402
from sources.geocode import Place                                  # noqa: E402

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


AUSTIN = Place(name="Austin", osm_type="relation", osm_id=113314, lat=30.2672,
               lon=-97.7431, country_code="us", area_id=3_600_113_314)

CSV = ("title,latitude,longitude,review_rating,review_count,website,phone\r\n"
       "Congress Dental,30.2700,-97.7400,4.7,812,https://cd.com,+15125550101\r\n"
       "Mueller Dental Group,30.2800,-97.7300,4.5,356,,\r\n"
       "Totally Unknown Clinic,30.9000,-97.1000,3.9,12,,\r\n")


class FakeEngine:
    """Speaks the engine's documented protocol, in memory."""

    def __init__(self, *, status_sequence=("working", "ok"), csv_text=CSV, up=True):
        self.calls = []
        self._statuses = list(status_sequence)
        self._csv = csv_text
        self._up = up

    def request(self, url, *, method="GET", data=None, **kw):
        self.calls.append((method, url))
        if not self._up:
            raise http.HttpError(0, "connection refused")
        if url.endswith("/api/v1/jobs") and method == "GET":
            return http.Response(200, b"[]", {})
        if url.endswith("/api/v1/jobs") and method == "POST":
            self.body = json.loads(data)
            return http.Response(201, b'{"id":"job-abc-123"}', {})
        if url.endswith("/download"):
            return http.Response(200, self._csv.encode(), {})
        if method == "DELETE":
            return http.Response(200, b"", {})
        status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        return http.Response(200, json.dumps({"Status": status}).encode(), {})


def _cfg(tmp, *, enabled=True):
    c = Config()
    c.db_path = Path(tmp) / "t.db"
    c.contact_url = "https://github.com/example"
    c.tier3_enabled = enabled
    c.scraper_base_url = "http://127.0.0.1:8080"
    return c


def _seeded(conn):
    db.upsert_business(conn, "osm", "node/1", name="Congress Dental",
                       website="https://cd.com", lat=30.2701, lon=-97.7401)
    db.upsert_business(conn, "osm", "node/2", name="Mueller Dental Group",
                       lat=30.2805, lon=-97.7302)
    conn.commit()


print("\ntier 3 — gating (it must be hard to turn on by accident)")


def test_refused_when_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp, enabled=False)
        conn = db.connect(cfg.db_path)
        try:
            gmaps.run(cfg, conn, "dentists", AUSTIN, confirmed=True, client=FakeEngine())
        except gmaps.Tier3Disabled as e:
            assert "LLI_TIER3_ENABLED" in str(e), f"unhelpful: {e}"
            return
        raise AssertionError("ran with Tier 3 disabled")


def test_refused_without_confirmation():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        try:
            gmaps.run(cfg, conn, "dentists", AUSTIN, confirmed=False, client=FakeEngine())
        except gmaps.Tier3Disabled:
            return
        raise AssertionError("ran without explicit confirmation")


def test_clear_error_when_engine_is_down():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        try:
            gmaps.run(cfg, conn, "dentists", AUSTIN, confirmed=True,
                      client=FakeEngine(up=False))
        except gmaps.Tier3Error as e:
            assert "--profile tier3" in str(e), f"no remedy offered: {e}"
            return
        raise AssertionError("did not notice the engine was down")


for fn in (test_refused_when_disabled, test_refused_without_confirmation,
           test_clear_error_when_engine_is_down):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\ntier 3 — engine protocol")


def test_job_body_matches_the_api_contract():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        engine = FakeEngine()
        gmaps.create_job(cfg, "dentists in Austin", 30.2672, -97.7431, client=engine)
        body = engine.body
        # The API 422s on any of these being the wrong shape.
        assert isinstance(body["lat"], str) and isinstance(body["lon"], str), body
        assert isinstance(body["max_time"], int), "max_time must be seconds, as an int"
        assert body["keywords"] == ["dentists in Austin"], body
        assert body["email"] is False, "Tier 3 must not re-crawl sites Tier 2 cached"


def test_polls_until_ok():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        engine = FakeEngine(status_sequence=("working", "working", "ok"))
        naps = []
        gmaps.wait_for(cfg, "job-1", client=engine, sleep=naps.append)
        assert len(naps) == 2, f"polled {len(naps)} times before success"


def test_failed_job_explains_rate_limiting():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        try:
            gmaps.wait_for(cfg, "job-1", client=FakeEngine(status_sequence=("failed",)),
                           sleep=lambda s: None)
        except gmaps.Tier3Error as e:
            assert "rate-limit" in str(e), f"unhelpful failure message: {e}"
            return
        raise AssertionError("a failed job was not reported")


def test_poll_timeout_is_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        try:
            gmaps.wait_for(cfg, "job-1", client=FakeEngine(status_sequence=("working",)),
                           sleep=lambda s: None, max_polls=3)
        except gmaps.Tier3Error as e:
            assert "did not finish" in str(e), e
            return
        raise AssertionError("polled forever")


def test_job_is_deleted_even_when_the_run_fails():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        conn = db.connect(cfg.db_path)
        engine = FakeEngine(status_sequence=("failed",))
        try:
            gmaps.run(cfg, conn, "dentists", AUSTIN, confirmed=True,
                      client=engine, sleep=lambda s: None)
        except gmaps.Tier3Error:
            pass
        assert any(m == "DELETE" for m, _ in engine.calls), \
            "a failed job was left on the engine's disk"


for fn in (test_job_body_matches_the_api_contract, test_polls_until_ok,
           test_failed_job_explains_rate_limiting, test_poll_timeout_is_bounded,
           test_job_is_deleted_even_when_the_run_fails):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\ntier 3 — matching scraped rows to businesses we hold")


def test_matches_by_name_and_proximity():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        _seeded(conn)
        rows = list(__import__("csv").DictReader(__import__("io").StringIO(CSV)))
        r = gmaps.apply_ratings(conn, rows)
        assert r.matched == 2, f"matched {r.matched} of 2 known businesses"
        assert r.unmatched == ["Totally Unknown Clinic"], r.unmatched


def test_ratings_are_actually_written():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        _seeded(conn)
        rows = list(__import__("csv").DictReader(__import__("io").StringIO(CSV)))
        gmaps.apply_ratings(conn, rows)
        row = conn.execute(
            "SELECT rating, review_count FROM businesses WHERE name='Congress Dental'"
        ).fetchone()
        assert row["rating"] == 4.7 and row["review_count"] == 812, dict(row)


def test_tier3_never_overwrites_contact_data():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        _seeded(conn)
        bid = conn.execute("SELECT id FROM businesses WHERE name='Congress Dental'"
                           ).fetchone()[0]
        before = conn.execute("SELECT website, dedupe_key FROM businesses WHERE id=?",
                              (bid,)).fetchone()
        rows = list(__import__("csv").DictReader(__import__("io").StringIO(CSV)))
        gmaps.apply_ratings(conn, rows)
        after = conn.execute("SELECT website, dedupe_key FROM businesses WHERE id=?",
                             (bid,)).fetchone()
        assert after["website"] == before["website"], "Tier 3 clobbered the website"
        assert after["dedupe_key"] == before["dedupe_key"], "Tier 3 re-keyed the row"


def test_far_away_namesake_is_not_matched():
    """Two branches with the same name are different businesses."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        db.upsert_business(conn, "osm", "node/9", name="Starbucks",
                           lat=30.2672, lon=-97.7431)
        conn.commit()
        rows = [{"title": "Starbucks", "latitude": "30.3500", "longitude": "-97.6000",
                 "review_rating": "4.1", "review_count": "99"}]
        r = gmaps.apply_ratings(conn, rows)
        assert r.matched == 0, "matched a branch 9km away"


def test_distance_is_real_haversine():
    # Austin city hall to roughly 1km north.
    d = gmaps._metres(30.2672, -97.7431, 30.2762, -97.7431)
    assert 990 < d < 1010, f"haversine wrong: {d}m"


def test_rows_without_ratings_are_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        _seeded(conn)
        r = gmaps.apply_ratings(conn, [
            {"title": "Congress Dental", "latitude": "30.2700", "longitude": "-97.7400",
             "review_rating": "", "review_count": ""}])
        assert r.updated == 0, "wrote a row with no rating data"


def test_malformed_numbers_do_not_crash():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "t.db")
        _seeded(conn)
        r = gmaps.apply_ratings(conn, [
            {"title": "Congress Dental", "latitude": "n/a", "longitude": "",
             "review_rating": "four", "review_count": "1,234"}])
        assert r.updated == 0 and r.matched == 0, r


for fn in (test_matches_by_name_and_proximity, test_ratings_are_actually_written,
           test_tier3_never_overwrites_contact_data, test_far_away_namesake_is_not_matched,
           test_distance_is_real_haversine, test_rows_without_ratings_are_skipped,
           test_malformed_numbers_do_not_crash):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print(f"\n{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
