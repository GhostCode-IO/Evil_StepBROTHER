"""Brother QL-800/810W/820NWB raster protocol: status block, tokenizer, test job.

Pure functions and classes, no I/O, standard library only.

Sources (see docs/architecture.md):

* Brother "Software Developer's Manual, Raster Command Reference
  QL-800/810W/820NWB" v1.01 (command set, status layout, print-area pins).
* Hardware: ``dev/notes/usb-ground-truth.md`` (the tested QL-810Wc) and
  status frames that others have published from a QL-810W (fw 1.22), a
  QL-800 and a QL-820NWBc.
* Client semantics: static analysis of P-touch Editor 5.3 for Mac.

Where real printers differ from the manual, the printers win:

* byte 11 (media type) reads ``0x0A``/``0x0B``, not the manual's ``0x4A``/``0x4B``;
* byte 14 is a per-roll code, not a fixed ``0x3F``;
* byte 25 is ``0x81`` on the black/red DK-22251 roll and ``0x01`` on
  single-colour rolls (bit 7 = two-colour media);
* the reply to ``ESC i S`` is not always status type ``0x00``: with the cover
  open it arrives as ``0x02`` (error occurred) with err2 ``0x10``;
* a removed roll reads width 0 and media type 0 **without** the err1 "no
  media" bit, and byte 25 keeps the removed roll's value (so it is ignored
  without media; see :attr:`Status.has_media`).

Status frames are passed through verbatim everywhere in this project; this
module only *reads* them (and lets the proxy override the phase bytes while a
job is in flight, see :meth:`Status.with_overrides`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Wire constants
# ---------------------------------------------------------------------------

#: 400 x NUL. A run of NULs is a no-op at a command position; Brother's own
#: drivers start every QL-800-series job with 400 of them. Followed by
#: ``ESC @`` it also resynchronises a printer left mid-command.
INVALIDATE = bytes(400)
#: ``ESC @`` (initialize). Clears the print buffer and resets per-job modes.
ESC_INIT = b"\x1b\x40"
#: ``ESC i S`` (status information request). The reply is one 32-byte block.
STATUS_REQUEST = b"\x1b\x69\x53"
#: What to send to recover a printer that was left mid-job by a torn stream.
RESYNC = INVALIDATE + ESC_INIT
#: Length of every status block (``ESC i S`` reply, unsolicited frame, SNMP value).
STATUS_LEN = 32
#: First three bytes of every status block: print head mark, size, 'B'.
STATUS_HEADER = b"\x80\x20\x42"

#: (series code byte 3, model code byte 4) -> model, per the Brother manual.
MODEL_NAMES: Dict[Tuple[int, int], str] = {
    (0x34, 0x38): "QL-800",
    (0x34, 0x39): "QL-810W",   # also the QL-810Wc (verified on the tested unit)
    (0x34, 0x41): "QL-820NWB",
}
QL810W_SERIES_CODE = 0x34
QL810W_MODEL_CODE = 0x39

# Status type (byte 18).
ST_REPLY = 0x00
ST_PRINTING_COMPLETED = 0x01
ST_ERROR = 0x02
ST_TURNED_OFF = 0x04
ST_NOTIFICATION = 0x05
ST_PHASE_CHANGE = 0x06
STATUS_TYPES: Dict[int, str] = {
    ST_REPLY: "reply to status request",
    ST_PRINTING_COMPLETED: "printing completed",
    ST_ERROR: "error occurred",
    ST_TURNED_OFF: "turned off",
    ST_NOTIFICATION: "notification",
    ST_PHASE_CHANGE: "phase change",
}

# Phase type (byte 19).
PHASE_RECEIVING = 0x00
PHASE_PRINTING = 0x01
PHASE_TYPES: Dict[int, str] = {
    PHASE_RECEIVING: "receiving",
    PHASE_PRINTING: "printing",
}

# Media type (byte 11, low nibble).
MEDIA_NONE = 0x00
MEDIA_CONTINUOUS = 0x0A
MEDIA_DIE_CUT = 0x0B
MEDIA_TYPES: Dict[int, str] = {
    MEDIA_NONE: "none",
    MEDIA_CONTINUOUS: "continuous",
    MEDIA_DIE_CUT: "die-cut",
}

#: Byte 25 bit 7: a black/red (two-colour) roll is loaded.
TWO_COLOR_BIT = 0x80
#: Exact byte-25 value of DK-22251 on QL-800/810W/820NWBc (P-touch for Mac
#: requires exactly this value before it offers red/black printing).
TEXT_COLOR_BLACK_RED = 0x81

#: Error information 1 (byte 8). 0x10 "printer in use" is a busy flag, not a
#: fault: see :attr:`Status.in_use`.
ERR1_IN_USE = 0x10
#: err1 "no media". The tested unit leaves it clear when the roll is simply
#: removed (that reads as width 0 / type 0); it is set by a job without media.
ERR1_NO_MEDIA = 0x01
ERROR1_BITS: Dict[int, str] = {
    0x01: "no media",
    0x02: "end of media",
    0x04: "cutter jam",
    0x10: "printer in use",
    0x20: "printer turned off",
    0x40: "high-voltage adapter error",
    0x80: "fan motor error",
}
#: Error information 2 (byte 9).
ERROR2_BITS: Dict[int, str] = {
    0x01: "replace media",
    0x02: "expansion buffer full",
    0x04: "communication error",
    0x08: "communication buffer full",
    0x10: "cover open",
    0x20: "cancel key",
    0x40: "media cannot be fed",
    0x80: "system error",
}
#: Status byte offset -> its error-bit table.
ERROR_BITS: Dict[int, Dict[int, str]] = {8: ERROR1_BITS, 9: ERROR2_BITS}
#: Extended error codes. Byte 7 per P-touch for Mac's decoder; the QL-800 puts
#: the colour-mismatch codes in byte 31 of an "error occurred" frame instead.
EXT_ERRORS: Dict[int, str] = {
    0x21: "unsupported media",
    0x23: "media does not support two-color printing",
    0x24: "media does not support monochrome printing",
}
#: Notification numbers (byte 22).
NOTIFICATIONS: Dict[int, str] = {
    0x03: "cooling started",
    0x04: "cooling finished",
}

#: Single-byte status fields by name (used by :meth:`Status.with_overrides`).
FIELD_OFFSETS: Dict[str, int] = {
    "series_code": 3,
    "model_code": 4,
    "ext_error": 7,
    "err1": 8,
    "err2": 9,
    "media_width_mm": 10,
    "media_type_raw": 11,
    "roll_code": 14,
    "mode": 15,
    "media_length_mm": 17,
    "status_type": 18,
    "phase_type": 19,
    "notification": 22,
    "bg_color": 24,
    "text_color": 25,
    "error_detail": 31,
}
#: Bytes 20-21 hold the phase number, big-endian.
PHASE_NUMBER_OFFSET = 20


# ---------------------------------------------------------------------------
# Status block
# ---------------------------------------------------------------------------

def _bit_names(value: int, table: Mapping[int, str], label: str, skip: int = 0) -> List[str]:
    """Names of the bits set in ``value`` (ascending), unknown bits included."""
    names = []
    for bit in (1 << n for n in range(8)):
        if value & bit and not bit & skip:
            names.append(table.get(bit, f"unknown {label} bit 0x{bit:02x}"))
    return names


@dataclass(frozen=True, repr=False)
class Status:
    """One 32-byte Brother status block, decoded.

    ``raw`` is always the exact frame; every other attribute is computed from
    it. Construction validates the length and the ``80 20 42`` header.
    """

    raw: bytes

    def __post_init__(self) -> None:
        raw = self.raw
        if isinstance(raw, (bytearray, memoryview)):
            raw = bytes(raw)
            object.__setattr__(self, "raw", raw)
        if not isinstance(raw, bytes):
            raise TypeError(f"status block must be bytes, not {type(raw).__name__}")
        if len(raw) != STATUS_LEN:
            raise ValueError(f"status block must be {STATUS_LEN} bytes, got {len(raw)}")
        if raw[:3] != STATUS_HEADER:
            raise ValueError(f"bad status header {raw[:3].hex(' ')} (expected 80 20 42)")

    def __repr__(self) -> str:
        return f"Status({self.raw.hex(' ')})"

    def __bytes__(self) -> bytes:
        return self.raw

    @classmethod
    def from_hex(cls, text: str) -> "Status":
        """Parse ``"80 20 42 ..."`` or ``"802042..."`` (whitespace is ignored)."""
        try:
            data = bytes.fromhex("".join(str(text).split()))
        except ValueError as exc:
            raise ValueError(f"status block is not valid hex: {exc}") from None
        return cls(data)

    # -- raw fields --------------------------------------------------------
    @property
    def series_code(self) -> int:
        """Byte 3: series code (``0x34`` = '4' for the QL-800 family)."""
        return self.raw[3]

    @property
    def model_code(self) -> int:
        """Byte 4: model code (``0x39`` = '9' for the QL-810W/810Wc)."""
        return self.raw[4]

    @property
    def ext_error(self) -> int:
        """Byte 7: extended error code (see :data:`EXT_ERRORS`), 0 when none."""
        return self.raw[7]

    @property
    def err1(self) -> int:
        """Byte 8: error information 1 (see :data:`ERROR1_BITS`)."""
        return self.raw[8]

    @property
    def err2(self) -> int:
        """Byte 9: error information 2 (see :data:`ERROR2_BITS`)."""
        return self.raw[9]

    @property
    def media_width_mm(self) -> int:
        """Byte 10: loaded media width in millimetres."""
        return self.raw[10]

    @property
    def media_type_raw(self) -> int:
        """Byte 11 as reported (``0x0A``/``0x0B`` on real printers)."""
        return self.raw[11]

    @property
    def media_type(self) -> int:
        """Byte 11 low nibble: 0 none, ``0x0A`` continuous, ``0x0B`` die-cut."""
        return self.raw[11] & 0x0F

    @property
    def roll_code(self) -> int:
        """Byte 14: per-roll code (``0x23`` DK-22251, ``0x15`` DK-22205, ...)."""
        return self.raw[14]

    @property
    def mode(self) -> int:
        """Byte 15: last ``ESC i M`` (various mode) value."""
        return self.raw[15]

    @property
    def media_length_mm(self) -> int:
        """Byte 17: die-cut label length in millimetres, 0 for continuous."""
        return self.raw[17]

    @property
    def status_type(self) -> int:
        """Byte 18: frame kind (see :data:`STATUS_TYPES`)."""
        return self.raw[18]

    @property
    def phase_type(self) -> int:
        """Byte 19: 0 receiving (idle), 1 printing."""
        return self.raw[19]

    @property
    def phase_number(self) -> int:
        """Bytes 20-21, big-endian."""
        return int.from_bytes(self.raw[20:22], "big")

    @property
    def notification(self) -> int:
        """Byte 22: notification number (see :data:`NOTIFICATIONS`)."""
        return self.raw[22]

    @property
    def bg_color(self) -> int:
        """Byte 24: media background colour id (0 on QL rolls)."""
        return self.raw[24]

    @property
    def text_color(self) -> int:
        """Byte 25: ink colour id; ``0x81`` black/red, ``0x01`` single colour."""
        return self.raw[25]

    @property
    def error_detail(self) -> int:
        """Byte 31: colour-mismatch code in QL-800 error frames (else 0)."""
        return self.raw[31]

    # -- derived -----------------------------------------------------------
    @property
    def model_name(self) -> Optional[str]:
        """Model from bytes 3/4 (``"QL-810W"``), or None for other models."""
        return MODEL_NAMES.get((self.series_code, self.model_code))

    @property
    def two_color(self) -> bool:
        """True when a black/red roll is loaded (byte 25 bit 7).

        Always False without media (:attr:`has_media`): when a roll is
        removed the printer keeps its byte 25, so a DK-22251 taken out would
        otherwise still read as two-colour."""
        return self.has_media and bool(self.text_color & TWO_COLOR_BIT)

    @property
    def has_media(self) -> bool:
        """False when the printer reports no roll: media width 0, media type 0
        (byte 11 low nibble) or the err1 "no media" bit.

        Only the width and type bytes are trusted. On the tested unit a
        roll removed with the cover open reads width 0 and type 0 while err1
        stays clear and byte 25 keeps the removed roll's value, so the other
        media bytes (length, roll code, byte 25) may be stale."""
        if self.err1 & ERR1_NO_MEDIA:
            return False
        return self.media_width_mm != 0 and self.media_type != MEDIA_NONE

    @property
    def in_use(self) -> bool:
        """err1 bit 0x10: the printer reports itself busy ("printer in use")."""
        return bool(self.err1 & ERR1_IN_USE)

    @property
    def is_printing(self) -> bool:
        """Phase type is "printing"."""
        return self.phase_type == PHASE_PRINTING

    def errors(self) -> List[str]:
        """Human-readable fault conditions; empty when there is no fault.

        Lists the error bits of bytes 8 and 9 (except the "printer in use"
        busy flag), the extended error code of byte 7 and, for "error
        occurred" frames, the colour-mismatch code of byte 31. An "error
        occurred" frame without any detail yields one generic entry.
        """
        out = _bit_names(self.err1, ERROR1_BITS, "error 1", skip=ERR1_IN_USE)
        out += _bit_names(self.err2, ERROR2_BITS, "error 2")
        if self.ext_error:
            out.append(EXT_ERRORS.get(self.ext_error, f"extended error 0x{self.ext_error:02x}"))
        if self.status_type == ST_ERROR:
            detail = self.error_detail
            if detail and detail != self.ext_error:
                out.append(EXT_ERRORS.get(detail, f"error detail 0x{detail:02x}"))
            if not out:
                out.append("error reported without detail")
        return out

    @property
    def is_error(self) -> bool:
        """True when :meth:`errors` reports at least one fault."""
        return bool(self.errors())

    @property
    def is_idle(self) -> bool:
        """Ready for a job: a reply or "printing completed" frame, phase
        "receiving", and no error/busy bits in bytes 7-9."""
        return (self.status_type in (ST_REPLY, ST_PRINTING_COMPLETED)
                and self.phase_type == PHASE_RECEIVING
                and self.err1 == 0 and self.err2 == 0 and self.ext_error == 0)

    def to_dict(self) -> Dict[str, object]:
        """JSON-safe decoding of every field, including ``raw_hex``."""
        return {
            "raw_hex": self.raw.hex(),
            "series_code": self.series_code,
            "model_code": self.model_code,
            "model": self.model_name,
            "ext_error": self.ext_error,
            "err1": self.err1,
            "err2": self.err2,
            "media_width_mm": self.media_width_mm,
            "media_type": self.media_type,
            "media_type_raw": self.media_type_raw,
            "media_type_name": MEDIA_TYPES.get(self.media_type, "unknown"),
            "media_length_mm": self.media_length_mm,
            "roll_code": self.roll_code,
            "mode": self.mode,
            "status_type": self.status_type,
            "status_type_name": STATUS_TYPES.get(self.status_type, "unknown"),
            "phase_type": self.phase_type,
            "phase_name": PHASE_TYPES.get(self.phase_type, "unknown"),
            "phase_number": self.phase_number,
            "notification": self.notification,
            "notification_name": NOTIFICATIONS.get(self.notification) if self.notification else None,
            "bg_color": self.bg_color,
            "text_color": self.text_color,
            "error_detail": self.error_detail,
            "two_color": self.two_color,
            "has_media": self.has_media,
            "in_use": self.in_use,
            "is_printing": self.is_printing,
            "is_error": self.is_error,
            "is_idle": self.is_idle,
            "errors": self.errors(),
        }

    def with_overrides(self, overrides: Optional[Mapping[Union[int, str], int]] = None,
                       /, **bytes_by_offset: int) -> "Status":
        """Return a copy with some bytes replaced (this frame is unchanged).

        Used to present a synthesised "printing" phase while a job is in
        flight. Bytes can be named three ways, freely mixed::

            st.with_overrides(status_type=0x06, phase_type=0x01)  # field names
            st.with_overrides({18: 0x06, 19: 0x01})               # offsets
            st.with_overrides(**{"18": 0x06, "19": 0x01})         # offsets as text

        Field names are the keys of :data:`FIELD_OFFSETS`, plus
        ``phase_number`` (16-bit, bytes 20-21). Offsets are 3..31: the header
        (bytes 0-2) cannot be changed. Raises ValueError on a bad name, offset
        or value.
        """
        items: List[Tuple[Union[int, str], int]] = []
        if overrides is not None:
            items.extend(overrides.items())
        items.extend(bytes_by_offset.items())
        frame = bytearray(self.raw)
        for key, value in items:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"override {key!r}: value must be an int, got {value!r}")
            if key == "phase_number":
                if not 0 <= value <= 0xFFFF:
                    raise ValueError(f"override phase_number: {value} is not a 16-bit value")
                frame[PHASE_NUMBER_OFFSET:PHASE_NUMBER_OFFSET + 2] = value.to_bytes(2, "big")
                continue
            offset = _override_offset(key)
            if not 0 <= value <= 0xFF:
                raise ValueError(f"override {key!r}: {value} is not a byte value")
            frame[offset] = value
        return Status(bytes(frame))


def _override_offset(key: Union[int, str]) -> int:
    """Resolve a :meth:`Status.with_overrides` key to a byte offset (3..31)."""
    if isinstance(key, str):
        if key in FIELD_OFFSETS:
            return FIELD_OFFSETS[key]
        text = key[1:] if key[:1] == "b" else key
        if not text.isdigit():
            raise ValueError(f"unknown status field {key!r}")
        offset = int(text)
    elif isinstance(key, int) and not isinstance(key, bool):
        offset = key
    else:
        raise ValueError(f"unknown status field {key!r}")
    if not 3 <= offset < STATUS_LEN:
        raise ValueError(f"status byte offset {offset} cannot be overridden (allowed 3..{STATUS_LEN - 1})")
    return offset


def parse_status(data: Union[bytes, bytearray, memoryview]) -> Status:
    """Decode exactly one 32-byte status block.

    Raises ValueError unless ``data`` is exactly 32 bytes starting ``80 20 42``.
    """
    return Status(bytes(data))


def split_status_frames(buf: Union[bytes, bytearray, memoryview]) -> Tuple[List[Status], bytes]:
    """Find status frames in a byte stream (e.g. accumulated USB IN reads).

    Returns ``(frames, remainder)``: every complete frame found (leading or
    interleaved garbage is skipped) and the unconsumed tail that may be the
    start of a frame still being received. Feed ``remainder + next_read``
    back in to continue.
    """
    data = bytes(buf)
    frames: List[Status] = []
    pos = 0
    n = len(data)
    while True:
        i = data.find(STATUS_HEADER, pos)
        if i < 0:
            break
        if i + STATUS_LEN > n:
            return frames, data[i:]
        frames.append(Status(data[i:i + STATUS_LEN]))
        pos = i + STATUS_LEN
    # No further header: keep a trailing partial header ("80" or "80 20").
    for j in range(max(pos, n - len(STATUS_HEADER) + 1), n):
        if STATUS_HEADER.startswith(data[j:]):
            return frames, data[j:]
    return frames, b""


def iter_status_frames(buf: Union[bytes, bytearray, memoryview]) -> List[Status]:
    """All complete status frames in ``buf``, tolerant of leading garbage."""
    return split_status_frames(buf)[0]


# ---------------------------------------------------------------------------
# Job tokenizer (it never modifies bytes; proxyd's colour check and hold-back act on what it sees)
# ---------------------------------------------------------------------------

class Event(NamedTuple):
    """One parsed command. ``offset`` is the stream offset of its first byte;
    ``params`` are the bytes after the opcode."""

    kind: str
    offset: int
    params: bytes = b""


# ESC i <x>: (event kind, parameter bytes after the 3-byte opcode).
_ESC_I_COMMANDS: Dict[int, Tuple[str, int]] = {
    0x53: ("status_request", 0),   # ESC i S
    0x61: ("command_mode", 1),     # ESC i a: 00 ESC/P, 01 raster, 03 P-touch Template, FF after a job
    0x21: ("auto_status", 1),      # ESC i !: 00 notify, 01 silent (until power-off)
    0x7A: ("print_info", 10),      # ESC i z
    0x4D: ("various_mode", 1),     # ESC i M: 0x40 auto cut
    0x41: ("cut_every", 1),        # ESC i A
    0x4B: ("expanded_mode", 1),    # ESC i K: 0x01 two-colour, 0x08 cut at end, 0x40 600 dpi
    0x64: ("margin", 2),           # ESC i d: feed in dots, little-endian
    0x4A: ("esc_i_j", 1),          # ESC i J: QL-700 driver only, meaning unknown
}
# ESC i U <x>: (event kind, parameter bytes after the 4-byte opcode). Undocumented.
_ESC_I_U_COMMANDS: Dict[int, Tuple[str, int]] = {
    0x4A: ("job_id", 14),          # sent by Brother's driver on every page
    0x74: ("esc_i_u_t", 2),        # P-touch for Mac's job trailer 1B 69 55 74 1C 00; length provisional
    0x77: ("media_block", 128),    # 01 + a 127-byte media record
}
_RASTER_COMMAND_MODE = 0x01
#: ``ESC i a FF``: undocumented. P-touch for Mac sends it after every job's last
#: ``0x1A`` and Brother's QL Windows driver ends its jobs with it; it reads as
#: "back to the printer's own command mode". It ends no job, but what follows
#: is no longer known to be raster, so parsing stops there.
_COMMAND_MODE_RESTORE = 0xFF
_NON_NUL = re.compile(rb"[^\x00]")


def _is_post_job_command(kind: str, params: bytes) -> bool:
    """Commands that may follow a job's ``0x1A`` without starting a new job: the
    status request and P-touch's trailer (``ESC i U t`` + 2, ``ESC i a FF``)."""
    if kind == "command_mode":
        return params[0] == _COMMAND_MODE_RESTORE
    return kind in ("status_request", "esc_i_u_t")


