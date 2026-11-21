#!/usr/bin/env python3
"""dev/snmpwalk/scrub.py: replace identifying values in an SNMP walk.

A development tool (see dev/snmpwalk/README.md). Raw walks never enter git:
scrub any excerpt before you share it (an issue, a pull request, dev/notes/).
Serials, MAC addresses and IPv4 addresses are replaced as text and as hex
bytes (-Ox output), MAC addresses also inside Brother node names (BRN/BRW +
MAC) and IPv4 addresses also inside OID indexes. The placeholders have the
same byte length, so byte offsets used to derive encodings stay valid:

    serial "K1Z123456" -> "XXXXXXXXX"
    MAC                -> 00:00:5E:00:53:NN  (RFC 7042 documentation range)
    IPv4               -> 192.0.2.N          (RFC 5737 documentation range)

    dev/snmpwalk/scrub.py WALK [--serial S]... [--mac M]... [--ip A]... [-o OUT]

Besides the values given as options, values are collected from the walk
itself: ifPhysAddress and ipAdEntAddr (MIB-2 walks) and serial-like strings
(readable walks of the Brother MIB). A final pass then replaces any private
IPv4 address and any ':'- or '-'-separated MAC address that is left.
Exit code 2 if a private IPv4 address is still present afterwards.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import sys

MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})\b")
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
LINE_RE = re.compile(r"^(\.?\d+(?:\.\d+)*)\s*=\s*(?:([A-Za-z][A-Za-z0-9-]*):\s?)?(.*)$")
# OIDs whose values identify the unit (auto-collection).
IFPHYS = "1.3.6.1.2.1.2.2.1.6."
IPADENT = "1.3.6.1.2.1.4.20.1.1."
# Brother serials are short upper-case alphanumerics with at least one digit, e.g. "E12345A6".
SERIAL_LIKE = re.compile(r"^[A-Z0-9]{8,16}$")
NODE_NAME = re.compile(r"^BR[NW][0-9A-F]{12}$")


def hexsp(b: bytes) -> str:
    return b.hex(" ").upper()


def parse_mac(s: str) -> bytes:
    h = re.sub(r"[^0-9A-Fa-f]", "", s)
    if len(h) != 12:
        raise ValueError(f"not a MAC: {s}")
    return bytes.fromhex(h)


def collect(lines: list[str]) -> tuple[set[str], set[bytes], set[str]]:
    serials: set[str] = set()
    macs: set[bytes] = set()
    ips: set[str] = set()
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            continue
        oid, vtype, val = m.group(1).lstrip("."), m.group(2) or "", m.group(3).strip()
        if oid.startswith(IFPHYS) and val:
            try:
                b = bytes.fromhex(re.sub(r"[^0-9A-Fa-f]", "", val))
                if len(b) == 6 and b != bytes(6):
                    macs.add(b)
            except ValueError:
                pass
        if oid.startswith(IPADENT) and vtype == "IpAddress":
            ips.add(val)
        if oid.startswith("1.3.6.1.4.1.2435.") and vtype == "STRING":
            s = val.strip('"').strip()
            # Node names (BRN/BRW + MAC) and bare MACs are handled by the MAC pass.
            if NODE_NAME.match(s) or re.fullmatch(r"[0-9A-F]{12}", s):
                continue
            if SERIAL_LIKE.match(s) and re.search(r"\d", s) and re.search(r"[A-Z]", s):
                serials.add(s)
    for ip in list(ips):
        try:
            a = ipaddress.ip_address(ip)
            if a.is_loopback or a.is_unspecified:
                ips.discard(ip)
        except ValueError:
            ips.discard(ip)
    return serials, macs, ips


def scrub_text(text: str, serials: set[str], macs: set[bytes], ips: set[str]) -> str:
    # Serials: ASCII and hex forms, same length.
    for s in sorted(serials, key=len, reverse=True):
        x = "X" * len(s)
        text = text.replace(s, x)
        for hx in (hexsp(s.encode()), hexsp(s.encode()).lower()):
            text = text.replace(hx, hexsp(x.encode()))
    # MACs: all separator styles, hex bytes, and BRN/BRW node names.
    for i, mac in enumerate(sorted(macs)):
        doc = bytes([0x00, 0x00, 0x5E, 0x00, 0x53, i & 0xFF])
        forms = {
            ":".join(f"{b:02X}" for b in mac): ":".join(f"{b:02X}" for b in doc),
            ":".join(f"{b:02x}" for b in mac): ":".join(f"{b:02x}" for b in doc),
            "-".join(f"{b:02X}" for b in mac): "-".join(f"{b:02X}" for b in doc),
            "-".join(f"{b:02x}" for b in mac): "-".join(f"{b:02x}" for b in doc),
            hexsp(mac): hexsp(doc),
            hexsp(mac).lower(): hexsp(doc).lower(),
            mac.hex().upper(): doc.hex().upper(),
            mac.hex(): doc.hex(),
        }
        for k, v in forms.items():
            text = text.replace(k, v)
    # IPv4: dotted (values and OID indexes) and 4-byte hex.
    for i, ip in enumerate(sorted(ips), start=1):
        doc = f"192.0.2.{i}"
        text = re.sub(rf"(?<![\d]){re.escape(ip)}(?![\d])", doc, text)
        raw = ipaddress.ip_address(ip).packed
        dpk = ipaddress.ip_address(doc).packed
        text = text.replace(hexsp(raw), hexsp(dpk)).replace(hexsp(raw).lower(), hexsp(dpk).lower())
    return text


def catch_all(text: str) -> str:
    def ip_sub(m: re.Match[str]) -> str:
        try:
            a = ipaddress.ip_address(m.group(1))
        except ValueError:
            return m.group(0)
        if a.is_private and not a.is_loopback and str(a) not in ("0.0.0.0",):
            if not (str(a).startswith("192.0.2.") or str(a).startswith("198.51.100.") or str(a).startswith("203.0.113.")):
                return "192.0.2.254"
        return m.group(0)

    def mac_sub(m: re.Match[str]) -> str:
        b = bytes(int(g, 16) for g in m.groups())
        if b[:5] == bytes([0, 0, 0x5E, 0, 0x53]) or b in (bytes(6), b"\xff" * 6):
            return m.group(0)
        return "00:00:5E:00:53:FF"

    # Only touch values, not OIDs: a dotted run inside an OID is not an IP.
    out = []
    for line in text.splitlines(keepends=True):
        m = LINE_RE.match(line.rstrip("\n"))
        if m:
            head, _, tail = line.partition("=")
            out.append(head + "=" + MAC_RE.sub(mac_sub, IPV4_RE.sub(ip_sub, tail)))
        else:
            out.append(MAC_RE.sub(mac_sub, IPV4_RE.sub(ip_sub, line)))
    return "".join(out)


def still_sensitive(text: str) -> list[str]:
    hits = []
    for line in text.splitlines():
        _, _, tail = line.partition("=") if LINE_RE.match(line) else ("", "", line)
        for m in IPV4_RE.finditer(tail):
            try:
                a = ipaddress.ip_address(m.group(1))
            except ValueError:
                continue
            if a.is_private and not a.is_loopback and not str(a).startswith(("192.0.2.", "198.51.100.", "203.0.113.")):
                hits.append(line)
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("walk")
    ap.add_argument("--serial", action="append", default=[])
    ap.add_argument("--mac", action="append", default=[])
    ap.add_argument("--ip", action="append", default=[])
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    args = ap.parse_args(argv)

    with open(args.walk, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    serials, macs, ips = collect(text.splitlines())
    serials |= set(args.serial)
    macs |= {parse_mac(m) for m in args.mac}
    ips |= set(args.ip)

    text = catch_all(scrub_text(text, serials, macs, ips))
    print(f"scrub: {len(serials)} serial(s), {len(macs)} MAC(s), {len(ips)} IP(s) replaced", file=sys.stderr)
    left = still_sensitive(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    if left:
        print(f"scrub: WARNING {len(left)} line(s) may still be sensitive:", file=sys.stderr)
        for line in left[:5]:
            print("  " + line, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
