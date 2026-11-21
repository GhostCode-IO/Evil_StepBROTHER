"""The printer persona: what ql810wc-snmp-agent answers as a Brother QL-810W.

The agent never touches USB. Its inputs are:

* ``oid-map.yaml`` (docs/architecture.md section 7): every OID of the persona,
  its type, and where its value comes from (a constant, a runtime setting, a
  template, or one of the named providers in :data:`PROVIDERS`). Nothing is
  evaluated as code.
* ``state.json`` (section 5), written by ql810wc-proxyd. It is checked at most
  every 0.5 s and re-read only when the file changed.
* the runtime settings (``runtime.env`` and ``identity.env``, see config.py).

:class:`Persona` turns these into values. :class:`PersonaTree` serves them in
SNMP order, both as the AgentX handler (agentx.py) and for the pass_persist
fallback (passpersist.py). Every answer comes from memory, plus at most one
stat of state.json, so the 3-GET bursts P-touch sends every second during a
job never wait on I/O.

The status block ``1.3.6.1.4.1.2435.3.3.9.1.6.1.0``
---------------------------------------------------
P-touch Editor for Mac reads printer state and the loaded roll only from this
OID (SNMPv1, community ``internal``). An error-status such as noSuchName fails
its print at once, so the OID is answered in every situation (:func:`evaluate`).
The one gap lies outside the persona: while snmpd runs but ql810wc-snmp-agent
is not registered with it (restarting, or after a crash), snmpd itself answers
the OID with noSuchName. That is why the agent applies new settings and a new
OID map in place on SIGHUP (``systemctl reload``) instead of restarting.

How the block is chosen:

* **Normally** the last frame ql810wc-proxyd read from the printer is served
  verbatim.
* **While a job is in flight** the proxy never polls the printer, so the last
  frame still predates the job. In that case only bytes 18-21 are synthesised:
  status type ``00``, phase ``01`` (printing), phase number ``00 00``. P-touch
  therefore keeps polling instead of taking the pre-job "idle" frame for "label
  done". The media bytes (10, 11, 14, 17, 24, 25) and everything else are never
  touched. "In flight" means ``job.active`` and no frame read since the job
  started. Once the proxy's post-job polls deliver real frames, those are
  served verbatim and show when the printer really finishes. P-touch sends its
  job trailer only after that, and the proxy holds the trailer until the
  printer reports "receiving" again (section 4). Synthesising past that point
  would stall every print.
  The synthesis applies only when that last frame is a status-type-00 reply
  that is idle and has media. A frame showing an error or no media, or an
  unsolicited one (printing completed, phase change, error), is served as it
  is. It is capped at :data:`BUSY_WINDOW_SECONDS` after the job's last
  observed byte, the proxy's post-job poll limit. A job still in flight after
  that (a stalled write, a client that half-closed without ``0x1A`` and whose
  last data the printer has not taken, a client gone silent mid-job) gets the
  "turned off" block below: the pre-job idle reply would claim an idle
  printer, and no word from the printer for that long makes its status stale.
  There is no synthesis once ``job.active`` is false.
* **When the printer is unavailable** the agent serves a "turned off" block.
  That covers a printer that is absent, in Editor Lite mode, in error, or
  whose status is stale (also during a job, past the cap above). It also
  covers a state.json that is missing, that says ql810wc-proxyd has stopped
  (``proxy.pid`` null), or that has not been rewritten for more than
  3 x STATUS_POLL_SECONDS. The block has the QL-810W
  header, status type ``02`` (error occurred), err1 ``0x20`` ("printer turned
  off") and no media. A real QL that is switched off does not answer at
  all, and P-touch then retries for 60 s before it gives up. P-touch's own
  decoder turns err1 ``0x20`` into its power-off message at once. Status type
  ``04`` ("turned off") would show "Unexpected status value" in P-touch instead.

The Host Resources and Printer-MIB values (hrDeviceStatus, hrPrinterStatus,
hrPrinterDetectedErrorState, prtCoverStatus, prtInputStatus,
prtConsoleDisplayBufferText) are derived from the served block. They follow
the state matrix recorded on a real QL-810W (fw 1.22): ready, cover open, no
media while idle, no media after a job, and the media-mismatch latch.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
import sys
import threading
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import (Any, Callable, Dict, FrozenSet, Iterable, Iterator, List, Mapping, NamedTuple,
                    Optional, Sequence, Tuple, Union)

from . import config as qconfig
from . import media, statecache
from .agentx import EncodeError, Oid, OidLike, VarBind, VarType, encode_varbind, format_oid, parse_oid
from .rasterproto import (MODEL_NAMES, PHASE_PRINTING, ST_ERROR, ST_REPLY, STATUS_HEADER, STATUS_LEN,
                          Status)

__all__ = [
    "SCHEMA_VERSION", "PERSONA_CONTEXT", "AGENTX_SUBTREES", "PASS_PERSIST_SUBTREES", "TYPES",
    "CLIENTS", "PTOUCH_MAC_TYPES", "PROVIDERS", "TEMPLATE_NAMES", "DISPLAY_STRING_MAX",
    "JOB_STALL_SECONDS", "BUSY_WINDOW_SECONDS", "STATE_REREAD_SECONDS", "SYNTH_BYTES",
    "LIVE", "WARMUP", "OFFLINE", "UNKNOWN",
    "OidMapError", "Source", "Entry", "OidMap", "load_oid_map",
    "Policy", "PrinterView", "Condition", "evaluate", "offline_block", "busy_block", "condition",
    "normalize_ieee1284", "StateSource", "NetInfo", "Persona", "PersonaTree",
    "within", "subtree_end", "describe_varbind",
]

log = logging.getLogger("ql810wc.persona")

SCHEMA_VERSION = 1
#: SNMP context of the persona (docs/architecture.md section 6).
PERSONA_CONTEXT = "printer"

#: Subtrees the AgentX subagent registers in context "printer". They must equal
#: VIEW_AGENTX in scripts/30-snmp-agent.sh (the VACM view of the communities).
#: They are registered in :meth:`PersonaTree.registrations` order: Brother first.
AGENTX_SUBTREES: Tuple[Oid, ...] = (
    (1, 3, 6, 1, 2, 1, 1),       # system
    (1, 3, 6, 1, 2, 1, 2),       # interfaces
    (1, 3, 6, 1, 2, 1, 25),      # HOST-RESOURCES-MIB
    (1, 3, 6, 1, 2, 1, 43),      # Printer-MIB
    (1, 3, 6, 1, 4, 1, 2435),    # Brother
)
#: Subtrees the pass_persist fallback serves in the default context. They must
#: equal VIEW_PASS_PERSIST in scripts/30-snmp-agent.sh. The other persona
#: subtrees would collide with the host's own MIBs there.
PASS_PERSIST_SUBTREES: Tuple[Oid, ...] = ((1, 3, 6, 1, 2, 1, 43), (1, 3, 6, 1, 4, 1, 2435))

#: oid-map ``type`` -> SNMP type. ``octets`` and ``string`` are both OCTET
#: STRING; ``string`` marks text (DisplayString), ``octets`` binary data.
TYPES: Dict[str, VarType] = {
    "integer": VarType.INTEGER,
    "octets": VarType.OCTET_STRING,
    "string": VarType.OCTET_STRING,
    "oid": VarType.OBJECT_IDENTIFIER,
    "timeticks": VarType.TIME_TICKS,
    "counter32": VarType.COUNTER32,
    "gauge32": VarType.GAUGE32,
    "ipaddress": VarType.IP_ADDRESS,
}

#: Client ids accepted in an entry's ``clients`` list.
CLIENTS: Dict[str, str] = {
    "ptouch-mac": "P-touch Editor 5.3 for Mac (MacPrintSDK): SNMPv1, community 'internal', "
                  "accepts only INTEGER and OCTET STRING replies",
    "win-ptouch": "Brother Windows driver / P-touch Editor for Windows (paper detection, status)",
    "win-installer": "Brother Windows driver installer network search (SNMPv1 broadcast, 5 varbinds)",
    "mobile-sdk": "Brother Print SDK / iPrint&Label discovery (one GET of 6 varbinds)",
    "bradmin": "BRAdmin Light / Professional",
    "cups": "CUPS socket and snmp backends",
    "tcpmon": "Windows Standard TCP/IP port monitor with SNMP status",
    "nms": "generic network monitoring (LibreNMS, Observium, Checkmk, ...)",
    "third-party": "open-source label tools (labelito, BrotherRaster, partdb, thermal-label, ql-unidrv)",
}
#: Types P-touch Editor for Mac accepts; any other reply type fails its read.
PTOUCH_MAC_TYPES: FrozenSet[str] = frozenset({"integer", "octets", "string"})

#: Named value providers: name -> (allowed types, what the value is).
PROVIDERS: Dict[str, Tuple[FrozenSet[str], str]] = {
    "status_block": (frozenset({"octets"}),
                     "the 32-byte Brother status block (see evaluate())"),
    "ieee1284_id": (frozenset({"string", "octets"}),
                    "the printer's USB IEEE-1284 device ID with MDL forced to the map's model"),
    "serial": (frozenset({"string", "octets"}),
               "PERSONA_SERIAL, else the printer's USB serial number"),
    "mac": (frozenset({"octets"}),
            "6-byte MAC: PERSONA_MAC, else the interface holding PROXY_IP, else the default route's"),
    "sysname": (frozenset({"string", "octets"}),
                "PERSONA_SYSNAME, else 'BRW' + the MAC as 12 upper-case hex digits"),
    "sysuptime": (frozenset({"timeticks"}),
                  "hundredths of a second since the agent started"),
    "hr_device_status": (frozenset({"integer"}),
                         "hrDeviceStatus: 2 running, 5 down (error or offline), 1 unknown"),
    "hr_printer_status": (frozenset({"integer"}),
                          "hrPrinterStatus: 3 idle, 4 printing, 5 warmup, 1 other (error, offline)"),
    "hr_detected_error_state": (frozenset({"octets"}),
                                "hrPrinterDetectedErrorState: one octet of RFC 3805 bits"),
    "cover_status": (frozenset({"integer"}),
                     "prtCoverStatus: 3 open, 4 closed, 2 unknown"),
    "input_status": (frozenset({"integer"}),
                     "prtInputStatus: 0 media loaded, 19 no media, 37 offline, 69 warming up"),
    "media_name": (frozenset({"string", "octets"}),
                   "prtInputMediaName of the loaded roll, e.g. '62mm / 2.4\"'; empty without media"),
    "media_width": (frozenset({"integer"}),
                    "roll width in 1/100 mm; -2 when unknown"),
    "media_length": (frozenset({"integer"}),
                     "label length in 1/100 mm, -1 for continuous rolls, -2 when unknown"),
    "console_text": (frozenset({"string", "octets"}),
                     "prtConsoleDisplayBufferText, 16 characters: READY, BUSY, COVER OPEN, ERROR, OFFLINE"),
}

#: Placeholders a template may use besides the runtime setting names.
TEMPLATE_NAMES: FrozenSet[str] = frozenset({"MODEL"})

#: Longest DisplayString (SNMPv2-TC). Text values are cut to this many bytes.
DISPLAY_STRING_MAX = 255
#: Extra time allowed between state.json writes while a job is in flight (the
#: proxy does not poll during a job).
JOB_STALL_SECONDS = 300.0
#: Longest synthesised "printing" phase after a job's last byte: the proxy's
#: post-job poll limit (docs/architecture.md sections 4 and 6).
BUSY_WINDOW_SECONDS = 60.0
#: state.json is checked at most this often.
STATE_REREAD_SECONDS = 0.5
#: How long a found MAC address is reused, and how soon a failed lookup is retried.
NETINFO_TTL_SECONDS = 60.0
NETINFO_RETRY_SECONDS = 5.0
#: The only status bytes the persona ever changes (status type, phase type, phase number).
SYNTH_BYTES: Tuple[int, ...] = (18, 19, 20, 21)

# Availability of the printer as seen by the persona.
LIVE = "live"          # a status frame from the printer is served
WARMUP = "warmup"      # printer attached, first status not read yet
OFFLINE = "offline"    # printer absent, unusable, or its status is stale
UNKNOWN = "unknown"    # no usable state.json: ql810wc-proxyd is not running

_MAX_MAP_BYTES = 1 << 20
_TOP_KEYS = frozenset({"schema_version", "model", "validated_firmware", "entries"})
_ENTRY_KEYS = frozenset({"oid", "name", "type", "source", "context", "verified", "clients", "notes"})
_SOURCE_KINDS = ("const", "config", "template", "provider")
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,63}")
_TEMPLATE_RE = re.compile(r"\$(\$|\{([A-Za-z_][A-Za-z0-9_]*)\})?")
_HEX_SEPARATORS = re.compile(r"[\s:]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_INT32_MIN, _INT32_MAX = -(1 << 31), (1 << 31) - 1
_UINT32_MAX = (1 << 32) - 1


# ---------------------------------------------------------------------------
# The OID map
# ---------------------------------------------------------------------------
class OidMapError(ValueError):
    """oid-map.yaml cannot be read or breaks the schema. The message lists every problem."""


@dataclass(frozen=True)
class Source:
    """Where an entry's value comes from.

    ``kind`` is ``const`` (``arg`` = the typed constant), ``config`` (``arg`` = a
    runtime setting name), ``template`` (``arg`` = text with ``${NAME}``
    placeholders) or ``provider`` (``arg`` = a :data:`PROVIDERS` name).
    ``default`` (providers only) is the typed value used when the provider has
    nothing; None means the OID is absent then.
    """

    kind: str
    arg: Any
    default: Any = None


@dataclass(frozen=True)
class Entry:
    """One OID instance of the persona."""

    oid: Oid
    name: str
    type: str
    source: Source
    context: str = PERSONA_CONTEXT
    verified: bool = False
    clients: Tuple[str, ...] = ()
    notes: str = ""

    @property
    def vtype(self) -> VarType:
        """The SNMP type of the value."""
        return TYPES[self.type]

    @property
    def dotted(self) -> str:
        """The OID in dotted notation, without a leading dot."""
        return format_oid(self.oid)


@dataclass(frozen=True)
class OidMap:
    """A validated oid-map: the persona's entries in ascending OID order."""

    schema_version: int
    model: str
    validated_firmware: Tuple[str, ...]
    entries: Tuple[Entry, ...]
    path: Optional[str] = None

    @classmethod
    def from_dict(cls, doc: Any, *, path: Optional[str] = None) -> "OidMap":
        """Validate a parsed oid-map document. Raises :class:`OidMapError` listing every problem."""
        where = path or "oid-map"
        problems: List[str] = []
        if not isinstance(doc, Mapping):
            raise OidMapError(f"{where}: the document must be a mapping with schema_version, model, "
                              f"validated_firmware and entries")
        for key in sorted(set(doc) - _TOP_KEYS, key=str):
            problems.append(f"unknown top-level key {key!r}")
        schema = doc.get("schema_version")
        if type(schema) is not int or schema != SCHEMA_VERSION:
            problems.append(f"schema_version must be {SCHEMA_VERSION}, got {schema!r}")
        model = doc.get("model")
        if not isinstance(model, str) or model not in MODEL_NAMES.values():
            problems.append(f"model must be one of {', '.join(sorted(MODEL_NAMES.values()))}, got {model!r}")
            model = "QL-810W"
        firmware = doc.get("validated_firmware")
        if not isinstance(firmware, list) or not all(isinstance(f, str) and f for f in firmware):
            problems.append("validated_firmware must be a list of firmware strings (empty until "
                            "the map is confirmed on a real printer)")
            firmware = []
        raw_entries = doc.get("entries")
        entries: List[Entry] = []
        if not isinstance(raw_entries, list) or not raw_entries:
            problems.append("entries must be a non-empty list")
            raw_entries = []
        for index, raw in enumerate(raw_entries, 1):
            entry = _build_entry(index, raw, problems)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda e: e.oid)
        _check_entry_set(entries, problems)
        if problems:
            raise OidMapError(f"{where}: {len(problems)} problem(s):\n  " + "\n  ".join(problems))
        return cls(schema, model, tuple(firmware), tuple(entries), path)

    def get(self, oid: OidLike) -> Optional[Entry]:
        """The entry at exactly ``oid``, or None."""
        key = parse_oid(oid)
        keys = [e.oid for e in self.entries]
        i = bisect_left(keys, key)
        if i < len(keys) and keys[i] == key:
            return self.entries[i]
        return None

    def within(self, subtrees: Iterable[OidLike]) -> Tuple[Entry, ...]:
        """The entries that lie inside any of ``subtrees``."""
        roots = tuple(parse_oid(s) for s in subtrees)
        return tuple(e for e in self.entries if any(within(e.oid, root) for root in roots))


