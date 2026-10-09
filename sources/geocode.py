"""Place names to searchable areas, via OpenStreetMap Nominatim.

The useful trick here: Nominatim returns the OSM object behind a place, so a
city resolves to its actual administrative boundary rather than a point. An
Overpass `area` built from that relation gives exact city limits, where the
`around:radius` circle the reference kit uses either clips the suburbs or spills
into the next town.

Only a place that has no polygon falls back to a radius.

Nominatim's usage policy: at most 1 request/second, a real identifying
User-Agent, and no bulk reverse geocoding. `Geocoder` holds the rate limit for
you; one instance per run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from core import http

# Overpass area ids are offset OSM ids: relations +3600000000, ways +2400000000.
# Nodes have no area, hence the radius fallback.
_AREA_OFFSET = {"relation": 3_600_000_000, "way": 2_400_000_000}


@dataclass(frozen=True)
class Place:
    name: str              # Nominatim's display name
    osm_type: str          # node | way | relation
    osm_id: int
    lat: float
    lon: float
    country_code: str      # ISO-3166 alpha-2, lowercase, for phone normalization
    area_id: int = 0       # 0 when the place has no polygon

    @property
    def has_area(self) -> bool:
        return self.area_id > 0

    def __str__(self) -> str:
        how = f"area {self.area_id}" if self.has_area else f"point {self.lat},{self.lon}"
        return f"{self.name} ({how})"


class GeocodeError(Exception):
    pass


class Geocoder:
    def __init__(self, cfg, *, sleep=time.sleep):
        self.cfg = cfg
        self._sleep = sleep
        self._last_call = 0.0
        self._cache = {}

    def _throttle(self):
        """Nominatim allows ~1 req/sec. Wait out the remainder, every time."""
        gap = time.monotonic() - self._last_call
        if self._last_call and gap < 1.1:
            self._sleep(1.1 - gap)
        self._last_call = time.monotonic()

    def lookup(self, place) -> Place:
        key = (place or "").strip().lower()
        if not key:
            raise GeocodeError("no place given")
        if key in self._cache:
            return self._cache[key]

        self._throttle()
        try:
            hits = http.get_json(
                self.cfg.nominatim_url,
                {"q": place, "format": "jsonv2", "limit": 5, "addressdetails": 1},
                user_agent=self.cfg.user_agent, timeout=30.0)
        except http.HttpError as e:
            raise GeocodeError(f"Nominatim failed for {place!r}: {e}") from e
        if not hits:
            raise GeocodeError(
                f"no match for {place!r} — try adding a region, e.g. 'Austin, Texas, USA'")

        # Prefer a hit with a polygon: it gives exact boundaries instead of a circle.
        best = next((h for h in hits if h.get("osm_type") in _AREA_OFFSET), hits[0])
        osm_type = best.get("osm_type", "node")
        osm_id = int(best.get("osm_id", 0))
        area_id = _AREA_OFFSET[osm_type] + osm_id if osm_type in _AREA_OFFSET else 0
        resolved = Place(
            name=best.get("display_name", place),
            osm_type=osm_type,
            osm_id=osm_id,
            lat=float(best.get("lat", 0.0)),
            lon=float(best.get("lon", 0.0)),
            country_code=((best.get("address") or {}).get("country_code") or "").lower(),
            area_id=area_id,
        )
        self._cache[key] = resolved
        return resolved
