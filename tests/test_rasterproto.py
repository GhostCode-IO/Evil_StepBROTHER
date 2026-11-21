"""Tests for agent/ql810wc/rasterproto.py (status block, tokenizer, test job).

Status vectors are real frames: the tested QL-810Wc over USB
(dev/notes/usb-ground-truth.md) and frames that others have published from a
QL-810W (SNMP), a QL-800 and a QL-820NWBc (USB). Frames marked "constructed"
were not read from a printer.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import random
import sys
import unittest

AGENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from ql810wc import rasterproto as rp  # noqa: E402

# The tested QL-810Wc, USB, 2026-11-20 (dev/notes/usb-ground-truth.md).
REF_DK22251_IDLE = "80 20 42 34 39 30 04 00 00 00 3e 0a 00 00 23 00 00 00 00 00 00 00 00 00 00 81 00 00 00 00 00 00"
REF_DK22251_COVER_OPEN = "80 20 42 34 39 30 04 00 00 10 3e 0a 00 00 23 00 00 00 02 00 00 00 00 00 00 81 00 00 00 00 00 00"
REF_DK11201_IDLE = "80 20 42 34 39 30 04 00 00 00 1d 0b 00 00 01 00 00 5a 00 00 00 00 00 00 00 01 00 00 00 00 00 00"
# The second media swap on the tested unit (same note, polled every 3 s). The note
# records status type, width, media type, byte 25 and err1/err2 per step; the other
# bytes are taken from the full frames above. After the roll is removed, the
# length and roll-code bytes were not recorded: one variant has them cleared, the
# other keeps the die-cut roll's values (byte 25 demonstrably kept its old value).
REF_DK11201_COVER_OPEN = "80 20 42 34 39 30 04 00 00 10 1d 0b 00 00 01 00 00 5a 02 00 00 00 00 00 00 01 00 00 00 00 00 00"
REF_ROLL_REMOVED = "80 20 42 34 39 30 04 00 00 10 00 00 00 00 00 00 00 00 02 00 00 00 00 00 00 01 00 00 00 00 00 00"
REF_ROLL_REMOVED_STALE = "80 20 42 34 39 30 04 00 00 10 00 00 00 00 01 00 00 5a 02 00 00 00 00 00 00 01 00 00 00 00 00 00"
# P-touch Editor for Mac's trailer after a job's 0x1A (dev/notes/ptouch-mac-session.md).
PTOUCH_TRAILER = b"\x1b\x69\x55\x74\x1c\x00" + b"\x1b\x69\x61\xff"
# QL-810W fw 1.22 over SNMPv1 (public BrotherRaster fixtures).
QL810W_SNMP_62 = "802042343930040000003e0a0000150000000000000000000001000000000000"
QL810W_SNMP_DK22251 = "802042343930040000003e0a0000230000000000000000000081000000000000"
# QL-820NWBc over USB (status frames published by thermal-label).
QL820_DK22251 = "80 20 42 34 41 30 04 00 00 00 3E 0A 00 00 23 00 00 00 00 00 00 00 00 00 00 81 00 00 00 00 00 00"
QL820_DK22205 = "80 20 42 34 41 30 04 00 00 00 3E 0A 00 00 15 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00"
QL820_DK22214 = "80 20 42 34 41 30 04 00 00 00 0C 0A 00 00 1A 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00"
QL820_DK11201 = "80 20 42 34 41 30 04 00 00 00 1D 0B 00 00 01 00 00 5A 00 00 00 00 00 00 00 01 00 00 00 00 00 00"
# QL-800 over USB (public ql-unidrv fixtures, recorded).
QL800_IDLE = "80 20 42 34 38 30 00 00 00 00 3E 0A 00 00 15 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00"
QL800_PRINTING = "80 20 42 34 38 30 00 00 00 00 3E 0A 00 00 15 00 00 00 06 01 00 00 00 00 00 01 00 00 00 00 00 00"
QL800_WRONG_MEDIA = "80 20 42 34 38 30 00 00 00 01 3E 0A 00 00 15 00 00 00 02 01 00 00 00 00 00 01 00 00 00 00 00 00"
QL800_RED_JOB_ON_MONO_ROLL = "80 20 42 34 38 30 00 00 00 00 3E 0A 00 00 15 00 00 00 02 01 00 00 00 00 00 01 00 00 00 00 00 23"
QL800_AFTER_COVER = "80 20 42 34 38 30 00 00 20 00 3E 0A 00 00 15 00 00 00 02 00 00 00 00 00 00 01 00 00 00 00 00 00"
QL800_DK22251_IDLE = "80 20 42 34 38 30 00 00 00 00 3E 0A 00 00 23 00 00 00 00 00 00 00 00 00 00 81 00 00 00 00 00 00"
QL800_NO_MEDIA_CONSTRUCTED = "80 20 42 34 38 30 00 00 01 00 00 00 00 00 15 00 00 00 02 00 00 00 00 00 00 01 00 00 00 00 00 00"
# Constructed: QL810W_SNMP_62 with a model code (byte 4) that no known QL uses.
UNKNOWN_MODEL_CONSTRUCTED = "80 20 42 34 44 30 04 00 00 00 3E 0A 00 00 15 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00"


def st(text: str) -> rp.Status:
    return rp.Status.from_hex(text)


class StatusDecodeTest(unittest.TestCase):
    # frame, model, width, type, length, roll, two_color, status_type, idle, errors
    CASES = [
        (REF_DK22251_IDLE, "QL-810W", 62, 0x0A, 0, 0x23, True, 0x00, True, []),
        (REF_DK22251_COVER_OPEN, "QL-810W", 62, 0x0A, 0, 0x23, True, 0x02, False, ["cover open"]),
        (REF_DK11201_IDLE, "QL-810W", 29, 0x0B, 90, 0x01, False, 0x00, True, []),
        (QL810W_SNMP_62, "QL-810W", 62, 0x0A, 0, 0x15, False, 0x00, True, []),
        (QL810W_SNMP_DK22251, "QL-810W", 62, 0x0A, 0, 0x23, True, 0x00, True, []),
        (QL820_DK22251, "QL-820NWB", 62, 0x0A, 0, 0x23, True, 0x00, True, []),
        (QL820_DK22205, "QL-820NWB", 62, 0x0A, 0, 0x15, False, 0x00, True, []),
        (QL820_DK22214, "QL-820NWB", 12, 0x0A, 0, 0x1A, False, 0x00, True, []),
        (QL820_DK11201, "QL-820NWB", 29, 0x0B, 90, 0x01, False, 0x00, True, []),
        (QL800_IDLE, "QL-800", 62, 0x0A, 0, 0x15, False, 0x00, True, []),
        (QL800_PRINTING, "QL-800", 62, 0x0A, 0, 0x15, False, 0x06, False, []),
        (QL800_WRONG_MEDIA, "QL-800", 62, 0x0A, 0, 0x15, False, 0x02, False, ["replace media"]),
        (QL800_RED_JOB_ON_MONO_ROLL, "QL-800", 62, 0x0A, 0, 0x15, False, 0x02, False,
         ["media does not support two-color printing"]),
        (QL800_AFTER_COVER, "QL-800", 62, 0x0A, 0, 0x15, False, 0x02, False, ["printer turned off"]),
        (QL800_DK22251_IDLE, "QL-800", 62, 0x0A, 0, 0x23, True, 0x00, True, []),
        (UNKNOWN_MODEL_CONSTRUCTED, None, 62, 0x0A, 0, 0x15, False, 0x00, True, []),
    ]

    def test_real_frames(self):
        for (frame, model, width, mtype, length, roll, two, stype, idle, errors) in self.CASES:
            with self.subTest(frame=frame):
                s = st(frame)
                self.assertEqual(len(s.raw), rp.STATUS_LEN)
                self.assertEqual(s.model_name, model)
                self.assertEqual(s.media_width_mm, width)
                self.assertEqual(s.media_type, mtype)
                self.assertEqual(s.media_type_raw, mtype)
                self.assertEqual(s.media_length_mm, length)
                self.assertEqual(s.roll_code, roll)
                self.assertEqual(s.two_color, two)
                self.assertEqual(s.status_type, stype)
                self.assertEqual(s.is_idle, idle)
                self.assertEqual(s.errors(), errors)
                self.assertEqual(s.is_error, bool(errors))
                self.assertTrue(s.has_media)

    def test_reference_unit_fields(self):
        s = st(REF_DK22251_IDLE)
        self.assertEqual((s.series_code, s.model_code), (0x34, 0x39))
        self.assertEqual(s.text_color, 0x81)
        self.assertEqual(s.bg_color, 0)
        self.assertEqual((s.ext_error, s.err1, s.err2, s.mode, s.notification), (0, 0, 0, 0, 0))
        self.assertEqual((s.phase_type, s.phase_number, s.error_detail), (0, 0, 0))
        self.assertFalse(s.is_printing)
        self.assertFalse(s.in_use)

    def test_cover_open_reply_is_an_error_frame(self):
        s = st(REF_DK22251_COVER_OPEN)
        self.assertEqual(s.err2, 0x10)
        self.assertEqual(s.status_type, rp.ST_ERROR)
        self.assertTrue(s.is_error)
        self.assertFalse(s.is_idle)
        self.assertTrue(s.two_color, "media bytes keep describing the loaded roll")

    def test_printing_phase(self):
        s = st(QL800_PRINTING)
        self.assertTrue(s.is_printing)
        self.assertEqual(s.status_type, rp.ST_PHASE_CHANGE)
        self.assertFalse(s.is_idle)
        self.assertFalse(s.is_error)

    def test_no_media(self):
        s = st(QL800_NO_MEDIA_CONSTRUCTED)
        self.assertFalse(s.has_media)
        self.assertEqual(s.errors(), ["no media"])
        blank = st(REF_DK22251_IDLE).with_overrides(media_width_mm=0, media_type_raw=0, text_color=0)
        self.assertFalse(blank.has_media)
        self.assertTrue(blank.is_idle, "no media while idle is not an error (real QL-810W shows READY)")

    def test_reference_media_swap_sequence(self):
        # die-cut in -> cover opened -> roll removed -> DK-22251 inserted -> cover closed
        # (status type, width, media type, byte 25, err1, err2 as recorded on the unit).
        steps = [
            (REF_DK11201_IDLE, 0x00, 29, 0x0B, 0x01, 0x00, 0x00),
            (REF_DK11201_COVER_OPEN, 0x02, 29, 0x0B, 0x01, 0x00, 0x10),
            (REF_ROLL_REMOVED, 0x02, 0, 0x00, 0x01, 0x00, 0x10),
            (REF_DK22251_COVER_OPEN, 0x02, 62, 0x0A, 0x81, 0x00, 0x10),
            (REF_DK22251_IDLE, 0x00, 62, 0x0A, 0x81, 0x00, 0x00),
        ]
        expected = [  # has_media, two_color, errors, idle
            (True, False, [], True),
            (True, False, ["cover open"], False),
            (False, False, ["cover open"], False),
            (True, True, ["cover open"], False),
            (True, True, [], True),
        ]
        for (frame, stype, width, mtype, color, err1, err2), (media, two, errors, idle) in zip(steps, expected):
            with self.subTest(frame=frame):
                s = st(frame)
                self.assertEqual((s.status_type, s.media_width_mm, s.media_type_raw, s.text_color, s.err1, s.err2),
                                 (stype, width, mtype, color, err1, err2), "the frame matches the recorded step")
                self.assertEqual((s.has_media, s.two_color, s.errors(), s.is_idle), (media, two, errors, idle))
                self.assertEqual(s.is_error, bool(errors))

    def test_removed_roll_without_the_no_media_bit(self):
        # The unit reads width 0 / type 0 with err1 0x01 clear; other media bytes may be stale.
        for frame in (REF_ROLL_REMOVED, REF_ROLL_REMOVED_STALE):
            with self.subTest(frame=frame):
                s = st(frame)
                self.assertEqual(s.err1 & rp.ERR1_NO_MEDIA, 0)
                self.assertFalse(s.has_media)
                self.assertEqual(s.errors(), ["cover open"], "no 'no media' error without the err1 bit")
                d = s.to_dict()
                self.assertEqual((d["has_media"], d["two_color"], d["media_type_name"]), (False, False, "none"))
        closed = st(REF_ROLL_REMOVED_STALE).with_overrides(status_type=rp.ST_REPLY, err2=0)
        self.assertFalse(closed.has_media)
        self.assertTrue(closed.is_idle, "an idle printer without media is ready, not in error")

    def test_byte_25_is_ignored_without_media(self):
        # The printer keeps byte 25 when a roll is removed: a DK-22251 taken out still reads 0x81.
        removed = st(REF_DK22251_COVER_OPEN).with_overrides(media_width_mm=0, media_type_raw=0)
        self.assertEqual(removed.text_color, rp.TEXT_COLOR_BLACK_RED, "the raw byte is kept")
        self.assertFalse(removed.has_media)
        self.assertFalse(removed.two_color)
        self.assertFalse(removed.to_dict()["two_color"])
        self.assertTrue(st(REF_DK22251_COVER_OPEN).two_color, "with the roll in, byte 25 counts")
        no_media_bit = st(REF_DK22251_IDLE).with_overrides(err1=rp.ERR1_NO_MEDIA)
        self.assertFalse(no_media_bit.two_color)

    def test_no_media_rule(self):
        base = st(REF_DK22251_IDLE)
        cases = {
            "width 0": base.with_overrides(media_width_mm=0),
            "media type 0": base.with_overrides(media_type_raw=0),
            "type 0x40 (low nibble 0)": base.with_overrides(media_type_raw=0x40),
            "err1 no media": base.with_overrides(err1=0x01),
            "all media bytes 0": base.with_overrides(media_width_mm=0, media_type_raw=0, roll_code=0,
                                                     text_color=0),
        }
        for name, s in cases.items():
            with self.subTest(case=name):
                self.assertFalse(s.has_media)
                self.assertFalse(s.two_color)
        for name, s in {"continuous": base, "die-cut": st(REF_DK11201_IDLE),
                        "manual type code": base.with_overrides(media_type_raw=0x4A),
                        "unsupported roll": base.with_overrides(media_type_raw=0xFF),
                        "end of media": st(REF_DK11201_IDLE).with_overrides(err1=0x02)}.items():
            with self.subTest(case=name):
                self.assertTrue(s.has_media)

    def test_manual_media_codes_are_masked(self):
        s = st(QL810W_SNMP_62).with_overrides(media_type_raw=0x4A)
        self.assertEqual(s.media_type_raw, 0x4A)
        self.assertEqual(s.media_type, rp.MEDIA_CONTINUOUS)

    def test_busy_flag_and_unknown_bits(self):
        busy = st(REF_DK22251_IDLE).with_overrides(err1=rp.ERR1_IN_USE)
        self.assertTrue(busy.in_use)
        self.assertEqual(busy.errors(), [])
        self.assertFalse(busy.is_idle)
        odd = st(REF_DK22251_IDLE).with_overrides(err1=0x08, ext_error=0x99)
        self.assertEqual(odd.errors(), ["unknown error 1 bit 0x08", "extended error 0x99"])
        ext = st(REF_DK22251_IDLE).with_overrides(ext_error=0x24)
        self.assertEqual(ext.errors(), ["media does not support monochrome printing"])
        self.assertFalse(ext.is_idle)

    def test_error_frame_without_detail(self):
        s = st(REF_DK22251_IDLE).with_overrides(status_type=rp.ST_ERROR)
        self.assertEqual(s.errors(), ["error reported without detail"])

    def test_error_detail_not_duplicated(self):
        s = st(QL800_RED_JOB_ON_MONO_ROLL).with_overrides(ext_error=0x23)
        self.assertEqual(s.errors(), ["media does not support two-color printing"])

    def test_printing_completed_counts_as_idle(self):
        s = st(REF_DK22251_IDLE).with_overrides(status_type=rp.ST_PRINTING_COMPLETED)
        self.assertTrue(s.is_idle)

    def test_parse_status_validation(self):
        good = bytes.fromhex(QL810W_SNMP_62)
        self.assertEqual(rp.parse_status(good).raw, good)
        self.assertEqual(rp.parse_status(bytearray(good)).raw, good)
        self.assertEqual(rp.parse_status(memoryview(good)).raw, good)
        self.assertIsInstance(rp.parse_status(bytearray(good)).raw, bytes)
        for bad in (good[:31], good + b"\x00", b"", b"\x81" + good[1:], good[:2] + b"C" + good[3:]):
            with self.subTest(bad=bad.hex()):
                with self.assertRaises(ValueError):
                    rp.parse_status(bad)
        with self.assertRaises(TypeError):
            rp.Status(good.hex())

    def test_from_hex(self):
        self.assertEqual(st(REF_DK22251_IDLE), st(REF_DK22251_IDLE.replace(" ", "").upper()))
        with self.assertRaises(ValueError):
            rp.Status.from_hex("80 20 4g")
        self.assertEqual(bytes(st(QL810W_SNMP_62)), bytes.fromhex(QL810W_SNMP_62))
        self.assertIn("80 20 42 34 39", repr(st(QL810W_SNMP_62)))

    def test_to_dict_is_json_safe(self):
        d = st(REF_DK22251_COVER_OPEN).to_dict()
        self.assertEqual(json.loads(json.dumps(d)), d)
        self.assertEqual(d["raw_hex"], REF_DK22251_COVER_OPEN.replace(" ", ""))
        self.assertEqual(d["model"], "QL-810W")
        self.assertEqual(d["model_code"], 0x39)
        self.assertEqual(d["media_width_mm"], 62)
        self.assertEqual(d["media_type"], 10)
        self.assertEqual(d["media_type_name"], "continuous")
        self.assertTrue(d["two_color"])
        self.assertEqual(d["status_type_name"], "error occurred")
        self.assertEqual(d["errors"], ["cover open"])
        self.assertIsNone(d["notification_name"])
        self.assertEqual(rp.Status.from_hex(d["raw_hex"]), st(REF_DK22251_COVER_OPEN))

    def test_frozen(self):
        s = st(QL810W_SNMP_62)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s.raw = b""  # type: ignore[misc]
        self.assertEqual(hash(s), hash(st(QL810W_SNMP_62)))


class WithOverridesTest(unittest.TestCase):
    def setUp(self):
        self.base = st(REF_DK22251_IDLE)

    def test_field_names(self):
        busy = self.base.with_overrides(status_type=rp.ST_PHASE_CHANGE, phase_type=rp.PHASE_PRINTING)
        self.assertTrue(busy.is_printing)
        self.assertEqual(busy.status_type, 0x06)
        self.assertEqual(busy.raw[:18], self.base.raw[:18])
        self.assertEqual(busy.raw[20:], self.base.raw[20:])
        self.assertEqual(self.base.status_type, 0, "original is unchanged")

    def test_offsets_in_every_spelling(self):
        expected = self.base.with_overrides(status_type=6, phase_type=1)
        self.assertEqual(self.base.with_overrides({18: 6, 19: 1}), expected)
        self.assertEqual(self.base.with_overrides({"18": 6, "19": 1}), expected)
        self.assertEqual(self.base.with_overrides(**{"18": 6, "b19": 1}), expected)
        self.assertEqual(self.base.with_overrides({18: 6}, phase_type=1), expected)

    def test_phase_number(self):
        s = self.base.with_overrides(phase_type=1, phase_number=0x0014)
        self.assertEqual(s.phase_number, 0x14)
        self.assertEqual(s.raw[20:22], b"\x00\x14")

    def test_rejects_bad_overrides(self):
        bad = [
            {"nonsense": 1}, {0: 0x81}, {2: 0x42}, {32: 0}, {"-1": 0}, {"b": 0},
            {"status_type": 256}, {"status_type": -1}, {"status_type": True},
            {"status_type": "6"}, {"phase_number": 0x10000}, {1.5: 0},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self.base.with_overrides(overrides)

    def test_returns_status(self):
        self.assertIsInstance(self.base.with_overrides(), rp.Status)
        self.assertEqual(self.base.with_overrides(), self.base)


class StatusFramesTest(unittest.TestCase):
    def setUp(self):
        self.a = bytes.fromhex(REF_DK22251_IDLE.replace(" ", ""))
        self.b = bytes.fromhex(REF_DK22251_COVER_OPEN.replace(" ", ""))

    def test_leading_and_interleaved_garbage(self):
        frames = rp.iter_status_frames(b"\x00\x80garbage\x80\x20" + self.a + b"\xff\xff" + self.b)
        self.assertEqual([f.raw for f in frames], [self.a, self.b])

    def test_split_keeps_partial_tail(self):
        frames, rest = rp.split_status_frames(self.a + self.b[:10])
        self.assertEqual([f.raw for f in frames], [self.a])
        self.assertEqual(rest, self.b[:10])
        frames, rest = rp.split_status_frames(rest + self.b[10:])
        self.assertEqual([f.raw for f in frames], [self.b])
        self.assertEqual(rest, b"")

    def test_partial_header_tail(self):
        for tail in (b"\x80", b"\x80\x20"):
            frames, rest = rp.split_status_frames(self.a + b"zz" + tail)
            self.assertEqual(len(frames), 1)
            self.assertEqual(rest, tail)
        self.assertEqual(rp.split_status_frames(b"\x01\x02\x03")[1], b"")

    def test_tail_never_reuses_consumed_bytes(self):
        frame = self.a[:31] + b"\x80"            # last byte looks like a header start
        frames, rest = rp.split_status_frames(frame + b"\x20")
        self.assertEqual(len(frames), 1)
        self.assertEqual(rest, b"", "the 0x80 belongs to the parsed frame")

    def test_streaming_in_small_reads(self):
        stream = b"\x00" * 7 + self.a + self.b + self.a
        rest, got = b"", []
        for i in range(0, len(stream), 5):
            frames, rest = rp.split_status_frames(rest + stream[i:i + 5])
            got.extend(frames)
        self.assertEqual([f.raw for f in got], [self.a, self.b, self.a])


# --- tokenizer -------------------------------------------------------------

def driver_like_job() -> bytes:
    """A two-page job shaped like Brother's Windows driver output: ESC i !,
    the undocumented ESC i U J, PackBits rows (M 02) with Z lines, FF between
    pages and ^Z at the end. Row payloads deliberately contain bytes that are
    also opcodes (1A, 0C, 5A, 1B 69 53, 00)."""
    tricky = [b"\x1a" * 12, b"\x0c\x5a\x1b\x69\x53\x00\x1a", b"\x1b\x69\x53" * 5, b"\x00" * 9 + b"\x1a"]
    out = bytearray(rp.INVALIDATE + rp.ESC_INIT)
    for page in range(2):
        out += b"\x1b\x69\x61\x01" + b"\x1b\x69\x21\x01"
        out += b"\x1b\x69\x55\x4a" + bytes([0x00, 0x0C] + [0] * 9 + [0x42, 0, 0])
        out += b"\x1b\x69\x7a\x86\x0a\x3e\x00" + (6).to_bytes(4, "little") + bytes([page, 0])
        out += b"\x1b\x69\x4d\x40" + b"\x1b\x69\x41\x01" + b"\x1b\x69\x4b\x09" + b"\x1b\x69\x64\x23\x00"
        out += b"\x4d\x02"
        for payload in tricky:
            out += b"\x77\x01" + bytes([len(payload)]) + payload
            out += b"\x77\x02" + bytes([len(payload)]) + payload
        out += b"\x5a\x5a"
        out += b"\x0c" if page == 0 else b"\x1a"
    return bytes(out)


def feed_all(data: bytes, sizes) -> tuple:
    """Feed ``data`` in chunks drawn from the ``sizes`` iterator."""
    tok = rp.RasterTokenizer()
    events = []
    i = 0
    while i < len(data):
        size = next(sizes)
        events += tok.feed(data[i:i + size])
        i += size
    return events, tok.summary()


def random_sizes(rng: random.Random):
    while True:
        yield rng.choice((1, 2, 3, 4, 5, 13, 64, 93, 131, 400, 4096))


class TokenizerTest(unittest.TestCase):
    def test_test_job_is_parsed(self):
        job = rp.make_test_job()
        tok = rp.RasterTokenizer()
        events = tok.feed(job)
        self.assertEqual([e.kind for e in events], [
            "init", "command_mode", "print_info", "various_mode", "cut_every",
            "expanded_mode", "margin", "print_last"])
        self.assertTrue(tok.job_ended)
        self.assertFalse(tok.in_job)
        self.assertFalse(tok.needs_resync)
        self.assertTrue(tok.at_command_boundary)
        self.assertTrue(tok.two_color)
        self.assertEqual(tok.expanded_mode, 0x09)
        self.assertEqual(tok.requested_media, {"flags": 0x86, "type": 0x0A, "width": 62,
                                               "length": 0, "lines": 600, "page": 0})
        self.assertEqual(tok.raster_lines, 600)
        self.assertEqual(tok.row_counts, {"g": 0, "w1": 600, "w2": 600, "Z": 0})
        self.assertEqual((tok.jobs, tok.pages, tok.nul_bytes, tok.bytes_seen), (1, 1, 400, len(job)))
        self.assertEqual(tok.status_requests, 0)
        self.assertIsNone(tok.auto_status)
        self.assertIsNone(tok.opaque)
        self.assertEqual(json.loads(json.dumps(tok.summary())), tok.summary())

    def test_event_offsets_and_params(self):
        job = driver_like_job()
        events = rp.RasterTokenizer().feed(job)
        expected_opcodes = {
            "init": b"\x1b\x40", "command_mode": b"\x1b\x69\x61", "auto_status": b"\x1b\x69\x21",
            "job_id": b"\x1b\x69\x55\x4a", "print_info": b"\x1b\x69\x7a", "various_mode": b"\x1b\x69\x4d",
            "cut_every": b"\x1b\x69\x41", "expanded_mode": b"\x1b\x69\x4b", "margin": b"\x1b\x69\x64",
            "compression": b"\x4d", "print": b"\x0c", "print_last": b"\x1a",
        }
        for ev in events:
            opcode = expected_opcodes[ev.kind]
            self.assertEqual(job[ev.offset:ev.offset + len(opcode)], opcode, ev)
            self.assertEqual(job[ev.offset + len(opcode):ev.offset + len(opcode) + len(ev.params)], ev.params)
        job_id = [e for e in events if e.kind == "job_id"]
        self.assertEqual([len(e.params) for e in job_id], [14, 14])

    def test_driver_like_job(self):
        tok = rp.RasterTokenizer()
        events = tok.feed(driver_like_job())
        kinds = [e.kind for e in events]
        self.assertEqual(kinds.count("print"), 1)
        self.assertEqual(kinds.count("print_last"), 1)
        self.assertEqual(kinds[-1], "print_last")
        self.assertEqual(tok.status_requests, 0, "ESC i S inside payloads is not a command")
        self.assertEqual((tok.jobs, tok.pages), (1, 2))
        self.assertEqual(tok.auto_status, 0x01)
        self.assertEqual(tok.compression, 0x02)
        self.assertEqual(tok.row_counts, {"g": 0, "w1": 8, "w2": 8, "Z": 4})
        self.assertEqual(tok.raster_lines, 12)
        self.assertEqual(tok.requested_media["page"], 1)
        self.assertTrue(tok.two_color)
        self.assertFalse(tok.needs_resync)

    def test_chunking_does_not_matter(self):
        rng = random.Random(810)
        for job in (rp.make_test_job(rows=150), driver_like_job(), rp.make_test_job(two_color=False, rows=150)):
            whole = feed_all(job, iter([len(job)]))
            self.assertEqual(feed_all(job, iter(lambda: 1, 0)), whole, "byte by byte")
            for _ in range(25):
                self.assertEqual(feed_all(job, random_sizes(rng)), whole)

    def test_every_prefix_is_judged_correctly(self):
        job = driver_like_job()
        # NUL padding, then the set-up commands ESC @, ESC i a 01 and ESC i ! 01:
        # right behind each of them the printer is at a command boundary, no job open.
        setup_ends = {len(rp.INVALIDATE) + 2, len(rp.INVALIDATE) + 6, len(rp.INVALIDATE) + 10}
        self.assertEqual(job[len(rp.INVALIDATE):max(setup_ends)], b"\x1b\x40\x1b\x69\x61\x01\x1b\x69\x21\x01")
        tok = rp.RasterTokenizer()
        for i in range(len(job) - 1):
            tok.feed(job[i:i + 1])
            if i < len(rp.INVALIDATE):
                self.assertFalse(tok.needs_resync, "NUL padding alone is harmless")
            elif i + 1 in setup_ends:
                self.assertFalse(tok.needs_resync, f"a {i + 1}-byte prefix ends with a set-up command")
                self.assertFalse(tok.in_job)
            else:
                self.assertTrue(tok.needs_resync, f"a {i + 1}-byte prefix leaves the printer mid-job")
        tok.feed(job[-1:])
        self.assertFalse(tok.needs_resync)

    def test_payload_opcode_bytes_are_skipped(self):
        payload = b"\x1a\x0c\x5a\x1b\x69\x53\x1b\x40" * 11 + b"\x1a\x1a"
        job = (rp.INVALIDATE + rp.ESC_INIT + b"\x67\x00" + bytes([len(payload)]) + payload
               + b"\x77\x01\x02\x1a\x1a" + b"\x77\x02\x01\x0c" + b"\x1a")
        tok = rp.RasterTokenizer()
        events = tok.feed(job)
        self.assertEqual([e.kind for e in events], ["init", "print_last"])
        self.assertEqual(tok.status_requests, 0)
        self.assertEqual(tok.jobs, 1)

    def test_torn_inside_a_row(self):
        job = rp.make_test_job(rows=150)
        cut = len(job) - 50        # inside the last raster row
        tok = rp.RasterTokenizer()
        tok.feed(job[:cut])
        self.assertFalse(tok.at_command_boundary)
        self.assertFalse(tok.job_ended)
        self.assertTrue(tok.needs_resync)

    def test_torn_inside_a_command_header(self):
        tok = rp.RasterTokenizer()
        tok.feed(rp.INVALIDATE + rp.ESC_INIT + b"\x1b\x69\x7a\x86\x0a")
        self.assertFalse(tok.at_command_boundary)
        self.assertTrue(tok.needs_resync)
        self.assertIsNone(tok.requested_media)

    def test_job_without_terminator(self):
        job = rp.make_test_job(rows=150)
        tok = rp.RasterTokenizer()
        tok.feed(job[:-1])
        self.assertTrue(tok.at_command_boundary)
        self.assertTrue(tok.in_job)
        self.assertFalse(tok.job_ended)
        self.assertTrue(tok.needs_resync)

    def test_status_request_after_the_job_keeps_it_complete(self):
        tok = rp.RasterTokenizer()
        events = tok.feed(rp.make_test_job(rows=150) + rp.STATUS_REQUEST + b"\x00\x00")
        self.assertEqual(events[-1].kind, "status_request")
        self.assertEqual(tok.status_requests, 1)
        self.assertTrue(tok.job_ended)
        self.assertFalse(tok.needs_resync)

    def test_streams_without_a_job(self):
        for stream in (b"", rp.INVALIDATE, rp.INVALIDATE + rp.STATUS_REQUEST):
            with self.subTest(stream=len(stream)):
                tok = rp.RasterTokenizer()
                tok.feed(stream)
                self.assertFalse(tok.in_job)
                self.assertFalse(tok.job_ended)
                self.assertFalse(tok.needs_resync)
        tok = rp.RasterTokenizer()
        tok.feed(rp.STATUS_REQUEST[:2])
        self.assertTrue(tok.needs_resync, "a partial command still needs a resync")

    def test_new_job_after_a_finished_one(self):
        tok = rp.RasterTokenizer()
        tok.feed(rp.make_test_job(rows=150))
        tok.feed(rp.INVALIDATE + rp.ESC_INIT + b"\x1b\x69\x61\x01")
        self.assertEqual(tok.jobs, 1)
        self.assertFalse(tok.in_job, "set-up commands start no job")
        self.assertTrue(tok.job_ended)
        self.assertFalse(tok.needs_resync)
        self.assertFalse(tok.two_color, "ESC @ resets the job modes")
        self.assertIsNone(tok.requested_media)
        self.assertEqual(tok.command_mode, 0x01)
        tok.feed(b"\x1b\x69\x7a\x86\x0a\x3e\x00" + (150).to_bytes(4, "little") + b"\x00\x00")
        self.assertTrue(tok.in_job, "ESC i z starts the next job")
        self.assertFalse(tok.job_ended)
        self.assertTrue(tok.needs_resync)
        self.assertEqual(tok.requested_media["width"], 62)

    def test_setup_commands_alone_are_no_job(self):
        """iPrint&Label for iOS opens a 402-byte connection before each print and
        for Check Media (presumably 400 NULs and ESC @, not captured): the
        printer is left at a command boundary, with no job open."""
        cases = {
            "iPrint&Label check": rp.INVALIDATE + rp.ESC_INIT,
            "raster mode": rp.INVALIDATE + rp.ESC_INIT + b"\x1b\x69\x61\x01",
            "notifications": rp.ESC_INIT + b"\x1b\x69\x21\x00" + b"\x1b\x69\x21\x01",
            "status request": rp.INVALIDATE + rp.ESC_INIT + b"\x1b\x69\x61\x01" + rp.STATUS_REQUEST,
            "the proxy's resync": rp.RESYNC + rp.STATUS_REQUEST,
        }
        for name, stream in cases.items():
            with self.subTest(name):
                tok = rp.RasterTokenizer()
                events = tok.feed(stream)
                self.assertFalse(tok.in_job)
                self.assertFalse(tok.job_ended)
                self.assertFalse(tok.needs_resync)
                self.assertTrue(tok.at_command_boundary)
                self.assertEqual(tok.jobs, 0)
                self.assertFalse(tok.color_decided)
                self.assertEqual(tok.setup_commands,
                                 sum(1 for e in events if e.kind in ("init", "command_mode", "auto_status")))
                self.assertGreater(tok.setup_commands, 0)
        tok = rp.RasterTokenizer()
        tok.feed(rp.INVALIDATE + rp.ESC_INIT[:1])
        self.assertTrue(tok.needs_resync, "half an ESC @ leaves the printer mid-command")
        tok = rp.RasterTokenizer()
        tok.feed(rp.ESC_INIT + b"\x1b\x69\x61\x03")
        self.assertIsNotNone(tok.opaque, "another command mode is still no set-up command")
        self.assertTrue(tok.in_job)

    def test_requested_color(self):
        """The colour mode a job asks for: its ESC i K bit 0, decided at the first
        g/w raster row (which must agree) or, without one, at the first print command."""
        mono = rp.make_test_job(two_color=False, rows=150)
        two = rp.make_test_job(rows=150)
        head = mono[:mono.index(b"\x1b\x69\x4b")]            # up to ESC i K
        row_g = b"\x67\x00\x02\xff\x00"
        row_w = b"\x77\x01\x02\xff\x00" + b"\x77\x02\x02\x00\x00"
        k = {True: b"\x1b\x69\x4b\x09", False: b"\x1b\x69\x4b\x08"}
        cases = {
            "two-colour test job": (two, True),
            "black-only test job": (mono, False),
            "Windows driver shape": (driver_like_job(), True),
            "ESC i K two-colour, g rows": (head + k[True] + row_g + b"\x1a", None),
            "ESC i K black, w rows": (head + k[False] + row_w + b"\x1a", None),
            "no ESC i K, g rows": (head + row_g + b"\x1a", None),
            "no ESC i K, w rows": (head + row_w + b"\x1a", None),
            "blank black-only page": (head + k[False] + b"\x5a" * 20 + b"\x1a", False),
            "blank two-colour page": (head + k[True] + b"\x5a" * 20 + b"\x0c", True),
            "ESC @ clears ESC i K": (head + k[False] + rp.ESC_INIT + row_g + b"\x1a", None),
            "the first row decides": (head + k[False] + row_g + k[True] + row_w + b"\x1a", False),
        }
        for name, (stream, expected) in cases.items():
            with self.subTest(name):
                tok = rp.RasterTokenizer()
                tok.feed(stream)
                self.assertTrue(tok.color_decided)
                self.assertIs(tok.requested_two_color, expected)
                self.assertEqual(tok.summary()["requested_two_color"], expected)
        tok = rp.RasterTokenizer()
        tok.feed(head + k[False] + b"\x4d\x02" + b"\x5a" * 5)
        self.assertFalse(tok.color_decided, "no raster row and no print command yet")
        self.assertIsNone(tok.requested_two_color)
        tok.feed(row_g[:2])
        self.assertFalse(tok.color_decided, "a row decides once its header is complete")
        tok.feed(row_g[2:])
        self.assertIs(tok.requested_two_color, False)

    def test_partial_command(self):
        """The bytes of the command a stream stops in (proxyd keeps them back while
        no job has started)."""
        job = rp.make_test_job(rows=150)
        z = job.index(b"\x1b\x69\x7a")
        cases = {
            b"": b"",
            rp.INVALIDATE: b"",
            rp.INVALIDATE + b"\x1b": b"\x1b",
            rp.ESC_INIT + b"\x1b\x69\x61": b"\x1b\x69\x61",
            job[:z + 5]: job[z:z + 5],
            job[:-1]: b"",                                  # at a command boundary, in the job
            job[:-50]: b"",                                 # inside a raster row's payload
            rp.ESC_INIT + b"\x1b\x69\x58" + b"\x1b": b"",     # opaque
        }
        for stream, expected in cases.items():
            with self.subTest(stream=stream[-6:].hex(" ")):
                tok = rp.RasterTokenizer()
                tok.feed(stream)
                self.assertEqual(tok.partial_command, expected)
        tok = rp.RasterTokenizer()
        steps = ((z + 1, job[z:z + 1]), (1, job[z:z + 2]), (11, b""),   # ESC i z, byte by byte, then whole
                 (2, b"\x1b\x69"), (2, b""))                          # half of ESC i M, then all of it
        for size, expected in steps:
            tok.feed(job[tok.bytes_seen:tok.bytes_seen + size])
            self.assertEqual(tok.partial_command, expected, f"after {tok.bytes_seen} bytes")

    def test_requested_color_is_per_job(self):
        tok = rp.RasterTokenizer()
        tok.feed(rp.make_test_job(rows=150))
        self.assertIs(tok.requested_two_color, True)
        tok.feed(rp.INVALIDATE + rp.ESC_INIT)
        self.assertIs(tok.requested_two_color, True, "kept until the next job's content starts")
        tok.feed(rp.make_test_job(two_color=False, rows=150)[len(rp.INVALIDATE) + 2:])
        self.assertIs(tok.requested_two_color, False)
        tok = rp.RasterTokenizer()
        tok.feed(rp.ESC_INIT + b"\x1b\x69\x4b\x08" + b"\x1b\x69\x58\x00" + b"\x67\x00\x01\x00\x1a")
        self.assertIsNotNone(tok.opaque)
        self.assertFalse(tok.color_decided, "nothing is decided once the stream is opaque")

    def test_media_block(self):
        record = b"\x01" + bytes(range(127))
        tok = rp.RasterTokenizer()
        events = tok.feed(rp.ESC_INIT + b"\x1b\x69\x55\x77" + record + b"\x1b\x69\x4a\x00")
        self.assertEqual([(e.kind, len(e.params)) for e in events],
                         [("init", 0), ("media_block", 128), ("esc_i_j", 1)])
        self.assertEqual(events[1].params, record)
        self.assertIsNone(tok.opaque)

    def test_ptouch_trailer_after_the_job(self):
        job = rp.make_test_job(rows=150)
        tok = rp.RasterTokenizer()
        events = tok.feed(job + PTOUCH_TRAILER)
        self.assertEqual([(e.kind, e.offset, e.params) for e in events[-4:]], [
            ("print_last", len(job) - 1, b""),
            ("esc_i_u_t", len(job), b"\x1c\x00"),
            ("command_mode", len(job) + 6, b"\xff"),
            ("opaque", len(job) + 6, b"\x1b\x69\x61\xff"),
        ])
        self.assertIn("ESC i a FF", tok.opaque, "ESC i a FF leaves raster mode: parsing stops there")
        self.assertTrue(tok.job_ended, "the trailer belongs to the finished job")
        self.assertFalse(tok.in_job)
        self.assertFalse(tok.needs_resync, "never resync on a trailer")
        self.assertEqual((tok.jobs, tok.pages, tok.command_mode), (1, 1, 0xFF))
        self.assertTrue(tok.two_color, "the job's observed modes are kept")
        self.assertEqual(tok.requested_media["width"], 62)
        self.assertEqual(tok.bytes_seen, len(job) + len(PTOUCH_TRAILER))

    def test_esc_i_u_t_alone_is_parsed_by_length(self):
        job = rp.make_test_job(rows=150)
        tok = rp.RasterTokenizer()
        events = tok.feed(job + PTOUCH_TRAILER[:6] + rp.STATUS_REQUEST)
        self.assertEqual([(e.kind, e.params) for e in events[-2:]],
                         [("esc_i_u_t", b"\x1c\x00"), ("status_request", b"")])
        self.assertIsNone(tok.opaque)
        self.assertTrue(tok.at_command_boundary)
        self.assertTrue(tok.job_ended)
        self.assertFalse(tok.needs_resync)
        self.assertEqual(tok.status_requests, 1)

    def test_ptouch_trailer_split_at_every_position(self):
        job = rp.make_test_job(rows=150)
        whole = rp.RasterTokenizer()
        expected_events = whole.feed(job + PTOUCH_TRAILER)
        expected = whole.summary()
        n = len(PTOUCH_TRAILER)
        for i in range(n + 1):
            for j in range(i, n + 1):
                with self.subTest(split=(i, j)):
                    tok = rp.RasterTokenizer()
                    events = tok.feed(job)
                    for part in (PTOUCH_TRAILER[:i], PTOUCH_TRAILER[i:j], PTOUCH_TRAILER[j:]):
                        events += tok.feed(part)
                    self.assertEqual(events, expected_events)
                    self.assertEqual(tok.summary(), expected)

    def test_ptouch_trailer_in_random_chunks_and_bytewise(self):
        stream = driver_like_job() + PTOUCH_TRAILER
        whole = feed_all(stream, iter([len(stream)]))
        self.assertEqual([e.kind for e in whole[0]][-3:], ["esc_i_u_t", "command_mode", "opaque"])
        self.assertEqual(feed_all(stream, iter(lambda: 1, 0)), whole, "byte by byte")
        rng = random.Random(9100)
        for _ in range(25):
            self.assertEqual(feed_all(stream, random_sizes(rng)), whole)

    def test_ptouch_trailer_prefixes(self):
        # Mid-command the printer would wait for the rest; after ESC i U t + 2 it would
        # not. ESC i a FF then ends parsing, and an opaque stream's end is trusted.
        tok = rp.RasterTokenizer()
        tok.feed(rp.make_test_job(rows=150))
        for i, byte in enumerate(PTOUCH_TRAILER, 1):
            tok.feed(bytes([byte]))
            with self.subTest(prefix=i):
                self.assertTrue(tok.job_ended)
                self.assertFalse(tok.in_job)
                self.assertEqual(tok.opaque is not None, i == len(PTOUCH_TRAILER))
                self.assertEqual(tok.at_command_boundary, i == 6)
                self.assertEqual(tok.needs_resync, i not in (6, len(PTOUCH_TRAILER)))

    def test_trailer_alone_is_not_a_job(self):
        for stream in (PTOUCH_TRAILER, PTOUCH_TRAILER[6:], PTOUCH_TRAILER[:6], rp.INVALIDATE + PTOUCH_TRAILER):
            with self.subTest(stream=stream[-10:].hex(" ")):
                tok = rp.RasterTokenizer()
                tok.feed(stream)
                self.assertFalse(tok.in_job)
                self.assertFalse(tok.job_ended)
                self.assertFalse(tok.needs_resync)
                self.assertEqual(tok.jobs, 0)
                self.assertEqual(tok.opaque is not None, stream.endswith(b"\x1b\x69\x61\xff"))

    def test_bytes_after_the_trailer_are_only_counted(self):
        first = rp.make_test_job(rows=150)
        tok = rp.RasterTokenizer()
        tok.feed(first + PTOUCH_TRAILER)
        self.assertEqual(tok.feed(first), [], "nothing is parsed once opaque")
        self.assertEqual(tok.jobs, 1)
        self.assertTrue(tok.job_ended, "the finished job stays finished")
        self.assertFalse(tok.needs_resync)
        self.assertEqual(tok.bytes_seen, 2 * len(first) + len(PTOUCH_TRAILER))

    def test_opaque_end_marks_the_end_of_a_mode_switch(self):
        """Where a complete command stopped parsing: proxyd may poll right behind
        P-touch's trailer until anything else follows it."""
        job = rp.make_test_job(rows=150)
        for pieces in ([job + PTOUCH_TRAILER], [job, PTOUCH_TRAILER[:8], PTOUCH_TRAILER[8:]]):
            with self.subTest(pieces=len(pieces)):
                tok = rp.RasterTokenizer()
                for piece in pieces:
                    tok.feed(piece)
                self.assertEqual(tok.opaque_end, len(job) + len(PTOUCH_TRAILER))
                self.assertEqual(tok.bytes_seen, tok.opaque_end)
                tok.feed(b"\x00")
                self.assertEqual(tok.opaque_end, len(job) + len(PTOUCH_TRAILER), "it stays where parsing stopped")
                self.assertGreater(tok.bytes_seen, tok.opaque_end)
                tok.reset()
                self.assertIsNone(tok.opaque_end)
        tok = rp.RasterTokenizer()
        tok.feed(rp.ESC_INIT + b"\x1b\x69\x61\x00" + b"\x1b\x40")
        self.assertEqual(tok.opaque_end, 6, "ESC i a 00: the switch to ESC/P ends at offset 6")
        for stream in (b"\x47\x05\x00ABCDE", b"\x1b\x69\x58\x47", b"\x77\x03\x01\x00"):
            with self.subTest(stream=stream.hex(" ")):
                tok = rp.RasterTokenizer()
                tok.feed(rp.ESC_INIT + stream)
                self.assertIsNotNone(tok.opaque)
                self.assertIsNone(tok.opaque_end, "an unknown command's end is unknown")
        self.assertIsNone(rp.RasterTokenizer().opaque_end)

    def test_esc_i_u_t_inside_a_job_does_not_end_it(self):
        tok = rp.RasterTokenizer()
        tok.feed(rp.INVALIDATE + rp.ESC_INIT + b"\x1b\x69\x61\x01" + b"\x1b\x69\x4d\x40"
                 + PTOUCH_TRAILER[:6])
        self.assertIsNone(tok.opaque)
        self.assertTrue(tok.in_job)
        self.assertFalse(tok.job_ended)
        self.assertTrue(tok.needs_resync, "the job still has no 0x1A")

    def test_unknown_opcode_goes_opaque(self):
        prefix = rp.INVALIDATE + rp.ESC_INIT
        tok = rp.RasterTokenizer()
        events = tok.feed(prefix + b"\x47\x05\x00ABCDE" + b"\x1a")
        self.assertEqual([e.kind for e in events], ["init", "opaque"])
        self.assertEqual(events[-1].offset, len(prefix))
        self.assertIn("0x47", tok.opaque)
        self.assertIn(f"offset {len(prefix)}", tok.opaque)
        self.assertFalse(tok.at_command_boundary)
        self.assertFalse(tok.needs_resync, "an opaque stream's EOF is trusted")
        self.assertEqual(tok.feed(b"\x1a\x1b\x40"), [])
        self.assertEqual(tok.jobs, 0)
        self.assertEqual(tok.bytes_seen, len(prefix) + 9 + 3)

    def test_opaque_reasons(self):
        cases = {
            b"\x1b\x69\x58\x47": "ESC i command 0x58",
            b"\x1b\x69\x55\x41\x00": "ESC i U sub-command 0x41",
            b"\x1b\x41": "escape sequence 1b 41",
            b"\x67\x01\x01\x00": "selector 0x01",
            b"\x77\x03\x01\x00": "plane 0x03",
            b"\x1b\x69\x61\x03^II": "command mode 0x03",
            b"\x1b\x69\x61\x00\x1b\x40": "command mode 0x00",
            b"\x1b\x69\x61\xff": "printer's own command mode (ESC i a FF)",
            b"\x1b\x69\x55\x75\x00\x00": "ESC i U sub-command 0x75",
        }
        for stream, reason in cases.items():
            with self.subTest(reason=reason):
                tok = rp.RasterTokenizer()
                tok.feed(rp.ESC_INIT + stream)
                self.assertIsNotNone(tok.opaque)
                self.assertIn(reason, tok.opaque)

    def test_mono_rows_and_reset(self):
        tok = rp.RasterTokenizer()
        tok.feed(rp.make_test_job(two_color=False, rows=150))
        self.assertEqual(tok.row_counts["g"], 150)
        self.assertFalse(tok.two_color)
        self.assertEqual(tok.expanded_mode, 0x08)
        tok.reset()
        self.assertEqual(tok.summary(), rp.RasterTokenizer().summary())

    def test_accepts_buffer_types(self):
        job = rp.make_test_job(rows=150)
        a = rp.RasterTokenizer()
        b = rp.RasterTokenizer()
        self.assertEqual(a.feed(bytearray(job)), b.feed(memoryview(job)))
        self.assertEqual(a.summary(), b.summary())


