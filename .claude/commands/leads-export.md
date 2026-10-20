---
description: Export the lead list to a spreadsheet
argument-hint: [--city "City"] [--out file.xlsx]
---
Export leads. Options: **$ARGUMENTS**

`python3 cli.py export --out <name>.xlsx --socials`

Prefer `.xlsx` over `.csv`: cells carry a type, so a business named
`=HYPERLINK(...)` cannot execute and phone numbers display without the
apostrophe that CSV needs. Files are written `0600`.

Report the row count and the path. Show a few sample rows at most — the file
holds personal data and does not belong pasted into a conversation.
