"""Pull contact details out of a business website's HTML.

This is where the reference kit's two extraction bugs get fixed properly
rather than patched:

  * It stripped backslashes from the *matched URL* but matched against raw
    HTML, so escaped links (`https:\\/\\/instagram.com\\/x`) never matched at
    all and the stripping was dead code. Modern site builders — Squarespace,
    Wix, Shopify, Next.js — embed social links inside JSON exactly that way.
  * It rejected every all-digit Facebook handle to skip years like `/2008`,
    which also threw away legitimate numeric page IDs.
"""
from __future__ import annotations

import html as _html
import re
from urllib.parse import urlsplit

# ── emails ──────────────────────────────────────────────────────────────────

_EMAIL = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
_MAILTO = re.compile(r"mailto:([^\"'>?\s&]+)", re.I)

# Addresses that exist on a page but are never a lead.
_JUNK_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
               "mailer-daemon", "postmaster", "abuse", "bounce", "bounces",
               "wordpress", "root", "nobody", "user", "username", "email",
               "youremail", "name", "firstname", "example", "sentry"}
_JUNK_DOMAIN = {"example.com", "example.org", "example.net", "domain.com",
                "yourdomain.com", "email.com", "sentry.io", "wixpress.com",
                "sentry-next.wixpress.com", "godaddy.com", "squarespace.com",
                "wix.com", "shopify.com", "placeholder.com", "test.com",
                "yoursite.com", "mysite.com", "company.com", "gravatar.com"}
# A filename that happens to look like an address: logo@2x.png, icon@3x.jpg
_ASSET_TAIL = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js|ico|woff2?|ttf|mp4)$", re.I)

# A generic inbox is still worth contacting, but it is not a named person.
_ROLE_LOCAL = {"info", "contact", "hello", "hi", "enquiries", "enquiry", "inquiries",
               "inquiry", "office", "admin", "mail", "team", "sales", "support",
               "help", "reception", "booking", "bookings", "appointments",
               "appointment", "service", "services", "orders", "shop", "studio",
               "frontdesk", "front desk", "general", "welcome", "customerservice"}


def _unescape(text: str) -> str:
    r"""Normalise HTML entities and JSON escaping before matching.

    `https:\/\/instagram.com\/x` and `https:&#x2F;&#x2F;...` both have to
    become a plain URL, or the patterns below never see them.
    """
    if not text:
        return ""
    out = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    out = out.replace("\\x2F", "/").replace("\\x2f", "/")
    if "&" in out:
        out = _html.unescape(out)
    return out


_CFEMAIL = re.compile(
    r"(?:data-cfemail=[\"']|/cdn-cgi/l/email-protection#)([0-9a-fA-F]{6,})")


def decode_cfemail(hex_text: str) -> str:
    """Decode Cloudflare's email obfuscation.

    Cloudflare rewrites published addresses to a hex blob whose first byte is
    an XOR key. It is trivially reversible and common on small-business sites,
    so skipping it loses real addresses that the owner did choose to publish.
    """
    try:
        data = bytes.fromhex(hex_text)
    except ValueError:
        return ""
    if len(data) < 2:
        return ""
    key = data[0]
    out = "".join(chr(b ^ key) for b in data[1:])
    return out if out.isprintable() else ""


def _clean_email(raw: str) -> str:
    addr = (raw or "").strip().strip(".,;:<>()[]\"'").lower()
    addr = addr.split("?")[0]                      # mailto:a@b.com?subject=…
    if addr.count("@") != 1:
        return ""
    local, _, domain = addr.partition("@")
    if not local or not domain or "." not in domain:
        return ""
    if _ASSET_TAIL.search(domain) or _ASSET_TAIL.search(addr):
        return ""
    if local in _JUNK_LOCAL:
        return ""
    # Suffix match, not equality: platform noise arrives on subdomains too
    # (sentry.wixpress.com, sentry-next.wixpress.com, cdn.shopify.com).
    if any(domain == junk or domain.endswith("." + junk) for junk in _JUNK_DOMAIN):
        return ""
    if domain.split(".")[-1].isdigit():            # 1.2.3.4 style
        return ""
    if len(addr) > 254 or len(local) > 64:
        return ""
    return addr


def classify_email(addr: str) -> str:
    local = addr.partition("@")[0]
    base = re.split(r"[._\-+]", local)[0]
    return "role" if local in _ROLE_LOCAL or base in _ROLE_LOCAL else "personal"


