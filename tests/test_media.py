"""Tests for agent/ql810wc/media.py (DK media table and the P-touch match rule)."""
from __future__ import annotations

import pathlib
import sys
import unittest

AGENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from ql810wc import media, rasterproto as rp  # noqa: E402

# Real frames (see tests/test_rasterproto.py for provenance).
REF_DK22251_IDLE = "80 20 42 34 39 30 04 00 00 00 3e 0a 00 00 23 00 00 00 00 00 00 00 00 00 00 81 00 00 00 00 00 00"
REF_DK22251_COVER_OPEN = "80 20 42 34 39 30 04 00 00 10 3e 0a 00 00 23 00 00 00 02 00 00 00 00 00 00 81 00 00 00 00 00 00"
REF_DK11201_IDLE = "80 20 42 34 39 30 04 00 00 00 1d 0b 00 00 01 00 00 5a 00 00 00 00 00 00 00 01 00 00 00 00 00 00"
QL810W_SNMP_62 = "802042343930040000003e0a0000150000000000000000000001000000000000"
QL810W_SNMP_DK22251 = "802042343930040000003e0a0000230000000000000000000081000000000000"
QL820_DK22214 = "80 20 42 34 41 30 04 00 00 00 0C 0A 00 00 1A 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00"
QL820_DK22205 = "80 20 42 34 41 30 04 00 00 00 3E 0A 00 00 15 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00"
QL800_DK22251_IDLE = "80 20 42 34 38 30 00 00 00 00 3E 0A 00 00 23 00 00 00 00 00 00 00 00 00 00 81 00 00 00 00 00 00"
# Tested unit, second media swap (dev/notes/usb-ground-truth.md; see test_rasterproto.py):
# the die-cut roll with the cover open, then the roll removed (cover still open). After
# removal the printer reads width 0 / type 0, leaves err1 0x01 clear and keeps byte 25;
# the length and roll-code bytes were not recorded, so the stale variant keeps them too.
REF_DK11201_COVER_OPEN = "80 20 42 34 39 30 04 00 00 10 1d 0b 00 00 01 00 00 5a 02 00 00 00 00 00 00 01 00 00 00 00 00 00"
REF_ROLL_REMOVED = "80 20 42 34 39 30 04 00 00 10 00 00 00 00 00 00 00 00 02 00 00 00 00 00 00 01 00 00 00 00 00 00"
REF_ROLL_REMOVED_STALE = "80 20 42 34 39 30 04 00 00 10 00 00 00 00 01 00 00 5a 02 00 00 00 00 00 00 01 00 00 00 00 00 00"


def st(text: str) -> rp.Status:
    return rp.Status.from_hex(text)


def frame(width: int, mtype: int, length: int, text_color: int = 0x01) -> rp.Status:
    return st(QL810W_SNMP_62).with_overrides(media_width_mm=width, media_type_raw=mtype,
                                             media_length_mm=length, text_color=text_color)