def _where(index: int, raw: Any) -> str:
    if isinstance(raw, Mapping):
        label = raw.get("name") or raw.get("oid")
        if isinstance(label, str) and label:
            return f"entry {index} ({label})"
    return f"entry {index}"


def _build_entry(index: int, raw: Any, problems: List[str]) -> Optional[Entry]:
    """Validate one entry; append problems and return None when it is unusable."""
    where = _where(index, raw)
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be a mapping")
        return None
    before = len(problems)
    for key in sorted(set(raw) - _ENTRY_KEYS, key=str):
        problems.append(f"{where}: unknown key {key!r}")
    oid: Oid = ()
    raw_oid = raw.get("oid")
    if not isinstance(raw_oid, str):
        problems.append(f"{where}: oid must be a quoted dotted string, got {raw_oid!r}")
    else:
        try:
            oid = parse_oid(raw_oid)
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
        else:
            if len(oid) < 2:
                problems.append(f"{where}: oid {raw_oid!r} is too short")
                oid = ()
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        problems.append(f"{where}: name must be 1-64 letters, digits, '_', '.' or '-' starting with a "
                        f"letter, got {name!r}")
    vtype = raw.get("type")
    if vtype not in TYPES:
        problems.append(f"{where}: type must be one of {', '.join(TYPES)}, got {vtype!r}")
        vtype = None
    context = raw.get("context", PERSONA_CONTEXT)
    if context != PERSONA_CONTEXT:
        problems.append(f"{where}: context must be {PERSONA_CONTEXT!r} (schema {SCHEMA_VERSION}), got {context!r}")
    verified = raw.get("verified")
    if not isinstance(verified, bool):
        problems.append(f"{where}: verified must be true or false, got {verified!r}")
    clients = raw.get("clients", [])
    if not isinstance(clients, list) or not all(isinstance(c, str) for c in clients):
        problems.append(f"{where}: clients must be a list of client ids")
        clients = []
    for client in clients:
        if client not in CLIENTS:
            problems.append(f"{where}: unknown client {client!r} (known: {', '.join(CLIENTS)})")
    if len(set(clients)) != len(clients):
        problems.append(f"{where}: clients lists a client twice")
    if vtype is not None and "ptouch-mac" in clients and vtype not in PTOUCH_MAC_TYPES:
        problems.append(f"{where}: P-touch Editor for Mac reads this OID but accepts only "
                        f"{'/'.join(sorted(PTOUCH_MAC_TYPES))} replies, not {vtype!r}")
    notes = raw.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        problems.append(f"{where}: notes must say where the OID and its value come from")
    source = _build_source(where, raw.get("source"), vtype, problems)
    if len(problems) > before or source is None or vtype is None or not oid:
        return None
    return Entry(oid=oid, name=name, type=vtype, source=source, context=context, verified=verified,
                 clients=tuple(clients), notes=notes.strip())


