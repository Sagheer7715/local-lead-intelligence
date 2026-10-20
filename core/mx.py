"""Minimal DNS client for MX validation, standard library only.

An address whose domain has no mail exchanger cannot receive mail. Checking
that is the cheapest, highest-leverage quality filter available for a lead
list: it is one UDP packet, costs nothing, and removes the bounces that get a
sending domain blocklisted.

Python has no DNS resolver in the standard library and `socket` only exposes
A/AAAA lookups, so this speaks enough of RFC 1035 to ask one question.

Deliberately uses the resolver from /etc/resolv.conf rather than a hardcoded
public one — queries reveal which domains are being researched, and that is
not something to hand to a third party that the user did not choose.
"""
from __future__ import annotations

import random
import re
import socket
import struct

TYPE_A = 1
TYPE_MX = 15
CLASS_IN = 1

_RESOLV_CONF = "/etc/resolv.conf"
_LABEL_OK = re.compile(r"^[A-Za-z0-9_-]{1,63}$")


class DnsUnavailable(Exception):
    """No usable resolver, so MX status is unknown rather than false."""


def system_resolvers(path=_RESOLV_CONF) -> list:
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) > 1:
                        out.append(parts[1])
    except OSError:
        pass
    return out


def _encode_name(domain: str) -> bytes:
    labels = domain.strip(".").split(".")
    # Empty labels are rejected, not filtered out: "a..b" is malformed, and
    # silently turning it into "a.b" would query a domain nobody asked about.
    if not labels or any(not label for label in labels):
        raise ValueError(f"malformed domain {domain!r}")
    out = b""
    for label in labels:
        try:
            encoded = label.encode("idna") if not label.isascii() else label.encode("ascii")
        except UnicodeError as e:
            raise ValueError(f"bad label {label!r}") from e
        if not _LABEL_OK.match(encoded.decode("ascii", "replace")):
            raise ValueError(f"bad label {label!r}")
        out += bytes([len(encoded)]) + encoded
    return out + b"\x00"


def build_query(domain: str, qtype: int = TYPE_MX):
    txid = random.randint(0, 0xFFFF)
    # RD=1 (recursion desired), one question, no other sections.
    header = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    body = _encode_name(domain) + struct.pack(">HH", qtype, CLASS_IN)
    return txid, header + body


def _skip_name(data: bytes, i: int) -> int:
    """Step over a (possibly compressed) name and return the next offset."""
    while True:
        if i >= len(data):
            raise ValueError("truncated name")
        length = data[i]
        if length == 0:
            return i + 1
        if length & 0xC0 == 0xC0:          # pointer: two bytes, ends the name
            return i + 2
        i += 1 + length


def parse_answer_types(data: bytes, txid: int) -> list:
    """Return the record types present in the answer section.

    The rdata is never parsed — the question is only whether a mail exchanger
    exists, so the type codes are enough and there is less to get wrong.
    """
    if len(data) < 12:
        raise ValueError("short response")
    rid, flags, qdcount, ancount, _, _ = struct.unpack(">HHHHHH", data[:12])
    if rid != txid:
        raise ValueError("transaction id mismatch")
    rcode = flags & 0x000F
    if rcode == 3:                          # NXDOMAIN: definitively no domain
        return []
    if rcode != 0:
        raise ValueError(f"server returned rcode {rcode}")

    i = 12
    for _ in range(qdcount):
        i = _skip_name(data, i) + 4         # + qtype, qclass
    types = []
    for _ in range(ancount):
        i = _skip_name(data, i)
        if i + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", data[i:i + 10])
        types.append(rtype)
        i += 10 + rdlength
    return types


def query(domain: str, qtype: int = TYPE_MX, *, resolvers=None, timeout=3.0) -> list:
    servers = resolvers if resolvers is not None else system_resolvers()
    if not servers:
        raise DnsUnavailable("no nameserver in /etc/resolv.conf")
    txid, packet = build_query(domain, qtype)
    last = None
    for server in servers[:3]:
        try:
            family = socket.AF_INET6 if ":" in server else socket.AF_INET
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(packet, (server, 53))
                data, _ = s.recvfrom(4096)
            return parse_answer_types(data, txid)
        except (OSError, ValueError) as e:
            last = e
            continue
    raise DnsUnavailable(f"no resolver answered for {domain}: {last}")


def has_mx(domain: str, *, resolvers=None, timeout=3.0):
    """True / False / None (unknown).

    None matters: a resolver timeout is not evidence that an address is bad,
    and treating it as such would silently discard good leads. Only a clean
    answer counts.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return False
    try:
        types = query(domain, TYPE_MX, resolvers=resolvers, timeout=timeout)
    except DnsUnavailable:
        return None
    except ValueError:
        return None
    if TYPE_MX in types:
        return True
    # RFC 5321 §5.1: with no MX, an A record is itself a valid mail target.
    try:
        return TYPE_A in query(domain, TYPE_A, resolvers=resolvers, timeout=timeout)
    except (DnsUnavailable, ValueError):
        return None
