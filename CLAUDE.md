# CLAUDE.md — local-lead-intelligence

Free local-business lead lists. Zero dependencies, Python 3.9+, everything runs
on this machine.

## When the user wants leads

**Load the `local-leads` skill** (`.claude/skills/local-leads/SKILL.md`) and
follow it. Don't improvise the commands — the skill has the real yield numbers,
the gating rules, and the things that will bite you.

## Architecture, in one paragraph

OpenStreetMap is the primary source, not Google. That one decision is why the
project is free, legal and un-blockable: Google-first tools burn your IP as the
consumable, violate ToS in the critical path, and need paid proxies to scale.
Tier 1 discovers from Overpass, Tier 2 enriches from each business's own
website, and Tier 3 — opt-in, off, double-gated — adds the ratings OSM lacks.

## Things to get right

- **Never WebFetch a business website.** `cli.py enrich` does it behind the
  SSRF guard in `core/safe_fetch.py`, cached and throttled. These URLs come out
  of scraped data and are exactly the attacker-controlled input the guard exists
  for.
- **Two HTTP clients, deliberately.** `core/safe_fetch.py` for untrusted scraped
  URLs — strict, IP-pinned, no fallbacks. `core/http.py` for endpoints named in
  config, where localhost and odd ports are legitimate. Never mix them.
- **Places need a region and country.** `"Austin, Texas, USA"`, not `"Austin"`.
- **Zero pip dependencies is a security property**, not an aesthetic. Argue for
  a dependency before adding one; there is a stdlib DNS client in `core/mx.py`
  rather than a dependency on `dnspython`.
- **Don't edit `.env`.** `LLI_CONTACT_URL` and `LLI_TIER3_ENABLED` are the
  user's decisions — the first sends an identifier to a third party, the second
  turns on ToS-violating scraping.
- **Personal data.** Save the file, show a few rows, give the path. Never paste
  a list of emails and phones into chat.

## Test before you claim

`make test` — 213 offline tests, no network, no Docker, no pip. Run it after
any change. Every bug fixed in this project was found by a test or by a live
run, not by reading the code.

## Setup

`cp .env.example .env && chmod 600 .env`, then set `LLI_CONTACT_URL`. Full
detail in [README.md](README.md).