def _build_source(where: str, raw: Any, vtype: Optional[str], problems: List[str]) -> Optional[Source]:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: source must be a mapping with one of {', '.join(_SOURCE_KINDS)}")
        return None
    kinds = [k for k in _SOURCE_KINDS if k in raw]
    unknown = sorted(set(raw) - set(_SOURCE_KINDS) - {"default"}, key=str)
    if unknown:
        problems.append(f"{where}: unknown source key(s) {', '.join(map(repr, unknown))}")
    if len(kinds) != 1:
        problems.append(f"{where}: source needs exactly one of {', '.join(_SOURCE_KINDS)}, "
                        f"got {sorted(map(str, raw))}")
        return None
    kind = kinds[0]
    arg = raw[kind]
    if "default" in raw and kind != "provider":
        problems.append(f"{where}: 'default' is only meaningful with a provider source")
        return None
    if vtype is None:
        return None
    try:
        if kind == "const":
            return Source(kind, _typed_constant(vtype, arg))
        if kind == "config":
            if not isinstance(arg, str) or arg not in qconfig.SETTINGS:
                raise ValueError(f"config source must name a runtime setting ({', '.join(qconfig.SETTINGS)}), "
                                 f"got {arg!r}")
            if vtype not in ("string", "octets", "integer"):
                raise ValueError(f"a config source cannot produce type {vtype!r}")
            return Source(kind, arg)
        if kind == "template":
            if not isinstance(arg, str):
                raise ValueError(f"template must be text, got {arg!r}")
            if vtype not in ("string", "octets"):
                raise ValueError(f"a template produces text, not {vtype!r}")
            for name in _template_names(arg):
                if name not in qconfig.SETTINGS and name not in TEMPLATE_NAMES:
                    raise ValueError(f"template placeholder ${{{name}}} is neither a runtime setting nor "
                                     f"one of {', '.join(sorted(TEMPLATE_NAMES))}")
            return Source(kind, arg)
        if not isinstance(arg, str) or arg not in PROVIDERS:
            raise ValueError(f"unknown provider {arg!r} (known: {', '.join(PROVIDERS)})")
        allowed = PROVIDERS[arg][0]
        if vtype not in allowed:
            raise ValueError(f"provider {arg!r} produces {'/'.join(sorted(allowed))}, not {vtype!r}")
        default = _typed_constant(vtype, raw["default"]) if "default" in raw else None
        return Source(kind, arg, default)
    except (ValueError, EncodeError) as exc:
        problems.append(f"{where}: {exc}")
        return None


