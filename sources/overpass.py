"""Tier 1 discovery: businesses from OpenStreetMap via the Overpass API.

This is the free, legal, un-blockable half of the project. Open data under ODbL,
no key, no quota beyond politeness, and a `website`/`phone`/`contact:email` tag
on a surprising number of POIs already.

What OSM does not have is ratings and review counts. That is Tier 3's whole job
and the only reason it exists.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from core import db, http, phone
from sources import categories
from sources.geocode import Place

# Overpass answers can be slow; its own timeout must exceed our patience for it.
QUERY_TIMEOUT = 180
HTTP_TIMEOUT = 240.0


class OverpassError(Exception):
    pass


@dataclass
class Discovery:
    """One discovery run's results and the numbers worth reporting."""
    businesses: list = field(default_factory=list)
    elements: int = 0
    unnamed: int = 0
    query: str = ""
    canonical: str = ""
    seconds: float = 0.0

    @property
    def with_phone(self) -> int:
        return sum(1 for b in self.businesses if b.get("phone_e164"))

    @property
    def with_website(self) -> int:
        return sum(1 for b in self.businesses if b.get("website"))

    @property
    def with_email(self) -> int:
        return sum(1 for b in self.businesses if b.get("_email"))


def _escape(value: str) -> str:
    r"""Quote a tag value for Overpass QL.

    Overpass string literals are double-quoted with backslash escapes. A city
    name like `Washington "DC"` or a regex fallback from user input must not be
    able to close the quote and append its own statements.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def build_query(tag_sets, place: Place, *, radius=15000, timeout=QUERY_TIMEOUT) -> str:
    """Compose Overpass QL for a set of tag filters inside a place.

    `nwr` covers nodes, ways and relations in one statement: a business may be
    mapped as a point, a building outline or a multipolygon, and all three are
    equally real. `out center tags` returns one representative coordinate per
    element plus every tag, which is all Tier 1 needs.
    """
    if not tag_sets:
        raise ValueError("no tag sets to query")

    if place.has_area:
        scope = f"area({place.area_id})->.searchArea;"
        selector = "(area.searchArea)"
    else:
        # No polygon for this place, so fall back to a circle around the point.
        scope = ""
        selector = f"(around:{radius},{place.lat},{place.lon})"

    clauses = []
    for tags in tag_sets:
        filters = ""
        for key, value in tags.items():
            if key.startswith("~"):              # regex match on a key's value
                filters += f'["{_escape(key[1:])}"~"{_escape(value)}",i]'
            else:
                filters += f'["{_escape(key)}"="{_escape(value)}"]'
        clauses.append(f"  nwr{filters}{selector};")

    return (f"[out:json][timeout:{timeout}];\n"
            f"{scope}\n"
            "(\n" + "\n".join(clauses) + "\n);\n"
            "out center tags;")


def _address(tags) -> str:
    parts = []
    house, street = tags.get("addr:housenumber"), tags.get("addr:street")
    if house and street:
        parts.append(f"{house} {street}")
    elif street:
        parts.append(street)
    elif tags.get("addr:place"):
        parts.append(tags["addr:place"])
    for key in ("addr:city", "addr:state", "addr:postcode"):
        if tags.get(key):
            parts.append(tags[key])
    return ", ".join(parts)


def _category(tags) -> str:
    """A human-readable category from whichever classifying tag is present."""
    for key in ("amenity", "shop", "craft", "office", "healthcare", "leisure",
                "tourism", "club"):
        if tags.get(key):
            value = tags[key].replace("_", " ")
            speciality = tags.get("healthcare:speciality")
            if speciality:
                return f"{value} ({speciality.replace('_', ' ')})"
            return value
    return ""


def _first(tags, *keys):
    for key in keys:
        value = (tags.get(key) or "").strip()
        if value:
            return value
    return ""


def _website(tags) -> str:
    url = _first(tags, "website", "contact:website", "url", "contact:url")
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def parse_elements(elements, place: Place) -> Discovery:
    """Turn raw Overpass elements into business records."""
    out = Discovery(elements=len(elements))
    country = (place.country_code or "").upper()

    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or tags.get("operator") or "").strip()
        if not name:
            # An unnamed POI cannot be contacted or deduped. Counted, not kept.
            out.unnamed += 1
            continue

        center = el.get("center") or {}
        lat = el.get("lat", center.get("lat"))
        lon = el.get("lon", center.get("lon"))
        raw_phone = _first(tags, "phone", "contact:phone", "contact:mobile", "mobile")

        record = {
            "source_id": f"{el.get('type', 'node')}/{el.get('id')}",
            "name": name,
            "category": _category(tags),
            "address": _address(tags),
            "city": _first(tags, "addr:city") or place.name.split(",")[0].strip(),
            "region": _first(tags, "addr:state", "addr:province"),
            "country": country,
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "phone_raw": raw_phone or None,
            "phone_e164": phone.normalize(raw_phone, country) if raw_phone else None,
            "website": _website(tags) or None,
            # Carried separately: these go to the emails/socials tables, not to
            # a column on businesses.
            "_email": _first(tags, "email", "contact:email"),
            "_socials": {
                p: tags[f"contact:{p}"].strip()
                for p in ("instagram", "facebook", "linkedin")
                if (tags.get(f"contact:{p}") or "").strip()
            },
        }
        out.businesses.append(record)
    return out


def discover(cfg, term, place: Place, *, radius=15000, client=http) -> Discovery:
    """Run one Tier 1 discovery query. Does not touch the database."""
    canonical, tag_sets = categories.resolve(term)
    if not tag_sets:
        tag_sets = categories.name_fallback(term)
        if not tag_sets:
            raise OverpassError(f"cannot interpret {term!r} as a business type")

    query = build_query(tag_sets, place, radius=radius)
    started = time.monotonic()
    try:
        payload = client.post_json(
            cfg.overpass_url, {"data": query},
            user_agent=cfg.user_agent, timeout=HTTP_TIMEOUT)
    except http.RateLimited as e:
        raise OverpassError(
            "Overpass is rate-limiting this client. It is free shared "
            "infrastructure — wait a few minutes, or run your own instance "
            "from a Geofabrik extract and set LLI_OVERPASS_URL.") from e
    except http.HttpError as e:
        if e.status in (502, 504):
            # The public instance says this itself: it is "intended for
            # browser-based and human-operated tools". Repeated automated runs
            # will keep hitting the queue, and more retries only add load.
            raise OverpassError(
                f"Overpass is at capacity ({e.status}: {e}). This public endpoint "
                "is meant for human-operated use, not batch runs. Wait and retry, "
                "or run your own instance from a Geofabrik extract and point "
                "LLI_OVERPASS_URL at it — no queue, no limits, no contact needed."
            ) from e
        detail = f" — {e.body.strip()[:200]}" if getattr(e, "body", "") else ""
        raise OverpassError(f"Overpass query failed: {e}{detail}") from e

    if "remark" in payload and not payload.get("elements"):
        raise OverpassError(f"Overpass returned: {payload['remark']}")

    result = parse_elements(payload.get("elements") or [], place)
    result.query = query
    result.seconds = time.monotonic() - started
    result.canonical = canonical or f"name~{term}"
    return result


def store(conn, result: Discovery, source="osm") -> dict:
    """Persist a Discovery. Returns counts. Commits once at the end."""
    new = updated = emails = socials = 0
    for rec in result.businesses:
        fields = {k: v for k, v in rec.items() if not k.startswith("_")}
        source_id = fields.pop("source_id")
        bid, created = db.upsert_business(conn, source, source_id, **fields)
        new += created
        updated += not created
        if rec.get("_email"):
            # An address straight off the business's own OSM entry is the
            # strongest signal available before any crawling happens.
            if db.add_email(conn, bid, rec["_email"], kind="unknown",
                            confidence=0.9, source_url=f"osm:{source_id}"):
                emails += 1
        for platform, url in (rec.get("_socials") or {}).items():
            socials += db.add_social(conn, bid, platform, url)
    conn.commit()
    return {"new": new, "updated": updated, "emails": emails, "socials": socials}