def find_emails(html_text, site_domain="") -> list:
    """Return [{email, kind, confidence}], best first.

    A `mailto:` is a deliberate publication of an address; one found loose in
    body text might be a customer's, a supplier's, or the web designer's. An
    address on the site's own domain is far more likely to be the business's.
    """
    text = _unescape(html_text or "")
    found = {}

    def add(addr, confidence):
        clean = _clean_email(addr)
        if clean and confidence > found.get(clean, 0.0):
            found[clean] = confidence

    for match in _CFEMAIL.finditer(text):
        # Deliberately published, just obfuscated in transit — same weight as
        # a mailto, which is what it was before Cloudflare rewrote it.
        add(decode_cfemail(match.group(1)), 0.9)
    for match in _MAILTO.finditer(text):
        add(match.group(1), 0.9)
    for match in _EMAIL.finditer(text):
        add(match.group(0), 0.6)

    site = (site_domain or "").lower().lstrip("www.")
    results = []
    for addr, confidence in found.items():
        domain = addr.partition("@")[2]
        if site and (domain == site or domain.endswith("." + site)):
            confidence = min(1.0, confidence + 0.2)
        results.append({"email": addr, "kind": classify_email(addr),
                        "confidence": round(confidence, 2)})
    # Personal addresses first at equal confidence: a named human outranks a
    # shared inbox for outreach.
    results.sort(key=lambda r: (-r["confidence"], r["kind"] != "personal", r["email"]))
    return results


# ── socials ─────────────────────────────────────────────────────────────────

SOCIAL_PATTERNS = {
    "instagram": re.compile(
        r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)", re.I),
    "facebook": re.compile(
        r"https?://(?:www\.|m\.|web\.|[a-z]{2}-[a-z]{2}\.)?facebook\.com/([A-Za-z0-9_.\-]+)",
        re.I),
    "linkedin": re.compile(
        r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in|school)/([A-Za-z0-9_.\-%]+)",
        re.I),
}

# Path segments that are site furniture, not an account.
_NOT_A_HANDLE = {
    "", "home", "pages", "page", "people", "help", "about", "policies", "policy",
    "legal", "tos", "terms", "privacy", "settings", "sharer", "share", "tr",
    "profile.php", "plugins", "dialog", "intent", "login", "signup", "share.php",
    "permalink.php", "p", "reel", "reels", "explore", "stories", "tv", "watch",
    "events", "event", "groups", "group", "marketplace", "gaming", "photo",
    "photos", "hashtag", "search", "directory", "posts", "media", "video",
    "videos", "story", "embed", "oauth", "sharing", "l.php", "flx", "accounts",
}


def _plausible_handle(platform: str, handle: str) -> bool:
    h = handle.lower()
    if h in _NOT_A_HANDLE or len(h) < 2:
        return False
    if h.isdigit():
        # A real Facebook page id is a long number; a short one is a year or a
        # version from a copyright line. Rejecting all digits — as the
        # reference kit does — discards legitimate pages.
        return platform == "facebook" and len(h) >= 9
    return True


def find_socials(html_text) -> dict:
    text = _unescape(html_text or "")
    out = {}
    for platform, pattern in SOCIAL_PATTERNS.items():
        for match in pattern.finditer(text):
            handle = match.group(1)
            if not _plausible_handle(platform, handle):
                continue
            url = match.group(0).rstrip("\"'/\\)")
            out[platform] = {"url": url, "handle": handle}
            break
    return out


# ── link discovery ──────────────────────────────────────────────────────────

_HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
_CONTACT_HINT = re.compile(
    r"(contact|kontakt|about|impressum|team|reach-?us|get-?in-?touch|connect)", re.I)


def find_contact_links(html_text, base_url, limit=2) -> list:
    """Same-site pages most likely to carry an address.

    Homepages often carry only a form. The address usually lives one click
    away, so following a couple of obvious links roughly doubles the yield.
    """
    text = _unescape(html_text or "")
    base = urlsplit(base_url)
    if not base.hostname:
        return []
    seen, out = set(), []
    for match in _HREF.finditer(text):
        href = match.group(1).strip()
        if not _CONTACT_HINT.search(href):
            continue
        if href.startswith("//"):
            url = f"{base.scheme}:{href}"
        elif href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = f"{base.scheme}://{base.netloc}{href}"
        else:
            url = f"{base.scheme}://{base.netloc}/{href.lstrip('./')}"
        parts = urlsplit(url)
        # Same registrable host only: never follow a contact link off-site.
        if (parts.hostname or "").lstrip("www.") != (base.hostname or "").lstrip("www."):
            continue
        if url in seen or url.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out