def _typed_constant(vtype: str, value: Any) -> Any:
    """Convert a constant from the map to its typed value; ValueError if it does not fit ``vtype``."""
    if vtype == "integer":
        if isinstance(value, bool) or not isinstance(value, int) or not _INT32_MIN <= value <= _INT32_MAX:
            raise ValueError(f"integer constant must be a 32-bit signed number, got {value!r}")
        typed: Any = value
    elif vtype in ("timeticks", "counter32", "gauge32"):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _UINT32_MAX:
            raise ValueError(f"{vtype} constant must be a number 0..{_UINT32_MAX}, got {value!r}")
        typed = value
    elif vtype == "octets":
        if not isinstance(value, str):
            raise ValueError(f"octets constant must be hex text such as \"80 20 42\", got {value!r}")
        try:
            typed = bytes.fromhex(_HEX_SEPARATORS.sub("", value))
        except ValueError:
            raise ValueError(f"octets constant is not hex: {value!r}") from None
    elif vtype == "string":
        if not isinstance(value, str):
            raise ValueError(f"string constant must be text, got {value!r}")
        if len(value.encode("utf-8")) > DISPLAY_STRING_MAX:
            raise ValueError(f"string constant is longer than {DISPLAY_STRING_MAX} bytes")
        typed = value
    elif vtype == "oid":
        if not isinstance(value, str):
            raise ValueError(f"oid constant must be a quoted dotted string, got {value!r}")
        typed = parse_oid(value)
        if len(typed) < 2:
            raise ValueError(f"oid constant {value!r} needs at least two sub-identifiers")
    elif vtype == "ipaddress":
        if not isinstance(value, str):
            raise ValueError(f"ipaddress constant must be a dotted quad, got {value!r}")
        typed = value
    else:  # pragma: no cover - TYPES and this function list the same names
        raise ValueError(f"unsupported type {vtype!r}")
    encode_varbind(((1, 3), TYPES[vtype], typed))  # raises EncodeError if it cannot be sent
    return typed


def _template_names(text: str) -> List[str]:
    """Placeholder names in ``text``; ValueError on a lone ``$``."""
    names = []
    for match in _TEMPLATE_RE.finditer(text):
        if match.group(1) is None:
            raise ValueError(f"template {text!r}: write '$$' for a literal '$' or '${{NAME}}' for a value")
        if match.group(2):
            names.append(match.group(2))
    return names


def _render_template(text: str, values: Mapping[str, str]) -> str:
    def sub(match: "re.Match[str]") -> str:
        if match.group(1) == "$":
            return "$"
        return values.get(match.group(2), "")
    return _TEMPLATE_RE.sub(sub, text)


def _check_entry_set(entries: Sequence[Entry], problems: List[str]) -> None:
    """Rules across entries (``entries`` sorted by OID)."""
    seen_names: Dict[str, str] = {}
    for entry in entries:
        if entry.name in seen_names:
            problems.append(f"name {entry.name!r} is used by {seen_names[entry.name]} and {entry.dotted}")
        seen_names.setdefault(entry.name, entry.dotted)
        if not any(within(entry.oid, root) for root in AGENTX_SUBTREES):
            problems.append(f"{entry.name} ({entry.dotted}) is outside the persona subtrees "
                            f"{', '.join(format_oid(s) for s in AGENTX_SUBTREES)}")
    for prev, cur in zip(entries, entries[1:]):
        if prev.oid == cur.oid:
            problems.append(f"OID {cur.dotted} is listed twice ({prev.name}, {cur.name})")
        elif within(cur.oid, prev.oid):
            problems.append(f"{prev.name} ({prev.dotted}) is a prefix of {cur.name} ({cur.dotted}); "
                            f"an instance cannot have children")


def load_oid_map(path: Union[str, "os.PathLike[str]"]) -> OidMap:
    """Load and validate an oid-map file (``.yaml``/``.yml`` or ``.json``).

    YAML needs PyYAML (``python3-pyyaml``). Without it, an ``oid-map.json`` next to
    the YAML file is read instead. Raises :class:`OidMapError` with a clear
    message when the file cannot be read, parsed or validated.
    """
    name = os.fspath(path)
    stem, suffix = os.path.splitext(name)
    suffix = suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # python3-pyyaml; optional at run time
        except ImportError:
            sibling = stem + ".json"
            if os.path.exists(sibling):
                log.info("PyYAML is not installed; reading %s instead of %s", sibling, name)
                return OidMap.from_dict(_parse_json(sibling), path=sibling)
            raise OidMapError(f"{name}: PyYAML is not installed (dnf install python3-pyyaml) and there "
                              f"is no {os.path.basename(sibling)} next to it") from None
        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader
        try:
            doc = yaml.load(_read_text(name), Loader=loader)  # nosec - a SafeLoader only builds plain data
        except yaml.YAMLError as exc:
            raise OidMapError(f"{name}: not valid YAML: {exc}") from None
        return OidMap.from_dict(doc, path=name)
    if suffix == ".json":
        return OidMap.from_dict(_parse_json(name), path=name)
    raise OidMapError(f"{name}: expected a .yaml, .yml or .json file")