# --- test job generator --------------------------------------------------------

def split_job(job: bytes):
    """(header bytes, rows as (black, red) ints, trailer) of a make_test_job() job."""
    events = rp.RasterTokenizer().feed(job)
    margin = [e for e in events if e.kind == "margin"][0]
    start = margin.offset + 5
    header, body = job[:start], job[start:-1]
    rows = []
    i = 0
    while i < len(body):
        op, sel, n = body[i], body[i + 1], body[i + 2]
        data = int.from_bytes(body[i + 3:i + 3 + n], "big")
        if op == 0x77 and sel == 0x01:
            rows.append([data, 0])
        elif op == 0x77 and sel == 0x02:
            rows[-1][1] = data
        else:
            rows.append([data, 0])
        i += 3 + n
    return header, rows, job[-1:]


def pins(start: int, stop: int) -> int:
    return ((1 << (stop - start)) - 1) << (720 - stop)


class MakeTestJobTest(unittest.TestCase):
    def test_default_job_bytes(self):
        job = rp.make_test_job()
        header, rows, trailer = split_job(job)
        expected = (rp.INVALIDATE + b"\x1b\x40" + b"\x1b\x69\x61\x01"
                    + b"\x1b\x69\x7a\x86\x0a\x3e\x00" + (600).to_bytes(4, "little") + b"\x00\x00"
                    + b"\x1b\x69\x4d\x40" + b"\x1b\x69\x41\x01" + b"\x1b\x69\x4b\x09"
                    + b"\x1b\x69\x64\x23\x00")
        self.assertEqual(header, expected)
        self.assertEqual(trailer, b"\x1a")
        self.assertEqual(len(rows), 600)
        self.assertEqual(len(job), len(expected) + 600 * 2 * (3 + 90) + 1)
        self.assertNotIn(b"\x1b\x69\x21", header, "ESC i ! must not be sent")
        self.assertEqual(job, rp.make_test_job(), "deterministic")

    def test_two_color_layout(self):
        _, rows, _ = split_job(rp.make_test_job())
        area = pins(12, 708)
        for y, (black, red) in enumerate(rows):
            self.assertEqual(black & ~area, 0, f"black outside the print area at line {y}")
            self.assertEqual(red & ~area, 0, f"red outside the print area at line {y}")
            self.assertEqual(black & red, 0, f"black and red overlap at line {y}")
        self.assertEqual(rows[0], [area, 0], "top border is solid black")
        self.assertEqual(rows[-1], [area, 0], "bottom border is solid black")
        sides = pins(12, 24) | pins(696, 708)
        self.assertTrue(all(black & sides == sides for black, _ in rows), "side borders on every line")
        red_lines = sum(1 for _, red in rows if red)
        mixed_lines = sum(1 for black, red in rows if red and black & ~sides)
        self.assertGreater(red_lines, 100)
        self.assertGreater(mixed_lines, 20, "split bars put black and red on the same lines")

    def test_black_only_on_two_color_media(self):
        job = rp.make_test_job(pattern="black")
        tok = rp.RasterTokenizer()
        tok.feed(job)
        self.assertEqual(tok.expanded_mode, 0x09, "still a two-color job (DK-22251 needs it)")
        self.assertEqual(tok.row_counts["w2"], 600)
        _, rows, _ = split_job(job)
        self.assertTrue(all(red == 0 for _, red in rows))
        self.assertGreater(sum(1 for black, _ in rows if black), 500)

    def test_mono_job(self):
        job = rp.make_test_job(two_color=False)
        self.assertIn(b"\x1b\x69\x4b\x08", job[:500])
        tok = rp.RasterTokenizer()
        tok.feed(job)
        self.assertEqual(tok.row_counts, {"g": 600, "w1": 0, "w2": 0, "Z": 0})
        self.assertFalse(tok.two_color)

    def test_print_area_per_width(self):
        # Brother raster reference 2.3.5: (print-area pins, right-margin pins).
        manual = {12: (106, 29), 29: (306, 6), 38: (413, 12), 50: (554, 12), 54: (590, 0), 62: (696, 12)}
        self.assertEqual(rp.CONTINUOUS_PINS, manual)
        for width, (printable, first) in manual.items():
            with self.subTest(width=width):
                job = rp.make_test_job(width_mm=width, two_color=False, rows=150)
                tok = rp.RasterTokenizer()
                tok.feed(job)
                self.assertEqual(tok.requested_media["width"], width)
                _, rows, _ = split_job(job)
                area = pins(first, first + printable)
                self.assertEqual(rows[0][0], area)
                self.assertTrue(all(black & ~area == 0 for black, _ in rows))

    def test_length_limits(self):
        for lines in (150, 11811):
            tok = rp.RasterTokenizer()
            tok.feed(rp.make_test_job(rows=lines))
            self.assertEqual(tok.raster_lines, lines)
            self.assertEqual(tok.requested_media["lines"], lines)
        for bad in (149, 11812, 0, -1, True, 600.0, "600"):
            with self.subTest(rows=bad):
                with self.assertRaises(ValueError):
                    rp.make_test_job(rows=bad)

    def test_argument_validation(self):
        with self.assertRaises(ValueError):
            rp.make_test_job(width_mm=61)
        with self.assertRaises(ValueError):
            rp.make_test_job(width_mm="62")
        with self.assertRaises(ValueError):
            rp.make_test_job(width_mm=29, two_color=True)
        with self.assertRaises(ValueError):
            rp.make_test_job(pattern="checker")


class ConstantsTest(unittest.TestCase):
    def test_wire_constants(self):
        self.assertEqual(rp.INVALIDATE, b"\x00" * 400)
        self.assertEqual(rp.ESC_INIT, b"\x1b\x40")
        self.assertEqual(rp.STATUS_REQUEST, b"\x1b\x69\x53")
        self.assertEqual(rp.STATUS_LEN, 32)
        self.assertEqual(rp.RESYNC, rp.INVALIDATE + rp.ESC_INIT)

    def test_tables(self):
        self.assertEqual(rp.STATUS_TYPES[0x00], "reply to status request")
        self.assertEqual(rp.STATUS_TYPES[0x06], "phase change")
        self.assertEqual(rp.PHASE_TYPES, {0: "receiving", 1: "printing"})
        self.assertEqual(rp.ERROR_BITS[9][0x10], "cover open")
        self.assertEqual(rp.ERROR1_BITS[0x01], "no media")
        for name, offset in rp.FIELD_OFFSETS.items():
            self.assertTrue(3 <= offset < 32, name)


if __name__ == "__main__":
    unittest.main()