class DescribeTest(unittest.TestCase):
    def test_real_frames(self):
        cases = [
            (REF_DK22251_IDLE, 38, "62red", True),
            (REF_DK22251_COVER_OPEN, 38, "62red", True),
            (QL810W_SNMP_DK22251, 38, "62red", True),
            (QL800_DK22251_IDLE, 38, "62red", True),
            (QL810W_SNMP_62, 16, "62", False),
            (QL820_DK22205, 16, "62", False),
            (REF_DK11201_IDLE, 5, "29x90", False),
            (QL820_DK22214, 11, "12", False),
        ]
        for text, sdk_id, label, two in cases:
            with self.subTest(frame=text):
                for exact in (True, False):
                    found = media.describe(st(text), exact=exact)
                    self.assertIsNotNone(found)
                    self.assertEqual(found.sdk_id, sdk_id)
                    self.assertEqual(found.label_id, label)
                    self.assertEqual(found.needs_two_color, two)

    def test_last_match_wins_for_black_red(self):
        s = st(REF_DK22251_IDLE)
        matching = [m.sdk_id for m in media.MEDIA if media._matches(m, s, True, 0)]
        self.assertEqual(matching, [16, 38])
        self.assertEqual(media.describe(s).sdk_id, 38)

    def test_two_color_requires_exactly_0x81(self):
        s = frame(62, 0x0A, 0, text_color=0x80)
        self.assertEqual(media.describe(s).sdk_id, 16, "the client compares byte 25 == 0x81")
        self.assertEqual(media.describe(s, exact=False).sdk_id, 38, "lenient mode uses bit 7")

    def test_no_media_and_unsupported(self):
        self.assertIsNone(media.describe(frame(0, 0x00, 0, text_color=0)))
        self.assertIsNone(media.describe(frame(61, 0x0A, 0)))
        self.assertIsNone(media.describe(frame(62, 0x0B, 30)))
        self.assertIsNone(media.describe(frame(62, 0xFF, 0)))
        no_media_bit = st(QL810W_SNMP_62).with_overrides(err1=0x01)
        self.assertIsNone(media.describe(no_media_bit))

    def test_cover_open_frames_still_name_the_roll(self):
        # With the cover open the reply is an "error occurred" frame, but its media
        # bytes keep describing the loaded roll.
        for text, label in ((REF_DK22251_COVER_OPEN, "62red"), (REF_DK11201_COVER_OPEN, "29x90")):
            with self.subTest(label=label):
                s = st(text)
                self.assertEqual(s.status_type, rp.ST_ERROR)
                for exact in (True, False):
                    self.assertEqual(media.describe(s, exact=exact).label_id, label)
                self.assertEqual(media.dk_parts_for(s), media.lookup_label(label).dk_parts)

    def test_removed_roll_is_no_media(self):
        dk22251_removed = st(REF_DK22251_COVER_OPEN).with_overrides(media_width_mm=0, media_type_raw=0)
        for s in (st(REF_ROLL_REMOVED), st(REF_ROLL_REMOVED_STALE), dk22251_removed):
            with self.subTest(frame=s.raw.hex(" ")):
                self.assertFalse(s.has_media)
                self.assertFalse(s.two_color, "byte 25 of the removed roll is ignored")
                for exact in (True, False):
                    self.assertIsNone(media.describe(s, exact=exact))
                self.assertEqual(media.dk_parts_for(s), ())
                self.assertEqual(media.mib_media_name(media.describe(s, exact=False)), "")

    def test_manual_type_codes(self):
        s = frame(62, 0x4A, 0)
        self.assertIsNone(media.describe(s), "P-touch for Mac needs 0x0A exactly")
        self.assertEqual(media.describe(s, exact=False).sdk_id, 16)

    def test_rounded_lengths(self):
        self.assertEqual(media.describe(frame(60, 0x0B, 87)).sdk_id, 17)       # 60x86 reads 87
        self.assertIsNone(media.describe(frame(60, 0x0B, 86)))
        self.assertEqual(media.describe(frame(60, 0x0B, 86), exact=False).sdk_id, 17)
        self.assertEqual(media.describe(frame(62, 0x0B, 61)).sdk_id, 58)       # 62x60 reads 61
        self.assertEqual(media.describe(frame(62, 0x0B, 100)).sdk_id, 10)
        self.assertEqual(media.describe(frame(62, 0x0B, 101), exact=False).sdk_id, 10)
        self.assertIsNone(media.describe(frame(62, 0x0B, 102), exact=False))
        self.assertEqual(media.describe(frame(29, 0x0B, 91), exact=False).sdk_id, 5)

    def test_exact_match_preferred_over_tolerance(self):
        # 62x60 reads 61: a 62-wide die-cut of length 61 must not drift to anything else.
        self.assertEqual(media.describe(frame(62, 0x0B, 61), exact=False).sdk_id, 58)
        self.assertEqual(media.describe(frame(62, 0x0B, 29), exact=False).sdk_id, 9)


