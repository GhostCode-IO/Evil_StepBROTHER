"""AgentX (RFC 2741) subagent protocol for the ql810wc printer persona.

``ql810wc-snmp-agent`` uses this module to serve the printer persona from
inside net-snmp's ``snmpd``, which stays the only process on UDP/161. The
persona is registered in the SNMP context ``printer``; v1/v2c communities are
mapped to that context with ``com2sec -Cn printer`` (docs/architecture.md section 6).
Standard library only; runs on Python 3.9+.

Contents
  * A complete PDU codec: the 20-byte header, both byte orders (we always send
    network byte order and accept either), OIDs with the ``1.3.6.1.<n>`` prefix
    compression and the include flag, octet strings padded to 4 bytes,
    SearchRanges and every VarBind type.
  * :class:`Subagent`: Open, Register (optionally in a non-default context),
    periodic Ping, Close; answers Get, GetNext, GetBulk, TestSet (always
    ``notWritable``), CommitSet/UndoSet, and CleanupSet (no response, RFC 2741
    section 7.2.4.4). It reconnects and re-registers whenever the master goes away.
    :meth:`Subagent.update` swaps the handler and the registration set of a
    running session (a configuration reload) without closing it: new subtrees
    are registered before dropped ones are unregistered.
  * :class:`TableHandler`: sorted per-context tables of static or computed
    values that implement the handler interface.

Handler interface (implement it, or use :class:`TableHandler`)::

    handler.get(oid, context) -> VarBind | None
    handler.getnext(start, end, include, context) -> VarBind | None

``oid``, ``start`` and ``end`` are tuples of ints. ``end`` is ``None`` when the
range is unbounded. ``context`` is ``""`` for the default context. A
:class:`VarBind` (or a plain ``(oid, type, value)`` tuple) answers the request.
``None`` from ``get`` means the object is absent, and the subagent answers
noSuchObject or noSuchInstance as appropriate. ``None`` from ``getnext`` means
there is nothing in the range (endOfMibView). Handlers run on the subagent's
thread and must return quickly. SNMP clients give up within seconds (P-touch
waits 2 s), and net-snmp disconnects a subagent that stays silent through
``agentXTimeout`` (1 s) and ``agentXRetries`` (5).
"""
from __future__ import annotations

import enum
import ipaddress
import logging
import operator
import re
import socket
import struct
import threading
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterable, List, Mapping, NamedTuple,
                    Optional, Sequence, Tuple, Union)

__all__ = [
    "AGENTX_VERSION", "HEADER_SIZE", "DEFAULT_SOCKET", "DEFAULT_PRIORITY",
    "MAX_OID_LENGTH", "MAX_PDU_SIZE", "MAX_INCOMING_PAYLOAD",
    "FLAG_INSTANCE_REGISTRATION", "FLAG_NEW_INDEX", "FLAG_ANY_INDEX",
    "FLAG_NON_DEFAULT_CONTEXT", "FLAG_NETWORK_BYTE_ORDER",
    "NO_ERROR", "TOO_BIG", "GEN_ERR", "NOT_WRITABLE", "OPEN_FAILED", "NOT_OPEN",
    "DUPLICATE_REGISTRATION", "UNKNOWN_REGISTRATION", "PARSE_ERROR", "ERROR_NAMES",
    "PduType", "VarType", "CloseReason", "Oid", "OidLike",
    "VarBind", "SearchRange", "Header", "Pdu", "Registration",
    "AgentXError", "ParseError", "EncodeError", "ConnectionLost", "SessionError",
    "error_name", "parse_oid", "format_oid",
    "encode_oid", "decode_oid", "encode_octets", "decode_octets",
    "encode_varbind", "decode_varbind", "encode_pdu", "decode_pdu", "decode_header",
    "TableHandler", "Subagent",
]

log = logging.getLogger("ql810wc.agentx")

Oid = Tuple[int, ...]
OidLike = Union[str, Sequence[int]]

AGENTX_VERSION = 1
HEADER_SIZE = 20
DEFAULT_SOCKET = "/var/agentx/master"
#: RFC 2741 default registration priority (lower value = higher priority).
DEFAULT_PRIORITY = 127
#: SNMP limit on sub-identifiers per OID (net-snmp's MAX_OID_LEN).
MAX_OID_LENGTH = 128
#: Largest PDU, header included, that we send: net-snmp's agentx_parse()
#: rejects anything bigger.
MAX_PDU_SIZE = 65536
#: Largest payload we accept. A bigger length means the stream lost framing.
MAX_INCOMING_PAYLOAD = 4 * 1024 * 1024

# h.flags bits (RFC 2741 section 6.1).
FLAG_INSTANCE_REGISTRATION = 0x01
FLAG_NEW_INDEX = 0x02
FLAG_ANY_INDEX = 0x04
FLAG_NON_DEFAULT_CONTEXT = 0x08
FLAG_NETWORK_BYTE_ORDER = 0x10

# res.error values used by this module (the full table is ERROR_NAMES).
NO_ERROR = 0
TOO_BIG = 1
GEN_ERR = 5
NOT_WRITABLE = 17
OPEN_FAILED = 256
NOT_OPEN = 257
DUPLICATE_REGISTRATION = 263
UNKNOWN_REGISTRATION = 264
PARSE_ERROR = 266

#: res.error names: SNMP error-status values (RFC 3416) and AgentX
#: administrative errors (RFC 2741 section 6.2.16).
ERROR_NAMES: Dict[int, str] = {
    0: "noError", 1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly",
    5: "genErr", 6: "noAccess", 7: "wrongType", 8: "wrongLength",
    9: "wrongEncoding", 10: "wrongValue", 11: "noCreation",
    12: "inconsistentValue", 13: "resourceUnavailable", 14: "commitFailed",
    15: "undoFailed", 16: "authorizationError", 17: "notWritable",
    18: "inconsistentName",
    256: "openFailed", 257: "notOpen", 258: "indexWrongType",
    259: "indexAlreadyAllocated", 260: "indexNoneAvailable",
    261: "indexNotAllocated", 262: "unsupportedContext",
    263: "duplicateRegistration", 264: "unknownRegistration",
    265: "unknownAgentCaps", 266: "parseError", 267: "requestDenied",
    268: "processingError",
}


class PduType(enum.IntEnum):
    """AgentX PDU types (h.type)."""

    OPEN = 1
    CLOSE = 2
    REGISTER = 3
    UNREGISTER = 4
    GET = 5
    GETNEXT = 6
    GETBULK = 7
    TESTSET = 8
    COMMITSET = 9
    UNDOSET = 10
    CLEANUPSET = 11
    NOTIFY = 12
    PING = 13
    INDEXALLOCATE = 14
    INDEXDEALLOCATE = 15
    ADDAGENTCAPS = 16
    REMOVEAGENTCAPS = 17
    RESPONSE = 18


class VarType(enum.IntEnum):
    """VarBind value types (v.type)."""

    INTEGER = 2
    OCTET_STRING = 4
    NULL = 5
    OBJECT_IDENTIFIER = 6
    IP_ADDRESS = 64
    COUNTER32 = 65
    GAUGE32 = 66
    TIME_TICKS = 67
    OPAQUE = 68
    COUNTER64 = 70
    NO_SUCH_OBJECT = 128
    NO_SUCH_INSTANCE = 129
    END_OF_MIB_VIEW = 130

    @classmethod
    def from_name(cls, name: str) -> "VarType":
        """Map a type name such as ``"integer"``, ``"octets"`` or ``"oid"`` to a VarType.

        Case, ``-`` and ``_`` are ignored. The names cover the ``type:`` values of
        ``oid-map.yaml`` and net-snmp's own spellings. Raises ValueError if the
        name is unknown.
        """
        key = re.sub(r"[-_\s]", "", str(name)).lower()
        try:
            return _TYPE_NAMES[key]
        except KeyError:
            raise ValueError(f"unknown SNMP type name {name!r}") from None


_TYPE_NAMES: Dict[str, VarType] = {
    "integer": VarType.INTEGER, "integer32": VarType.INTEGER, "int": VarType.INTEGER,
    "octets": VarType.OCTET_STRING, "octetstring": VarType.OCTET_STRING,
    "string": VarType.OCTET_STRING, "null": VarType.NULL,
    "oid": VarType.OBJECT_IDENTIFIER, "objectid": VarType.OBJECT_IDENTIFIER,
    "objectidentifier": VarType.OBJECT_IDENTIFIER,
    "ipaddress": VarType.IP_ADDRESS, "counter": VarType.COUNTER32,
    "counter32": VarType.COUNTER32, "gauge": VarType.GAUGE32,
    "gauge32": VarType.GAUGE32, "unsigned": VarType.GAUGE32,
    "unsigned32": VarType.GAUGE32, "timeticks": VarType.TIME_TICKS,
    "opaque": VarType.OPAQUE, "counter64": VarType.COUNTER64,
}


