---
description: Crawl business websites already in the database for emails and social profiles
---
Run Tier 2 enrichment over businesses already discovered.

1. `python3 cli.py stats` — see how many have websites and how many lack emails.
2. `python3 cli.py enrich` (add `--limit N` for a trial run first on a big database).
3. Report emails and socials found, plus anything the SSRF guard refused.

Never use WebFetch on the business sites. `enrich` fetches behind the guard,
caches every result, respects robots.txt and throttles per host. Doing it by
hand costs thousands of tokens and skips all of that.

A second run is cheap: cached sites are skipped, and businesses that already
have an email are not revisited.
