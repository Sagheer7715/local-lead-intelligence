---
description: Build a lead list — discover from OpenStreetMap, then enrich with emails and socials
argument-hint: <business type> in <City, Region, Country>
---
Build a lead list. Request: **$ARGUMENTS**

Use the `local-leads` skill. Steps:

1. Parse the business type and the place. If the place lacks a region or country,
   add them (`"Austin"` → `"Austin, Texas, USA"`) — Nominatim needs it to resolve
   an administrative boundary rather than guessing.
2. Check the term maps to an OSM tag: `python3 cli.py categories`. If it does not,
   say so — the name-search fallback finds fewer results.
3. `python3 cli.py discover "<type>" --in "<place>"`
4. `python3 cli.py enrich` — never fetch the sites yourself with WebFetch.
5. `python3 cli.py export --out <type>-<city>.xlsx --socials`
6. Report counts, show 3-5 sample rows, give the file path. Do NOT paste the
   whole list into chat; it is personal data.

Set expectations before step 3: roughly half of these will have a phone, and
crawling yields an email about 17% of the time because most small businesses
publish a contact form instead. Say that up front rather than explaining it after.

If Overpass returns a 502 it is at capacity, not broken — wait, do not retry in
a loop, and mention self-hosting from a Geofabrik extract.
