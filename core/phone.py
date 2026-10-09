"""Best-effort E.164 phone normalization, standard library only.

Honest scope: this handles the cases that actually show up in OSM contact tags —
an already-international `+…` number, a national number plus a country context,
and NANP's various habits. It does not know area-code validity, mobile-vs-fixed
ranges, or carrier rules.

`phonenumbers` (Google's libphonenumber port) does all of that properly. If
dedupe accuracy by phone ever matters more than the zero-dependency rule, that
is the trade to make, and `normalize()` is the single place to swap.
"""
from __future__ import annotations

import re

# Country calling codes for the places this is likely to be pointed at. A miss
# here means the number is returned unnormalized, not mangled.
CALLING_CODES = {
    "US": "1", "CA": "1", "GB": "44", "IE": "353", "DE": "49", "AT": "43",
    "CH": "41", "FR": "33", "ES": "34", "PT": "351", "IT": "39", "NL": "31",
    "BE": "32", "LU": "352", "DK": "45", "SE": "46", "NO": "47", "FI": "358",
    "IS": "354", "PL": "48", "CZ": "420", "SK": "421", "HU": "36", "RO": "40",
    "BG": "359", "GR": "30", "HR": "385", "SI": "386", "RS": "381", "TR": "90",
    "RU": "7", "UA": "380", "AU": "61", "NZ": "64", "JP": "81", "KR": "82",
    "CN": "86", "HK": "852", "SG": "65", "MY": "60", "TH": "66", "ID": "62",
    "PH": "63", "VN": "84", "IN": "91", "PK": "92", "BD": "880", "LK": "94",
    "AE": "971", "SA": "966", "QA": "974", "KW": "965", "BH": "973", "OM": "968",
    "IL": "972", "EG": "20", "ZA": "27", "NG": "234", "KE": "254", "GH": "233",
    "MA": "212", "TN": "216", "BR": "55", "AR": "54", "CL": "56", "CO": "57",
    "PE": "51", "MX": "52", "UY": "598", "EC": "593",
}

_EXT = re.compile(r"(?:\s|^)(?:ext|x|extension|anexo|durchwahl)\.?\s*\d+\s*$", re.I)
_NOT_DIGITS = re.compile(r"[^\d+]")


def _first_number(raw: str) -> str:
    """OSM tags routinely hold several numbers in one value."""
    for sep in (";", ",", " / ", " or ", "|"):
        if sep in raw:
            raw = raw.split(sep)[0]
    return raw.strip()


def normalize(raw, country=None):
    """Return an E.164 string, or None if it cannot be done confidently.

    `country` is an ISO-3166 alpha-2 code supplying the national context,
    normally the country of the place being scraped.
    """
    if not raw:
        return None
    text = _EXT.sub("", str(raw).strip())
    text = _first_number(text)
    if not text:
        return None

    # Keep a leading + and drop every other non-digit, including the "(0)"
    # that German and Dutch listings like to put inside international numbers.
    text = re.sub(r"\(\s*0\s*\)", "", text)
    plus = text.lstrip().startswith("+") or text.lstrip().startswith("00")
    digits = _NOT_DIGITS.sub("", text).lstrip("+")
    if digits.startswith("00"):
        digits = digits[2:]
    if not digits:
        return None

    if plus:
        return "+" + digits if 8 <= len(digits) <= 15 else None

    code = CALLING_CODES.get((country or "").upper())
    if code is None:
        return None

    if code == "1":                              # NANP
        if len(digits) == 11 and digits[0] == "1":
            digits = digits[1:]
        if len(digits) != 10:
            return None
        return "+1" + digits

    # Everywhere else: a leading 0 is a national trunk prefix, not part of the
    # subscriber number, so it is dropped before the country code goes on.
    national = digits[1:] if digits.startswith("0") else digits
    if not national:
        return None
    if digits.startswith(code) and len(digits) > len(code) + 5:
        candidate = "+" + digits        # already carried its country code
    else:
        candidate = "+" + code + national
    return candidate if 8 <= len(candidate) - 1 <= 15 else None


def digits_only(e164) -> str:
    return re.sub(r"\D", "", e164 or "")