def _is_setup_command(kind: str, params: bytes) -> bool:
    """Printer set-up commands that start no job: ``ESC i a 01`` (raster
    command mode) and ``ESC i !`` (status notifications). Like ``ESC @``, they
    leave the printer at a command boundary with no job open, and clients send
    them on their own as well as at the start of a job (iPrint&Label for iOS
    opens a 402-byte connection before it prints, presumably 400 NULs and
    ``ESC @``)."""
    if kind == "command_mode":
        return params[0] == _RASTER_COMMAND_MODE
    return kind == "auto_status"


class RasterTokenizer:
    """Incremental, length-driven parser of a Brother QL raster job stream.

    Observation only: it never changes what is forwarded. It parses strictly
    by command length (``0x1A``, ``0x0C``, ``0x5A`` and ``1B 69 53`` occur
    inside raster payloads all the time) and handles data split across
    :meth:`feed` calls at any byte boundary.

    Attributes (all reset by :meth:`reset`):

    * ``bytes_seen`` - bytes fed since the last reset.
    * ``at_command_boundary`` - the next byte starts a new command (False
      mid-command and once opaque).
    * ``in_job`` - job content was parsed and the job has not ended yet. Job
      content is every command except NUL padding, the set-up commands
      (``ESC @``, ``ESC i a 01``, ``ESC i !``) and the post-job commands:
      ``ESC i S`` and the trailer P-touch for Mac sends on the same
      connection after a job (``ESC i U t`` + 2 bytes, then ``ESC i a FF``).
      So a stream of NULs and set-up commands alone is no job (iPrint&Label's
      402-byte check presumably is one).
    * ``job_ended`` - a ``0x1A`` was parsed at a command position and no job
      content followed it (trailing NULs, set-up and post-job commands keep it
      True).
    * ``setup_commands`` - set-up commands parsed (``ESC @``, ``ESC i a 01``,
      ``ESC i !``).
    * ``color_decided`` / ``requested_two_color`` - the colour mode the job
      asks for, decided at its first ``g`` or ``w`` raster row or, without
      one, at its first print command: True (two-colour), False (black only)
      or None (unknown). It is known only when an ``ESC i K`` came before that
      point and, at a raster row, agrees with it (bit 0 set for ``w`` rows,
      clear for ``g`` rows); ``ESC @`` clears the ``ESC i K`` it relies on.
      Both reset when the next job's content starts.
    * ``jobs`` / ``pages`` - ``0x1A`` count / print commands (``0x0C`` + ``0x1A``).
    * ``two_color`` - bit 0 of the last ``ESC i K`` (``expanded_mode`` holds
      the raw value). Reset, with ``requested_media``, ``compression`` and
      ``various_mode``, by ``ESC @`` (which resets the printer's modes).
    * ``requested_media`` - last ``ESC i z`` as a dict (``flags``, ``type``,
      ``width``, ``length``, ``lines``, ``page``), or None.
    * ``command_mode`` / ``auto_status`` - last ``ESC i a`` / ``ESC i !``
      value (both persist in the printer until power-off).
    * ``status_requests`` - ``ESC i S`` commands at command positions.
    * ``row_counts`` - raster rows by kind: ``g``, ``w1`` (black plane),
      ``w2`` (red plane), ``Z`` (blank line). Rows produce no events.
    * ``nul_bytes`` - NUL padding bytes seen at command positions.
    * ``opaque`` - None while parsing; otherwise the reason parsing stopped
      (unknown opcode, or a switch out of raster command mode, including the
      ``ESC i a FF`` that ends P-touch's trailer). After that the stream is
      only counted. A job that ended before the stream went opaque stays
      ended.
    * ``partial_command`` (property) - the bytes of the command the stream
      stops in, while it stops inside a command header (b"" otherwise).
    * ``opaque_end`` - when parsing stopped at a complete command (an
      ``ESC i a`` switch out of raster mode), the stream offset just past it,
      else None. While ``bytes_seen`` equals it, nothing has followed that
      command, so the stream still stands at a command boundary.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget everything (start of a new connection)."""
        self.bytes_seen = 0
        self.at_command_boundary = True
        self.in_job = False
        self.job_ended = False
        self.jobs = 0
        self.pages = 0
        self.two_color = False
        self.expanded_mode: Optional[int] = None
        self.requested_media: Optional[Dict[str, int]] = None
        self.compression: Optional[int] = None
        self.various_mode: Optional[int] = None
        self.command_mode: Optional[int] = None
        self.auto_status: Optional[int] = None
        self.status_requests = 0
        self.setup_commands = 0
        self.color_decided = False
        self.requested_two_color: Optional[bool] = None
        self.nul_bytes = 0
        self.row_counts: Dict[str, int] = {"g": 0, "w1": 0, "w2": 0, "Z": 0}
        self.opaque: Optional[str] = None
        self.opaque_end: Optional[int] = None
        self._carry = b""   # start of a command whose fixed part is incomplete
        self._skip = 0      # raster payload bytes still to pass over

    # -- derived state -----------------------------------------------------
    @property
    def raster_lines(self) -> int:
        """Raster lines as ``ESC i z`` counts them (a w01+w02 pair is one line)."""
        rc = self.row_counts
        return rc["g"] + rc["Z"] + max(rc["w1"], rc["w2"])

    @property
    def partial_command(self) -> bytes:
        """The bytes of the command the stream stops in, when it stops inside a
        command header: b"" at a command boundary, inside a raster row's
        payload, and once opaque. ql810wc-proxyd keeps these bytes back while
        no job has started, so a read that ends inside a command never leaves
        the printer waiting for the rest of it."""
        if self.opaque is not None or self._skip:
            return b""
        return bytes(self._carry)

    @property
    def needs_resync(self) -> bool:
        """True if the stream ending here would leave the printer waiting
        mid-job or mid-command (send :data:`RESYNC`). Never True once opaque:
        the stream can no longer be judged, so the client's EOF is trusted."""
        return self.opaque is None and (self.in_job or not self.at_command_boundary)

    def summary(self) -> Dict[str, object]:
        """JSON-safe snapshot of everything observed so far (for logs/state)."""
        return {
            "bytes": self.bytes_seen,
            "jobs": self.jobs,
            "pages": self.pages,
            "raster_lines": self.raster_lines,
            "row_counts": dict(self.row_counts),
            "two_color": self.two_color,
            "expanded_mode": self.expanded_mode,
            "requested_media": dict(self.requested_media) if self.requested_media else None,
            "compression": self.compression,
            "various_mode": self.various_mode,
            "command_mode": self.command_mode,
            "auto_status": self.auto_status,
            "status_requests": self.status_requests,
            "setup_commands": self.setup_commands,
            "color_decided": self.color_decided,
            "requested_two_color": self.requested_two_color,
            "nul_bytes": self.nul_bytes,
            "in_job": self.in_job,
            "job_ended": self.job_ended,
            "at_command_boundary": self.at_command_boundary,
            "needs_resync": self.needs_resync,
            "opaque": self.opaque,
        }

    # -- parsing -----------------------------------------------------------
    def feed(self, data: Union[bytes, bytearray, memoryview]) -> List[Event]:
        """Parse the next chunk of the stream; returns the commands completed
        in it (raster rows and NUL padding are counted, not reported)."""
        data = bytes(data)
        n = len(data)
        start = self.bytes_seen
        self.bytes_seen += n
        events: List[Event] = []
        if self.opaque is not None or not n:
            return events
        pos = 0
        if self._skip:
            pos = min(self._skip, n)
            self._skip -= pos
            if self._skip:
                return events
            self.at_command_boundary = True
        if self._carry:
            buf = self._carry + data[pos:]
            base = start + pos - len(self._carry)
            self._carry = b""
            self._scan(buf, 0, base, events)
        else:
            self._scan(data, pos, start, events)
        return events

    def _scan(self, buf: bytes, i: int, base: int, events: List[Event]) -> None:
        """Parse ``buf`` from index ``i``; ``base`` is the stream offset of buf[0]."""
        n = len(buf)
        rows = self.row_counts
        while i < n:
            op = buf[i]
            if op == 0x00:                                   # invalidate padding
                match = _NON_NUL.search(buf, i)
                end = match.start() if match else n
                self.nul_bytes += end - i
                i = end
                continue
            if op == 0x67 or op == 0x77:                     # g / w raster row
                if i + 3 > n:
                    break
                sel = buf[i + 1]
                if op == 0x67 and sel == 0x00:
                    rows["g"] += 1
                elif op == 0x77 and sel in (0x01, 0x02):
                    rows["w1" if sel == 0x01 else "w2"] += 1
                else:
                    what = "g raster row with selector" if op == 0x67 else "w raster row for plane"
                    self._go_opaque(events, base + i, buf[i:i + 3], f"{what} 0x{sel:02x}")
                    return
                self._job_content()
                if not self.color_decided:
                    self._decide_color(op == 0x77)
                end = i + 3 + buf[i + 2]
                if end > n:
                    self._skip = end - n
                    self.at_command_boundary = False
                    return
                i = end
                continue
            if op == 0x5A:                                   # Z: one blank line
                rows["Z"] += 1
                self._job_content()
                i += 1
                continue
            if op == 0x0C:                                   # FF: print, more pages follow
                self._job_content()
                if not self.color_decided:
                    self._decide_color(None)
                self.pages += 1
                events.append(Event("print", base + i))
                i += 1
                continue
            if op == 0x1A:                                   # ^Z: print last page = job end
                if not self.color_decided:
                    self._decide_color(None)
                self.pages += 1
                self.jobs += 1
                self.in_job = False
                self.job_ended = True
                events.append(Event("print_last", base + i))
                i += 1
                continue
            if op == 0x4D:                                   # M n: compression
                if i + 2 > n:
                    break
                self._job_content()
                self.compression = buf[i + 1]
                events.append(Event("compression", base + i, buf[i + 1:i + 2]))
                i += 2
                continue
            if op != 0x1B:
                self._go_opaque(events, base + i, buf[i:i + 1], f"unknown opcode 0x{op:02x}")
                return
            if i + 2 > n:
                break
            op2 = buf[i + 1]
            if op2 == 0x40:                                  # ESC @: set-up, starts no job
                self.setup_commands += 1
                self.two_color = False
                self.expanded_mode = None
                self.requested_media = None
                self.compression = None
                self.various_mode = None
                events.append(Event("init", base + i))
                i += 2
                continue
            if op2 != 0x69:
                self._go_opaque(events, base + i, buf[i:i + 2], f"unknown escape sequence 1b {op2:02x}")
                return
            if i + 3 > n:
                break
            op3 = buf[i + 2]
            if op3 == 0x55:                                  # ESC i U <sub>
                if i + 4 > n:
                    break
                spec = _ESC_I_U_COMMANDS.get(buf[i + 3])
                if spec is None:
                    self._go_opaque(events, base + i, buf[i:i + 4],
                                    f"unknown ESC i U sub-command 0x{buf[i + 3]:02x}")
                    return
                head = 4
            else:
                spec = _ESC_I_COMMANDS.get(op3)
                if spec is None:
                    self._go_opaque(events, base + i, buf[i:i + 3], f"unknown ESC i command 0x{op3:02x}")
                    return
                head = 3
            kind, nparams = spec
            end = i + head + nparams
            if end > n:
                break
            offset = base + i
            params = buf[i + head:end]
            i = end
            events.append(Event(kind, offset, params))
            if not self._apply(kind, params, offset, events):
                return
        if i < n:
            self._carry = buf[i:]
            self.at_command_boundary = False
        else:
            self.at_command_boundary = True

    def _apply(self, kind: str, params: bytes, offset: int, events: List[Event]) -> bool:
        """Record the effect of an ``ESC i`` command. False if parsing must stop."""
        if kind == "status_request":
            self.status_requests += 1
        elif kind == "command_mode":
            self.command_mode = params[0]
        if _is_setup_command(kind, params):
            self.setup_commands += 1
        elif not _is_post_job_command(kind, params):
            self._job_content()
        if kind == "expanded_mode":
            self.expanded_mode = params[0]
            self.two_color = bool(params[0] & 0x01)
        elif kind == "print_info":
            self.requested_media = {
                "flags": params[0],
                "type": params[1],
                "width": params[2],
                "length": params[3],
                "lines": int.from_bytes(params[4:8], "little"),
                "page": params[8],
            }
        elif kind == "various_mode":
            self.various_mode = params[0]
        elif kind == "auto_status":
            self.auto_status = params[0]
        elif kind == "command_mode" and params[0] != _RASTER_COMMAND_MODE:
            if params[0] == _COMMAND_MODE_RESTORE:
                reason = "switched back to the printer's own command mode (ESC i a FF)"
            else:
                reason = f"switched to command mode 0x{params[0]:02x} (not raster)"
            head = b"\x1b\x69\x61" + params
            self._go_opaque(events, offset, head, reason, end=offset + len(head))
            return False
        return True

    def _job_content(self) -> None:
        if not self.in_job:
            self.in_job = True
            self.job_ended = False
            self.color_decided = False
            self.requested_two_color = None

    def _decide_color(self, two_color_row: Optional[bool]) -> None:
        """Record the colour mode the job asks for, at its first raster row
        (``two_color_row``: True for ``w``, False for ``g``) or, without one,
        at its first print command (None): the last ``ESC i K`` bit 0, unless
        the row disagrees with it; unknown without an ``ESC i K``."""
        self.color_decided = True
        mode = self.expanded_mode
        if mode is None:
            self.requested_two_color = None
            return
        two = bool(mode & 0x01)
        self.requested_two_color = None if two_color_row is not None and two_color_row != two else two

    def _go_opaque(self, events: List[Event], offset: int, head: bytes, reason: str,
                   end: Optional[int] = None) -> None:
        self.opaque = f"{reason} at offset {offset}"
        self.opaque_end = end
        self.at_command_boundary = False
        self._carry = b""
        self._skip = 0
        events.append(Event("opaque", offset, bytes(head)))


