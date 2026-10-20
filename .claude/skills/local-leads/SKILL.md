---
name: local-leads
description: Build free local-business lead lists from OpenStreetMap, then enrich them with emails and social profiles from the businesses' own websites. Use when the user wants leads, prospects, or a contact list for a business type in a place — "dentists in Denver", "build me a lead list of plumbers", "find gyms in Miami with emails". NOT for Amazon sellers (that is a different project) and NOT for scraping social platforms.
---

# Local Lead Intelligence

Drive the CLI in this folder. Everything runs locally; there are no API keys.

## Mental model

Three tiers, and the order matters:

| Tier | Command | Source | Gets you |
|---|---|---|---|
| 1 | `discover` | OpenStreetMap via Overpass | names, addresses, phones, websites |
| 2 | `enrich` | each business's own website | emails, Instagram/Facebook/LinkedIn |
| 3 | `ratings` | Google Maps *(opt-in, off)* | star rating, review count |

Tiers 1 and 2 are free, legal and un-blockable. **Tier 3 scrapes Google, which
violates their ToS** — never run it unless the user asks for ratings explicitly,
and tell them the tradeoff when they do.

## The normal workflow

```bash
python3 cli.py discover "dentists" --in "Austin, Texas, USA"
python3 cli.py enrich
python3 cli.py export --out leads.xlsx --socials
```

`discover` takes several terms at once — `discover "dentists" "orthodontists"
--in "Denver, Colorado, USA"` — which is one Overpass job per term.

Always give the place with its region and country: `"Austin, Texas, USA"`, not
`"Austin"`. Nominatim resolves it to an administrative boundary, so the search
uses exact city limits rather than a radius.

## Rules

**Never fetch business websites yourself with WebFetch.** `enrich` does it in
code, behind an SSRF guard, cached and rate-limited. Doing it yourself costs
thousands of tokens, skips the guard, and hammers the sites.

**Expect partial coverage, and say so up front.** Measured on 433 real Austin
businesses: about half have a phone in OSM, half have a website, and crawling
those sites yields an email about 17% of the time — most small businesses
publish a contact form instead of an address. A realistic promise is "roughly
half of these will be contactable", not "a full contact list".

**Unknown business types still work, worse.** 66 categories map to OSM tags;
`python3 cli.py categories` lists them. Anything else falls back to matching
the business *name*, which finds fewer results. Tell the user when that happens.

**Overpass is donated capacity.** A 502 means it is at capacity, not broken.
Wait rather than retrying in a loop, and mention that self-hosting from a
Geofabrik extract removes the limit entirely.

**Scraped emails and phones are personal data.** Don't paste a whole list into
chat — save the file, show a few rows, give the path. If the user will contact
these people, remind them about GDPR/CCPA/CAN-SPAM, and use
`python3 cli.py suppress email <addr>` for anyone who opts out. Suppression
survives re-scraping; that is the point of it.

## Tier 3, if the user asks for ratings

It is gated twice on purpose: `LLI_TIER3_ENABLED=true` in `.env` *and* `--yes`
on the command. Before running it, say plainly that it scrapes Google Maps
against their Terms of Service and can get their IP temporarily rate-limited.

```bash
docker compose --profile tier3 up -d          # starts the engine
python3 cli.py ratings "dentists" --in "Austin, Texas, USA" --yes
docker compose --profile tier3 down
```

It adds ratings to businesses already in the database; it discovers nothing.
Start at `--depth 5`.

## Other commands

```bash
python3 cli.py stats                  # what is in the database
python3 cli.py categories             # business types that map to OSM tags
python3 cli.py suppress email a@b.com --reason "unsubscribed"
python3 cli.py suppress --list
python3 cli.py purge --days 90 --yes  # delete records past retention
make test                             # 213 offline tests
```

## Output

`export` writes `.xlsx`, `.csv` or `.json`, chosen by the file extension.
**Prefer `.xlsx`** — cells carry a type, so a business named
`=HYPERLINK(...)` cannot execute, and phone numbers display cleanly. CSV is
safe too but neutralises formulas with a visible leading apostrophe.

Every output file is written `0600`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `a real contact User-Agent is required` | `LLI_CONTACT_URL` unset in `.env` |
| `Overpass is at capacity` | public instance busy; wait or self-host |
| `TLS handshake refused` | macOS Python has no TLS 1.3; the curl fallback handles it |
| `nothing to enrich` | no websites yet, or all already enriched |
| `no engine at http://127.0.0.1:8080` | Tier 3 container not started |
