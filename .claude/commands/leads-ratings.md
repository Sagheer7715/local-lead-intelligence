---
description: OPT-IN — add Google ratings and review counts to businesses already found
argument-hint: <business type> in <City, Region, Country>
---
Tier 3. Request: **$ARGUMENTS**

**Tell the user this before doing anything:** this scrapes Google Maps, which
violates Google's Terms of Service and can get their IP temporarily
rate-limited (minutes to hours; their Google account is unaffected). It adds
ratings and review counts to businesses already in the database — it discovers
nothing new. Wait for them to agree.

Then:
1. Confirm `LLI_TIER3_ENABLED=true` in `.env`. If not, tell them; do not edit it
   for them — the setting is their decision to make.
2. `docker compose --profile tier3 up -d`, then wait ~10s.
3. `python3 cli.py ratings "<type>" --in "<place>" --yes --depth 5`
4. `docker compose --profile tier3 down` when finished.

Start at depth 5. If the job fails repeatedly, Google is throttling the IP —
stop, do not retry in a loop.