class TableTest(unittest.TestCase):
    def test_order_and_uniqueness(self):
        ids = [m.sdk_id for m in media.MEDIA]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        labels = [m.label_id for m in media.MEDIA if m.label_id]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(set(media.LABEL_IDS), set(labels))

    def test_entries_are_consistent(self):
        for m in media.MEDIA:
            with self.subTest(paper=m.sdk_id):
                self.assertIn(m.media_type, (rp.MEDIA_CONTINUOUS, rp.MEDIA_DIE_CUT))
                self.assertEqual(m.is_continuous, m.length_mm == 0)
                self.assertTrue(0 < m.width_mm <= 62)
                self.assertTrue(m.inch.endswith('"') or m.inch.endswith('" Dia'))
                self.assertTrue(m.name.startswith(f"{m.width_mm}mm"))
                for part in m.dk_parts:
                    self.assertRegex(part, r"^DK-[N0-9][0-9]{4,5}$")
        self.assertEqual([m.sdk_id for m in media.MEDIA if m.needs_two_color], [38])

    def test_lookup_label(self):
        self.assertEqual(media.lookup_label("62red").dk_parts, ("DK-22251",))
        self.assertEqual(media.lookup_label("62").sdk_id, 16)
        self.assertEqual(media.lookup_label("29x90").dk_parts, ("DK-11201",))
        self.assertEqual(media.lookup_label("39x90").name, "38mm x 90mm")
        self.assertEqual(media.lookup_label("60x86").length_mm, 87)
        self.assertIsNone(media.lookup_label("62x60"))
        self.assertIsNone(media.lookup_label("DK-22251"))

    def test_description(self):
        self.assertEqual(media.lookup_label("62red").description, "62mm continuous black/red (DK-22251)")
        self.assertEqual(media.lookup_label("29x90").description, "29mm x 90mm die-cut (DK-11201)")
        self.assertEqual(media.lookup_label("29x42").description, "29mm x 42mm die-cut")


class MibNameTest(unittest.TestCase):
    def test_observed_names(self):
        observed = {"62": '62mm / 2.4"', "62red": '62mm / 2.4"', "29": '29mm / 1.1"',
                    "29x90": '29mm x 90mm / 1.1" x 3.5"'}
        for label, name in observed.items():
            with self.subTest(label=label):
                m = media.lookup_label(label)
                self.assertEqual(m.mib_name, name)
                self.assertTrue(m.mib_name_verified)
                self.assertEqual(media.mib_media_name(m), name)

    def test_unverified_and_empty(self):
        m = media.lookup_label("62x29")
        self.assertEqual(m.mib_name, '62mm x 29mm / 2.4" x 1.1"')
        self.assertFalse(m.mib_name_verified)
        self.assertEqual(media.mib_media_name(None), "", "no media reads as an empty name")


class SynthesizeTest(unittest.TestCase):
    def test_reproduces_real_frames(self):
        expected = {"62red": REF_DK22251_IDLE, "62": QL810W_SNMP_62, "29x90": REF_DK11201_IDLE}
        for label, text in expected.items():
            with self.subTest(label=label):
                self.assertEqual(media.synthesize_status(media.lookup_label(label)), st(text))

    def test_round_trip_for_every_paper(self):
        for m in media.MEDIA:
            with self.subTest(paper=m.sdk_id):
                s = media.synthesize_status(m)
                self.assertTrue(s.is_idle)
                self.assertEqual(s.model_name, "QL-810W")
                self.assertIs(media.describe(s), m)
                self.assertIs(media.describe(s, exact=False), m)

    def test_other_model_codes(self):
        s = media.synthesize_status(media.lookup_label("62"), model_code=0x38)
        self.assertEqual(s.model_name, "QL-800")


class DkPartsTest(unittest.TestCase):
    def test_roll_codes(self):
        self.assertEqual(media.dk_parts_for(st(REF_DK22251_IDLE)), ("DK-22251",))
        self.assertEqual(media.dk_parts_for(st(REF_DK11201_IDLE)), ("DK-11201",))
        self.assertEqual(media.dk_parts_for(st(QL810W_SNMP_62)), ("DK-22205", "DK-44205", "DK-44605"))
        self.assertEqual(media.dk_parts_for(st(QL820_DK22214)), ("DK-22214",))

    def test_unknown_or_inconsistent_roll_code(self):
        unknown = st(QL810W_SNMP_62).with_overrides(roll_code=0x77)
        self.assertEqual(media.dk_parts_for(unknown), ("DK-22205", "DK-44205", "DK-44605"))
        mismatch = st(REF_DK11201_IDLE).with_overrides(roll_code=0x23)
        self.assertEqual(media.dk_parts_for(mismatch), ("DK-11201",))
        self.assertEqual(media.dk_parts_for(frame(61, 0x0A, 0)), ())


if __name__ == "__main__":
    unittest.main()