# ---------------------------------------------------------------------------
# Test job generator (stdlib only; no Pillow, no GPL code)
# ---------------------------------------------------------------------------

DPI = 300
HEAD_PINS = 720
ROW_BYTES = HEAD_PINS // 8              # 90 bytes per raster row, uncompressed
MIN_LINES = 150                          # continuous tape: 12.7 mm ...
MAX_LINES = 11811                        # ... to 1 m (Brother manual 2.3.4)
DEFAULT_TEST_LINES = 600                 # 50.8 mm
FEED_MARGIN_DOTS = 35                    # ESC i d for continuous tape (3 mm)
TWO_COLOR_WIDTH_MM = 62                  # DK-22251 is the only black/red roll
#: Continuous tape width (mm) -> (print-area pins, pins before the print area).
#: Brother manual 2.3.5: a row is 90 bytes = 720 pins and the "right margin"
#: pins come first (the MSB of byte 0 is the first pin).
CONTINUOUS_PINS: Dict[int, Tuple[int, int]] = {
    12: (106, 29),
    29: (306, 6),
    38: (413, 12),
    50: (554, 12),
    54: (590, 0),
    62: (696, 12),
}
TEST_PATTERNS = ("bars", "black")
_BORDER, _GAP, _BAND = 12, 24, 48       # dots (1 dot = 1/300 in)


