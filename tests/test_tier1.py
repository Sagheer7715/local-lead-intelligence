"""Offline tests for Tier 1: categories, query building, parsing, phones, HTTP.

No network: the Overpass and Nominatim clients are stubbed.
Run:  python3 -I tests/test_tier1.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db, http, phone                                   # noqa: E402
from sources import categories, overpass                           # noqa: E402
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


AUSTIN = Place(name="Austin, Travis County, Texas, United States", osm_type="relation",
               osm_id=113314, lat=30.2672, lon=-97.7431, country_code="us",
               area_id=3_600_113_314)
POINT = Place(name="Tiny Village", osm_type="node", osm_id=42, lat=51.5, lon=-0.12,
              country_code="gb", area_id=0)

print("\ncategories — plain language to OSM tags")
CASES = [
    ("dentists", "dentist"), ("dental practice", "dentist"), ("Dentist", "dentist"),
    ("coffee shops", "coffee shop"), ("cafes", "cafe"),
    ("emergency plumbers", "plumber"), ("plumbing", "plumber"),
    ("law firm", "lawyer"), ("attorneys", "lawyer"),
    ("best gyms near me", "gym"), ("crossfit", "gym"),
    ("hair salons", "hairdresser"), ("barbershop", "barber"),
    ("auto repair", "car repair"), ("mechanic", "car repair"),
    ("wedding photographer", "photographer"),
    ("realtors", "real estate"), ("daycare", "childcare"),
    ("top rated dental offices", "dentist"),
]
for term, want in CASES:
    def run(term=term, want=want):
        name, tags = categories.resolve(term)
        assert name == want, f"{term!r} -> {name!r}, wanted {want!r}"
        assert tags, f"{term!r} resolved to {name!r} with no tags"
    check(f"{term!r:30} -> {want}", run)


def test_unknown_term_falls_back_to_name_search():
    name, tags = categories.resolve("artisanal yurt restoration")
    assert tags is None, f"unexpectedly matched a category: {name}"
    fb = categories.name_fallback("artisanal yurt restoration")
    assert fb and "~name" in fb[0], f"no usable fallback: {fb}"


def test_empty_term_has_no_fallback():
    assert categories.name_fallback("   ") == [], "empty term produced a query"


def test_every_alias_points_at_a_real_category():
    missing = sorted({v for v in categories.ALIASES.values()} - set(categories.TAGS))
    assert not missing, f"aliases point at undefined categories: {missing}"


def test_every_category_has_tag_sets():
    empty = [k for k, v in categories.TAGS.items() if not v or not all(v)]
    assert not empty, f"categories with empty tag sets: {empty}"


for fn in (test_unknown_term_falls_back_to_name_search, test_empty_term_has_no_fallback,
           test_every_alias_points_at_a_real_category, test_every_category_has_tag_sets):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\noverpass — query construction")


def test_area_query_uses_the_boundary():
    q = overpass.build_query([{"amenity": "dentist"}], AUSTIN)
    assert "area(3600113314)->.searchArea;" in q, q
    assert "(area.searchArea)" in q, q
    assert "around:" not in q, "used a radius despite having a polygon"
    assert "[out:json]" in q and "out center tags;" in q, q


def test_pointonly_place_falls_back_to_radius():
    q = overpass.build_query([{"amenity": "cafe"}], POINT, radius=8000)
    assert "around:8000,51.5,-0.12" in q, q
    assert "searchArea" not in q, "built an area for a node"


def test_multiple_tag_sets_are_ored():
    q = overpass.build_query([{"amenity": "dentist"}, {"healthcare": "dentist"}], AUSTIN)
    assert q.count("nwr") == 2, q
    assert '["amenity"="dentist"]' in q and '["healthcare"="dentist"]' in q, q


def test_tags_within_a_set_are_anded():
    q = overpass.build_query([{"amenity": "cafe", "cuisine": "coffee_shop"}], AUSTIN)
    assert '["amenity"="cafe"]["cuisine"="coffee_shop"]' in q, q
    assert q.count("nwr") == 1, "ANDed tags were split into separate statements"


def test_regex_fallback_is_case_insensitive():
    q = overpass.build_query(categories.name_fallback("upholstery"), AUSTIN)
    assert '["name"~"upholstery",i]' in q, q


def _outside_strings(query):
    """Blank out double-quoted literals, honouring backslash escapes.

    A payload that merely *appears* in the query text is harmless if it sits
    inside a string literal — substring checks cannot tell the difference, so
    they would pass on a real injection and fail on a safe one. What survives
    this is the query's actual structure.
    """
    out, i, n = [], 0, len(query)
    while i < n:
        if query[i] == '"':
            i += 1
            while i < n:
                if query[i] == "\\":
                    i += 2
                    continue
                if query[i] == '"':
                    i += 1
                    break
                i += 1
            out.append('""')
        else:
            out.append(query[i])
            i += 1
    return "".join(out)


def test_outside_strings_helper_is_honest():
    # The helper itself must not be fooled, or every test below is worthless.
    assert _outside_strings('a["k"="v"];') == 'a[""=""];'
    assert _outside_strings(r'["k"~"x\"]; out count; [\"y"]') == '[""~""]'
    assert "out" in _outside_strings('["k"="v"]; out count;'), "helper hid real structure"


def test_quote_injection_cannot_escape_the_literal():
    # A hostile term reaches build_query through the name-search fallback.
    nasty = 'x"]; out count; node["a"="b'
    q = overpass.build_query([{"~name": nasty}], AUSTIN)
    assert '\\"' in q, "quotes were not escaped at all"
    structure = _outside_strings(q)
    assert "count" not in structure, f"injected a statement:\n{structure}"
    assert structure.count("out ") == 1, f"more than one out statement:\n{structure}"
    assert structure.count("nwr") == 1, f"injected an extra query:\n{structure}"


def test_backslash_injection_is_escaped():
    q = overpass.build_query([{"~name": r'a\"]; out count; ["b'}], AUSTIN)
    structure = _outside_strings(q)
    assert "count" not in structure, f"escaped via a trailing backslash:\n{structure}"
    assert structure.count("out ") == 1, structure


def test_injection_through_a_tag_key_is_escaped():
    q = overpass.build_query([{'a"]; out count; ["b': "v"}], AUSTIN)
    structure = _outside_strings(q)
    assert "count" not in structure, f"tag KEY was not escaped:\n{structure}"


def test_empty_tag_sets_rejected():
    try:
        overpass.build_query([], AUSTIN)
    except ValueError:
        return
    raise AssertionError("built a query with no filters")


for fn in (test_area_query_uses_the_boundary, test_pointonly_place_falls_back_to_radius,
           test_multiple_tag_sets_are_ored, test_tags_within_a_set_are_anded,
           test_regex_fallback_is_case_insensitive, test_outside_strings_helper_is_honest,
           test_quote_injection_cannot_escape_the_literal,
           test_backslash_injection_is_escaped,
           test_injection_through_a_tag_key_is_escaped, test_empty_tag_sets_rejected):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\noverpass — parsing real-shaped elements")

ELEMENTS = [
    {"type": "node", "id": 1, "lat": 30.27, "lon": -97.74, "tags": {
        "name": "Congress Dental", "amenity": "dentist", "phone": "(512) 555-0101",
        "website": "joescoffee.com", "addr:housenumber": "100",
        "addr:street": "Congress Ave", "addr:city": "Austin", "addr:state": "TX",
        "addr:postcode": "78701", "contact:email": "Hello@Congress.example",
        "contact:instagram": "https://instagram.com/congressdental"}},
    {"type": "way", "id": 2, "center": {"lat": 30.25, "lon": -97.75}, "tags": {
        "name": "South Lamar Orthodontics", "healthcare": "dentist",
        "healthcare:speciality": "orthodontics",
        "contact:phone": "+1-512-555-0102", "addr:street": "S Lamar Blvd"}},
    {"type": "node", "id": 3, "lat": 30.3, "lon": -97.7,
     "tags": {"amenity": "dentist"}},                         # unnamed
    {"type": "relation", "id": 4, "center": {"lat": 30.28, "lon": -97.73}, "tags": {
        "operator": "Mueller Dental Group", "amenity": "dentist",
        "phone": "512.555.0103; 512.555.0104", "website": "//mueller.example/x"}},
]


def test_parse_counts_and_skips_unnamed():
    out = overpass.parse_elements(ELEMENTS, AUSTIN)
    assert out.elements == 4, out.elements
    assert out.unnamed == 1, f"unnamed count {out.unnamed}"
    assert len(out.businesses) == 3, f"kept {len(out.businesses)}"


def test_parse_extracts_contact_fields():
    b = overpass.parse_elements(ELEMENTS, AUSTIN).businesses[0]
    assert b["name"] == "Congress Dental"
    assert b["phone_e164"] == "+15125550101", b["phone_e164"]
    assert b["website"] == "https://joescoffee.com", b["website"]
    assert b["address"] == "100 Congress Ave, Austin, TX, 78701", b["address"]
    assert b["_email"] == "Hello@Congress.example"
    assert b["_socials"]["instagram"].endswith("congressdental")
    assert b["source_id"] == "node/1"


def test_parse_handles_way_center_and_speciality():
    b = overpass.parse_elements(ELEMENTS, AUSTIN).businesses[1]
    assert b["lat"] == 30.25 and b["lon"] == -97.75, "way center not used"
    assert b["category"] == "dentist (orthodontics)", b["category"]
    assert b["phone_e164"] == "+15125550102", b["phone_e164"]
    assert b["source_id"] == "way/2"


def test_parse_uses_operator_and_first_phone_and_fixes_scheme():
    b = overpass.parse_elements(ELEMENTS, AUSTIN).businesses[2]
    assert b["name"] == "Mueller Dental Group", "operator not used as a name"
    assert b["phone_e164"] == "+15125550103", f"multi-phone: {b['phone_e164']}"
    assert b["website"] == "https://mueller.example/x", b["website"]


def test_parse_tolerates_empty_input():
    out = overpass.parse_elements([], AUSTIN)
    assert out.businesses == [] and out.elements == 0


def test_discovery_summary_counts():
    out = overpass.parse_elements(ELEMENTS, AUSTIN)
    assert out.with_phone == 3, out.with_phone
    assert out.with_website == 2, out.with_website
    assert out.with_email == 1, out.with_email


for fn in (test_parse_counts_and_skips_unnamed, test_parse_extracts_contact_fields,
           test_parse_handles_way_center_and_speciality,
           test_parse_uses_operator_and_first_phone_and_fixes_scheme,
           test_parse_tolerates_empty_input, test_discovery_summary_counts):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\noverpass — storing a discovery")


def _conn():
    return db.connect(Path(tempfile.mkdtemp()) / "t.db")


def test_store_writes_businesses_emails_and_socials():
    conn = _conn()
    out = overpass.parse_elements(ELEMENTS, AUSTIN)
    counts = overpass.store(conn, out)
    assert counts["new"] == 3, counts
    assert counts["emails"] == 1 and counts["socials"] == 1, counts
    rows = db.export_rows(conn)
    assert len(rows) == 3, f"exported {len(rows)}"
    assert any(r["emails"] and "hello@congress.example" in r["emails"] for r in rows), \
        "email not lowercased into the export"


def test_store_is_idempotent():
    conn = _conn()
    out = overpass.parse_elements(ELEMENTS, AUSTIN)
    overpass.store(conn, out)
    second = overpass.store(conn, overpass.parse_elements(ELEMENTS, AUSTIN))
    assert second["new"] == 0 and second["updated"] == 3, second
    assert len(db.export_rows(conn)) == 3, "re-running duplicated rows"


def test_store_respects_suppression():
    conn = _conn()
    overpass.store(conn, overpass.parse_elements(ELEMENTS, AUSTIN))
    db.suppress(conn, "email", "hello@congress.example", "opted out")
    row = next(r for r in db.export_rows(conn) if r["title"] == "Congress Dental")
    assert not row["emails"], "suppressed email survived a re-store"


for fn in (test_store_writes_businesses_emails_and_socials, test_store_is_idempotent,
           test_store_respects_suppression):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print("\nphone — best-effort E.164")
PHONES = [
    ("(512) 555-0101", "US", "+15125550101"),
    ("512-555-0101", "US", "+15125550101"),
    ("1 512 555 0101", "US", "+15125550101"),
    ("+1 512 555 0101", "US", "+15125550101"),
    ("+1 512 555 0101", None, "+15125550101"),     # explicit + needs no context
    ("020 7946 0958", "GB", "+442079460958"),      # trunk 0 dropped
    ("+44 (0) 20 7946 0958", "GB", "+442079460958"),
    ("0049 30 123456", "DE", "+4930123456"),       # 00 international prefix
    ("030 123456", "DE", "+4930123456"),
    ("512.555.0103; 512.555.0104", "US", "+15125550103"),
    ("(512) 555-0101 ext 23", "US", "+15125550101"),
    ("555-0101", "US", None),                      # too short to be sure
    ("not a phone", "US", None),
    ("", "US", None),
    (None, "US", None),
    ("030 123456", None, None),                    # no country context
    ("030 123456", "ZZ", None),                    # unknown country
]
for raw, country, want in PHONES:
    def run(raw=raw, country=country, want=want):
        got = phone.normalize(raw, country)
        assert got == want, f"normalize({raw!r}, {country!r}) -> {got!r}, wanted {want!r}"
    check(f"{str(raw)[:26]:28} [{country}] -> {want}", run)

print("\nhttp — trusted-endpoint client")


def test_user_agent_is_mandatory():
    for ua in ("", None, "local-lead-intelligence/0.1 (+https://example.invalid/bot)"):
        try:
            http.request("http://x.test/", user_agent=ua)
        except ValueError as e:
            assert "LLI_CONTACT_URL" in str(e), f"unhelpful: {e}"
            continue
        raise AssertionError(f"accepted placeholder user agent {ua!r}")


def test_backoff_grows_and_is_jittered():
    waits = [http._sleep_for(i) for i in range(4)]
    assert waits[0] < waits[-1], f"not increasing: {waits}"
    assert all(w <= 60.0 for w in waits), f"unbounded: {waits}"
    assert len({round(http._sleep_for(1), 6) for _ in range(8)}) > 1, "no jitter"


def test_retry_after_header_is_honoured():
    assert http._sleep_for(0, "30") == 30.0
    assert http._sleep_for(0, "9999") == 120.0, "did not clamp a huge Retry-After"
    assert http._sleep_for(0, "garbage") > 0, "fell over on a bad header"


for fn in (test_user_agent_is_mandatory, test_backoff_grows_and_is_jittered,
           test_retry_after_header_is_honoured):
    check(fn.__name__.replace("test_", "").replace("_", " "), fn)

print(f"\n{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