def _read_text(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read(_MAX_MAP_BYTES + 1)
    except OSError as exc:
        raise OidMapError(f"{path}: cannot read: {exc.strerror or exc}") from None
    if len(data) > _MAX_MAP_BYTES:
        raise OidMapError(f"{path}: larger than {_MAX_MAP_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OidMapError(f"{path}: not UTF-8 ({exc})") from None


def _parse_json(path: str) -> Any:
    try:
        return json.loads(_read_text(path))
    except ValueError as exc:
        raise OidMapError(f"{path}: not valid JSON: {exc}") from None


# ---------------------------------------------------------------------------
# Status policy (pure functions)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Policy:
    """Time limits of :func:`evaluate`, in seconds."""

    #: state.json not rewritten for longer than this (outside a job) = ql810wc-proxyd has stalled.
    heartbeat_limit: float = 9.0
    #: A status frame older than this while no job runs is stale (STATUS_MAX_AGE_SECONDS).
    frame_max_age: float = 60.0
    #: Longest synthesised "printing" phase after the job's last observed byte.
    busy_window: float = BUSY_WINDOW_SECONDS
    #: Extra heartbeat allowance while a job is in flight.
    job_stall: float = JOB_STALL_SECONDS

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "Policy":
        """Limits from the runtime settings: the heartbeat is 3 x STATUS_POLL_SECONDS, the frame
        age STATUS_MAX_AGE_SECONDS. Raises ConfigError on invalid values."""
        poll = qconfig.get_int(cfg, "STATUS_POLL_SECONDS")
        max_age = qconfig.get_int(cfg, "STATUS_MAX_AGE_SECONDS")
        return cls(heartbeat_limit=3.0 * poll, frame_max_age=float(max_age))


@dataclass(frozen=True)
class PrinterView:
    """What the persona currently says about the printer.

    ``block`` is the status block served. ``frame`` is the real frame it is
    based on, or None when the view is not live. ``busy`` is true while the
    printer prints, from a real or a synthesised phase. ``synthesized`` is true
    when ``block`` carries the synthesised "printing" phase.
    """

    availability: str
    reason: str
    block: Status
    frame: Optional[Status] = None
    busy: bool = False
    synthesized: bool = False

    @property
    def live(self) -> bool:
        """True when the block comes from the printer."""
        return self.availability == LIVE

    def summary(self) -> str:
        """One line for logs and systemd STATUS=."""
        if not self.live:
            return f"{self.availability}: {self.reason}"
        block = self.block
        if block.has_media:
            found = media.describe(block)
            roll = found.description if found is not None else (
                f"unsupported media (width {block.media_width_mm} mm, type 0x{block.media_type_raw:02x}, "
                f"length {block.media_length_mm} mm)")
        else:
            roll = "no media"
        errors = block.errors()
        state = ", ".join(errors) if errors else ("printing" if self.busy else "idle")
        return f"live: {roll}, {state}" + (f" ({self.reason})" if self.reason else "")


def offline_block(model: str = "QL-810W") -> Status:
    """The "turned off" block: header and model codes of ``model``, status type 02
    (error occurred), err1 0x20 (printer turned off), no media."""
    codes = {name: code for code, name in MODEL_NAMES.items()}
    series, model_code = codes[model]
    frame = bytearray(STATUS_LEN)
    frame[0:3] = STATUS_HEADER
    frame[3] = series
    frame[4] = model_code
    frame[5] = 0x30
    frame[6] = 0x04   # as in every real QL-810W frame (meaning undocumented)
    frame[8] = 0x20   # err1: printer turned off
    frame[18] = ST_ERROR
    return Status(bytes(frame))


def busy_block(frame: Status) -> Status:
    """``frame`` with only bytes 18-21 replaced: status type 00 (reply), phase 01
    (printing), phase number 00 00. The media bytes are never touched."""
    return frame.with_overrides(status_type=ST_REPLY, phase_type=PHASE_PRINTING, phase_number=0)


_DEVICE_REASONS = {
    "absent": "printer not connected or switched off",
    "mass-storage": "printer is in Editor Lite mode (USB drive); press and hold the Editor Lite "
                    "button until its green LED turns off",
    "error": "ql810wc-proxyd cannot use the printer (USB error)",
}


def _device_reason(device_state: Any, detail: Any) -> str:
    """Why a printer whose device state is not "ready" is offline, with the proxy's detail text."""
    if isinstance(device_state, str) and device_state in _DEVICE_REASONS:
        reason = _DEVICE_REASONS[device_state]
    elif isinstance(device_state, str) and device_state:
        reason = f"printer device state {device_state[:40]!r}"
    else:
        reason = "printer state unknown (state.json has no valid device.state)"
    if isinstance(detail, str) and detail.strip():
        reason += ": " + " ".join(detail.split())[:160]
    return reason


def _timestamp(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def evaluate(state: Optional[Mapping[str, Any]], now: float, policy: Policy = Policy(),
             offline: Optional[Status] = None, *, job_activity: Optional[float] = None) -> PrinterView:
    """Decide the status block to serve from a state.json document at wall-clock time ``now``.

    Pure function; the policy is described in the module docstring.
    ``offline`` is the "turned off" block (default: :func:`offline_block`).
    ``job_activity`` is the wall-clock time of the active job's last observed
    progress (its byte count last changed). The busy-phase cap counts from it,
    or from ``job.started`` when it is None or earlier.
    """
    off = offline if offline is not None else offline_block()

    def down(availability: str, reason: str) -> PrinterView:
        return PrinterView(availability, reason, off)

    if state is None:
        return down(UNKNOWN, "no readable state.json (is ql810wc-proxyd running?)")
    proxy = state.get("proxy")
    if isinstance(proxy, Mapping) and "pid" in proxy and proxy["pid"] is None:
        return down(UNKNOWN, "ql810wc-proxyd has stopped")
    device_state = statecache.get_path(state, "device.state")
    if device_state != "ready":
        return down(OFFLINE, _device_reason(device_state, statecache.get_path(state, "device.detail")))
    updated = _timestamp(state.get("updated"))
    if updated is None:
        return down(UNKNOWN, "state.json has no valid 'updated' time")
    raw_job = state.get("job")
    job: Mapping[str, Any] = raw_job if isinstance(raw_job, Mapping) else {}
    job_active = job.get("active") is True
    updated_age = max(0.0, now - updated)
    if updated_age > policy.heartbeat_limit + (policy.job_stall if job_active else 0.0):
        return down(UNKNOWN, f"state.json not rewritten for {updated_age:.0f} s "
                             f"(ql810wc-proxyd stalled or stopped)")
    frame = statecache.status_from_state(state)
    if frame is None:
        started_age = statecache.age_seconds(state, "proxy.started", now)
        if started_age is not None and started_age <= policy.frame_max_age:
            return down(WARMUP, "printer connected, waiting for its first status")
        return down(OFFLINE, "no status from the printer")
    polled = _timestamp(statecache.get_path(state, "status.polled"))
    if polled is None:
        polled = updated  # a frame without its own time is as old as the file
    live_view = PrinterView(LIVE, "", frame, frame, busy=frame.is_printing or frame.in_use)

    job_started = _timestamp(job.get("started")) if job_active else None
    if job_started is not None and polled < job_started:
        # In flight: the printer has not been read since the job started.
        if job_started - polled > policy.frame_max_age:
            return down(OFFLINE, f"no fresh status from the printer for {job_started - polled:.0f} s "
                                 f"before the job started")
        last_progress = max(job_started, job_activity if job_activity is not None else job_started)
        # Only the last idle-poll reply can be older than the job; an unsolicited
        # frame (printing completed, phase change, error) is the printer's own news.
        if frame.status_type == ST_REPLY and frame.is_idle and frame.has_media:
            silent = now - last_progress
            if silent <= policy.busy_window:
                return PrinterView(LIVE, "job in progress", busy_block(frame), frame, busy=True, synthesized=True)
            # Past the cap that idle reply would claim an idle printer while the job
            # has not ended (a stalled write, a half-close drain, a silent client):
            # with no word from the printer for that long, its status is stale.
            return down(OFFLINE, f"no word from the printer for {silent:.0f} s since the job's last data, "
                                 f"and the job has not ended")
        return live_view
    if job_active:
        return live_view  # a frame read during or after the job: the printer's own word
    frame_age = max(0.0, now - polled)
    if frame_age > policy.frame_max_age:
        return down(OFFLINE, f"no fresh status from the printer for {frame_age:.0f} s")
    return live_view


class Condition(NamedTuple):
    """Host Resources / Printer-MIB view of a :class:`PrinterView`."""

    device_status: int       # hrDeviceStatus
    printer_status: int      # hrPrinterStatus
    error_state: int         # hrPrinterDetectedErrorState, first octet
    console: str             # prtConsoleDisplayBufferText (16 characters)
    cover: int               # prtCoverStatus
    input_status: int        # prtInputStatus


# RFC 2790 / RFC 3805 values.
_HR_UNKNOWN, _HR_RUNNING, _HR_DOWN = 1, 2, 5
_PRT_OTHER, _PRT_IDLE, _PRT_PRINTING, _PRT_WARMUP = 1, 3, 4, 5
_ES_NO_PAPER, _ES_DOOR_OPEN, _ES_JAMMED, _ES_OFFLINE, _ES_SERVICE = 0x40, 0x08, 0x04, 0x02, 0x01
_COVER_UNKNOWN, _COVER_OPEN, _COVER_CLOSED = 2, 3, 4
# PrtSubUnitStatusTC sums: availability (0 idle, 3 unavailable, 5 unknown) + 16 critical
# alert, + 32 off-line, + 64 transitioning.
_INPUT_OK, _INPUT_NO_MEDIA, _INPUT_OFFLINE, _INPUT_WARMUP = 0, 3 + 16, 5 + 32, 5 + 64
_CONSOLE_WIDTH = 16
#: Status error bits -> hrPrinterDetectedErrorState bits. err2 0x01 ("replace
#: media") maps to nothing: a real QL-810W in the media-mismatch latch reports 00.
_ERR1_STATE = {0x01: _ES_NO_PAPER, 0x02: _ES_NO_PAPER, 0x04: _ES_JAMMED, 0x20: _ES_OFFLINE,
               0x40: _ES_SERVICE, 0x80: _ES_SERVICE}
_ERR2_STATE = {0x02: _ES_SERVICE, 0x04: _ES_SERVICE, 0x08: _ES_SERVICE, 0x10: _ES_DOOR_OPEN,
               0x40: _ES_JAMMED, 0x80: _ES_SERVICE}
_COVER_OPEN_BIT = 0x10


def _console(text: str) -> str:
    return text.ljust(_CONSOLE_WIDTH)[:_CONSOLE_WIDTH]


def condition(view: PrinterView) -> Condition:
    """hrDeviceStatus, hrPrinterStatus, hrPrinterDetectedErrorState, console text,
    prtCoverStatus and prtInputStatus for ``view`` (real QL-810W state matrix)."""
    if not view.live:
        if view.availability == WARMUP:
            return Condition(_HR_RUNNING, _PRT_WARMUP, 0, _console("BUSY"), _COVER_UNKNOWN, _INPUT_WARMUP)
        device = _HR_UNKNOWN if view.availability == UNKNOWN else _HR_DOWN
        return Condition(device, _PRT_OTHER, _ES_OFFLINE, _console("OFFLINE"), _COVER_UNKNOWN, _INPUT_OFFLINE)
    block = view.block
    cover = _COVER_OPEN if block.err2 & _COVER_OPEN_BIT else _COVER_CLOSED
    input_status = _INPUT_OK if block.has_media else _INPUT_NO_MEDIA
    if block.is_error:
        bits = 0
        for bit, flag in _ERR1_STATE.items():
            if block.err1 & bit:
                bits |= flag
        for bit, flag in _ERR2_STATE.items():
            if block.err2 & bit:
                bits |= flag
        text = "COVER OPEN" if cover == _COVER_OPEN else "ERROR"
        return Condition(_HR_DOWN, _PRT_OTHER, bits, _console(text), cover, input_status)
    if view.busy:
        return Condition(_HR_RUNNING, _PRT_PRINTING, 0, _console("BUSY"), cover, input_status)
    return Condition(_HR_RUNNING, _PRT_IDLE, 0, _console("READY"), cover, input_status)


def normalize_ieee1284(device_id: Optional[str], model: str) -> Optional[str]:
    """The IEEE-1284 device ID with its model field forced to ``MDL:<model>``.

    P-touch Editor for Mac accepts a printer only if a field is exactly
    ``MDL:QL-810W``, with no space after the colon and no "c". ``MODEL:`` is
    rewritten to ``MDL:``. A missing model field is inserted after ``MFG:``.
    Control characters are removed: the client reads the value as a C string.
    Other fields are kept as they are. Returns None when nothing is left.
    """
    if not isinstance(device_id, str):
        return None
    fields = [f.strip() for f in _CONTROL_CHARS.sub("", device_id).split(";")]
    fields = [f for f in fields if f]
    if not fields:
        return None
    out: List[str] = []
    have_model = False
    for item in fields:
        key, sep, _value = item.partition(":")
        if sep and key.strip().upper() in ("MDL", "MODEL"):
            if not have_model:
                out.append(f"MDL:{model}")
                have_model = True
            continue
        out.append(item)
    if not have_model:
        position = next((i + 1 for i, f in enumerate(out)
                         if f.partition(":")[0].strip().upper() in ("MFG", "MANUFACTURER")), 0)
        out.insert(position, f"MDL:{model}")
    return ";".join(out) + ";"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
class StateSource:
    """state.json, checked at most every ``min_interval`` seconds and re-read only when it changed.

    ``generation`` increases whenever a different document, or its absence,
    is observed.
    """

    def __init__(self, path: Union[str, "os.PathLike[str]"], *, min_interval: float = STATE_REREAD_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.path = os.fspath(path)
        self._cache = statecache.StateCache(self.path)
        self._min_interval = float(min_interval)
        self._mono = monotonic
        self._checked: Optional[float] = None
        self._signature: Optional[Tuple[int, ...]] = None
        self._doc: Optional[Dict[str, Any]] = None
        self.generation = 0
        self.reads = 0

    def get(self) -> Optional[Dict[str, Any]]:
        """The current document, or None when missing or unreadable. Never raises."""
        now = self._mono()
        if self._checked is not None and now - self._checked < self._min_interval:
            return self._doc
        self._checked = now
        try:
            st = os.stat(self.path)
        except OSError:
            if self._signature is not None or self._doc is not None:
                self.generation += 1
            self._signature, self._doc = None, None
            return None
        signature = (st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if signature != self._signature:
            self._signature = signature
            self._doc = self._cache.read()
            self.reads += 1
            self.generation += 1
        return self._doc


_RTF_UP = 0x0001
_RTF_GATEWAY = 0x0002


class NetInfo:
    """MAC address of the client-facing interface (Linux: /proc/net/route + sysfs).

    The interface is the one whose connected route holds ``proxy_ip`` (the
    longest matching prefix), else the default route's. A found MAC is reused
    for ``ttl`` seconds; a failed lookup is retried after ``retry`` seconds.
    The result is None where it cannot be determined (no such files, no MAC,
    not Linux).
    """

    def __init__(self, proxy_ip: str = "", *, route_path: str = "/proc/net/route",
                 sysfs_net: str = "/sys/class/net", ttl: float = NETINFO_TTL_SECONDS,
                 retry: float = NETINFO_RETRY_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._target: Optional[int] = None
        text = (proxy_ip or "").strip()
        if text:
            try:
                self._target = int(ipaddress.IPv4Address(text))
            except ValueError:
                log.warning("PROXY_IP=%r is not an IPv4 address; using the default route's interface", text)
        self._route_path = route_path
        self._sysfs_net = sysfs_net
        self._ttl = float(ttl)
        self._retry = float(retry)
        self._mono = monotonic
        self._cached: Optional[Tuple[float, Optional[str], Optional[bytes]]] = None

    def interface(self) -> Optional[str]:
        """Name of the client-facing interface, or None."""
        return self._lookup()[0]

    def mac(self) -> Optional[bytes]:
        """Its 6-byte MAC address, or None."""
        return self._lookup()[1]

    def _lookup(self) -> Tuple[Optional[str], Optional[bytes]]:
        now = self._mono()
        cached = self._cached
        if cached is not None:
            limit = self._ttl if cached[2] is not None else self._retry
            if now - cached[0] < limit:
                return cached[1], cached[2]
        iface: Optional[str] = None
        mac: Optional[bytes] = None
        for candidate in self._candidates():
            mac = self._read_mac(candidate)
            if mac is not None:
                iface = candidate
                break
        self._cached = (now, iface, mac)
        return iface, mac

    def _candidates(self) -> List[str]:
        try:
            with open(self._route_path, encoding="ascii", errors="replace") as fh:
                lines = fh.read(1 << 20).splitlines()[1:]
        except OSError:
            return []
        matches: List[Tuple[int, int, str]] = []
        defaults: List[Tuple[int, str]] = []
        for line in lines:
            fields = line.split()
            if len(fields) < 8:
                continue
            try:
                dest = _route_addr(fields[1])
                flags = int(fields[3], 16)
                metric = int(fields[6])
                mask = _route_addr(fields[7])
            except ValueError:
                continue
            if not flags & _RTF_UP:
                continue
            if mask == 0 and dest == 0:
                defaults.append((metric, fields[0]))
            elif self._target is not None and self._target & mask == dest:
                # The connected route (no gateway) of the address, most specific first.
                matches.append((1 if flags & _RTF_GATEWAY else 0, -bin(mask).count("1"), fields[0]))
        ordered = [m[2] for m in sorted(matches)] + [name for _, name in sorted(defaults)]
        return list(dict.fromkeys(ordered))

    def _read_mac(self, iface: str) -> Optional[bytes]:
        if not iface or "/" in iface or iface in (".", ".."):
            return None
        try:
            with open(os.path.join(self._sysfs_net, iface, "address"), encoding="ascii") as fh:
                text = fh.read(64)
        except (OSError, UnicodeDecodeError):
            return None
        return _parse_mac(text)


def _route_addr(hex_text: str) -> int:
    """An address field of /proc/net/route as a network-order integer.

    The kernel prints the in-memory (network byte order) value as a host-order
    hex number, so converting it back through the native byte order restores
    the address bytes.
    """
    value = int(hex_text, 16)
    if not 0 <= value <= _UINT32_MAX:
        raise ValueError(f"not a 32-bit address: {hex_text!r}")
    return int.from_bytes(value.to_bytes(4, sys.byteorder), "big")


def _parse_mac(text: str) -> Optional[bytes]:
    """``00:00:5e:00:53:01`` -> 6 bytes; None for anything else or an all-zero address."""
    parts = text.strip().split(":")
    if len(parts) != 6 or not all(len(p) == 2 for p in parts):
        return None
    try:
        mac = bytes(int(p, 16) for p in parts)
    except ValueError:
        return None
    return None if mac == bytes(6) else mac


# ---------------------------------------------------------------------------
# The persona
# ---------------------------------------------------------------------------
class Persona:
    """Values of the oid-map entries for the current printer state.

    ``cfg`` is the runtime configuration (``config.load_runtime_config()``);
    ``state_path`` the proxy's state.json. ``clock`` (wall time, compared with
    the proxy's timestamps) and ``monotonic`` can be replaced in tests. All
    methods are thread-safe and never block on anything but a local file stat.
    Raises ConfigError for an unusable PERSONA_MAC or invalid status settings.

    ``previous`` is the persona this one replaces when the agent reloads its
    settings and OID map (both must use the same clocks). The new persona
    carries on where it left off: sysUpTime keeps counting from the agent's
    start, and, for the same state.json, the remembered USB identity and the
    progress of a job in flight (the busy-phase cap counts from its last byte)
    are kept.
    """

    def __init__(self, oid_map: OidMap, cfg: Mapping[str, Any], state_path: Union[str, "os.PathLike[str]"],
                 *, policy: Optional[Policy] = None, netinfo: Optional[NetInfo] = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 previous: Optional["Persona"] = None) -> None:
        self.oid_map = oid_map
        self._cfg = cfg
        self.policy = policy if policy is not None else Policy.from_config(cfg)
        self._clock = clock
        self._mono = monotonic
        self._started = monotonic()
        self.state = StateSource(state_path, monotonic=monotonic)
        self._net = netinfo if netinfo is not None else NetInfo(self._setting("PROXY_IP"), monotonic=monotonic)
        mac_text = self._setting("PERSONA_MAC")
        self._mac_override = _parse_mac(mac_text) if mac_text else None
        if mac_text and self._mac_override is None:
            raise qconfig.ConfigError(f"PERSONA_MAC={mac_text!r}: not a usable MAC address "
                                      f"(six colon-separated hex pairs, not all zero)")
        self._offline = offline_block(oid_map.model)
        self._lock = threading.RLock()
        self._view: Optional[PrinterView] = None
        self._view_at = 0.0
        self._view_generation = -1
        self._logged: Optional[Tuple[str, str]] = None
        self._identity: Dict[str, str] = {}
        self._job: Optional[Tuple[Tuple[Any, Any], Any, float]] = None  # (job key, bytes, progress time)
        self._failed: Dict[Oid, float] = {}
        self._mac_missing_logged = False
        self._providers: Dict[str, Callable[[PrinterView], Any]] = {
            "status_block": lambda v: v.block.raw,
            "ieee1284_id": lambda v: normalize_ieee1284(self._identity.get("ieee1284_id"), self.oid_map.model),
            "serial": self._serial,
            "mac": lambda v: self._mac(),
            "sysname": self._sysname,
            "sysuptime": lambda v: int((self._mono() - self._started) * 100) & _UINT32_MAX,
            "hr_device_status": lambda v: condition(v).device_status,
            "hr_printer_status": lambda v: condition(v).printer_status,
            "hr_detected_error_state": lambda v: bytes((condition(v).error_state,)),
            "cover_status": lambda v: condition(v).cover,
            "input_status": lambda v: condition(v).input_status,
            "media_name": self._media_name,
            "media_width": lambda v: v.block.media_width_mm * 100 if v.live and v.block.has_media else -2,
            "media_length": self._media_length,
            "console_text": lambda v: condition(v).console,
        }
        if previous is not None:
            self._carry_over(previous)

    def _carry_over(self, previous: "Persona") -> None:
        """Continue the state of the persona this one replaces (see the class docstring)."""
        with previous._lock:
            self._started = previous._started
            if previous.state.path == self.state.path:
                self._identity = dict(previous._identity)
                self._job = previous._job

    @property
    def provider_names(self) -> FrozenSet[str]:
        """Names of the providers this persona implements (equal to :data:`PROVIDERS`)."""
        return frozenset(self._providers)

    # -- state ------------------------------------------------------------
    def view(self) -> PrinterView:
        """The current :class:`PrinterView`, re-evaluated at most every 0.5 s."""
        with self._lock:
            now_m = self._mono()
            if self._view is not None and now_m - self._view_at < STATE_REREAD_SECONDS:
                return self._view
            doc = self.state.get()
            if self.state.generation != self._view_generation:
                self._view_generation = self.state.generation
                self._remember_identity(doc)
                self._track_job(doc)
            activity = self._job[2] if self._job is not None else None
            view = evaluate(doc, self._clock(), self.policy, self._offline, job_activity=activity)
            self._view, self._view_at = view, now_m
            key = (view.availability, view.summary())
            if key != self._logged:
                self._logged = key
                log.info("printer persona: %s", view.summary())
            return view

    def summary(self) -> str:
        """One line describing the printer as the persona presents it."""
        return self.view().summary()

    def _remember_identity(self, doc: Optional[Mapping[str, Any]]) -> None:
        """Keep the printer-mode USB identity, so it survives an unplugged printer.

        ql810wc-proxyd keeps the last printer-mode identity while the printer is
        absent or in Editor Lite mode. Values from those states only fill gaps
        (an agent started while the printer is away); they never replace what a
        "ready" printer reported, because Editor Lite has another USB serial.
        """
        ready = statecache.get_path(doc, "device.state") == "ready"
        for key in ("ieee1284_id", "serial"):
            value = statecache.get_path(doc, f"device.{key}")
            if isinstance(value, str) and value.strip() and (ready or key not in self._identity):
                self._identity[key] = value.strip()

    def _track_job(self, doc: Optional[Mapping[str, Any]]) -> None:
        """Note when the active job's byte count last changed (its last observed byte)."""
        raw_job = doc.get("job") if doc is not None else None
        if not isinstance(raw_job, Mapping) or raw_job.get("active") is not True:
            self._job = None
            return
        key = (raw_job.get("started"), raw_job.get("peer"))
        count = raw_job.get("bytes")
        stamp = _timestamp(doc.get("updated") if doc is not None else None)
        if stamp is None:
            stamp = self._clock()
        if self._job is None or self._job[0] != key:
            started = _timestamp(raw_job.get("started"))
            self._job = (key, count, started if started is not None else stamp)
        elif count != self._job[1]:
            self._job = (key, count, stamp)

    # -- values -----------------------------------------------------------
    def value(self, entry: Entry) -> Any:
        """The typed value of ``entry`` now, or None when it is absent. Never raises."""
        source = entry.source
        try:
            if source.kind == "const":
                value = source.arg
            elif source.kind == "config":
                value = self._config_value(entry)
            elif source.kind == "template":
                value = _render_template(source.arg, self._template_values())
            else:
                value = self._providers[source.arg](self.view())
                if value is None:
                    value = source.default
        except Exception:  # a provider bug must not take the agent down
            now = self._mono()
            if now - self._failed.get(entry.oid, -3600.0) >= 300.0:
                self._failed[entry.oid] = now
                log.exception("cannot compute %s (%s); answering it as absent", entry.name, entry.dotted)
            if source.kind == "provider" and source.arg == "status_block":
                return self._offline.raw  # never absent: P-touch fails hard on noSuchName
            return None
        if value is None:
            return None
        if entry.type == "string":
            value = _display_string(value)
        elif entry.type == "octets" and isinstance(value, str):
            value = value.encode("utf-8")
        return value

    def varbind(self, entry: Entry) -> Optional[VarBind]:
        """``entry``'s VarBind now, or None when absent."""
        value = self.value(entry)
        return None if value is None else VarBind(entry.oid, entry.vtype, value)

    def _setting(self, key: str) -> str:
        value = self._cfg.get(key)
        if value is None:
            value = qconfig.DEFAULTS.get(key, "")
        return str(value).strip()

    def _config_value(self, entry: Entry) -> Any:
        text = self._setting(entry.source.arg)
        if entry.type == "integer":
            if not text:
                return None
            return qconfig.get_int({entry.source.arg: text}, entry.source.arg,
                                   minimum=_INT32_MIN, maximum=_INT32_MAX)
        return text

    def _template_values(self) -> Dict[str, str]:
        values = {key: self._setting(key) for key in qconfig.SETTINGS}
        values["MODEL"] = self.oid_map.model
        return values

    def _serial(self, view: PrinterView) -> Optional[str]:
        return self._setting("PERSONA_SERIAL") or self._identity.get("serial")

    def _mac(self) -> Optional[bytes]:
        if self._mac_override is not None:
            return self._mac_override
        mac = self._net.mac()
        if mac is None and not self._mac_missing_logged:
            self._mac_missing_logged = True
            log.warning("cannot determine this host's MAC address for the persona (ifPhysAddress.1, "
                        "node name); set PERSONA_MAC if this persists")
        elif mac is not None and self._mac_missing_logged:
            self._mac_missing_logged = False
            log.info("persona MAC address found on interface %s", self._net.interface())
        return mac

    def _sysname(self, view: PrinterView) -> str:
        override = self._setting("PERSONA_SYSNAME")
        if override:
            return override
        mac = self._mac()
        return "BRW" + (mac.hex().upper() if mac is not None else "0" * 12)

    def _media_name(self, view: PrinterView) -> str:
        if not view.live or not view.block.has_media:
            return ""
        return media.mib_media_name(media.describe(view.block, exact=False))

    def _media_length(self, view: PrinterView) -> int:
        block = view.block
        if not view.live or not block.has_media:
            return -2
        return -1 if block.media_length_mm == 0 else block.media_length_mm * 100


def _display_string(value: Any) -> str:
    """Text cut to :data:`DISPLAY_STRING_MAX` bytes without splitting a character."""
    text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
    data = text.encode("utf-8")
    if len(data) <= DISPLAY_STRING_MAX:
        return text
    return data[:DISPLAY_STRING_MAX].decode("utf-8", "ignore")


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------
def within(oid: Sequence[int], root: Sequence[int]) -> bool:
    """True if ``oid`` is ``root`` or lies below it."""
    return tuple(oid[:len(root)]) == tuple(root)


def subtree_end(root: Sequence[int]) -> Oid:
    """The first OID after the subtree ``root`` (its next sibling)."""
    return tuple(root[:-1]) + (root[-1] + 1,)


class PersonaTree:
    """The persona's OIDs inside ``subtrees``, served in SNMP (lexicographic) order.

    Implements the agentx handler interface (``get``/``getnext``) for
    ``context``. :meth:`lookup` and :meth:`lookup_next` also return the
    oid-map entry, for the pass_persist formatter. Entries whose value is
    absent right now are skipped by GetNext.
    """

    def __init__(self, persona: Persona, *, context: str = PERSONA_CONTEXT,
                 subtrees: Iterable[OidLike] = AGENTX_SUBTREES) -> None:
        self.persona = persona
        self.context = context
        self.subtrees: Tuple[Oid, ...] = tuple(sorted(parse_oid(s) for s in subtrees))
        self._entries = persona.oid_map.within(self.subtrees)
        self._keys = [e.oid for e in self._entries]
        self._by_oid = {e.oid: e for e in self._entries}

    @property
    def entries(self) -> Tuple[Entry, ...]:
        """The served entries, in OID order."""
        return self._entries

    def registrations(self) -> Tuple[Oid, ...]:
        """The subtrees that contain at least one entry, in the order to register them.

        The subtree holding the status block comes first, then the others in
        OID order. snmpd creates the context with the first registration and
        answers the rest of it with noSuchObject (SNMPv1: noSuchName, which
        fails a P-touch print at once) until each subtree is registered. Before
        the first registration it drops requests for the context, and P-touch
        retries those.
        """
        used = [s for s in self.subtrees if any(within(e.oid, s) for e in self._entries)]
        status = [e.oid for e in self._entries if e.source.kind == "provider" and e.source.arg == "status_block"]
        return tuple(sorted(used, key=lambda s: (not any(within(oid, s) for oid in status), s)))

    def lookup(self, oid: OidLike) -> Optional[Tuple[Entry, VarBind]]:
        """(entry, VarBind) at exactly ``oid``, or None when it is not served or absent now."""
        entry = self._by_oid.get(parse_oid(oid))
        if entry is None:
            return None
        vb = self.persona.varbind(entry)
        return None if vb is None else (entry, vb)

    def lookup_next(self, start: OidLike, end: Optional[OidLike] = None,
                    include: bool = False) -> Optional[Tuple[Entry, VarBind]]:
        """The first served (entry, VarBind) after ``start`` (at it with ``include``), before ``end``."""
        start_oid = parse_oid(start)
        end_oid = parse_oid(end) if end else None
        keys = self._keys
        i = bisect_left(keys, start_oid) if include else bisect_right(keys, start_oid)
        while i < len(keys):
            entry = self._entries[i]
            if end_oid is not None and entry.oid >= end_oid:
                return None
            vb = self.persona.varbind(entry)
            if vb is not None:
                return entry, vb
            i += 1
        return None

    def get(self, oid: OidLike, context: str = "") -> Optional[VarBind]:
        """AgentX Get: the VarBind at ``oid`` in our context."""
        if (context or "") != self.context:
            return None
        key = parse_oid(oid)
        entry = self._by_oid.get(key)
        if entry is None:
            return None
        vb = self.persona.varbind(entry)
        return vb if vb is not None else VarBind(key, VarType.NO_SUCH_INSTANCE, None)

    def getnext(self, start: OidLike, end: Optional[OidLike], include: bool,
                context: str = "") -> Optional[VarBind]:
        """AgentX GetNext: the first VarBind after ``start`` and before ``end`` in our context."""
        if (context or "") != self.context:
            return None
        found = self.lookup_next(start, end, include)
        return None if found is None else found[1]

    def walk(self) -> Iterator[Tuple[Entry, VarBind]]:
        """Every served (entry, VarBind) in OID order, as a GETNEXT walk would see them."""
        found = self.lookup_next((), None, True)
        while found is not None:
            yield found
            found = self.lookup_next(found[0].oid)


def describe_varbind(entry: Entry, vb: VarBind) -> str:
    """``OID = TYPE: value`` like net-snmp's numeric output (for --dump)."""
    oid = "." + format_oid(vb.oid)
    value = vb.value
    if entry.type == "integer":
        text = f"INTEGER: {value}"
    elif entry.type in ("octets", "string"):
        data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if entry.type == "string" and all(0x20 <= b < 0x7F for b in data):
            text = 'STRING: "' + data.decode("ascii").replace("\\", "\\\\").replace('"', '\\"') + '"'
        elif data:
            text = "Hex-STRING: " + data.hex(" ").upper()
        else:
            text = '""'
    elif entry.type == "oid":
        text = "OID: ." + format_oid(parse_oid(value))
    elif entry.type == "timeticks":
        text = f"Timeticks: ({value})"
    elif entry.type == "counter32":
        text = f"Counter32: {value}"
    elif entry.type == "gauge32":
        text = f"Gauge32: {value}"
    else:
        text = f"IpAddress: {value}"
    return f"{oid} = {text}"