def _pins(start: int, stop: int) -> int:
    """Pins ``[start, stop)`` as a 720-bit big-endian row mask (pin 0 = MSB)."""
    if stop <= start:
        return 0
    return ((1 << (stop - start)) - 1) << (HEAD_PINS - stop)


def _test_rows(first_pin: int, printable: int, lines: int, use_red: bool) -> Iterable[Tuple[int, int]]:
    """(black mask, red mask) per raster line of the test pattern.

    A black frame around the print area; inside it, bands along the label in
    a repeating cycle of three: black, red, then a split band (one half
    black, the other red). Without red, the red bands are black and the split
    band's red half stays blank.
    """
    left, right = first_pin, first_pin + printable
    frame = _pins(left, right)
    sides = _pins(left, left + _BORDER) | _pins(right - _BORDER, right)
    inner_left, inner_right = left + _BORDER + _GAP, right - _BORDER - _GAP
    middle = (inner_left + inner_right) // 2
    full = _pins(inner_left, inner_right)
    half_a = _pins(inner_left, middle - _GAP // 2)
    half_b = _pins(middle + _GAP // 2, inner_right)
    if use_red:
        bands = ((full, 0), (0, full), (half_a, half_b))
    else:
        bands = ((full, 0), (full, 0), (half_a, 0))
    period = _BAND + _GAP
    for y in range(lines):
        if y < _BORDER or y >= lines - _BORDER:
            yield frame, 0
            continue
        r = y - _BORDER
        if r % period < _GAP or y >= lines - _BORDER - _GAP:
            yield sides, 0
            continue
        black, red = bands[(r // period) % len(bands)]
        yield sides | black, red


def make_test_job(width_mm: int = 62, two_color: bool = True, pattern: str = "bars",
                  rows: Optional[int] = None) -> bytes:
    """Build a complete QL-810W raster job printing a test label.

    Continuous tape of ``width_mm`` (12, 29, 38, 50, 54 or 62 mm), 300 dpi,
    uncompressed 90-byte rows, ``rows`` raster lines long (default 600 =
    50.8 mm; allowed 150..11811). The job is::

        400 x 00, ESC @, ESC i a 01,
        ESC i z 86 0A <width> 00 <lines LE32> 00 00,
        ESC i M 40 (auto cut), ESC i A 01, ESC i K 09|08, ESC i d 23 00,
        rows, 1A

    It never sends ``ESC i !``, so the printer keeps its default automatic
    status notifications.

    Two-colour (``two_color=True``) needs 62 mm DK-22251 black/red tape and
    sends every line as ``w 01`` (black plane) + ``w 02`` (red plane);
    otherwise lines are ``g 00`` rows. Patterns:

    * ``"bars"`` - black frame, black bars, red bars and split black|red bars
      (in mono, the red bars print black);
    * ``"black"`` - the same layout in black only; with ``two_color=True``
      the red plane is sent empty.

    Match the job to the loaded roll (read the status first): a **mono job on
    a DK-22251 roll puts the printer in an error that only a power cycle
    clears**, and a two-colour job on a single-colour roll is refused too.
    So use ``two_color=status.two_color``; on DK-22251 a black-only check is
    ``two_color=True, pattern="black"``. Die-cut rolls are not supported here
    (the job's ``ESC i z`` asks for continuous tape and the printer would
    report "replace media").
    """
    geometry = CONTINUOUS_PINS.get(width_mm) if isinstance(width_mm, int) else None
    if geometry is None:
        raise ValueError(f"unsupported tape width {width_mm!r} mm (supported: "
                         f"{', '.join(str(w) for w in sorted(CONTINUOUS_PINS))})")
    if two_color and width_mm != TWO_COLOR_WIDTH_MM:
        raise ValueError("two-color printing needs 62 mm black/red tape (DK-22251)")
    if pattern not in TEST_PATTERNS:
        raise ValueError(f"unknown test pattern {pattern!r} (use one of {', '.join(TEST_PATTERNS)})")
    lines = DEFAULT_TEST_LINES if rows is None else rows
    if isinstance(lines, bool) or not isinstance(lines, int) or not MIN_LINES <= lines <= MAX_LINES:
        raise ValueError(f"rows must be an int in {MIN_LINES}..{MAX_LINES}, got {rows!r}")
    printable, first_pin = geometry

    job = bytearray(INVALIDATE)
    job += ESC_INIT
    job += b"\x1b\x69\x61\x01"                                   # raster command mode
    job += b"\x1b\x69\x7a" + bytes((0x86, MEDIA_CONTINUOUS, width_mm, 0))
    job += lines.to_bytes(4, "little") + b"\x00\x00"             # line count, first page
    job += b"\x1b\x69\x4d\x40"                                   # auto cut
    job += b"\x1b\x69\x41\x01"                                   # cut every label
    job += b"\x1b\x69\x4b" + (b"\x09" if two_color else b"\x08")  # (two-colour +) cut at end
    job += b"\x1b\x69\x64" + FEED_MARGIN_DOTS.to_bytes(2, "little")
    encoded: Dict[int, bytes] = {}

    def row(mask: int) -> bytes:
        if mask not in encoded:
            encoded[mask] = mask.to_bytes(ROW_BYTES, "big")
        return encoded[mask]

    use_red = two_color and pattern == "bars"
    for black, red in _test_rows(first_pin, printable, lines, use_red):
        if two_color:
            job += b"\x77\x01\x5a" + row(black) + b"\x77\x02\x5a" + row(red)
        else:
            job += b"\x67\x00\x5a" + row(black)
    job += b"\x1a"
    return bytes(job)
