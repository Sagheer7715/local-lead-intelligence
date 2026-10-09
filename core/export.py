"""Exporters: CSV, XLSX and JSON.

CSV is the lossy-but-universal option — every cell goes through the formula
neutralizer, which makes an E.164 phone render with a leading apostrophe.
XLSX is the better default for spreadsheets: cells carry an explicit type, a
text cell is structurally incapable of being a formula (a formula needs an `<f>`
element, which we never write), and numbers stay numbers. Written by hand as
OOXML so the project keeps zero dependencies.

Every output file is created 0600. These files hold personal data.
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

from .sanitize import csv_cell

NUMERIC_FIELDS = frozenset({"review_rating", "review_count", "lat", "lon"})


def _private(path, flags):
    return os.open(path, flags, 0o600)


# ── CSV ─────────────────────────────────────────────────────────────────────

def write_csv(rows, fields, path) -> Path:
    import csv
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8", opener=_private) as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: csv_cell(r.get(k, "")) for k in fields})
    return path


# ── JSON ────────────────────────────────────────────────────────────────────

def write_json(rows, fields, path) -> Path:
    path = Path(path)
    trimmed = [{k: r.get(k) for k in fields} for r in rows]
    with open(path, "w", encoding="utf-8", opener=_private) as f:
        json.dump(trimmed, f, indent=2, ensure_ascii=False)
    return path


# ── XLSX ────────────────────────────────────────────────────────────────────

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Leads" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# Two cell formats: 0 = default, 1 = bold (the header row).
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
</styleSheet>"""


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(ord("A") + rem) + s
    return s


def _xml_escape(text: str) -> str:
    out = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
               .replace('"', "&quot;"))
    # Control characters other than tab/LF/CR are not legal in XML 1.0 and will
    # make Excel declare the file corrupt. Scraped text contains them.
    return "".join(c for c in out if c in "\t\n\r" or ord(c) >= 0x20)


def _as_number(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _cell(ref: str, value, *, numeric_ok: bool, style: int = 0) -> str:
    s = "" if style == 0 else f' s="{style}"'
    if numeric_ok:
        num = _as_number(value)
        if num is not None:
            return f'<c r="{ref}"{s}><v>{num}</v></c>'
    text = "" if value is None else str(value)
    if not text:
        return f'<c r="{ref}"{s}/>'
    # inlineStr: typed as a string, so it is never evaluated. No escaping
    # dance needed and no apostrophe prefix in the visible value.
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{_xml_escape(text)}</t></is></c>'


def write_xlsx(rows, fields, path, *, numeric_fields=NUMERIC_FIELDS) -> Path:
    path = Path(path)
    fields = list(fields)

    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             '<sheetData>']
    header = "".join(_cell(f"{_col_letter(i)}1", f, numeric_ok=False, style=1)
                     for i, f in enumerate(fields, start=1))
    parts.append(f'<row r="1">{header}</row>')
    for rownum, row in enumerate(rows, start=2):
        cells = "".join(
            _cell(f"{_col_letter(i)}{rownum}", row.get(f),
                  numeric_ok=f in numeric_fields)
            for i, f in enumerate(fields, start=1))
        parts.append(f'<row r="{rownum}">{cells}</row>')
    parts.append("</sheetData></worksheet>")
    sheet = "".join(parts)

    with open(path, "wb", opener=_private) as fh:
        with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", _CONTENT_TYPES)
            z.writestr("_rels/.rels", _ROOT_RELS)
            z.writestr("xl/workbook.xml", _WORKBOOK)
            z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
            z.writestr("xl/styles.xml", _STYLES)
            z.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


def write(rows, fields, path) -> Path:
    """Dispatch on the file extension."""
    ext = Path(path).suffix.lower()
    if ext == ".xlsx":
        return write_xlsx(rows, fields, path)
    if ext == ".json":
        return write_json(rows, fields, path)
    if ext in (".csv", ""):
        return write_csv(rows, fields, path)
    raise ValueError(f"unsupported output format {ext!r} (use .csv, .xlsx or .json)")
