#!/usr/bin/env python3
"""dev/snmpwalk/diffwalks.py: N-way diff of SNMP walks of one printer.

A development tool (see dev/snmpwalk/README.md). Compare walks of the same
printer taken in different states (media swapped, cover open, no media,
after a print). OIDs whose value differs between any of the walks are the
media/status fields; for octet strings the differing byte offsets are
listed, which is usually enough to read off the encoding.

    dev/snmpwalk/diffwalks.py BASE.walk OTHER.walk [MORE.walk ...]
        [--prefix 1.3.6.1.4.1.2435] [--all] [--show-same] [--ignore REGEX ...]

Input: net-snmp output with numeric OIDs (-On); hex values (-Ox) diff
exactly, but plain text output works too. See dev/snmpwalk/walk.sh.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

# ".1.3.6.1... = TYPE: value"   (TYPE absent for e.g. '""' or 'No Such Object')
_LINE = re.compile(r"^(\.?\d+(?:\.\d+)*)\s*=\s*(?:([A-Za-z][A-Za-z0-9-]*):\s?)?(.*)$")
_HEXBYTES = re.compile(r"^(?:[0-9A-Fa-f]{2}\s*)+$")

# Values that change on their own between walks; ignored unless --all.
DEFAULT_IGNORE = [
    r"^1\.3\.6\.1\.2\.1\.1\.3\.0$",            # sysUpTime
    r"^1\.3\.6\.1\.2\.1\.25\.1\.[12]\.0$",     # hrSystemUptime / hrSystemDate
    r"^1\.3\.6\.1\.2\.1\.2\.2\.1\.(1[0-9]|2[01])\.",   # ifTable counters
    r"^1\.3\.6\.1\.2\.1\.31\.1\.1\.1\.([2-9]|1[0-3])\.",  # ifXTable counters
    r"^1\.3\.6\.1\.2\.1\.(4|5|6|7|11)\.",      # ip/icmp/tcp/udp/snmp stats
]


@dataclass(frozen=True)
class Value:
    type: str
    text: str

    def as_bytes(self) -> bytes | None:
        """Octet-string payload as bytes when the walk printed hex."""
        if self.type in ("Hex-STRING", "STRING") and _HEXBYTES.match(self.text.strip() or "x"):
            try:
                return bytes.fromhex(self.text)
            except ValueError:
                return None
        return None

    def pretty(self) -> str:
        raw = self.as_bytes()
        if raw is not None:
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
            hexs = raw.hex(" ")
            if len(hexs) > 95:
                hexs = hexs[:92] + "..."
            return f"{self.type}[{len(raw)}] {hexs}  |{ascii_[:40]}|"
        return f"{self.type}: {self.text}" if self.type else self.text


def parse_walk(path: str) -> dict[str, Value]:
    """Parse one walk file. Continuation lines (wrapped hex or multi-line
    strings) are appended to the previous value."""
    out: dict[str, Value] = {}
    last: str | None = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            m = _LINE.match(line)
            if m:
                oid = m.group(1).lstrip(".")
                vtype = m.group(2) or ""
                text = m.group(3)
                if vtype in ("STRING",) and text.startswith('"') and text.endswith('"') and len(text) >= 2:
                    text = text[1:-1]
                out[oid] = Value(vtype, text.strip())
                last = oid
            elif last is not None and line.strip():
                prev = out[last]
                sep = " " if _HEXBYTES.match(line.strip()) else "\n"
                out[last] = Value(prev.type, (prev.text + sep + line.strip()).strip())
    return out


def oid_key(oid: str) -> tuple[int, ...]:
    return tuple(int(p) for p in oid.split("."))


def byte_diffs(a: bytes, b: bytes) -> str:
    n = max(len(a), len(b))
    parts = []
    for i in range(n):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            fx = "--" if x is None else f"{x:02x}"
            fy = "--" if y is None else f"{y:02x}"
            parts.append(f"[{i}] {fx}->{fy}")
    if len(parts) > 12:
        parts = parts[:12] + [f"... ({len(parts) - 12} more)"]
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("walks", nargs="+", help="walk files; the first is the baseline")
    ap.add_argument("--prefix", default="", help="only OIDs under this prefix (e.g. 1.3.6.1.4.1.2435)")
    ap.add_argument("--all", action="store_true", help="do not ignore volatile OIDs (uptime, counters)")
    ap.add_argument("--ignore", action="append", default=[], help="extra regex of OIDs to ignore")
    ap.add_argument("--show-same", action="store_true", help="also list OIDs identical everywhere")
    args = ap.parse_args(argv)
    if len(args.walks) < 2 and not args.show_same:
        ap.error("need at least two walks to diff")

    walks = [(os.path.basename(p), parse_walk(p)) for p in args.walks]
    ignore = [re.compile(r) for r in (args.ignore + ([] if args.all else DEFAULT_IGNORE))]
    prefix = args.prefix.lstrip(".")

    oids = sorted({o for _, w in walks for o in w}, key=oid_key)
    if prefix:
        oids = [o for o in oids if o == prefix or o.startswith(prefix + ".")]

    names = [n for n, _ in walks]
    print("walks:")
    for i, n in enumerate(names):
        print(f"  [{i}] {n}  ({len(walks[i][1])} varbinds)")
    print()

    changed = 0
    for oid in oids:
        if any(r.search(oid) for r in ignore):
            continue
        vals = [w.get(oid) for _, w in walks]
        same = all(v == vals[0] for v in vals)
        if same and not args.show_same:
            continue
        if not same:
            changed += 1
        print(("  " if same else "* ") + oid)
        base = vals[0]
        for i, v in enumerate(vals):
            label = f"[{i}]"
            if v is None:
                print(f"    {label} <absent>")
                continue
            note = ""
            if i > 0 and base is not None and v != base:
                ba, bb = base.as_bytes(), v.as_bytes()
                if ba is not None and bb is not None:
                    note = f"   bytes: {byte_diffs(ba, bb)}"
            print(f"    {label} {v.pretty()}{note}")
    print(f"\n{changed} OID(s) differ" + ("" if args.all else " (volatile OIDs ignored; --all to include)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