class CloseReason(enum.IntEnum):
    """agentx-Close-PDU reasons (c.reason)."""

    OTHER = 1
    PARSE_ERROR = 2
    PROTOCOL_ERROR = 3
    TIMEOUTS = 4
    SHUTDOWN = 5
    BY_MANAGER = 6


# Types that carry no value. The last three are the SNMPv2 "exceptions".
_EXCEPTION_TYPES = frozenset((VarType.NO_SUCH_OBJECT, VarType.NO_SUCH_INSTANCE,
                              VarType.END_OF_MIB_VIEW))
_VALUELESS_TYPES = _EXCEPTION_TYPES | {VarType.NULL}

# PDUs that may carry a context (RFC 2741 section 6.1.1).
_CONTEXT_PDUS = frozenset((
    PduType.REGISTER, PduType.UNREGISTER, PduType.GET, PduType.GETNEXT,
    PduType.GETBULK, PduType.TESTSET, PduType.NOTIFY, PduType.PING,
    PduType.INDEXALLOCATE, PduType.INDEXDEALLOCATE, PduType.ADDAGENTCAPS,
    PduType.REMOVEAGENTCAPS))
# PDUs carrying a VarBindList (after any fixed fields).
_VARBIND_PDUS = frozenset((PduType.TESTSET, PduType.NOTIFY,
                           PduType.INDEXALLOCATE, PduType.INDEXDEALLOCATE))
# Requests a master sends to a subagent.
_MASTER_REQUESTS = frozenset((
    PduType.GET, PduType.GETNEXT, PduType.GETBULK, PduType.TESTSET,
    PduType.COMMITSET, PduType.UNDOSET, PduType.CLEANUPSET, PduType.PING))


def error_name(code: int) -> str:
    """Return ``"name (code)"`` for an AgentX res.error value, e.g. ``"duplicateRegistration (263)"``."""
    return f"{ERROR_NAMES.get(code, 'unknown')} ({code})"


def _pdu_name(ptype: int) -> str:
    try:
        return PduType(ptype).name
    except ValueError:
        return f"type-{ptype}"


def _reason_name(reason: int) -> str:
    try:
        return f"{CloseReason(reason).name} ({reason})"
    except ValueError:
        return f"reason {reason}"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class AgentXError(Exception):
    """Base class of the errors raised by this module."""


class ParseError(AgentXError):
    """Received data is not a well-formed AgentX PDU."""


class EncodeError(AgentXError, ValueError):
    """A value or PDU field cannot be represented in AgentX."""


class ConnectionLost(AgentXError):
    """The transport to the master failed, closed, or stopped answering."""


class SessionError(AgentXError):
    """The master refused or ended our session."""


class _RequestFailed(Exception):
    """A request must be answered with genErr at the 1-based SearchRange *index*."""

    def __init__(self, index: int) -> None:
        super().__init__(index)
        self.index = index


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------
class VarBind(NamedTuple):
    """One variable binding: OID, value type (:class:`VarType` or int) and Python value.

    Values: INTEGER, Counter32, Gauge32, TimeTicks and Counter64 are ints.
    OCTET STRING and Opaque are bytes (str is encoded as UTF-8). OBJECT
    IDENTIFIER is an OID tuple or dotted string. IpAddress is a dotted-quad str,
    4 bytes or IPv4Address. Null and the exceptions carry ``None``. Decoding
    returns bytes, tuples and dotted-quad strs.
    """

    oid: Oid
    type: int
    value: Any = None


class SearchRange(NamedTuple):
    """A Get/GetNext/GetBulk SearchRange. An empty ``end`` means unbounded."""

    start: Oid
    end: Oid = ()
    include: bool = False


class Header(NamedTuple):
    """The decoded 20-byte AgentX header."""

    version: int
    type: int
    flags: int
    session_id: int
    transaction_id: int
    packet_id: int
    payload_length: int


@dataclass
class Pdu:
    """One AgentX PDU. Only the fields belonging to ``type`` are encoded.

    ``flags`` keeps the INSTANCE_REGISTRATION / NEW_INDEX / ANY_INDEX bits. The
    codec derives NETWORK_BYTE_ORDER and NON_DEFAULT_CONTEXT itself (decoding
    keeps the raw flags). ``context`` is ``None`` (or ``""``) for the default
    context.
    """

    type: int
    session_id: int = 0
    transaction_id: int = 0
    packet_id: int = 0
    flags: int = 0
    context: Optional[str] = None
    # Open (o.timeout, o.id, o.descr); Register r.timeout; AgentCaps a.id/a.descr
    timeout: int = 0
    id: Oid = ()
    descr: str = ""
    # Close
    reason: int = 0
    # Register / Unregister
    priority: int = DEFAULT_PRIORITY
    range_subid: int = 0
    subtree: Oid = ()
    upper_bound: int = 0
    # Get / GetNext / GetBulk
    non_repeaters: int = 0
    max_repetitions: int = 0
    ranges: List[SearchRange] = field(default_factory=list)
    # Response, TestSet, Notify, IndexAllocate, IndexDeallocate
    varbinds: List[VarBind] = field(default_factory=list)
    # Response
    sys_uptime: int = 0
    error: int = 0
    index: int = 0


class Registration(NamedTuple):
    """A subtree the :class:`Subagent` registers. ``context`` ``""`` = default context.

    ``range_subid``/``upper_bound`` register a range (RFC 2741 section 6.2.3):
    sub-identifier number ``range_subid`` (1-based, counted in the full OID)
    of ``subtree`` runs from its value up to ``upper_bound``.
    """

    context: str
    subtree: Oid
    priority: int = DEFAULT_PRIORITY
    range_subid: int = 0
    upper_bound: int = 0
    timeout: int = 0
    instance: bool = False


# ---------------------------------------------------------------------------
# OIDs
# ---------------------------------------------------------------------------
_DOTTED_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*")


