"""Brother DK media for the QL-800 series, and P-touch's media-match rule.

The table is P-touch Editor 5.3 for Mac's own QL paper list (from static
analysis of its SDK), which is what decides whether the client recognises the
installed roll from the 32-byte status block:

    status[10] == width_mm  and  status[17] == length_mm  and  status[11] == type
    and, for the black/red paper only, status[25] == 0x81

Papers are tried in ascending SDK id order and the **last** match wins, so a
62 mm continuous roll with byte 25 = 0x81 (DK-22251) resolves to the two-colour
paper (id 38) rather than plain 62 mm (id 16). No match = "Unsupported media".

Lengths are the *rounded* physical lengths the printer reports (60x86 reads 87,
62x60 reads 61). The client compares exactly; drivers such as ql-unidrv allow
+/-1 mm, available here as ``describe(status, exact=False)``.

Also recorded per paper: Brother DK part numbers known to report that size,
the brother_ql label identifier (used by ``MEDIA_DEFAULT`` in the config), the
inch designation from Brother's raster reference, and the roll code (status
byte 14) where a printer has been seen reporting one.

:func:`compare_media` holds the printer's own rule for a job's media (its
``ESC i z``), used by ql810wc-proxyd's media check (``RAW_MEDIA_CHECK``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from .rasterproto import (
    MEDIA_CONTINUOUS,
    MEDIA_DIE_CUT,
    PI_KIND,
    PI_LENGTH,
    PI_WIDTH,
    QL810W_MODEL_CODE,
    QL810W_SERIES_CODE,
    STATUS_HEADER,
    STATUS_LEN,
    TEXT_COLOR_BLACK_RED,
    Status,
)

#: Length tolerance used by ``describe(..., exact=False)`` and :func:`compare_media`.
LENGTH_TOLERANCE_MM = 1
#: Byte 25 of a single-colour roll (bit 0 is set in every QL-800-series status block on record).
TEXT_COLOR_SINGLE = 0x01


@dataclass(frozen=True)
class MediaInfo:
    """One paper of the P-touch QL paper table."""

    sdk_id: int                    # MacPrintSDK paper id (iteration order of the match rule)
    name: str                      # SDK paper name, e.g. "62mm", "29mm x 90mm", "12mm Dia"
    width_mm: int                  # status byte 10
    length_mm: int                 # status byte 17 (0 = continuous)
    media_type: int                # status byte 11: 0x0A continuous, 0x0B die-cut
    needs_two_color: bool = False  # matched only when byte 25 == 0x81 (black/red roll)
    dk_parts: Tuple[str, ...] = ()  # Brother DK rolls known to report this paper
    label_id: Optional[str] = None  # brother_ql label identifier
    inch: str = ""                 # Brother's inch designation (raster reference 2.3.2)
    roll_code: Optional[int] = None  # status byte 14 reported for dk_parts[0]

    @property
    def is_continuous(self) -> bool:
        """Continuous-length tape (cut to the job's length)."""
        return self.media_type == MEDIA_CONTINUOUS

    @property
    def description(self) -> str:
        """Human-readable summary, e.g. ``62mm continuous black/red (DK-22251)``."""
        text = f"{self.name} {'continuous' if self.is_continuous else 'die-cut'}"
        if self.needs_two_color:
            text += " black/red"
        if self.dk_parts:
            text += f" ({', '.join(self.dk_parts)})"
        return text

    @property
    def mib_name(self) -> str:
        """Printer-MIB ``prtInputMediaName`` style name, e.g. ``62mm / 2.4"``.

        Observed on real QL-810W/820NWB units for 62 mm (both rolls), 29 mm
        and 29x90 only (:attr:`mib_name_verified`); the others follow the same
        pattern but are unverified.
        """
        return f"{self.name} / {self.inch}"

    @property
    def mib_name_verified(self) -> bool:
        """True if :attr:`mib_name` was seen on a real printer."""
        return self.sdk_id in _OBSERVED_MIB_NAMES


def _paper(sdk_id: int, name: str, width: int, length: int, inch: str,
           label_id: Optional[str], dk_parts: Tuple[str, ...] = (),
           roll_code: Optional[int] = None, two_color: bool = False) -> MediaInfo:
    media_type = MEDIA_CONTINUOUS if length == 0 else MEDIA_DIE_CUT
    return MediaInfo(sdk_id, name, width, length, media_type, two_color, dk_parts,
                     label_id, inch, roll_code)


#: The QL paper table, in the client's iteration order (ascending SDK id).
#: Ids 11-16 follow the SDK's listing order; only ids 16 < 38 affect matching.
MEDIA: Tuple[MediaInfo, ...] = (
    _paper(1, "17mm x 54mm", 17, 54, '0.66" x 2.1"', "17x54", ("DK-11204",)),
    _paper(2, "17mm x 87mm", 17, 87, '0.66" x 3.4"', "17x87", ("DK-11203",)),
    _paper(3, "23mm x 23mm", 23, 23, '0.9" x 0.9"', "23x23", ("DK-11221",)),
    _paper(4, "29mm x 42mm", 29, 42, '1.1" x 1.6"', "29x42"),
    _paper(5, "29mm x 90mm", 29, 90, '1.1" x 3.5"', "29x90", ("DK-11201",), roll_code=0x01),
    _paper(6, "38mm x 90mm", 38, 90, '1.4" x 3.5"', "39x90", ("DK-11208",)),
    _paper(7, "39mm x 48mm", 39, 48, '1.5" x 1.8"', "39x48"),
    _paper(8, "52mm x 29mm", 52, 29, '2" x 1.1"', "52x29"),
    _paper(9, "62mm x 29mm", 62, 29, '2.4" x 1.1"', "62x29", ("DK-11209",)),
    _paper(10, "62mm x 100mm", 62, 100, '2.4" x 3.9"', "62x100", ("DK-11202",)),
    _paper(11, "12mm", 12, 0, '0.47"', "12", ("DK-22214",), roll_code=0x1A),
    _paper(12, "29mm", 29, 0, '1.1"', "29", ("DK-22210",)),
    _paper(13, "38mm", 38, 0, '1.4"', "38", ("DK-22225",)),
    _paper(14, "50mm", 50, 0, '1.9"', "50", ("DK-22223",)),
    _paper(15, "54mm", 54, 0, '2.1"', "54", ("DK-N55224",)),
    _paper(16, "62mm", 62, 0, '2.4"', "62", ("DK-22205", "DK-44205", "DK-44605"), roll_code=0x15),
    _paper(17, "60mm x 86mm", 60, 87, '2.3" x 3.4"', "60x86", ("DK-11234",)),
    _paper(38, "62mm", 62, 0, '2.4"', "62red", ("DK-22251",), roll_code=0x23, two_color=True),
    _paper(39, "54mm x 29mm", 54, 29, '2.1" x 1.1"', "54x29"),
    _paper(55, "12mm Dia", 12, 12, '0.47" Dia', "d12", ("DK-11219",)),
    _paper(56, "24mm Dia", 24, 24, '0.94" Dia', "d24", ("DK-11218",)),
    _paper(57, "58mm Dia", 58, 58, '2.2" Dia', "d58"),
    _paper(58, "62mm x 60mm", 62, 61, '2.4" x 2.3"', None),
    _paper(59, "62mm x 75mm", 62, 75, '2.4" x 2.9"', None),
)

#: Papers whose Printer-MIB media name was observed on real hardware.
_OBSERVED_MIB_NAMES = frozenset({5, 12, 16, 38})

#: brother_ql label identifier -> paper.
MEDIA_BY_LABEL: Dict[str, MediaInfo] = {m.label_id: m for m in MEDIA if m.label_id}
#: All label identifiers accepted by ``MEDIA_DEFAULT`` / ``MEDIA_SECOND``.
LABEL_IDS: Tuple[str, ...] = tuple(MEDIA_BY_LABEL)

#: Status byte 14 -> DK rolls that reported it (read from QL-800/810W/820NWBc printers).
#: Several SKUs share a code (same spool switch), so this narrows, not names.
ROLL_CODES: Dict[int, Tuple[str, ...]] = {
    0x01: ("DK-11201",),
    0x15: ("DK-22205", "DK-44205", "DK-44605"),
    0x1A: ("DK-22214",),
    0x23: ("DK-22251",),
}


def lookup_label(label_id: str) -> Optional[MediaInfo]:
    """Paper for a brother_ql label id (``"62red"``, ``"29x90"``...), or None."""
    return MEDIA_BY_LABEL.get(label_id)


def _matches(media: MediaInfo, status: Status, exact: bool, tolerance: int) -> bool:
    if status.media_width_mm != media.width_mm:
        return False
    if abs(status.media_length_mm - media.length_mm) > tolerance:
        return False
    if (status.media_type_raw if exact else status.media_type) != media.media_type:
        return False
    if media.needs_two_color:
        return status.text_color == TEXT_COLOR_BLACK_RED if exact else status.two_color
    return True


def describe(status: Status, *, exact: bool = True) -> Optional[MediaInfo]:
    """The paper P-touch would recognise from ``status``, or None.

    ``exact=True`` (default) applies the client's rule literally: raw byte 11,
    exact length, byte 25 == 0x81 for black/red. ``exact=False`` is lenient:
    byte 11's low nibble (accepts the manual's 0x4A/0x4B), byte 25 bit 7, and
    when nothing matches exactly, a length within +/-1 mm. None means "no
    media" (see :attr:`Status.has_media`) or an unsupported roll.
    """
    if not status.has_media:
        return None
    found = [m for m in MEDIA if _matches(m, status, exact, 0)]
    if not found and not exact:
        found = [m for m in MEDIA if _matches(m, status, exact, LENGTH_TOLERANCE_MM)]
    return found[-1] if found else None


def dk_parts_for(status: Status) -> Tuple[str, ...]:
    """Best guess of the loaded DK roll(s): the paper's DK parts, narrowed by
    the roll code (byte 14) when that code is known and consistent."""
    media = describe(status, exact=False)
    if media is None:
        return ()
    by_code = ROLL_CODES.get(status.roll_code, ())
    narrowed = tuple(part for part in media.dk_parts if part in by_code)
    return narrowed or media.dk_parts


#: The media fields of an ``ESC i z`` that :func:`compare_media` judges, in
#: order, with the n1 flag that asks the printer to check each one.
_REQUEST_FIELDS: Tuple[Tuple[str, int], ...] = (("type", PI_KIND), ("width", PI_WIDTH), ("length", PI_LENGTH))


def describe_request(requested: Mapping[str, int]) -> str:
    """The media an ``ESC i z`` asks for (rasterproto's ``requested_media``),
    e.g. ``62 mm continuous tape`` or ``29x90 mm die-cut labels``.

    Decided by the media type first, so that a refusal's text agrees with
    the type :func:`compare_media` judged (``proxyd.describe_requested_media``
    decides the same way): die-cut labels for type ``0x0B`` (``29 mm
    die-cut labels`` without a length), continuous tape for type ``0x0A``
    whatever the length, and for any other type die-cut labels when a
    length is given. Missing keys count as 0."""
    width, length = requested.get("width", 0), requested.get("length", 0)
    kind = requested.get("type", 0)
    if kind == MEDIA_DIE_CUT or (kind != MEDIA_CONTINUOUS and length):
        return f"{width}x{length} mm die-cut labels" if length else f"{width} mm die-cut labels"
    return f"{width} mm continuous tape"


def compare_media(requested: Mapping[str, int], status: Status) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """The media fields of a job's ``ESC i z`` that differ from the loaded roll.

    ``requested`` is rasterproto's ``requested_media`` (``flags``, ``type``,
    ``width``, ``length``; missing keys count as 0). Returns ``(mismatched,
    unchecked)``: the fields among ``"type"``, ``"width"`` and ``"length"``
    (in that order) whose value differs from the roll in ``status``, split by
    whether the job's n1 flags ask the printer to check them (``PI_KIND``,
    ``PI_WIDTH``, ``PI_LENGTH``). Brother's raster reference: the printer
    checks a field only when its flag is set, and reports "replace media"
    (error information 2 bit 0) when it differs, so only ``mismatched``
    fields make the printer reject the job.

    The rule:

    * nothing is judged (both empty) without a roll (:attr:`Status.has_media`)
      or for a roll :func:`describe` does not know (``exact=False``);
    * type: judged only when the job's type is exactly ``0x0A`` (continuous)
      or ``0x0B`` (die-cut); compared with the roll's type (status byte 11,
      low nibble);
    * width: judged only when the job gives one (not 0); compared with status
      byte 10;
    * length: judged only on a die-cut roll; it differs when the job's length
      is more than ``LENGTH_TOLERANCE_MM`` from status byte 17 (the printer's
      rounded label length). Continuous tape takes any length, so on a
      continuous roll no length is judged (the job's raster line count is
      never a media property).
    """
    if not status.has_media or describe(status, exact=False) is None:
        return (), ()
    kind = requested.get("type", 0)
    width = requested.get("width", 0)
    length = requested.get("length", 0)
    differs = {
        "type": kind in (MEDIA_CONTINUOUS, MEDIA_DIE_CUT) and kind != status.media_type,
        "width": width != 0 and width != status.media_width_mm,
        "length": (status.media_type == MEDIA_DIE_CUT
                   and abs(length - status.media_length_mm) > LENGTH_TOLERANCE_MM),
    }
    flags = requested.get("flags", 0)
    mismatched: List[str] = []
    unchecked: List[str] = []
    for name, flag in _REQUEST_FIELDS:
        if differs[name]:
            (mismatched if flags & flag else unchecked).append(name)
    return tuple(mismatched), tuple(unchecked)


def mib_media_name(media: Optional[MediaInfo]) -> str:
    """``prtInputMediaName`` text for a paper; empty when there is no media
    (as a real QL-810W reports). See :attr:`MediaInfo.mib_name`."""
    return media.mib_name if media is not None else ""


def synthesize_status(media: MediaInfo, *, series_code: int = QL810W_SERIES_CODE,
                      model_code: int = QL810W_MODEL_CODE) -> Status:
    """An idle status block describing ``media``, for when no live status is
    available (manual media, degraded mode).

    Built on the real QL-810W idle frame (byte 6 = 0x04, status type "reply",
    phase "receiving"), with the paper's width/type/length, its recorded roll
    code (0 if unknown) and byte 25 = 0x81 for black/red, else 0x01. For
    DK-22251, DK-22205 and DK-11201 this reproduces the printer's own frame
    byte for byte.
    """
    frame = bytearray(STATUS_LEN)
    frame[0:3] = STATUS_HEADER
    frame[3] = series_code
    frame[4] = model_code
    frame[5] = 0x30
    frame[6] = 0x04
    frame[10] = media.width_mm
    frame[11] = media.media_type
    frame[14] = media.roll_code or 0
    frame[17] = media.length_mm
    frame[25] = TEXT_COLOR_BLACK_RED if media.needs_two_color else TEXT_COLOR_SINGLE
    return Status(bytes(frame))