def parse_oid(value: OidLike) -> Oid:
    """Return *value* as a validated OID tuple.

    Accepts a dotted string (``"1.3.6.1"`` or ``".1.3.6.1"``) or a sequence of
    ints; ``""`` and ``()`` are the null OID. Raises ValueError for anything
    else, sub-identifiers outside 0..2**32-1, or more than 128 sub-identifiers.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("."):
            text = text[1:]
        if not text:
            return ()
        if not _DOTTED_RE.fullmatch(text):
            raise ValueError(f"invalid OID {value!r}")
        parts = tuple(int(p) for p in text.split("."))
    elif isinstance(value, (tuple, list)):
        parts = tuple(value)
        for p in parts:
            if not isinstance(p, int):
                raise ValueError(f"invalid OID {value!r}: sub-identifiers must be ints")
    else:
        raise ValueError(f"an OID must be a dotted string or a sequence of ints, not {type(value).__name__}")
    if len(parts) > MAX_OID_LENGTH:
        raise ValueError(f"OID has {len(parts)} sub-identifiers (maximum {MAX_OID_LENGTH})")
    for p in parts:
        if not 0 <= p <= 0xFFFFFFFF:
            raise ValueError(f"invalid OID {value!r}: sub-identifier {p} out of range")
    return parts


def format_oid(oid: Sequence[int]) -> str:
    """Return *oid* in dotted notation without a leading dot (``"1.3.6.1"``)."""
    return ".".join(str(p) for p in oid)


# ---------------------------------------------------------------------------
# Low-level codec
# ---------------------------------------------------------------------------
class _Fmt:
    """Precompiled struct formats for one byte order."""

    __slots__ = ("order", "u16", "u32", "i32", "u64", "ids")

    def __init__(self, order: str) -> None:
        self.order = order
        self.u16 = struct.Struct(order + "H")
        self.u32 = struct.Struct(order + "I")
        self.i32 = struct.Struct(order + "i")
        self.u64 = struct.Struct(order + "Q")
        self.ids = struct.Struct(order + "IIII")  # session, transaction, packet, length


# Without NETWORK_BYTE_ORDER, integers are little-endian (RFC 2741 section 6.1).
_FMT = {True: _Fmt(">"), False: _Fmt("<")}


def _oid_bytes(oid: Oid, include: bool, fmt: _Fmt) -> bytes:
    """Encode a validated OID tuple, using the 1.3.6.1.<n> prefix where possible."""
    if (len(oid) >= 5 and oid[0] == 1 and oid[1] == 3 and oid[2] == 6
            and oid[3] == 1 and 0 < oid[4] < 256):
        prefix, sub = oid[4], oid[5:]
    else:
        prefix, sub = 0, oid
    # net-snmp never sets "include" on the null OID; neither do we.
    head = bytes((len(sub), prefix, 1 if include and oid else 0, 0))
    return head + struct.pack(f"{fmt.order}{len(sub)}I", *sub)


def _octets_bytes(data: bytes, fmt: _Fmt) -> bytes:
    return fmt.u32.pack(len(data)) + data + b"\0" * (-len(data) % 4)


class _Reader:
    """Bounds-checked sequential reader over one PDU payload."""

    __slots__ = ("_buf", "_pos", "_end", "_fmt")

    def __init__(self, buf: bytes, pos: int, end: int, network_byte_order: bool) -> None:
        self._buf = buf
        self._pos = pos
        self._end = end
        self._fmt = _FMT[network_byte_order]

    @property
    def pos(self) -> int:
        return self._pos

    @property
    def remaining(self) -> int:
        return self._end - self._pos

    def _advance(self, n: int, what: str) -> int:
        pos = self._pos
        if n > self._end - pos:
            raise ParseError(f"truncated {what}: {n} bytes needed, {self._end - pos} left")
        self._pos = pos + n
        return pos

    def bytes4(self, what: str) -> bytes:
        pos = self._advance(4, what)
        return bytes(self._buf[pos:pos + 4])

    def u16(self, what: str) -> int:
        return self._fmt.u16.unpack_from(self._buf, self._advance(2, what))[0]

    def u32(self, what: str) -> int:
        return self._fmt.u32.unpack_from(self._buf, self._advance(4, what))[0]

    def i32(self, what: str) -> int:
        return self._fmt.i32.unpack_from(self._buf, self._advance(4, what))[0]

    def u64(self, what: str) -> int:
        return self._fmt.u64.unpack_from(self._buf, self._advance(8, what))[0]

    def oid(self, what: str) -> Tuple[Oid, bool]:
        n_subid, prefix, include, _reserved = self.bytes4(what)
        pos = self._advance(4 * n_subid, what)
        sub = struct.unpack_from(f"{self._fmt.order}{n_subid}I", self._buf, pos)
        return ((1, 3, 6, 1, prefix) + sub if prefix else sub), bool(include)

    def octets(self, what: str) -> bytes:
        n = self.u32(what + " length")
        pos = self._advance(n, what)
        self._advance(-n % 4, what + " padding")
        return bytes(self._buf[pos:pos + n])


def _integral(value: Any, vtype: VarType) -> int:
    try:
        return operator.index(value)
    except TypeError:
        raise EncodeError(f"{vtype.name} value must be an integer, not {type(value).__name__}") from None


def _unsigned(value: Any, vtype: VarType) -> int:
    v = _integral(value, vtype)
    if v < 0:
        raise EncodeError(f"{vtype.name} value must not be negative: {v}")
    return v


def _octet_value(value: Any, vtype: VarType) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise EncodeError(f"{vtype.name} value must be bytes or str, not {type(value).__name__}")


def _ipv4_value(value: Any) -> bytes:
    if isinstance(value, ipaddress.IPv4Address):
        return value.packed
    if isinstance(value, str):
        try:
            return ipaddress.IPv4Address(value).packed
        except ValueError:
            raise EncodeError(f"invalid IpAddress {value!r}") from None
    if isinstance(value, (bytes, bytearray, memoryview)) and len(value) == 4:
        return bytes(value)
    raise EncodeError(f"IpAddress must be a dotted quad, 4 bytes or IPv4Address, not {value!r}")


def _value_bytes(vtype: VarType, value: Any, fmt: _Fmt) -> bytes:
    """Encode a VarBind value.

    INTEGER must fit in 32 bits (signed). Counter32, TimeTicks and Counter64
    wrap modulo 2**32 / 2**64, and Gauge32 latches at 2**32-1, as SMIv2
    defines them. Negative values are rejected for all unsigned types.
    """
    if vtype == VarType.INTEGER:
        v = _integral(value, vtype)
        if not -0x80000000 <= v <= 0x7FFFFFFF:
            raise EncodeError(f"INTEGER out of range: {v}")
        return fmt.i32.pack(v)
    if vtype in (VarType.COUNTER32, VarType.TIME_TICKS):
        return fmt.u32.pack(_unsigned(value, vtype) & 0xFFFFFFFF)
    if vtype == VarType.GAUGE32:
        return fmt.u32.pack(min(_unsigned(value, vtype), 0xFFFFFFFF))
    if vtype == VarType.COUNTER64:
        return fmt.u64.pack(_unsigned(value, vtype) & 0xFFFFFFFFFFFFFFFF)
    if vtype in (VarType.OCTET_STRING, VarType.OPAQUE):
        return _octets_bytes(_octet_value(value, vtype), fmt)
    if vtype == VarType.IP_ADDRESS:
        return _octets_bytes(_ipv4_value(value), fmt)
    if vtype == VarType.OBJECT_IDENTIFIER:
        try:
            oid = parse_oid(value)
        except ValueError as exc:
            raise EncodeError(f"invalid OBJECT IDENTIFIER value: {exc}") from None
        return _oid_bytes(oid, False, fmt)
    return b""  # Null and the exceptions carry no value


def _vartype(code: Any) -> VarType:
    try:
        return VarType(code)
    except ValueError:
        raise EncodeError(f"unknown VarBind type {code!r}") from None


def _varbind_bytes(vb: Any, fmt: _Fmt) -> bytes:
    try:
        oid_value, type_code, value = vb
    except (TypeError, ValueError):
        raise EncodeError(f"a VarBind must be (oid, type, value), not {vb!r}") from None
    vtype = _vartype(type_code)
    try:
        oid = parse_oid(oid_value)
    except ValueError as exc:
        raise EncodeError(f"invalid VarBind name: {exc}") from None
    return fmt.u16.pack(vtype) + b"\0\0" + _oid_bytes(oid, False, fmt) + _value_bytes(vtype, value, fmt)


def _read_value(r: _Reader, vtype: int) -> Any:
    if vtype == VarType.INTEGER:
        return r.i32("INTEGER")
    if vtype in (VarType.COUNTER32, VarType.GAUGE32, VarType.TIME_TICKS):
        return r.u32("32-bit unsigned value")
    if vtype == VarType.COUNTER64:
        return r.u64("Counter64")
    if vtype in (VarType.OCTET_STRING, VarType.OPAQUE):
        return r.octets("OCTET STRING")
    if vtype == VarType.IP_ADDRESS:
        raw = r.octets("IpAddress")
        if len(raw) != 4:
            raise ParseError(f"IpAddress must be 4 octets, got {len(raw)}")
        return str(ipaddress.IPv4Address(raw))
    if vtype == VarType.OBJECT_IDENTIFIER:
        return r.oid("OBJECT IDENTIFIER value")[0]
    if vtype in _VALUELESS_TYPES:
        return None
    raise ParseError(f"unknown VarBind type {vtype}")


def _read_varbind(r: _Reader) -> VarBind:
    vtype = r.u16("VarBind type")
    r.u16("VarBind reserved field")
    oid, _include = r.oid("VarBind name")
    value = _read_value(r, vtype)
    return VarBind(oid, VarType(vtype), value)


def encode_oid(oid: OidLike, include: bool = False, network_byte_order: bool = True) -> bytes:
    """Encode an OID (RFC 2741 section 5.1), using the internet prefix where possible."""
    try:
        parsed = parse_oid(oid)
    except ValueError as exc:
        raise EncodeError(str(exc)) from None
    return _oid_bytes(parsed, include, _FMT[network_byte_order])


def decode_oid(data: bytes, offset: int = 0, network_byte_order: bool = True) -> Tuple[Oid, bool, int]:
    """Decode an OID at *offset*; return ``(oid, include, next_offset)``."""
    r = _Reader(data, offset, len(data), network_byte_order)
    oid, include = r.oid("OID")
    return oid, include, r.pos


def encode_octets(data: bytes, network_byte_order: bool = True) -> bytes:
    """Encode an Octet String: 4-byte length, data, zero padding to a multiple of 4."""
    return _octets_bytes(bytes(data), _FMT[network_byte_order])


def decode_octets(data: bytes, offset: int = 0, network_byte_order: bool = True) -> Tuple[bytes, int]:
    """Decode an Octet String at *offset*; return ``(data, next_offset)``."""
    r = _Reader(data, offset, len(data), network_byte_order)
    value = r.octets("Octet String")
    return value, r.pos


def encode_varbind(vb: Union[VarBind, Tuple[Any, int, Any]], network_byte_order: bool = True) -> bytes:
    """Encode one VarBind. Raises EncodeError if the value does not fit its type."""
    return _varbind_bytes(vb, _FMT[network_byte_order])


def decode_varbind(data: bytes, offset: int = 0, network_byte_order: bool = True) -> Tuple[VarBind, int]:
    """Decode a VarBind at *offset*; return ``(varbind, next_offset)``."""
    r = _Reader(data, offset, len(data), network_byte_order)
    vb = _read_varbind(r)
    return vb, r.pos


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _text_bytes(text: Union[str, bytes]) -> bytes:
    if isinstance(text, (bytes, bytearray)):
        return bytes(text)
    return text.encode("utf-8", "surrogateescape")


def _payload(ptype: PduType, pdu: Pdu, fmt: _Fmt) -> List[bytes]:
    """Encode the type-specific fields of *pdu* (everything after any context)."""
    parts: List[bytes] = []
    if ptype == PduType.OPEN:
        parts += [bytes((pdu.timeout, 0, 0, 0)), _oid_bytes(parse_oid(pdu.id), False, fmt),
                  _octets_bytes(_text_bytes(pdu.descr), fmt)]
    elif ptype == PduType.CLOSE:
        parts.append(bytes((pdu.reason, 0, 0, 0)))
    elif ptype in (PduType.REGISTER, PduType.UNREGISTER):
        subtree = parse_oid(pdu.subtree)
        if pdu.range_subid > len(subtree):
            raise EncodeError(f"range_subid {pdu.range_subid} is beyond the {len(subtree)}-component subtree")
        timeout = pdu.timeout if ptype == PduType.REGISTER else 0
        parts += [bytes((timeout, pdu.priority, pdu.range_subid, 0)), _oid_bytes(subtree, False, fmt)]
        if pdu.range_subid:
            parts.append(fmt.u32.pack(pdu.upper_bound))
    elif ptype in (PduType.GET, PduType.GETNEXT, PduType.GETBULK):
        if ptype == PduType.GETBULK:
            parts += [fmt.u16.pack(pdu.non_repeaters), fmt.u16.pack(pdu.max_repetitions)]
        for rng in pdu.ranges:
            parts += [_oid_bytes(parse_oid(rng.start), bool(rng.include), fmt),
                      _oid_bytes(parse_oid(rng.end), False, fmt)]
    elif ptype in _VARBIND_PDUS:
        parts += [_varbind_bytes(vb, fmt) for vb in pdu.varbinds]
    elif ptype == PduType.ADDAGENTCAPS:
        parts += [_oid_bytes(parse_oid(pdu.id), False, fmt), _octets_bytes(_text_bytes(pdu.descr), fmt)]
    elif ptype == PduType.REMOVEAGENTCAPS:
        parts.append(_oid_bytes(parse_oid(pdu.id), False, fmt))
    elif ptype == PduType.RESPONSE:
        parts += [fmt.u32.pack(pdu.sys_uptime), fmt.u16.pack(pdu.error), fmt.u16.pack(pdu.index)]
        parts += [_varbind_bytes(vb, fmt) for vb in pdu.varbinds]
    # CommitSet, UndoSet, CleanupSet and Ping have no payload beyond a context.
    return parts


def encode_pdu(pdu: Pdu, network_byte_order: bool = True) -> bytes:
    """Encode *pdu* as one frame (header and payload).

    The subagent always uses network byte order; ``False`` produces the
    little-endian form, which is useful for testing peers.
    """
    try:
        ptype = PduType(pdu.type)
    except ValueError:
        raise EncodeError(f"unknown PDU type {pdu.type!r}") from None
    fmt = _FMT[network_byte_order]
    flags = pdu.flags & ~(FLAG_NON_DEFAULT_CONTEXT | FLAG_NETWORK_BYTE_ORDER)
    if network_byte_order:
        flags |= FLAG_NETWORK_BYTE_ORDER
    try:
        parts: List[bytes] = []
        if pdu.context and ptype in _CONTEXT_PDUS:
            flags |= FLAG_NON_DEFAULT_CONTEXT
            parts.append(_octets_bytes(_text_bytes(pdu.context), fmt))
        parts += _payload(ptype, pdu, fmt)
        payload = b"".join(parts)
        header = bytes((AGENTX_VERSION, ptype, flags, 0)) + fmt.ids.pack(
            pdu.session_id, pdu.transaction_id, pdu.packet_id, len(payload))
    except EncodeError:
        raise
    except (struct.error, ValueError, TypeError, OverflowError) as exc:
        raise EncodeError(f"cannot encode {ptype.name} PDU: {exc}") from None
    return header + payload


def decode_header(data: bytes) -> Header:
    """Decode the 20-byte header at the start of *data* (the version is not checked here)."""
    if len(data) < HEADER_SIZE:
        raise ParseError(f"short AgentX header: {len(data)} bytes")
    flags = data[2]
    ids = _FMT[bool(flags & FLAG_NETWORK_BYTE_ORDER)].ids.unpack_from(data, 4)
    return Header(data[0], data[1], flags, *ids)


def decode_pdu(frame: bytes) -> Pdu:
    """Decode one complete frame (header and exactly ``payload_length`` bytes) into a Pdu."""
    hdr = decode_header(frame)
    if hdr.version != AGENTX_VERSION:
        raise ParseError(f"unsupported AgentX version {hdr.version}")
    if len(frame) != HEADER_SIZE + hdr.payload_length:
        raise ParseError(f"frame has {len(frame)} bytes, header announces {HEADER_SIZE + hdr.payload_length}")
    if hdr.payload_length % 4:
        raise ParseError(f"payload length {hdr.payload_length} is not a multiple of 4")
    try:
        ptype = PduType(hdr.type)
    except ValueError:
        raise ParseError(f"unknown PDU type {hdr.type}") from None
    r = _Reader(frame, HEADER_SIZE, len(frame), bool(hdr.flags & FLAG_NETWORK_BYTE_ORDER))
    pdu = Pdu(type=ptype, session_id=hdr.session_id, transaction_id=hdr.transaction_id,
              packet_id=hdr.packet_id, flags=hdr.flags)
    if ptype in _CONTEXT_PDUS and hdr.flags & FLAG_NON_DEFAULT_CONTEXT:
        pdu.context = _text(r.octets("context"))
    if ptype == PduType.OPEN:
        pdu.timeout = r.bytes4("Open timeout")[0]
        pdu.id = r.oid("Open id")[0]
        pdu.descr = _text(r.octets("Open descr"))
    elif ptype == PduType.CLOSE:
        pdu.reason = r.bytes4("Close reason")[0]
    elif ptype in (PduType.REGISTER, PduType.UNREGISTER):
        timeout, pdu.priority, pdu.range_subid, _reserved = r.bytes4("registration header")
        if ptype == PduType.REGISTER:
            pdu.timeout = timeout
        pdu.subtree = r.oid("registration subtree")[0]
        if pdu.range_subid:
            if pdu.range_subid > len(pdu.subtree):
                raise ParseError(f"range_subid {pdu.range_subid} is beyond the subtree")
            pdu.upper_bound = r.u32("registration upper bound")
    elif ptype in (PduType.GET, PduType.GETNEXT, PduType.GETBULK):
        if ptype == PduType.GETBULK:
            pdu.non_repeaters = r.u16("non_repeaters")
            pdu.max_repetitions = r.u16("max_repetitions")
        while r.remaining:
            start, include = r.oid("SearchRange start")
            end, _ = r.oid("SearchRange end")
            pdu.ranges.append(SearchRange(start, end, include))
    elif ptype in _VARBIND_PDUS:
        while r.remaining:
            pdu.varbinds.append(_read_varbind(r))
    elif ptype == PduType.ADDAGENTCAPS:
        pdu.id = r.oid("AgentCaps id")[0]
        pdu.descr = _text(r.octets("AgentCaps descr"))
    elif ptype == PduType.REMOVEAGENTCAPS:
        pdu.id = r.oid("AgentCaps id")[0]
    elif ptype == PduType.RESPONSE:
        pdu.sys_uptime = r.u32("Response sysUpTime")
        pdu.error = r.u16("Response error")
        pdu.index = r.u16("Response index")
        while r.remaining:
            pdu.varbinds.append(_read_varbind(r))
    if r.remaining:
        raise ParseError(f"{r.remaining} unexpected trailing bytes in {ptype.name} PDU")
    return pdu


# ---------------------------------------------------------------------------
# TableHandler
# ---------------------------------------------------------------------------
class _Table(NamedTuple):
    keys: List[Oid]
    entries: Dict[Oid, Tuple[VarType, Any]]


def _context_key(context: Optional[str]) -> str:
    return "" if context is None else context


def _oid_arg(value: OidLike) -> Oid:
    return value if isinstance(value, tuple) else parse_oid(value)


class TableHandler:
    """Handler serving per-context tables of static or computed values.

    ``tables`` maps a context (``""`` or ``None`` = default) to
    ``{oid: (type, value)}``. ``oid`` is a tuple or dotted string. ``type`` is a
    :class:`VarType`, its int code or a name accepted by
    :meth:`VarType.from_name`. ``value`` is a constant, or a zero-argument
    callable evaluated on every request. A callable returning ``None`` means
    "not instantiated right now": ``get`` reports it absent and ``getnext``
    skips it. Exceptions from callables reach the subagent, which answers
    genErr. Lookups use binary search over the keys in lexicographic OID order.
    """

    def __init__(self, tables: Optional[Mapping[Optional[str], Mapping[OidLike, Tuple[Any, Any]]]] = None) -> None:
        self._tables: Dict[str, _Table] = {}
        for context, entries in (tables or {}).items():
            self.set_table(context, entries)

    def set_table(self, context: Optional[str], entries: Mapping[OidLike, Tuple[Any, Any]]) -> None:
        """Replace the table of *context*.

        The new table is validated first: OIDs, types, and constant values are
        test-encoded. It then replaces the old one in a single reference swap,
        so concurrent lookups see either the old or the new table, never a mix.
        Raises ValueError (EncodeError for bad values) on invalid input.
        """
        parsed: Dict[Oid, Tuple[VarType, Any]] = {}
        for key, spec in entries.items():
            oid = parse_oid(key)
            if not oid:
                raise ValueError("table OIDs must not be empty")
            if oid in parsed:
                raise ValueError(f"OID {format_oid(oid)} appears twice in the table")
            try:
                type_spec, value = spec
            except (TypeError, ValueError):
                raise ValueError(f"{format_oid(oid)}: expected (type, value), got {spec!r}") from None
            vtype = _table_type(type_spec, oid)
            if not callable(value):
                try:
                    _value_bytes(vtype, value, _FMT[True])
                except EncodeError as exc:
                    raise EncodeError(f"{format_oid(oid)}: {exc}") from None
            parsed[oid] = (vtype, value)
        self._tables[_context_key(context)] = _Table(sorted(parsed), parsed)

    def get(self, oid: OidLike, context: Optional[str] = "") -> Optional[VarBind]:
        """Return the VarBind instantiated exactly at *oid*, or None."""
        table = self._tables.get(_context_key(context))
        if table is None:
            return None
        key = _oid_arg(oid)
        spec = table.entries.get(key)
        return None if spec is None else _materialize(key, spec)

    def getnext(self, start: OidLike, end: Optional[OidLike], include: bool,
                context: Optional[str] = "") -> Optional[VarBind]:
        """Return the first VarBind after *start*, or None.

        The result is at or after *start* when *include* is true, strictly after
        it otherwise, and strictly before *end* when *end* is given.
        """
        table = self._tables.get(_context_key(context))
        if table is None:
            return None
        start_oid = _oid_arg(start)
        end_oid = _oid_arg(end) if end else None
        keys = table.keys
        i = bisect_left(keys, start_oid) if include else bisect_right(keys, start_oid)
        while i < len(keys):
            key = keys[i]
            if end_oid is not None and key >= end_oid:
                return None
            vb = _materialize(key, table.entries[key])
            if vb is not None:
                return vb
            i += 1
        return None


def _table_type(type_spec: Any, oid: Oid) -> VarType:
    try:
        vtype = VarType.from_name(type_spec) if isinstance(type_spec, str) else VarType(type_spec)
    except ValueError:
        raise ValueError(f"{format_oid(oid)}: unknown type {type_spec!r}") from None
    if vtype in _EXCEPTION_TYPES:
        raise ValueError(f"{format_oid(oid)}: {vtype.name} is not a data type")
    return vtype


def _materialize(oid: Oid, spec: Tuple[VarType, Any]) -> Optional[VarBind]:
    vtype, value = spec
    if callable(value):
        value = value()
        if value is None:
            return None
    return VarBind(oid, vtype, value)


# ---------------------------------------------------------------------------
# Subagent
# ---------------------------------------------------------------------------
_POLL_INTERVAL = 0.5          # longest wait before re-checking the stop event
_CLOSE_TIMEOUT = 1.0          # how long to wait for the master to answer Close
_SLOW_HANDLER_SECONDS = 0.5   # warn well before clients (2 s) or agentXTimeout (1 s) give up
# Room for VarBinds in a Response: header plus sysUpTime/error/index.
_VARBIND_BUDGET = MAX_PDU_SIZE - HEADER_SIZE - 8


def _parse_master_address(spec: str) -> Tuple[str, Any]:
    """Parse ``/path``, ``unix:/path`` or ``tcp:HOST:PORT`` into (kind, address)."""
    if not isinstance(spec, str) or not spec:
        raise ValueError(f"invalid AgentX master address {spec!r}")
    if spec.startswith("tcp:"):
        host, sep, port = spec[4:].rpartition(":")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"invalid AgentX master address {spec!r} (expected tcp:HOST:PORT)")
        return "tcp", (host, int(port))
    path = spec[5:] if spec.startswith("unix:") else spec
    if not path:
        raise ValueError(f"invalid AgentX master address {spec!r}")
    if not hasattr(socket, "AF_UNIX"):
        raise ValueError("unix-domain AgentX sockets are not supported on this platform")
    return "unix", path


def _connect_hint(exc: OSError) -> str:
    if isinstance(exc, FileNotFoundError):
        return " (is snmpd running with 'master agentx'?)"
    if isinstance(exc, PermissionError):
        return " (check agentXPerms: this user needs write access to the socket)"
    if isinstance(exc, ConnectionRefusedError):
        return " (is snmpd running?)"
    return ""


def _as_registration(item: Any) -> Registration:
    """Validate and normalize a Registration or a ``(context, subtree[, priority])`` tuple."""
    if isinstance(item, Registration):
        reg = item
    else:
        try:
            fields = tuple(item)
        except TypeError:
            raise ValueError(f"invalid registration {item!r}") from None
        if not 2 <= len(fields) <= 3:
            raise ValueError(f"a registration is (context, subtree[, priority]), not {item!r}")
        reg = Registration(*fields)
    context = _context_key(reg.context)
    if not isinstance(context, str):
        raise ValueError(f"registration context must be a str, not {context!r}")
    subtree = parse_oid(reg.subtree)
    if not subtree:
        raise ValueError("a registration subtree must not be empty")
    if not 1 <= reg.priority <= 255:
        raise ValueError(f"registration priority must be 1..255, not {reg.priority}")
    if not 0 <= reg.timeout <= 255:
        raise ValueError(f"registration timeout must be 0..255 s, not {reg.timeout}")
    if reg.range_subid:
        if not 1 <= reg.range_subid <= len(subtree):
            raise ValueError(f"range_subid {reg.range_subid} is outside the subtree {format_oid(subtree)}")
        if not subtree[reg.range_subid - 1] <= reg.upper_bound <= 0xFFFFFFFF:
            raise ValueError(f"upper_bound {reg.upper_bound} must be between the range start "
                             f"{subtree[reg.range_subid - 1]} and 4294967295")
    elif reg.upper_bound:
        raise ValueError("upper_bound requires range_subid")
    return reg._replace(context=context, subtree=subtree, instance=bool(reg.instance))


def _registration_set(items: Iterable[Any]) -> Tuple[Registration, ...]:
    """Validate a non-empty list of distinct registrations (ValueError otherwise)."""
    regs = tuple(_as_registration(item) for item in items)
    if not regs:
        raise ValueError("at least one registration is required")
    if len(set(regs)) != len(regs):
        raise ValueError("duplicate registration")
    return regs


def _check_handler(handler: Any) -> None:
    if not (callable(getattr(handler, "get", None)) and callable(getattr(handler, "getnext", None))):
        raise TypeError("handler must provide get(oid, context) and getnext(start, end, include, context)")


def _describe(reg: Registration) -> str:
    text = format_oid(reg.subtree)
    if reg.range_subid:
        text += f" (sub-id {reg.range_subid} up to {reg.upper_bound})"
    return f"{text} in context {reg.context!r} priority {reg.priority}"


def _log_repeated(attempt: int, level: int, msg: str, *args: Any) -> None:
    """Log the first three occurrences of a recurring problem at *level*, then
    every 20th; the rest go to DEBUG so a long outage does not flood the journal."""
    log.log(level if attempt <= 3 or attempt % 20 == 0 else logging.DEBUG, msg, *args)


class _Connection:
    """Framed AgentX stream: buffered reads with timeouts, bounded sends."""

    def __init__(self, sock: socket.socket, send_timeout: float) -> None:
        self._sock = sock
        self._send_timeout = send_timeout
        self._buf = bytearray()
        self.last_rx = time.monotonic()

    def send(self, data: bytes) -> None:
        try:
            self._sock.settimeout(self._send_timeout)
            self._sock.sendall(data)
        except socket.timeout:
            raise ConnectionLost(f"master did not accept data within {self._send_timeout:g} s") from None
        except OSError as exc:
            raise ConnectionLost(f"send failed: {exc}") from None

    def read_frame(self, timeout: float) -> Optional[bytes]:
        """Return the next complete frame, or None if none arrives within *timeout*."""
        deadline = time.monotonic() + timeout
        while True:
            frame = self._pop_frame()
            if frame is not None:
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                self._sock.settimeout(remaining)
                chunk = self._sock.recv(65536)
            except socket.timeout:
                return None
            except OSError as exc:
                raise ConnectionLost(f"receive failed: {exc}") from None
            if not chunk:
                raise ConnectionLost("master closed the connection")
            self._buf += chunk
            self.last_rx = time.monotonic()

    def _pop_frame(self) -> Optional[bytes]:
        if len(self._buf) < HEADER_SIZE:
            return None
        nbo = bool(self._buf[2] & FLAG_NETWORK_BYTE_ORDER)
        length = _FMT[nbo].u32.unpack_from(self._buf, 16)[0]
        if length > MAX_INCOMING_PAYLOAD:
            raise ConnectionLost(f"refusing a {length}-byte payload: the stream is out of sync")
        total = HEADER_SIZE + length
        if len(self._buf) < total:
            return None
        frame = bytes(self._buf[:total])
        del self._buf[:total]
        return frame

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError as exc:
            log.debug("closing the AgentX socket: %s", exc)


def _ids(pdu: Union[Pdu, Header]) -> Tuple[int, int, int]:
    return pdu.session_id, pdu.transaction_id, pdu.packet_id


def _as_varbind(found: Any) -> VarBind:
    if isinstance(found, VarBind):
        return found
    oid, vtype, value = found
    return VarBind(oid, vtype, value)


class Subagent:
    """An AgentX subagent session manager that keeps its registrations alive.

    ``socket_path`` is the master's AgentX address: a unix socket path
    (``/var/agentx/master``, optionally prefixed ``unix:``) or
    ``tcp:HOST:PORT``. ``registrations`` lists :class:`Registration` objects or
    ``(context, subtree[, priority])`` tuples. ``name`` labels the logs.
    ``descr`` is the Open description the master shows (default: ``name``).
    ``connector``, if given, is a zero-argument callable that returns a
    connected stream socket and replaces ``socket_path``; tests and custom
    transports use it.

    :meth:`run_forever` serves until its stop event is set. It reconnects and
    re-registers after any failure, with exponential backoff from
    ``backoff_min`` to ``backoff_max`` seconds. It pings an idle master every
    ``ping_interval`` seconds, and a missing or failed Ping counts as a
    disconnect. :attr:`ready` is set while a session is open with every
    registration accepted.

    :meth:`update` replaces the handler and the registration set while the
    session runs (a configuration reload). Closing and reopening the session
    instead would leave the master with a context that has nothing registered
    in it: net-snmp keeps a context alive through its own null registrations
    once anything was registered there, and answers its OIDs with noSuchObject
    (noSuchName for SNMPv1) until the subagent is back.
    """

    def __init__(self, handler: Any, socket_path: str = DEFAULT_SOCKET,
                 registrations: Iterable[Any] = (), *, name: str = "ql810wc-snmp-agent",
                 descr: Optional[str] = None, agent_id: OidLike = (),
                 ping_interval: float = 15.0, response_timeout: float = 5.0,
                 connect_timeout: float = 5.0, backoff_min: float = 1.0,
                 backoff_max: float = 30.0,
                 connector: Optional[Callable[[], socket.socket]] = None) -> None:
        _check_handler(handler)
        regs = _registration_set(registrations)
        for label, value in (("ping_interval", ping_interval), ("response_timeout", response_timeout),
                             ("connect_timeout", connect_timeout), ("backoff_min", backoff_min)):
            if not value > 0:
                raise ValueError(f"{label} must be positive, not {value!r}")
        if backoff_max < backoff_min:
            raise ValueError("backoff_max must not be smaller than backoff_min")
        self._handler = handler
        self._registrations = regs
        self._address = "custom connector" if connector else socket_path
        self._target = None if connector else _parse_master_address(socket_path)
        self._connector = connector
        self.name = name
        self._descr = name if descr is None else descr
        self._agent_id = parse_oid(agent_id)
        self._ping_interval = float(ping_interval)
        self._response_timeout = float(response_timeout)
        self._connect_timeout = float(connect_timeout)
        self._backoff_min = float(backoff_min)
        self._backoff_max = float(backoff_max)
        self._epoch = time.monotonic()
        #: Set while a session is open and every registration has been accepted.
        self.ready = threading.Event()
        self._session_id: Optional[int] = None
        self._packet_id = 0
        self._status = "not started"
        # update() runs on another thread. This lock guards the handler, the
        # registration set, the Open description and the generation counters.
        self._lock = threading.Condition(threading.Lock())
        self._generation = 0         # bumped by update() whenever the registration set changes
        self._synced_generation = 0  # the registration set the open session (or the next one) has
        self._in_session = False
        # Session state, used by the session thread only.
        self._wanted: Tuple[Registration, ...] = regs   # the set this session registers
        self._registered: List[Registration] = []        # accepted by the master in this session
        self._pending: List[Registration] = []           # still to be (re)tried in this session
        self._fully_registered = False                   # this session had every registration accepted

    # -- public state --------------------------------------------------------
    @property
    def registrations(self) -> Tuple[Registration, ...]:
        """The normalized registrations this subagent maintains."""
        with self._lock:
            return self._registrations

    @property
    def handler(self) -> Any:
        """The handler that answers new requests."""
        with self._lock:
            return self._handler

    @property
    def session_id(self) -> Optional[int]:
        """The current AgentX session id, or None while disconnected."""
        return self._session_id

    @property
    def status(self) -> str:
        """A one-line description of the current state (e.g. for systemd STATUS=)."""
        return self._status

    # -- reconfiguration -----------------------------------------------------
    def update(self, handler: Any = None, registrations: Optional[Iterable[Any]] = None, *,
               descr: Optional[str] = None) -> int:
        """Replace the handler and/or the registration set without closing the session.

        Thread-safe; meant for a configuration reload while :meth:`run_forever`
        serves on another thread. The new ``handler`` answers every request
        that arrives after this call; a request already being answered is
        finished by the handler it started with, so one Response never mixes
        the two. A changed ``registrations`` set is applied by the session
        thread, normally within half a second: subtrees that are new are
        registered first, then subtrees that are no longer listed are
        unregistered. Subtrees in both sets stay registered throughout. While
        no session is open, the next session registers the new set. ``descr``
        replaces the Open description from the next session on.

        Returns a generation number for :meth:`wait_applied`. Raises
        TypeError or ValueError, as the constructor does; nothing changes then.
        """
        if handler is not None:
            _check_handler(handler)
        regs = _registration_set(registrations) if registrations is not None else None
        if descr is not None and not isinstance(descr, str):
            raise TypeError(f"descr must be a str, not {type(descr).__name__}")
        with self._lock:
            if handler is not None:
                self._handler = handler
            if descr is not None:
                self._descr = descr
            if regs is not None and regs != self._registrations:
                self._registrations = regs
                self._generation += 1
                if not self._in_session:
                    self._synced_generation = self._generation
                self._lock.notify_all()
            return self._generation

    def wait_applied(self, generation: int, timeout: Optional[float] = None) -> bool:
        """Wait until the registration set of update *generation* is in effect.

        That is the case once the session thread has sent its new
        registrations (accepted, or refused and being retried as at start-up)
        and withdrawn the dropped ones, or while no session is open (the next
        one registers the new set). Returns False if *timeout* seconds pass first.
        """
        with self._lock:
            return self._lock.wait_for(
                lambda: self._synced_generation >= generation or not self._in_session, timeout)

    def _set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            log.debug("status: %s", text)

    def _uptime(self) -> int:
        return int((time.monotonic() - self._epoch) * 100) & 0xFFFFFFFF

    def _backoff(self, failures: int) -> float:
        """Delay before retry number *failures* (1-based): doubles from backoff_min to backoff_max."""
        return min(self._backoff_max, self._backoff_min * 2 ** min(max(failures, 1) - 1, 30))

    # -- main loop ------------------------------------------------------------
    def run_forever(self, stop_event: threading.Event) -> None:
        """Serve the master until *stop_event* is set.

        Reconnects, reopens the session and re-registers after every master
        restart or transport failure. Master and transport problems are
        logged, never raised. On stop it sends Close (reason shutdown) and
        returns. Call it from one thread only.
        """
        log.info("AgentX subagent %r starting: master %s, %d registration(s)",
                 self.name, self._address, len(self.registrations))
        failures = 0
        while not stop_event.is_set():
            self._set_status(f"connecting to {self._address}")
            try:
                sock = self._open_socket()
            except OSError as exc:
                failures += 1
                delay = self._backoff(failures)
                _log_repeated(failures, logging.WARNING,
                              "cannot connect to AgentX master %s: %s%s; retrying in %.1f s",
                              self._address, exc, _connect_hint(exc), delay)
                self._set_status(f"waiting for AgentX master {self._address}: {exc}")
                stop_event.wait(delay)
                continue
            conn = _Connection(sock, self._response_timeout)
            self._fully_registered = False
            try:
                self._serve(conn, stop_event)
                fully_registered = self._fully_registered
            except (AgentXError, OSError) as exc:
                fully_registered = self._fully_registered
                _log_repeated(failures + 1, logging.WARNING,
                              "AgentX session with %s ended: %s", self._address, exc)
            except Exception:  # an internal bug must not kill a 24/7 agent
                fully_registered = False
                log.exception("internal error in the AgentX session with %s", self._address)
            finally:
                conn.close()
                with self._lock:
                    self._in_session = False
                    self._lock.notify_all()
                self._session_id = None
                self.ready.clear()
            if stop_event.is_set():
                break
            # A fully registered session proves the setup works: reconnect fast.
            failures = 0 if fully_registered else failures + 1
            delay = self._backoff(failures)
            self._set_status(f"reconnecting to {self._address} in {delay:.1f} s")
            stop_event.wait(delay)
        self._set_status("stopped")
        log.info("AgentX subagent %r stopped", self.name)

    def _open_socket(self) -> socket.socket:
        if self._connector is not None:
            return self._connector()
        kind, address = self._target
        if kind == "tcp":
            sock = socket.create_connection(address, timeout=self._connect_timeout)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if kind == "tcp":
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # small request/response PDUs
            else:
                sock.settimeout(self._connect_timeout)
                sock.connect(address)
        except BaseException:
            sock.close()
            raise
        return sock

    def _serve(self, conn: _Connection, stop_event: threading.Event) -> None:
        """Run one session until stop. Sets ``_fully_registered`` once every registration is accepted."""
        self._open_session(conn)
        with self._lock:
            self._in_session = True
            self._synced_generation = self._generation
            self._wanted = self._registrations
        self._registered = []
        self._pending = list(self._wanted)
        attempts = 0
        next_attempt = 0.0
        while not stop_event.is_set():
            if self._apply_update(conn):
                # Additions the master refused are retried like at start-up.
                attempts = 1
                next_attempt = time.monotonic() + self._backoff(attempts)
            now = time.monotonic()
            if self._pending and now >= next_attempt:
                attempts += 1
                retry_delay = self._backoff(attempts)
                self._register_pending(conn, attempts, retry_delay)
                if self._pending:
                    next_attempt = time.monotonic() + retry_delay
            if time.monotonic() - conn.last_rx >= self._ping_interval:
                self._ping(conn)
            wait = min(_POLL_INTERVAL, conn.last_rx + self._ping_interval - time.monotonic())
            if self._pending:
                wait = min(wait, next_attempt - time.monotonic())
            frame = conn.read_frame(max(wait, 0.01))
            if frame is not None:
                self._handle_frame(conn, frame, None)
        self._close_session(conn)

    def _open_session(self, conn: _Connection) -> None:
        with self._lock:
            descr = self._descr
        reply = self._request(conn, Pdu(type=PduType.OPEN, id=self._agent_id, descr=descr), "Open")
        if reply.error != NO_ERROR:
            raise SessionError(f"master refused Open: {error_name(reply.error)}")
        self._session_id = reply.session_id
        log.info("AgentX session %d open with %s", reply.session_id, self._address)
        self._set_status(f"session {reply.session_id} open, registering")

    def _apply_update(self, conn: _Connection) -> bool:
        """Bring this session's registrations in line with :meth:`update`. True if the set changed."""
        with self._lock:
            if self._synced_generation == self._generation:
                return False
            generation, wanted = self._generation, self._registrations
        keep = set(wanted)
        known = set(self._registered) | set(self._pending)
        added = [reg for reg in wanted if reg not in known]
        removed = [reg for reg in self._registered if reg not in keep]
        still_pending = set(self._pending) | set(added)
        self._wanted = wanted
        self._pending = [reg for reg in wanted if reg in still_pending]
        if added or removed:
            log.info("AgentX session %s: registration set changed: %s",
                     self._session_id, "; ".join(
                         [f"+{_describe(reg)}" for reg in added] + [f"-{_describe(reg)}" for reg in removed]))
        # Register first, unregister second: a subtree that stays is never
        # withdrawn, and the context never runs out of registrations.
        if self._pending:
            self._register_pending(conn, 1, self._backoff(1))
        for reg in removed:
            self._unregister(conn, reg)
        self._report_registrations()
        with self._lock:
            self._synced_generation = generation
            self._lock.notify_all()
        return True

    def _register_pending(self, conn: _Connection, attempt: int, retry_delay: float) -> None:
        still_pending: List[Registration] = []
        for reg in self._pending:
            pdu = Pdu(type=PduType.REGISTER, context=reg.context or None,
                      flags=FLAG_INSTANCE_REGISTRATION if reg.instance else 0,
                      timeout=reg.timeout, priority=reg.priority, range_subid=reg.range_subid,
                      subtree=reg.subtree, upper_bound=reg.upper_bound)
            reply = self._request(conn, pdu, f"Register {_describe(reg)}")
            if reply.error == NO_ERROR:
                self._registered.append(reg)
                log.info("registered %s", _describe(reg))
            elif reply.error == NOT_OPEN:
                raise SessionError(f"master no longer knows session {self._session_id}")
            else:
                still_pending.append(reg)
                _log_repeated(attempt, logging.ERROR,
                              "master refused registration of %s: %s; retrying in %.1f s",
                              _describe(reg), error_name(reply.error), retry_delay)
        self._pending = still_pending
        self._report_registrations()

    def _unregister(self, conn: _Connection, reg: Registration) -> None:
        pdu = Pdu(type=PduType.UNREGISTER, context=reg.context or None, priority=reg.priority,
                  range_subid=reg.range_subid, subtree=reg.subtree, upper_bound=reg.upper_bound)
        reply = self._request(conn, pdu, f"Unregister {_describe(reg)}")
        if reply.error == NO_ERROR:
            self._registered.remove(reg)
            log.info("unregistered %s", _describe(reg))
        elif reply.error == UNKNOWN_REGISTRATION:
            self._registered.remove(reg)
            log.warning("master no longer had %s registered: %s", _describe(reg), error_name(reply.error))
        elif reply.error == NOT_OPEN:
            raise SessionError(f"master no longer knows session {self._session_id}")
        else:
            # Still registered as far as the master knows. Requests that reach
            # us there are answered by the handler (absent: noSuchObject); the
            # registration ends with the session.
            log.warning("master refused to unregister %s: %s; it stays registered until the session ends",
                        _describe(reg), error_name(reply.error))

    def _report_registrations(self) -> None:
        """Status line and :attr:`ready` from the registration state of this session."""
        total = len(self._wanted)
        accepted = sum(1 for reg in self._wanted if reg in self._registered)
        if self._pending:
            self.ready.clear()
            self._set_status(f"session {self._session_id}: {accepted}/{total} registrations "
                             f"accepted, retrying the rest")
            return
        self._set_status(f"session {self._session_id}: all {total} registrations accepted")
        if not self.ready.is_set():
            log.info("AgentX session %s: all %d registration(s) accepted", self._session_id, total)
        self._fully_registered = True
        self.ready.set()

    def _ping(self, conn: _Connection) -> None:
        reply = self._request(conn, Pdu(type=PduType.PING), "Ping")
        if reply.error != NO_ERROR:
            raise SessionError(f"master answered Ping with {error_name(reply.error)}")

    def _close_session(self, conn: _Connection) -> None:
        sid = self._session_id
        if sid is None:
            return
        try:
            self._request(conn, Pdu(type=PduType.CLOSE, reason=CloseReason.SHUTDOWN), "Close",
                          timeout=min(_CLOSE_TIMEOUT, self._response_timeout))
        except (AgentXError, OSError) as exc:
            log.debug("session %d: no clean Close: %s", sid, exc)
        log.info("AgentX session %d closed", sid)

    def _request(self, conn: _Connection, pdu: Pdu, what: str, timeout: Optional[float] = None) -> Pdu:
        """Send *pdu* and wait for its Response, serving master requests meanwhile."""
        if pdu.type != PduType.OPEN:
            pdu.session_id = self._session_id or 0
        self._packet_id = self._packet_id % 0xFFFFFFFF + 1
        pdu.packet_id = self._packet_id
        conn.send(encode_pdu(pdu))
        limit = self._response_timeout if timeout is None else timeout
        deadline = time.monotonic() + limit
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionLost(f"no response to {what} within {limit:g} s")
            frame = conn.read_frame(remaining)
            if frame is not None:
                reply = self._handle_frame(conn, frame, pdu.packet_id)
                if reply is not None:
                    return reply

    # -- incoming PDUs --------------------------------------------------------
    def _handle_frame(self, conn: _Connection, frame: bytes, awaiting: Optional[int]) -> Optional[Pdu]:
        """Process one frame; return it if it is the Response with packet id *awaiting*."""
        try:
            pdu = decode_pdu(frame)
        except ParseError as exc:
            hdr = decode_header(frame)
            if hdr.type in _MASTER_REQUESTS and hdr.type != PduType.CLEANUPSET:
                log.warning("cannot parse %s from master (%s); answering parseError", _pdu_name(hdr.type), exc)
                self._send_response(conn, _ids(hdr), PARSE_ERROR)
            else:
                log.warning("dropping unparsable %s from master: %s", _pdu_name(hdr.type), exc)
            return None
        if pdu.type == PduType.RESPONSE:
            if awaiting is not None and pdu.packet_id == awaiting:
                return pdu
            log.debug("ignoring a Response to unknown packet %d", pdu.packet_id)
            return None
        self._dispatch(conn, pdu)
        return None

    def _dispatch(self, conn: _Connection, pdu: Pdu) -> None:
        ptype = pdu.type
        if ptype == PduType.CLOSE:
            raise SessionError(f"master closed the session ({_reason_name(pdu.reason)})")
        if ptype not in _MASTER_REQUESTS:
            log.warning("ignoring unexpected %s PDU from master", _pdu_name(ptype))
            return
        if self._session_id is None or pdu.session_id != self._session_id:
            log.warning("%s for unknown session %d (ours: %s)", _pdu_name(ptype), pdu.session_id,
                        self._session_id)
            if ptype != PduType.CLEANUPSET:
                self._send_response(conn, _ids(pdu), NOT_OPEN)
            return
        if ptype in (PduType.GET, PduType.GETNEXT, PduType.GETBULK):
            self._serve_read(conn, pdu)
        elif ptype == PduType.TESTSET:
            if pdu.varbinds:
                log.info("refusing SET of %s in context %r: the persona is read-only",
                         ", ".join(format_oid(vb.oid) for vb in pdu.varbinds), pdu.context or "")
                self._send_response(conn, _ids(pdu), NOT_WRITABLE, 1)
            else:
                self._send_response(conn, _ids(pdu))
        elif ptype == PduType.CLEANUPSET:
            log.debug("CleanupSet for transaction %d (no response, RFC 2741 7.2.4.4)", pdu.transaction_id)
        else:  # CommitSet, UndoSet, Ping
            self._send_response(conn, _ids(pdu))

    def _send_response(self, conn: _Connection, ids: Tuple[int, int, int], error: int = NO_ERROR,
                       index: int = 0, varbinds: Sequence[bytes] = ()) -> None:
        body = b"".join(varbinds)
        if len(body) > _VARBIND_BUDGET:
            log.warning("response of %d bytes exceeds the AgentX limit; answering tooBig", len(body))
            error, index, body = TOO_BIG, 0, b""
        fmt = _FMT[True]
        payload = fmt.u32.pack(self._uptime()) + fmt.u16.pack(error) + fmt.u16.pack(index) + body
        conn.send(bytes((AGENTX_VERSION, PduType.RESPONSE, FLAG_NETWORK_BYTE_ORDER, 0))
                  + fmt.ids.pack(*ids, len(payload)) + payload)

    def _serve_read(self, conn: _Connection, pdu: Pdu) -> None:
        context = pdu.context or ""
        handler = self.handler  # one handler answers the whole PDU, even across an update()
        started = time.monotonic()
        try:
            if pdu.type == PduType.GET:
                encoded = [self._encode(self._get(handler, rng.start, context, i), i)
                           for i, rng in enumerate(pdu.ranges, 1)]
            elif pdu.type == PduType.GETNEXT:
                encoded = [self._encode(self._getnext(handler, rng, context, i), i)
                           for i, rng in enumerate(pdu.ranges, 1)]
            else:
                encoded = self._getbulk(handler, pdu, context)
        except _RequestFailed as failed:
            self._send_response(conn, _ids(pdu), GEN_ERR, failed.index)
            return
        elapsed = time.monotonic() - started
        if elapsed > _SLOW_HANDLER_SECONDS:
            log.warning("%s took %.2f s; SNMP clients and snmpd's agentXTimeout expect about 1 s",
                        _pdu_name(pdu.type), elapsed)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("%s context=%r %s -> %d varbind(s)", _pdu_name(pdu.type), context,
                      " ".join(format_oid(r.start) for r in pdu.ranges), len(encoded))
        self._send_response(conn, _ids(pdu), varbinds=encoded)

    def _get(self, handler: Any, oid: Oid, context: str, index: int) -> VarBind:
        try:
            found = handler.get(oid, context)
            if found is not None:
                vb = _as_varbind(found)
                if vb.type in (VarType.NO_SUCH_OBJECT, VarType.NO_SUCH_INSTANCE):
                    return VarBind(oid, VarType(vb.type), None)
                if vb.type != VarType.END_OF_MIB_VIEW:
                    return VarBind(oid, vb.type, vb.value)
            return VarBind(oid, self._absent_type(handler, oid, context), None)
        except Exception:
            log.exception("handler failed on Get %s in context %r; answering genErr", format_oid(oid), context)
            raise _RequestFailed(index) from None

    @staticmethod
    def _absent_type(handler: Any, oid: Oid, context: str) -> VarType:
        """noSuchInstance if the handler instantiates anything under the parent
        of *oid* (the object exists, this instance does not), else noSuchObject."""
        if len(oid) < 2:
            return VarType.NO_SUCH_OBJECT
        parent = oid[:-1]
        found = handler.getnext(parent, None, True, context)
        if found is not None:
            vb = _as_varbind(found)
            if vb.type not in _EXCEPTION_TYPES and parse_oid(vb.oid)[:len(parent)] == parent:
                return VarType.NO_SUCH_INSTANCE
        return VarType.NO_SUCH_OBJECT

    def _getnext(self, handler: Any, rng: SearchRange, context: str, index: int) -> VarBind:
        end = rng.end or None
        try:
            found = handler.getnext(rng.start, end, bool(rng.include), context)
            if found is None:
                return VarBind(rng.start, VarType.END_OF_MIB_VIEW, None)
            vb = _as_varbind(found)
            oid = parse_oid(vb.oid)
        except Exception:
            log.exception("handler failed on GetNext %s in context %r; answering genErr",
                          format_oid(rng.start), context)
            raise _RequestFailed(index) from None
        if vb.type in _EXCEPTION_TYPES:
            return VarBind(rng.start, VarType.END_OF_MIB_VIEW, None)
        after_start = oid > rng.start or (rng.include and oid == rng.start)
        if not after_start or (end is not None and oid >= end):
            log.error("handler answered GetNext %s with %s, outside the search range; answering endOfMibView",
                      format_oid(rng.start), format_oid(oid))
            return VarBind(rng.start, VarType.END_OF_MIB_VIEW, None)
        return VarBind(oid, vb.type, vb.value)

    def _getbulk(self, handler: Any, pdu: Pdu, context: str) -> List[bytes]:
        """RFC 2741 section 7.2.3.3: N non-repeaters, then up to M rounds over the other R ranges."""
        ranges = pdu.ranges
        n = min(pdu.non_repeaters, len(ranges))
        out: List[bytes] = []
        for i in range(n):
            out.append(self._encode(self._getnext(handler, ranges[i], context, i + 1), i + 1))
        size = sum(len(b) for b in out)
        cursors = list(ranges[n:])
        done = [False] * len(cursors)
        for _ in range(pdu.max_repetitions if cursors else 0):
            row: List[bytes] = []
            for j, rng in enumerate(cursors):
                if done[j]:
                    # Name the exception after the column's last VarBind.
                    vb = VarBind(rng.start, VarType.END_OF_MIB_VIEW, None)
                else:
                    vb = self._getnext(handler, rng, context, n + j + 1)
                    if vb.type == VarType.END_OF_MIB_VIEW:
                        done[j] = True
                    else:
                        cursors[j] = SearchRange(vb.oid, rng.end, False)
                row.append(self._encode(vb, n + j + 1))
            row_size = sum(len(b) for b in row)
            if size + row_size > _VARBIND_BUDGET:
                break  # RFC: stop at a transport limit, keep complete rounds only
            out += row
            size += row_size
            if all(done):
                break
        return out

    def _encode(self, vb: VarBind, index: int) -> bytes:
        try:
            return encode_varbind(vb)
        except EncodeError as exc:
            log.error("cannot encode %s as %s: %s; answering genErr",
                      format_oid(vb.oid), _type_label(vb.type), exc)
            raise _RequestFailed(index) from None


def _type_label(code: Any) -> str:
    try:
        return VarType(code).name
    except ValueError:
        return f"type {code!r}"
