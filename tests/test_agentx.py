"""Tests for agent/ql810wc/agentx.py, the AgentX (RFC 2741) subagent protocol.

* Codec tests: byte layouts in both byte orders, prefix compression edge
  cases, every VarBind type and every PDU type.
* TableHandlerTest: lexicographic order, include / exclusive-end semantics.
* SubagentSessionTest: a fake master over a socketpair drives whole sessions
  (Open, Register in context "printer", reads, SETs, Ping, Close, reconnect).
* SubagentUpdateTest: update() (a reload) swaps the handler and the
  registration set in the open session: Register before Unregister, refusals,
  no session close, one handler per PDU.
* RealSnmpdTest: net-snmp's snmpd as the AgentX master with VACM, so the
  community -> context mapping is exercised for real, update() included.
  Linux / CI only: Apple's snmpd has no VACM.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests._util import load_module

ax = load_module("agent/ql810wc/agentx.py", "ql810wc_agentx_under_test")

T = ax.VarType
P = ax.PduType
NBO = ax.FLAG_NETWORK_BYTE_ORDER
NDC = ax.FLAG_NON_DEFAULT_CONTEXT

SYSTEM = (1, 3, 6, 1, 2, 1, 1)
SYSTEM_END = (1, 3, 6, 1, 2, 1, 2)
SYS_DESCR = SYSTEM + (1, 0)
SYS_OBJECT_ID = SYSTEM + (2, 0)
SYS_UPTIME = SYSTEM + (3, 0)
BROTHER = (1, 3, 6, 1, 4, 1, 2435)
BROTHER_END = (1, 3, 6, 1, 4, 1, 2436)
BR_IEEE1284 = BROTHER + (2, 3, 9, 1, 1, 7, 0)
BR_STATUS = BROTHER + (3, 3, 9, 1, 6, 1, 0)
IEEE1284_ID = b"MFG:Brother;CMD:PT-CBP;MDL:QL-810W;CLS:PRINTER;"
STATUS_BLOCK = bytes((0x80, 0x20, 0x42, 0x34, 0x39, 0x30)) + bytes(26)  # synthetic 32-byte block
LOGGER = "ql810wc.agentx"

_QUIET = logging.NullHandler()


def setUpModule():
    # Expected warnings (bad PDUs, refused registrations...) stay out of the
    # test output; assertLogs still sees them.
    logging.getLogger(LOGGER).addHandler(_QUIET)


def tearDownModule():
    logging.getLogger(LOGGER).removeHandler(_QUIET)


def make_handler() -> "ax.TableHandler":
    return ax.TableHandler({
        "printer": {
            SYS_DESCR: (T.OCTET_STRING, "Brother NC-XXXXw"),
            SYS_OBJECT_ID: (T.OBJECT_IDENTIFIER, "1.3.6.1.4.1.2435.2.3.9.1"),
            SYS_UPTIME: (T.TIME_TICKS, lambda: 4242),
            BR_IEEE1284: (T.OCTET_STRING, IEEE1284_ID),
            BR_STATUS: (T.OCTET_STRING, STATUS_BLOCK),
        },
        "": {BR_STATUS: (T.INTEGER, 1)},
    })


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------
class OidCodecTest(unittest.TestCase):
    CASES = [  # oid, expected n_subid, expected prefix
        ((), 0, 0),
        ((0, 0), 2, 0),
        ((1, 3, 6, 1), 4, 0),
        ((1, 3, 6, 1, 4), 0, 4),
        ((1, 3, 6, 1, 4, 1, 2435), 2, 4),
        ((1, 3, 6, 1, 2, 1, 1, 1, 0), 4, 2),
        ((1, 3, 6, 1, 0, 7), 6, 0),
        ((1, 3, 6, 1, 255, 7), 1, 255),
        ((1, 3, 6, 1, 256, 7), 6, 0),
        ((1, 3, 6, 2, 1), 5, 0),
        ((2, 3, 6, 1, 4, 1), 6, 0),
        ((1, 3, 6, 1, 4, 1, 0xFFFFFFFF), 2, 4),
    ]

    def test_prefix_compression_round_trip_both_orders(self):
        for oid, n_subid, prefix in self.CASES:
            for nbo in (True, False):
                for include in (False, True):
                    with self.subTest(oid=oid, nbo=nbo, include=include):
                        data = ax.encode_oid(oid, include, nbo)
                        effective = include and bool(oid)  # never "include" on the null OID
                        self.assertEqual(data[:4], bytes((n_subid, prefix, int(effective), 0)))
                        self.assertEqual(len(data), 4 + 4 * n_subid)
                        self.assertEqual(ax.decode_oid(b"pad!" + data + b"tail", 4, nbo),
                                         (oid, effective, 4 + len(data)))

    def test_exact_bytes(self):
        self.assertEqual(ax.encode_oid("1.3.6.1.4.1.2435", True),
                         bytes((2, 4, 1, 0)) + b"\x00\x00\x00\x01\x00\x00\x09\x83")
        self.assertEqual(ax.encode_oid(".1.3.6.1.4.1.2435", False, False),
                         bytes((2, 4, 0, 0)) + b"\x01\x00\x00\x00\x83\x09\x00\x00")

    def test_truncated_oid(self):
        with self.assertRaises(ax.ParseError):
            ax.decode_oid(bytes((3, 4, 0, 0)) + struct.pack(">2I", 1, 2))

    def test_invalid_oids(self):
        for bad in ((1, -1), (1, 2 ** 32), tuple(range(129)), "1..3", "1.3.x", "1.3_6", "1.3.6.",
                    (1.5,), ("1",), 42, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ax.encode_oid(bad)

    def test_parse_and_format(self):
        self.assertEqual(ax.parse_oid(".1.3.6.1.4.1.2435"), BROTHER)
        self.assertEqual(ax.parse_oid(" 1.3.6 "), (1, 3, 6))
        self.assertEqual(ax.parse_oid(""), ())
        self.assertEqual(ax.parse_oid([1, 3, 6]), (1, 3, 6))
        self.assertEqual(ax.format_oid(BROTHER), "1.3.6.1.4.1.2435")
        self.assertEqual(ax.format_oid(()), "")


class OctetsCodecTest(unittest.TestCase):
    def test_padding_and_round_trip(self):
        for n in range(9):
            data = bytes(range(1, n + 1))
            for nbo in (True, False):
                with self.subTest(n=n, nbo=nbo):
                    enc = ax.encode_octets(data, nbo)
                    self.assertEqual(len(enc), 4 + 4 * ((n + 3) // 4))
                    self.assertEqual(struct.unpack(">I" if nbo else "<I", enc[:4])[0], n)
                    self.assertEqual(enc[4:4 + n], data)
                    self.assertEqual(enc[4 + n:], bytes(len(enc) - 4 - n))
                    self.assertEqual(ax.decode_octets(enc + b"more", 0, nbo), (data, len(enc)))

    def test_truncated(self):
        with self.assertRaises(ax.ParseError):
            ax.decode_octets(b"\x00\x00\x00\x05abcd")  # 5 bytes announced, 4 present
        with self.assertRaises(ax.ParseError):
            ax.decode_octets(b"\x00\x00\x00\x03abc")  # padding missing
        with self.assertRaises(ax.ParseError):
            ax.decode_octets(b"\x00\x00")  # no complete length


class VarBindCodecTest(unittest.TestCase):
    CASES = [  # type, value, decoded value
        (T.INTEGER, -2 ** 31, -2 ** 31),
        (T.INTEGER, 2 ** 31 - 1, 2 ** 31 - 1),
        (T.INTEGER, True, 1),
        (T.OCTET_STRING, b"", b""),
        (T.OCTET_STRING, STATUS_BLOCK, STATUS_BLOCK),
        (T.OCTET_STRING, "QL-810W", b"QL-810W"),
        (T.OCTET_STRING, bytearray(b"abcde"), b"abcde"),
        (T.NULL, None, None),
        (T.OBJECT_IDENTIFIER, (1, 3, 6, 1, 4, 1, 2435, 2, 3, 9, 1), (1, 3, 6, 1, 4, 1, 2435, 2, 3, 9, 1)),
        (T.OBJECT_IDENTIFIER, "1.3.6.1.4.1.2435.2.3.9.1", (1, 3, 6, 1, 4, 1, 2435, 2, 3, 9, 1)),
        (T.OBJECT_IDENTIFIER, (), ()),
        (T.IP_ADDRESS, "192.0.2.1", "192.0.2.1"),
        (T.IP_ADDRESS, bytes((192, 0, 2, 7)), "192.0.2.7"),
        (T.COUNTER32, 2 ** 32 - 1, 2 ** 32 - 1),
        (T.GAUGE32, 7, 7),
        (T.TIME_TICKS, 4242, 4242),
        (T.OPAQUE, b"\x9f\x78\x04\x3f\x80\x00\x00", b"\x9f\x78\x04\x3f\x80\x00\x00"),
        (T.COUNTER64, 2 ** 64 - 1, 2 ** 64 - 1),
        (T.COUNTER64, 2 ** 32 + 5, 2 ** 32 + 5),
        (T.NO_SUCH_OBJECT, None, None),
        (T.NO_SUCH_INSTANCE, None, None),
        (T.END_OF_MIB_VIEW, None, None),
    ]

    def test_round_trip_every_type_both_orders(self):
        for vtype, value, expected in self.CASES:
            for nbo in (True, False):
                with self.subTest(type=vtype.name, value=value, nbo=nbo):
                    enc = ax.encode_varbind(ax.VarBind(SYS_DESCR, vtype, value), nbo)
                    self.assertEqual(len(enc) % 4, 0)
                    vb, end = ax.decode_varbind(enc + b"\xff" * 4, 0, nbo)
                    self.assertEqual(end, len(enc))
                    self.assertEqual(vb, ax.VarBind(SYS_DESCR, vtype, expected))
                    self.assertIsInstance(vb.type, ax.VarType)

    def test_layout_and_counter64_word_order(self):
        value = 0x0102030405060708
        enc = ax.encode_varbind((SYS_UPTIME, T.COUNTER64, value))
        self.assertEqual(enc[:4], b"\x00\x46\x00\x00")
        self.assertEqual(enc[-8:], bytes.fromhex("0102030405060708"))  # high word first
        enc = ax.encode_varbind((SYS_UPTIME, T.COUNTER64, value), False)
        self.assertEqual(enc[:4], b"\x46\x00\x00\x00")
        self.assertEqual(enc[-8:], bytes.fromhex("0807060504030201"))  # low word first, as net-snmp
        self.assertEqual(ax.encode_varbind((SYS_UPTIME, T.TIME_TICKS, 0x1234)),
                         b"\x00\x43\x00\x00" + bytes((4, 2, 0, 0)) + struct.pack(">4I", 1, 1, 3, 0)
                         + b"\x00\x00\x12\x34")

    def test_smi_wrap_and_latch(self):
        def round_trip(vtype, value):
            return ax.decode_varbind(ax.encode_varbind((SYS_UPTIME, vtype, value)))[0].value

        self.assertEqual(round_trip(T.COUNTER32, 2 ** 32 + 5), 5)
        self.assertEqual(round_trip(T.TIME_TICKS, 2 ** 32 + 6), 6)
        self.assertEqual(round_trip(T.COUNTER64, 2 ** 64 + 7), 7)
        self.assertEqual(round_trip(T.GAUGE32, 2 ** 40), 2 ** 32 - 1)

    def test_values_that_do_not_fit(self):
        bad = [(T.INTEGER, 2 ** 31), (T.INTEGER, -2 ** 31 - 1), (T.INTEGER, "5"), (T.INTEGER, 1.5),
               (T.COUNTER32, -1), (T.GAUGE32, -1), (T.TIME_TICKS, None), (T.COUNTER64, -1),
               (T.OCTET_STRING, 5), (T.IP_ADDRESS, "300.1.1.1"), (T.IP_ADDRESS, b"\x01\x02"),
               (T.OBJECT_IDENTIFIER, "1.3.x"), (99, 1)]
        for vtype, value in bad:
            with self.subTest(type=vtype, value=value), self.assertRaises(ax.EncodeError):
                ax.encode_varbind((SYS_DESCR, vtype, value))
        with self.assertRaises(ax.EncodeError):
            ax.encode_varbind((SYS_DESCR, T.INTEGER))  # not an (oid, type, value) triple
        with self.assertRaises(ax.EncodeError):
            ax.encode_varbind(("1.3.x", T.INTEGER, 1))

    def test_malformed_varbinds(self):
        name = ax.encode_oid(SYS_DESCR)
        with self.assertRaises(ax.ParseError):
            ax.decode_varbind(struct.pack(">HH", 99, 0) + name)
        with self.assertRaises(ax.ParseError):
            ax.decode_varbind(struct.pack(">HH", T.IP_ADDRESS, 0) + name + ax.encode_octets(b"\x01\x02\x03"))
        with self.assertRaises(ax.ParseError):
            ax.decode_varbind(struct.pack(">HH", T.INTEGER, 0) + name + b"\x00\x01")

    def test_type_names_and_error_names(self):
        self.assertIs(T.from_name("octets"), T.OCTET_STRING)
        self.assertIs(T.from_name("OID"), T.OBJECT_IDENTIFIER)
        self.assertIs(T.from_name("TimeTicks"), T.TIME_TICKS)
        self.assertIs(T.from_name("counter-32"), T.COUNTER32)
        self.assertIs(T.from_name("ip_address"), T.IP_ADDRESS)
        self.assertIs(T.from_name("Gauge32"), T.GAUGE32)
        with self.assertRaises(ValueError):
            T.from_name("float")
        self.assertEqual(ax.error_name(263), "duplicateRegistration (263)")
        self.assertEqual(ax.error_name(ax.NOT_WRITABLE), "notWritable (17)")
        self.assertEqual(ax.error_name(999), "unknown (999)")


class PduCodecTest(unittest.TestCase):
    @staticmethod
    def sample_pdus():
        vbs = [ax.VarBind(SYS_DESCR, T.OCTET_STRING, b"QL"), ax.VarBind(SYS_UPTIME, T.TIME_TICKS, 5),
               ax.VarBind(BR_STATUS, T.NULL, None)]
        return [
            ax.Pdu(type=P.OPEN, timeout=5, id=BROTHER + (99,), descr="ql810wc test"),
            ax.Pdu(type=P.OPEN),
            ax.Pdu(type=P.CLOSE, session_id=3, reason=ax.CloseReason.SHUTDOWN),
            ax.Pdu(type=P.REGISTER, session_id=3, packet_id=9, context="printer", timeout=2,
                   priority=100, subtree=BROTHER),
            ax.Pdu(type=P.REGISTER, session_id=3, flags=ax.FLAG_INSTANCE_REGISTRATION, subtree=SYS_UPTIME),
            ax.Pdu(type=P.REGISTER, session_id=3, context="printer",
                   subtree=(1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), range_subid=10, upper_bound=22),
            ax.Pdu(type=P.UNREGISTER, session_id=3, context="printer", priority=100, subtree=BROTHER),
            ax.Pdu(type=P.GET, session_id=3, transaction_id=4, packet_id=5, context="printer",
                   ranges=[ax.SearchRange(SYS_DESCR), ax.SearchRange(BR_STATUS)]),
            ax.Pdu(type=P.GETNEXT, session_id=3, ranges=[ax.SearchRange(SYSTEM, SYSTEM_END, True),
                                                         ax.SearchRange(BR_STATUS, (), False)]),
            ax.Pdu(type=P.GETBULK, session_id=3, context="printer", non_repeaters=1, max_repetitions=10,
                   ranges=[ax.SearchRange(SYSTEM, SYSTEM_END), ax.SearchRange(BROTHER, BROTHER_END)]),
            ax.Pdu(type=P.TESTSET, session_id=3, transaction_id=8, context="printer", varbinds=vbs),
            ax.Pdu(type=P.COMMITSET, session_id=3, transaction_id=8),
            ax.Pdu(type=P.UNDOSET, session_id=3, transaction_id=8),
            ax.Pdu(type=P.CLEANUPSET, session_id=3, transaction_id=8),
            ax.Pdu(type=P.NOTIFY, session_id=3, varbinds=vbs[:1]),
            ax.Pdu(type=P.PING, session_id=3, context="printer"),
            ax.Pdu(type=P.PING, session_id=3),
            ax.Pdu(type=P.INDEXALLOCATE, session_id=3, flags=ax.FLAG_ANY_INDEX, varbinds=vbs[1:2]),
            ax.Pdu(type=P.INDEXDEALLOCATE, session_id=3, varbinds=vbs[1:2]),
            ax.Pdu(type=P.ADDAGENTCAPS, session_id=3, context="printer", id=BROTHER, descr="caps"),
            ax.Pdu(type=P.REMOVEAGENTCAPS, session_id=3, id=BROTHER),
            ax.Pdu(type=P.RESPONSE, session_id=3, transaction_id=4, packet_id=5, sys_uptime=0xFFFFFFFF,
                   error=ax.NOT_WRITABLE, index=1),
            ax.Pdu(type=P.RESPONSE, session_id=3, packet_id=6, varbinds=vbs),
        ]

    def test_round_trip_every_pdu_type_both_orders(self):
        seen = set()
        for pdu in self.sample_pdus():
            for nbo in (True, False):
                with self.subTest(type=pdu.type.name, nbo=nbo):
                    frame = ax.encode_pdu(pdu, nbo)
                    hdr = ax.decode_header(frame)
                    self.assertEqual((hdr.version, hdr.type), (1, pdu.type))
                    self.assertEqual(hdr.payload_length, len(frame) - ax.HEADER_SIZE)
                    self.assertEqual(hdr.payload_length % 4, 0)
                    decoded = ax.decode_pdu(frame)
                    self.assertEqual(bool(decoded.flags & NBO), nbo)
                    self.assertEqual(bool(decoded.flags & NDC), bool(pdu.context))
                    decoded.flags &= ~(NBO | NDC)
                    self.assertEqual(decoded, pdu)
                    seen.add(pdu.type)
        self.assertEqual(seen, set(P))

    def test_register_golden_bytes_both_orders(self):
        pdu = ax.Pdu(type=P.REGISTER, session_id=0x01020304, packet_id=5, context="printer", subtree=BROTHER)
        payload = (b"\x00\x00\x00\x07printer\x00" + bytes((0, 127, 0, 0)) + bytes((2, 4, 0, 0))
                   + b"\x00\x00\x00\x01\x00\x00\x09\x83")
        self.assertEqual(ax.encode_pdu(pdu), bytes((1, 3, NBO | NDC, 0))
                         + struct.pack(">IIII", 0x01020304, 0, 5, len(payload)) + payload)
        payload = (b"\x07\x00\x00\x00printer\x00" + bytes((0, 127, 0, 0)) + bytes((2, 4, 0, 0))
                   + b"\x01\x00\x00\x00\x83\x09\x00\x00")
        self.assertEqual(ax.encode_pdu(pdu, network_byte_order=False), bytes((1, 3, NDC, 0))
                         + struct.pack("<IIII", 0x01020304, 0, 5, len(payload)) + payload)

    def test_little_endian_header(self):
        frame = bytes((1, P.PING, 0, 0)) + struct.pack("<IIII", 7, 8, 9, 0)
        self.assertEqual(ax.decode_header(frame), ax.Header(1, P.PING, 0, 7, 8, 9, 0))
        pdu = ax.decode_pdu(frame)
        self.assertEqual((pdu.type, pdu.session_id, pdu.transaction_id, pdu.packet_id, pdu.context),
                         (P.PING, 7, 8, 9, None))

    def test_malformed_frames(self):
        good = ax.encode_pdu(ax.Pdu(type=P.CLOSE, session_id=1, reason=5))

        def frame(ptype, payload, length=None):
            return bytes((1, ptype, NBO, 0)) + struct.pack(
                ">IIII", 1, 0, 1, len(payload) if length is None else length) + payload

        cases = {
            "short header": good[:19],
            "length mismatch": good + bytes(4),
            "bad version": b"\x02" + good[1:],
            "unknown type": frame(99, b""),
            "unaligned payload": frame(P.PING, b"abc"),
            "trailing bytes": frame(P.CLOSE, bytes((5, 0, 0, 0)) + bytes(4)),
            "truncated OID": frame(P.GET, bytes((5, 0, 0, 0)) + struct.pack(">I", 1)),
            "range_subid beyond subtree": frame(P.REGISTER, bytes((0, 127, 9, 0)) + ax.encode_oid(BROTHER)
                                                + struct.pack(">I", 5)),
            "truncated context": frame(P.GET, b"")[:2] + bytes((NBO | NDC,)) + frame(P.GET, b"")[3:],
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(ax.ParseError):
                ax.decode_pdu(data)

    def test_encode_rejects_invalid_fields(self):
        bad = [ax.Pdu(type=99), ax.Pdu(type=P.CLOSE, reason=300),
               ax.Pdu(type=P.REGISTER, subtree=BROTHER, priority=256),
               ax.Pdu(type=P.REGISTER, subtree=(1, 3), range_subid=3, upper_bound=9),
               ax.Pdu(type=P.GET, ranges=[ax.SearchRange("1.3.x")]),
               ax.Pdu(type=P.RESPONSE, error=70000), ax.Pdu(type=P.PING, session_id=-1),
               ax.Pdu(type=P.RESPONSE, varbinds=[ax.VarBind(SYS_DESCR, T.INTEGER, "x")])]
        for pdu in bad:
            with self.subTest(pdu=pdu), self.assertRaises(ax.EncodeError):
                ax.encode_pdu(pdu)


# ---------------------------------------------------------------------------
# TableHandler
# ---------------------------------------------------------------------------
class TableHandlerTest(unittest.TestCase):
    def setUp(self):
        self.calls = 0

        def uptime():
            self.calls += 1
            return 4242

        self.handler = ax.TableHandler({
            "printer": {
                "1.3.6.1.2.1.1.10.0": (T.INTEGER, 10),  # numerically after .9, textually before
                SYS_DESCR: ("octets", "Brother"),
                "1.3.6.1.2.1.1.9.0": ("integer", 9),
                SYS_UPTIME: (T.TIME_TICKS, uptime),
                BR_STATUS: (T.OCTET_STRING, STATUS_BLOCK),
                BR_IEEE1284: (T.OCTET_STRING, IEEE1284_ID),
                SYSTEM + (5, 0): (T.OCTET_STRING, lambda: None),  # not instantiated right now
            },
            None: {BR_STATUS: (T.INTEGER, 1)},
        })

    def walk(self, context="printer", start=(), end=None):
        found, cursor = [], start
        while True:
            vb = self.handler.getnext(cursor, end, False, context)
            if vb is None:
                return found
            found.append(vb.oid)
            cursor = vb.oid

    def test_lexicographic_order(self):
        self.assertEqual(self.walk(), [SYS_DESCR, SYS_UPTIME, SYSTEM + (9, 0), SYSTEM + (10, 0),
                                       BR_IEEE1284, BR_STATUS])
        self.assertEqual(self.walk(start=SYSTEM, end=SYSTEM_END),
                         [SYS_DESCR, SYS_UPTIME, SYSTEM + (9, 0), SYSTEM + (10, 0)])

    def test_include_and_exclusive_end(self):
        h = self.handler
        self.assertEqual(h.getnext(SYS_UPTIME, None, True, "printer").oid, SYS_UPTIME)
        self.assertEqual(h.getnext(SYS_UPTIME, None, False, "printer").oid, SYSTEM + (9, 0))
        self.assertEqual(h.getnext(SYSTEM, None, False, "printer").oid, SYS_DESCR)
        self.assertIsNone(h.getnext(SYSTEM + (10, 0), SYSTEM_END, False, "printer"))
        self.assertIsNone(h.getnext(SYSTEM + (9, 0), SYSTEM + (10, 0), False, "printer"))
        self.assertEqual(h.getnext(SYSTEM + (9, 0), SYSTEM + (10, 0, 0), False, "printer").oid,
                         SYSTEM + (10, 0))
        self.assertIsNone(h.getnext(BR_STATUS, None, False, "printer"))
        self.assertEqual(h.getnext("1.3.6.1.4.1.2435", "1.3.6.1.4.1.2436", True, "printer").oid,
                         BR_IEEE1284)
        self.assertEqual(h.getnext((), (), False, "printer").oid, SYS_DESCR)  # () = unbounded

    def test_get_and_dynamic_values(self):
        self.assertEqual(self.handler.get(SYS_UPTIME, "printer"), ax.VarBind(SYS_UPTIME, T.TIME_TICKS, 4242))
        self.handler.get(SYS_UPTIME, "printer")
        self.assertEqual(self.calls, 2)  # evaluated on every request
        self.assertEqual(self.handler.get("1.3.6.1.2.1.1.1.0", "printer").value, "Brother")
        self.assertIsNone(self.handler.get(SYSTEM + (5, 0), "printer"))
        self.assertIsNone(self.handler.get(SYS_OBJECT_ID, "printer"))
        self.assertIsNone(self.handler.get(SYS_DESCR, "no-such-context"))
        self.assertIsNone(self.handler.getnext((), None, False, "no-such-context"))

    def test_contexts_are_separate(self):
        self.assertEqual(self.handler.get(BR_STATUS, "").value, 1)
        self.assertEqual(self.handler.get(BR_STATUS, None).value, 1)
        self.assertEqual(self.handler.get(BR_STATUS, "printer").value, STATUS_BLOCK)
        self.assertIsNone(self.handler.get(SYS_DESCR, ""))

    def test_set_table_replaces_and_validates(self):
        self.handler.set_table("printer", {SYS_DESCR: (T.OCTET_STRING, b"new")})
        self.assertEqual(self.walk(), [SYS_DESCR])
        bad_tables = [
            {(): (T.INTEGER, 1)},
            {SYS_DESCR: (T.INTEGER, "x")},
            {SYS_DESCR: (T.INTEGER, 2 ** 31)},
            {SYS_DESCR: (99, 1)},
            {SYS_DESCR: ("float", 1.0)},
            {SYS_DESCR: (T.END_OF_MIB_VIEW, None)},
            {SYS_DESCR: T.INTEGER},
            {"1.3.6.1.2.1.1.1.0": (T.INTEGER, 1), SYS_DESCR: (T.INTEGER, 2)},
            {"1.3.x": (T.INTEGER, 1)},
        ]
        for entries in bad_tables:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.handler.set_table("printer", entries)
        self.assertEqual(self.walk(), [SYS_DESCR])  # failed updates leave the table alone


# ---------------------------------------------------------------------------
# Subagent against a fake master
# ---------------------------------------------------------------------------
class FakeMaster:
    """The master end of one AgentX connection, driven from the test thread."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()
        self.packet_id = 1000

    def close(self) -> None:
        self.sock.close()

    def recv_frame(self, timeout: float = 3.0) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            if len(self.buf) >= ax.HEADER_SIZE:
                total = ax.HEADER_SIZE + ax.decode_header(bytes(self.buf[:ax.HEADER_SIZE])).payload_length
                if len(self.buf) >= total:
                    frame = bytes(self.buf[:total])
                    del self.buf[:total]
                    return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no PDU from the subagent")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise EOFError("the subagent closed the connection")
            self.buf += chunk

    def expect(self, ptype, timeout: float = 3.0):
        pdu = ax.decode_pdu(self.recv_frame(timeout))
        if pdu.type != ptype:
            raise AssertionError(f"expected {ptype.name}, got {pdu}")
        return pdu

    def expect_eof(self, timeout: float = 3.0) -> None:
        try:
            frame = self.recv_frame(timeout)
        except EOFError:
            return
        raise AssertionError(f"expected the subagent to hang up, got {ax.decode_pdu(frame)}")

    def assert_silent(self, seconds: float) -> None:
        try:
            frame = self.recv_frame(seconds)
        except TimeoutError:
            return
        raise AssertionError(f"unexpected PDU from the subagent: {ax.decode_pdu(frame)}")

    def send(self, pdu, network_byte_order: bool = True) -> None:
        self.sock.sendall(ax.encode_pdu(pdu, network_byte_order))

    def respond(self, request, error: int = 0, session_id=None) -> None:
        self.send(ax.Pdu(type=P.RESPONSE, session_id=request.session_id if session_id is None else session_id,
                         transaction_id=request.transaction_id, packet_id=request.packet_id, error=error))

    def call(self, pdu, network_byte_order: bool = True):
        """Send a request to the subagent and return its Response."""
        self.packet_id += 1
        pdu.packet_id = self.packet_id
        self.send(pdu, network_byte_order)
        reply = self.expect(P.RESPONSE)
        if reply.packet_id != pdu.packet_id:
            raise AssertionError(f"response to packet {reply.packet_id}, expected {pdu.packet_id}")
        return reply

    def accept(self, session_id: int, registrations: int):
        """Answer Open and *registrations* Register PDUs successfully; return the Registers."""
        opened = self.expect(P.OPEN)
        self.respond(opened, session_id=session_id)
        regs = []
        for _ in range(registrations):
            reg = self.expect(P.REGISTER)
            self.respond(reg)
            regs.append(reg)
        return regs


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


class FakeMasterCase(unittest.TestCase):
    """A Subagent on a thread, connected to FakeMasters through socketpairs (no tests of its own)."""

    REGISTRATIONS = [("printer", SYSTEM), ("printer", BROTHER, 100)]

    def setUp(self):
        self.handler = make_handler()
        self.sockets = queue.Queue()
        self.addCleanup(self._close_unused_sockets)

    def _connector(self) -> socket.socket:
        try:
            return self.sockets.get(timeout=0.05)
        except queue.Empty:
            raise ConnectionRefusedError("fake master is not listening") from None

    def _close_unused_sockets(self) -> None:
        while True:
            try:
                self.sockets.get_nowait().close()
            except queue.Empty:
                return

    def master(self) -> FakeMaster:
        """Queue a connection for the subagent and return its master end."""
        ours, theirs = socket.socketpair()
        self.sockets.put(theirs)  # the subagent owns and closes it once connected
        self.addCleanup(ours.close)
        return FakeMaster(ours)

    def start(self, **overrides) -> None:
        options = dict(registrations=self.REGISTRATIONS, connector=self._connector, name="test-subagent",
                       ping_interval=60.0, response_timeout=2.0, backoff_min=0.05, backoff_max=0.2)
        options.update(overrides)
        self.subagent = ax.Subagent(self.handler, **options)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.subagent.run_forever, args=(self.stop,), daemon=True)
        self.thread.start()
        self.addCleanup(self._join)

    def _join(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive(), "subagent thread did not stop")

    def get(self, m: FakeMaster, session_id: int, *oids):
        reply = m.call(ax.Pdu(type=P.GET, session_id=session_id, context="printer",
                              ranges=[ax.SearchRange(oid) for oid in oids]))
        self.assertEqual(reply.error, 0)
        return reply.varbinds


class SubagentSessionTest(FakeMasterCase):
    def test_session_lifecycle_and_requests(self):
        self.start()
        m = self.master()
        opened = m.expect(P.OPEN)
        self.assertTrue(opened.flags & NBO)
        self.assertEqual((opened.session_id, opened.descr, opened.id, opened.timeout), (0, "test-subagent", (), 0))
        m.respond(opened, session_id=7)
        regs = []
        for _ in range(2):
            regs.append(m.expect(P.REGISTER))
            m.respond(regs[-1])
        for reg, subtree, priority in zip(regs, (SYSTEM, BROTHER), (127, 100)):
            self.assertEqual((reg.session_id, reg.context, reg.subtree, reg.priority, reg.range_subid, reg.timeout),
                             (7, "printer", subtree, priority, 0, 0))
            self.assertEqual(reg.flags, NBO | NDC)
        self.assertTrue(self.subagent.ready.wait(2))
        self.assertEqual(self.subagent.session_id, 7)
        self.assertIn("all 2 registrations accepted", self.subagent.status)

        # Get: values, ids echoed, absent object vs absent instance.
        reply = m.call(ax.Pdu(type=P.GET, session_id=7, transaction_id=77, context="printer", ranges=[
            ax.SearchRange(SYS_DESCR), ax.SearchRange(SYS_UPTIME), ax.SearchRange(SYSTEM + (99, 0)),
            ax.SearchRange(SYSTEM + (1, 5))]))
        self.assertEqual((reply.session_id, reply.transaction_id, reply.error, reply.index, reply.context),
                         (7, 77, 0, 0, None))
        self.assertEqual(reply.flags, NBO)
        self.assertEqual(reply.varbinds, [
            ax.VarBind(SYS_DESCR, T.OCTET_STRING, b"Brother NC-XXXXw"),
            ax.VarBind(SYS_UPTIME, T.TIME_TICKS, 4242),
            ax.VarBind(SYSTEM + (99, 0), T.NO_SUCH_OBJECT, None),
            ax.VarBind(SYSTEM + (1, 5), T.NO_SUCH_INSTANCE, None)])

        # The default context has its own table.
        reply = m.call(ax.Pdu(type=P.GET, session_id=7, ranges=[ax.SearchRange(BR_STATUS)]))
        self.assertEqual(reply.varbinds, [ax.VarBind(BR_STATUS, T.INTEGER, 1)])

        # GetNext: include flag, exclusive end, end of view.
        reply = m.call(ax.Pdu(type=P.GETNEXT, session_id=7, context="printer", ranges=[
            ax.SearchRange(SYSTEM, SYSTEM_END),
            ax.SearchRange(SYS_UPTIME, SYSTEM_END, include=True),
            ax.SearchRange(SYS_UPTIME, BR_IEEE1284),
            ax.SearchRange(BROTHER, BROTHER_END, include=True),
            ax.SearchRange(BR_STATUS, ())]))
        self.assertEqual([(vb.oid, vb.type) for vb in reply.varbinds], [
            (SYS_DESCR, T.OCTET_STRING), (SYS_UPTIME, T.TIME_TICKS), (SYS_UPTIME, T.END_OF_MIB_VIEW),
            (BR_IEEE1284, T.OCTET_STRING), (BR_STATUS, T.END_OF_MIB_VIEW)])

        # GetBulk: 1 non-repeater, then 3 rounds over 2 repeaters; endOfMibView
        # takes the name of the column's previous VarBind.
        reply = m.call(ax.Pdu(type=P.GETBULK, session_id=7, context="printer", non_repeaters=1,
                              max_repetitions=3, ranges=[ax.SearchRange(SYSTEM, ()),
                                                         ax.SearchRange(SYS_DESCR, SYSTEM_END),
                                                         ax.SearchRange(BROTHER, BROTHER_END)]))
        self.assertEqual([(vb.oid, vb.type) for vb in reply.varbinds], [
            (SYS_DESCR, T.OCTET_STRING),
            (SYS_OBJECT_ID, T.OBJECT_IDENTIFIER), (BR_IEEE1284, T.OCTET_STRING),
            (SYS_UPTIME, T.TIME_TICKS), (BR_STATUS, T.OCTET_STRING),
            (SYS_UPTIME, T.END_OF_MIB_VIEW), (BR_STATUS, T.END_OF_MIB_VIEW)])
        # ... and stops early once every repeater is exhausted.
        reply = m.call(ax.Pdu(type=P.GETBULK, session_id=7, context="printer", max_repetitions=50,
                              ranges=[ax.SearchRange(SYS_DESCR, SYSTEM_END)]))
        self.assertEqual([(vb.oid, vb.type) for vb in reply.varbinds], [
            (SYS_OBJECT_ID, T.OBJECT_IDENTIFIER), (SYS_UPTIME, T.TIME_TICKS), (SYS_UPTIME, T.END_OF_MIB_VIEW)])

        # SET: TestSet -> notWritable at index 1; CleanupSet gets no response.
        reply = m.call(ax.Pdu(type=P.TESTSET, session_id=7, transaction_id=90, context="printer", varbinds=[
            ax.VarBind(SYS_DESCR, T.OCTET_STRING, b"x"), ax.VarBind(SYS_UPTIME, T.TIME_TICKS, 1)]))
        self.assertEqual((reply.error, reply.index, reply.varbinds, reply.transaction_id),
                         (ax.NOT_WRITABLE, 1, [], 90))
        m.send(ax.Pdu(type=P.CLEANUPSET, session_id=7, transaction_id=90, packet_id=4242))
        m.assert_silent(0.3)
        for ptype in (P.COMMITSET, P.UNDOSET, P.PING):
            reply = m.call(ax.Pdu(type=ptype, session_id=7, transaction_id=91))
            self.assertEqual((reply.error, reply.index, reply.transaction_id), (0, 0, 91))

        # Unknown session -> notOpen; unparsable request -> parseError.
        reply = m.call(ax.Pdu(type=P.GET, session_id=999, ranges=[ax.SearchRange(SYS_DESCR)]))
        self.assertEqual((reply.error, reply.session_id), (ax.NOT_OPEN, 999))
        m.sock.sendall(bytes((1, P.GET, NBO, 0)) + struct.pack(">IIII", 7, 5, 4243, 8)
                       + bytes((5, 0, 0, 0)) + struct.pack(">I", 1))
        reply = m.expect(P.RESPONSE)
        self.assertEqual((reply.packet_id, reply.transaction_id, reply.error), (4243, 5, ax.PARSE_ERROR))

        # A little-endian master is understood; replies stay in network byte order.
        reply = m.call(ax.Pdu(type=P.GET, session_id=7, context="printer", ranges=[ax.SearchRange(BR_STATUS)]),
                       network_byte_order=False)
        self.assertEqual(reply.flags, NBO)
        self.assertEqual(reply.varbinds, [ax.VarBind(BR_STATUS, T.OCTET_STRING, STATUS_BLOCK)])

        # Stopping sends Close (shutdown) and ends run_forever.
        self.stop.set()
        closing = m.expect(P.CLOSE)
        self.assertEqual((closing.session_id, closing.reason), (7, ax.CloseReason.SHUTDOWN))
        m.respond(closing)
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.subagent.ready.is_set())
        self.assertEqual(self.subagent.status, "stopped")

    def test_reconnects_and_reregisters_after_master_restart(self):
        self.start()
        first = self.master()
        first.accept(session_id=7, registrations=2)
        self.assertTrue(self.subagent.ready.wait(2))
        first.close()  # the master goes away
        self.assertTrue(_wait_until(lambda: not self.subagent.ready.is_set(), 3))
        self.assertIsNone(self.subagent.session_id)
        time.sleep(0.3)  # a few refused connection attempts with backoff
        second = self.master()
        regs = second.accept(session_id=8, registrations=2)
        self.assertEqual([(r.context, r.subtree, r.priority) for r in regs],
                         [("printer", SYSTEM, 127), ("printer", BROTHER, 100)])
        self.assertTrue(self.subagent.ready.wait(2))
        self.assertEqual(self.subagent.session_id, 8)
        reply = second.call(ax.Pdu(type=P.GET, session_id=8, context="printer", ranges=[ax.SearchRange(SYS_DESCR)]))
        self.assertEqual(reply.varbinds, [ax.VarBind(SYS_DESCR, T.OCTET_STRING, b"Brother NC-XXXXw")])

    def test_close_from_master_ends_session_and_reconnects(self):
        self.start()
        first = self.master()
        first.accept(session_id=7, registrations=2)
        second = self.master()
        first.send(ax.Pdu(type=P.CLOSE, session_id=7, reason=ax.CloseReason.SHUTDOWN))
        first.expect_eof()  # hangs up without answering the Close
        second.accept(session_id=8, registrations=2)
        self.assertTrue(self.subagent.ready.wait(2))

    def test_open_refused_is_logged_and_retried(self):
        self.start()
        first = self.master()
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            opened = first.expect(P.OPEN)
            first.respond(opened, error=ax.OPEN_FAILED)
            first.expect_eof()
        self.assertIn("openFailed (256)", "\n".join(logs.output))
        second = self.master()
        second.accept(session_id=9, registrations=2)
        self.assertTrue(self.subagent.ready.wait(2))

    def test_refused_registration_is_logged_and_retried(self):
        self.start()
        m = self.master()
        opened = m.expect(P.OPEN)
        m.respond(opened, session_id=7)
        first = m.expect(P.REGISTER)
        self.assertEqual(first.subtree, SYSTEM)
        with self.assertLogs(LOGGER, level="ERROR") as logs:
            m.respond(first, error=ax.DUPLICATE_REGISTRATION)
            second = m.expect(P.REGISTER)  # the other registration goes ahead
        self.assertIn("duplicateRegistration (263)", "\n".join(logs.output))
        self.assertIn("1.3.6.1.2.1.1 in context 'printer'", "\n".join(logs.output))
        self.assertEqual(second.subtree, BROTHER)
        m.respond(second)
        retry = m.expect(P.REGISTER)  # after the backoff delay
        self.assertFalse(self.subagent.ready.is_set())
        self.assertEqual((retry.context, retry.subtree, retry.priority), ("printer", SYSTEM, 127))
        m.respond(retry)
        self.assertTrue(self.subagent.ready.wait(2))

    def test_pings_idle_master_and_reconnects_when_unanswered(self):
        self.start(ping_interval=0.3, response_timeout=0.5)
        first = self.master()
        first.accept(session_id=7, registrations=2)
        ping = first.expect(P.PING)
        self.assertEqual((ping.session_id, ping.context, ping.flags), (7, None, NBO))
        first.respond(ping)
        first.expect(P.PING)  # this one goes unanswered
        first.expect_eof()
        second = self.master()
        second.accept(session_id=8, registrations=2)
        self.assertTrue(self.subagent.ready.wait(2))

    def test_handler_failures_answer_generr_and_session_survives(self):
        def broken():
            raise RuntimeError("status cache unreadable")

        self.handler.set_table("printer", {SYS_DESCR: (T.OCTET_STRING, "ok"), BR_STATUS: (T.OCTET_STRING, broken),
                                           BR_IEEE1284: (T.INTEGER, lambda: "not an int")})
        self.start()
        m = self.master()
        m.accept(session_id=7, registrations=2)
        with self.assertLogs(LOGGER, level="ERROR") as logs:
            reply = m.call(ax.Pdu(type=P.GET, session_id=7, context="printer",
                                  ranges=[ax.SearchRange(SYS_DESCR), ax.SearchRange(BR_STATUS)]))
            self.assertEqual((reply.error, reply.index, reply.varbinds), (ax.GEN_ERR, 2, []))
            reply = m.call(ax.Pdu(type=P.GETNEXT, session_id=7, context="printer",
                                  ranges=[ax.SearchRange(BROTHER, BROTHER_END)]))
            self.assertEqual((reply.error, reply.index, reply.varbinds), (ax.GEN_ERR, 1, []))
        self.assertTrue(any(r.exc_info and isinstance(r.exc_info[1], RuntimeError) for r in logs.records))
        self.assertIn("cannot encode 1.3.6.1.4.1.2435.2.3.9.1.1.7.0 as INTEGER", "\n".join(logs.output))
        reply = m.call(ax.Pdu(type=P.GET, session_id=7, context="printer", ranges=[ax.SearchRange(SYS_DESCR)]))
        self.assertEqual((reply.error, reply.varbinds), (0, [ax.VarBind(SYS_DESCR, T.OCTET_STRING, b"ok")]))

    def test_responses_stay_within_the_agentx_size_limit(self):
        table = {BROTHER + (i, 0): (T.OCTET_STRING, bytes(10000)) for i in range(1, 21)}
        table[BROTHER + (99, 0)] = (T.OCTET_STRING, bytes(70000))
        self.handler.set_table("printer", table)
        self.start()
        m = self.master()
        m.accept(session_id=7, registrations=2)
        reply = m.call(ax.Pdu(type=P.GET, session_id=7, context="printer",
                              ranges=[ax.SearchRange(BROTHER + (99, 0))]))
        self.assertEqual((reply.error, reply.index, reply.varbinds), (ax.TOO_BIG, 0, []))
        m.send(ax.Pdu(type=P.GETBULK, session_id=7, packet_id=77, context="printer", max_repetitions=20,
                      ranges=[ax.SearchRange(BROTHER, BROTHER_END)]))
        frame = m.recv_frame()
        self.assertLessEqual(len(frame), ax.MAX_PDU_SIZE)
        reply = ax.decode_pdu(frame)
        self.assertEqual((reply.packet_id, reply.error), (77, 0))
        # 6 VarBinds of ~10 kB fit in 64 KiB, a 7th would not: whole rounds only.
        self.assertEqual([vb.oid for vb in reply.varbinds], [BROTHER + (i, 0) for i in range(1, 7)])


PRINTER_MIB = (1, 3, 6, 1, 2, 1, 43)


class SubagentUpdateTest(FakeMasterCase):
    """Subagent.update(): a reload swaps handler and registrations in the open session.

    Closing the session instead would leave snmpd's context with nothing
    registered: net-snmp then answers noSuchName to SNMPv1 (P-touch fails).
    """

    def open_session(self, session_id: int = 7) -> FakeMaster:
        self.start()
        m = self.master()
        m.accept(session_id=session_id, registrations=len(self.REGISTRATIONS))
        self.assertTrue(self.subagent.ready.wait(2))
        return m

    def test_new_handler_answers_in_the_same_session(self):
        m = self.open_session()
        self.assertEqual(self.get(m, 7, SYS_DESCR)[0].value, b"Brother NC-XXXXw")
        new = ax.TableHandler({"printer": {SYS_DESCR: (T.OCTET_STRING, "reloaded")}})
        generation = self.subagent.update(new)
        self.assertEqual(generation, 0, "the registration set did not change")
        self.assertTrue(self.subagent.wait_applied(generation, 0))
        self.assertIs(self.subagent.handler, new)
        self.assertEqual(self.get(m, 7, SYS_DESCR, BR_STATUS),
                         [ax.VarBind(SYS_DESCR, T.OCTET_STRING, b"reloaded"),
                          ax.VarBind(BR_STATUS, T.NO_SUCH_OBJECT, None)])
        m.assert_silent(0.7)   # no Register, Unregister or Close: the session stays as it is
        self.assertEqual(self.subagent.session_id, 7)
        self.assertTrue(self.subagent.ready.is_set())

    def test_new_subtrees_are_registered_before_dropped_ones_are_unregistered(self):
        m = self.open_session()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            generation = self.subagent.update(registrations=[("printer", BROTHER, 100), ("printer", PRINTER_MIB)])
            self.assertEqual(generation, 1)
            reg = m.expect(P.REGISTER)
            self.assertEqual((reg.session_id, reg.context, reg.subtree, reg.priority), (7, "printer", PRINTER_MIB, 127))
            self.assertFalse(self.subagent.wait_applied(generation, 0.05), "applied before the master answered")
            # The status OID keeps answering while the change is in progress.
            self.assertEqual(self.get(m, 7, BR_STATUS)[0].value, STATUS_BLOCK)
            m.respond(reg)
            unreg = m.expect(P.UNREGISTER)
            self.assertEqual((unreg.session_id, unreg.context, unreg.subtree, unreg.priority, unreg.flags),
                             (7, "printer", SYSTEM, 127, NBO | NDC))
            m.respond(unreg)
            self.assertTrue(self.subagent.wait_applied(generation, 2))
        text = "\n".join(logs.output)
        self.assertIn("registered 1.3.6.1.2.1.43 in context 'printer'", text)
        self.assertIn("unregistered 1.3.6.1.2.1.1 in context 'printer'", text)
        self.assertEqual(self.subagent.registrations, (ax.Registration("printer", BROTHER, 100),
                                                       ax.Registration("printer", PRINTER_MIB)))
        self.assertEqual(self.subagent.status, "session 7: all 2 registrations accepted")
        self.assertTrue(self.subagent.ready.is_set())
        m.assert_silent(0.3)
        # A later session registers the new set, in its order.
        m.close()
        second = self.master()
        regs = second.accept(session_id=8, registrations=2)
        self.assertEqual([r.subtree for r in regs], [BROTHER, PRINTER_MIB])
        self.assertTrue(self.subagent.ready.wait(2))

    def test_a_refused_addition_is_retried(self):
        m = self.open_session()
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            generation = self.subagent.update(registrations=[("printer", BROTHER, 100), ("printer", PRINTER_MIB)])
            m.respond(m.expect(P.REGISTER), error=ax.DUPLICATE_REGISTRATION)
            m.respond(m.expect(P.UNREGISTER), error=ax.UNKNOWN_REGISTRATION)   # already gone: fine
            self.assertTrue(self.subagent.wait_applied(generation, 2))
        text = "\n".join(logs.output)
        self.assertIn("duplicateRegistration (263)", text)
        self.assertIn("unknownRegistration (264)", text)
        self.assertFalse(self.subagent.ready.is_set())
        self.assertEqual(self.subagent.status, "session 7: 1/2 registrations accepted, retrying the rest")
        retry = m.expect(P.REGISTER)   # after the backoff
        self.assertEqual(retry.subtree, PRINTER_MIB)
        m.respond(retry)
        self.assertTrue(self.subagent.ready.wait(2))
        self.assertEqual(self.subagent.status, "session 7: all 2 registrations accepted")
        m.assert_silent(0.3)

    def test_a_refused_unregister_keeps_the_subtree_registered(self):
        m = self.open_session()
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            generation = self.subagent.update(registrations=[("printer", BROTHER, 100)])
            unreg = m.expect(P.UNREGISTER)
            self.assertEqual(unreg.subtree, SYSTEM)
            m.respond(unreg, error=267)   # requestDenied
            self.assertTrue(self.subagent.wait_applied(generation, 2))
        self.assertIn("stays registered until the session ends", "\n".join(logs.output))
        self.assertTrue(self.subagent.ready.is_set())
        # The master still has it, so listing it again sends nothing.
        generation = self.subagent.update(registrations=self.REGISTRATIONS)
        self.assertTrue(self.subagent.wait_applied(generation, 2))
        m.assert_silent(0.5)
        self.assertEqual(self.subagent.status, "session 7: all 2 registrations accepted")

    def test_an_update_while_disconnected_applies_to_the_next_session(self):
        self.start()   # no master yet: the subagent keeps retrying
        self.assertTrue(_wait_until(lambda: "waiting for AgentX master" in self.subagent.status, 3))
        generation = self.subagent.update(registrations=[("printer", BROTHER)])
        self.assertTrue(self.subagent.wait_applied(generation, 0), "nothing to wait for without a session")
        m = self.master()
        regs = m.accept(session_id=5, registrations=1)
        self.assertEqual([(r.subtree, r.priority) for r in regs], [(BROTHER, 127)])
        self.assertTrue(self.subagent.ready.wait(2))
        m.assert_silent(0.3)

    def test_one_pdu_is_answered_by_one_handler(self):
        new = ax.TableHandler({"printer": {SYS_DESCR: (T.OCTET_STRING, "new"),
                                           SYS_OBJECT_ID: (T.OCTET_STRING, "new"),
                                           SYS_UPTIME: (T.OCTET_STRING, "new")}})

        def swap_mid_request():
            self.subagent.update(new)   # as a reload on another thread would
            return 4242

        self.handler.set_table("printer", {SYS_DESCR: (T.OCTET_STRING, "old"),
                                           SYS_OBJECT_ID: (T.TIME_TICKS, swap_mid_request),
                                           SYS_UPTIME: (T.OCTET_STRING, "old")})
        m = self.open_session()
        reply = m.call(ax.Pdu(type=P.GETBULK, session_id=7, context="printer", max_repetitions=3,
                              ranges=[ax.SearchRange(SYSTEM, SYSTEM_END)]))
        self.assertEqual(reply.varbinds, [ax.VarBind(SYS_DESCR, T.OCTET_STRING, b"old"),
                                          ax.VarBind(SYS_OBJECT_ID, T.TIME_TICKS, 4242),
                                          ax.VarBind(SYS_UPTIME, T.OCTET_STRING, b"old")])
        self.assertEqual(self.get(m, 7, SYS_UPTIME)[0].value, b"new")


class SubagentTransportTest(unittest.TestCase):
    """The built-in unix and TCP connectors, and connection-failure handling."""

    def run_subagent(self, address: str):
        subagent = ax.Subagent(make_handler(), address, [("printer", BROTHER)], ping_interval=60.0,
                               backoff_min=0.05, backoff_max=0.1)
        stop = threading.Event()
        thread = threading.Thread(target=subagent.run_forever, args=(stop,), daemon=True)
        thread.start()

        def join():
            stop.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())

        self.addCleanup(join)
        return subagent, stop

    def serve_one_session(self, listener: socket.socket, address: str) -> None:
        listener.listen(1)
        listener.settimeout(5)
        subagent, stop = self.run_subagent(address)
        conn, _peer = listener.accept()
        self.addCleanup(conn.close)
        m = FakeMaster(conn)
        m.accept(session_id=3, registrations=1)
        self.assertTrue(subagent.ready.wait(2))
        reply = m.call(ax.Pdu(type=P.GET, session_id=3, context="printer", ranges=[ax.SearchRange(BR_STATUS)]))
        self.assertEqual(reply.varbinds, [ax.VarBind(BR_STATUS, T.OCTET_STRING, STATUS_BLOCK)])
        stop.set()
        m.respond(m.expect(P.CLOSE))

    def test_tcp(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        self.serve_one_session(listener, "tcp:127.0.0.1:%d" % listener.getsockname()[1])

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs unix-domain sockets")
    def test_unix(self):
        tmp = tempfile.mkdtemp(prefix="qlax-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "master")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        self.serve_one_session(listener, "unix:" + path)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs unix-domain sockets")
    def test_missing_master_is_retried_with_a_hint(self):
        tmp = tempfile.mkdtemp(prefix="qlax-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            subagent, _stop = self.run_subagent(os.path.join(tmp, "absent"))
            self.assertTrue(_wait_until(lambda: "waiting for AgentX master" in subagent.status, 3))
        self.assertIn("is snmpd running with 'master agentx'?", "\n".join(logs.output))
        self.assertFalse(subagent.ready.is_set())


class SubagentConfigTest(unittest.TestCase):
    def test_rejects_bad_configuration(self):
        handler = ax.TableHandler()
        regs = [("printer", BROTHER)]
        with self.assertRaises(TypeError):
            ax.Subagent(object(), registrations=regs)
        bad = [
            dict(registrations=[]),
            dict(registrations=regs, socket_path="tcp:localhost"),
            dict(registrations=regs, socket_path="tcp:localhost:99999"),
            dict(registrations=regs, socket_path=""),
            dict(registrations=[("printer", ())]),
            dict(registrations=[("printer", BROTHER, 0)]),
            dict(registrations=[("printer", BROTHER, 256)]),
            dict(registrations=[("printer",)]),
            dict(registrations=[ax.Registration("printer", BROTHER, range_subid=8, upper_bound=1)]),
            dict(registrations=[ax.Registration("printer", BROTHER, range_subid=7, upper_bound=1)]),
            dict(registrations=[ax.Registration("printer", BROTHER, upper_bound=5)]),
            dict(registrations=regs * 2),
            dict(registrations=regs, ping_interval=0),
            dict(registrations=regs, backoff_min=2, backoff_max=1),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ax.Subagent(handler, **kwargs)

    def test_update_validates_first_and_changes_nothing_on_error(self):
        handler = make_handler()
        sub = ax.Subagent(handler, "tcp:127.0.0.1:705", [("printer", BROTHER)], descr="before")
        bad = [dict(handler=object()), dict(registrations=[]), dict(registrations=[("printer", ())]),
               dict(registrations=[("printer", BROTHER)] * 2), dict(handler=make_handler(), descr=42),
               dict(handler=make_handler(), registrations=[("printer", BROTHER, 0)])]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                sub.update(**kwargs)
        self.assertIs(sub.handler, handler)
        self.assertEqual(sub.registrations, (ax.Registration("printer", BROTHER),))
        self.assertEqual(sub.update(registrations=[("printer", "1.3.6.1.4.1.2435")]), 0, "same set: no change")
        self.assertEqual(sub.update(registrations=[("printer", SYSTEM)]), 1)
        self.assertTrue(sub.wait_applied(1, 0), "without a session the next one registers the new set")
        self.assertEqual(sub.registrations, (ax.Registration("printer", SYSTEM),))

    def test_normalizes_registrations(self):
        sub = ax.Subagent(ax.TableHandler(), "tcp:[::1]:705",
                          [("printer", "1.3.6.1.4.1.2435", 100), (None, ".1.3.6.1.2.1.43")])
        self.assertEqual(sub.registrations, (ax.Registration("printer", BROTHER, 100),
                                             ax.Registration("", (1, 3, 6, 1, 2, 1, 43), 127)))
        self.assertEqual((sub.status, sub.session_id, sub.ready.is_set()), ("not started", None, False))
        ranged = ax.Registration("printer", (1, 3, 6, 1, 2, 1, 2, 2, 1, 1, 1), range_subid=10, upper_bound=22)
        self.assertEqual(ax.Subagent(ax.TableHandler(), "unix:/run/agentx", [ranged]).registrations, (ranged,))


# ---------------------------------------------------------------------------
# Real net-snmp master (Linux / CI)
# ---------------------------------------------------------------------------
def _which(name: str):
    extra = os.pathsep.join(("/usr/sbin", "/usr/local/sbin", "/sbin"))
    return shutil.which(name, path=os.environ.get("PATH", os.defpath) + os.pathsep + extra)


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _selinux_enforcing() -> bool:
    try:
        with open("/sys/fs/selinux/enforce", encoding="ascii") as fh:
            return fh.read().strip() == "1"
    except OSError:
        return False


class _SnmpdUnavailable(Exception):
    """The private snmpd could not be started or did not answer."""


class RealSnmpdTest(unittest.TestCase):
    """The subagent registered in context "printer" of a private snmpd (VACM + AgentX)."""

    SUBTREE = (1, 3, 6, 1, 4, 1, 2435, 99999)  # test-only arc
    PRINTER = "ql810wc-printer"  # v1/v2c community mapped to context "printer"
    WRITER = "ql810wc-writer"    # same context, with write access (reaches TestSet)
    HOST = "ql810wc-host"        # default context
    TOOLS = ("snmpd", "snmpget", "snmpgetnext", "snmpbulkget", "snmpwalk", "snmpset")

    @classmethod
    def setUpClass(cls):
        if sys.platform == "darwin":
            raise unittest.SkipTest("Apple's snmpd has no VACM; community->context mapping needs Linux net-snmp")
        if not hasattr(socket, "AF_UNIX"):
            raise unittest.SkipTest("needs unix-domain sockets")
        cls.tools = {}
        for name in cls.TOOLS:
            path = _which(name)
            if path is None:
                raise unittest.SkipTest(f"net-snmp tool {name!r} is not installed")
            cls.tools[name] = path
        cls.proc = None
        cls.output = None
        cls.tmp = tempfile.mkdtemp(prefix="qlax-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        for sub in ("clientconf", "persist"):
            os.mkdir(os.path.join(cls.tmp, sub))
        cls.env = dict(os.environ, HOME=cls.tmp, LC_ALL="C",
                       SNMPCONFPATH=os.path.join(cls.tmp, "clientconf"),
                       SNMP_PERSISTENT_DIR=os.path.join(cls.tmp, "persist"))
        usage = cls._run([cls.tools["snmpd"], "-C", "-H"])
        if not re.search(r"(?m)^\s+rocommunity\s", usage.stdout):
            raise unittest.SkipTest("this snmpd has no VACM ('rocommunity' is not a known directive)")
        if not re.search(r"(?mi)^\s+agentxsocket\s", usage.stdout):
            raise unittest.SkipTest("this snmpd has no AgentX master support")
        cls.port = _free_udp_port()
        cls.agentx_path = os.path.join(cls.tmp, "agentx.sock")
        cls.conf = os.path.join(cls.tmp, "snmpd.conf")
        cls.log_path = os.path.join(cls.tmp, "snmpd.log")
        with open(cls.conf, "w", encoding="utf-8") as fh:
            fh.write(cls._config())
        cls.addClassCleanup(cls._stop_snmpd)
        try:
            cls._start_snmpd()
        except _SnmpdUnavailable as exc:
            if not _is_root() or _selinux_enforcing():
                raise unittest.SkipTest(f"cannot run a private snmpd here: {exc}") from None
            raise AssertionError(str(exc)) from None
        handler = ax.TableHandler({"printer": cls._table()})
        cls.subagent = ax.Subagent(handler, cls.agentx_path, [("printer", cls.SUBTREE)],
                                   name="ql810wc-agentx-test", ping_interval=5.0, response_timeout=3.0,
                                   backoff_min=0.1, backoff_max=1.0)
        cls.stop = threading.Event()
        cls.thread = threading.Thread(target=cls.subagent.run_forever, args=(cls.stop,), daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls._stop_subagent)
        if not cls.subagent.ready.wait(20):
            raise AssertionError(f"subagent did not register: {cls.subagent.status}\n{cls._log_tail()}")

    @classmethod
    def _config(cls) -> str:
        return "\n".join([
            "# Private test instance written by tests/test_agentx.py",
            "master agentx",
            f"agentXSocket {cls.agentx_path}",
            "agentXTimeout 3",
            f"com2sec -Cn printer printerSec 127.0.0.1 {cls.PRINTER}",
            f"com2sec -Cn printer writerSec 127.0.0.1 {cls.WRITER}",
            f"com2sec hostSec 127.0.0.1 {cls.HOST}",
            "group printerGrp v1 printerSec",
            "group printerGrp v2c printerSec",
            "group writerGrp v2c writerSec",
            "group hostGrp v1 hostSec",
            "group hostGrp v2c hostSec",
            "view allView included .1",
            "access printerGrp printer any noauth exact allView none none",
            "access writerGrp printer any noauth exact allView allView none",
            'access hostGrp "" any noauth exact allView none none',
            "",
        ])

    @classmethod
    def _table(cls):
        s = cls.SUBTREE
        return {
            s + (1, 0): (T.INTEGER, -42),
            s + (2, 0): (T.OCTET_STRING, IEEE1284_ID),
            s + (3, 0): (T.OBJECT_IDENTIFIER, (1, 3, 6, 1, 4, 1, 2435, 2, 3, 9, 1)),
            s + (4, 0): (T.TIME_TICKS, 12345),
            s + (5, 0): (T.COUNTER64, 2 ** 40 + 7),
            s + (6, 0): (T.IP_ADDRESS, "192.0.2.1"),
            s + (7, 0): (T.GAUGE32, 2 ** 32 - 1),
            s + (8, 0): (T.OCTET_STRING, STATUS_BLOCK),
        }

    @classmethod
    def _run(cls, cmd, timeout: float = 30):
        return subprocess.run(cmd, env=cls.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False)

    @classmethod
    def _snmp(cls, tool, version, community, args, options=(), timeout=2, retries=1):
        return cls._run([cls.tools[tool], "-v", version, "-c", community, "-On", "-t", str(timeout),
                         "-r", str(retries), *options, f"127.0.0.1:{cls.port}", *args])

    @classmethod
    def _start_snmpd(cls) -> None:
        cls.output = open(os.path.join(cls.tmp, "snmpd.out"), "ab")
        cls.proc = subprocess.Popen(
            [cls.tools["snmpd"], "-f", "-C", "-c", cls.conf, "-Lf", cls.log_path, f"udp:127.0.0.1:{cls.port}"],
            env=cls.env, stdin=subprocess.DEVNULL, stdout=cls.output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                raise _SnmpdUnavailable(f"snmpd exited with status {cls.proc.returncode}: {cls._log_tail()}")
            probe = cls._snmp("snmpget", "2c", cls.HOST, [".1.3.6.1.2.1.1.3.0"], timeout=1, retries=0)
            if probe.returncode == 0 and "Timeticks" in probe.stdout:
                return
            time.sleep(0.2)
        raise _SnmpdUnavailable(f"snmpd did not answer on 127.0.0.1:{cls.port}: {cls._log_tail()}")

    @classmethod
    def _stop_snmpd(cls) -> None:
        proc, cls.proc = cls.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        if cls.output is not None:
            cls.output.close()
            cls.output = None

    @classmethod
    def _stop_subagent(cls) -> None:
        cls.stop.set()
        cls.thread.join(10)

    @classmethod
    def _log_tail(cls) -> str:
        tail = []
        for name in ("snmpd.log", "snmpd.out"):
            try:
                with open(os.path.join(cls.tmp, name), encoding="utf-8", errors="replace") as fh:
                    tail.append(f"--- {name} ---\n{fh.read()[-2000:]}")
            except OSError:
                continue
        return "\n".join(tail)

    def oid(self, *suffix: int) -> str:
        return "." + ax.format_oid(self.SUBTREE + suffix)

    def snmp(self, tool, version, community, args, options=()):
        return self._snmp(tool, version, community, args, options)

    def test_v2c_get_is_served_in_printer_context(self):
        o = self.oid
        out = self.snmp("snmpget", "2c", self.PRINTER, [o(i, 0) for i in range(1, 8)])
        self.assertEqual(out.returncode, 0, out.stdout)
        for line in (f"{o(1, 0)} = INTEGER: -42",
                     f'{o(2, 0)} = STRING: "{IEEE1284_ID.decode()}"',
                     f"{o(3, 0)} = OID: .1.3.6.1.4.1.2435.2.3.9.1",
                     f"{o(4, 0)} = Timeticks: (12345)",
                     f"{o(5, 0)} = Counter64: {2 ** 40 + 7}",
                     f"{o(6, 0)} = IpAddress: 192.0.2.1",
                     f"{o(7, 0)} = Gauge32: 4294967295"):
            self.assertIn(line, out.stdout)
        out = self.snmp("snmpget", "2c", self.PRINTER, [o(8, 0)])
        self.assertIn(f"{o(8, 0)} = Hex-STRING: 80 20 42 34 39 30 00", out.stdout)

    def test_v1_get_and_nosuchname(self):
        out = self.snmp("snmpget", "1", self.PRINTER, [self.oid(1, 0), self.oid(2, 0)])
        self.assertIn(f"{self.oid(1, 0)} = INTEGER: -42", out.stdout)
        self.assertIn(f'{self.oid(2, 0)} = STRING: "{IEEE1284_ID.decode()}"', out.stdout)
        out = self.snmp("snmpget", "1", self.PRINTER, [self.oid(99, 0)])
        self.assertIn("noSuchName", out.stdout)

    def test_getnext_walk_and_getbulk(self):
        # snmpwalk also prints the final endOfMibView ("No more variables...") line.
        persona = re.compile(r"(?m)^(\.1\.3\.6\.1\.4\.1\.2435\.99999\.[0-9.]+) = (?!No more variables)")
        out = self.snmp("snmpgetnext", "2c", self.PRINTER, [self.oid()])
        self.assertIn(f"{self.oid(1, 0)} = INTEGER: -42", out.stdout)
        out = self.snmp("snmpwalk", "2c", self.PRINTER, [self.oid()])
        self.assertEqual(persona.findall(out.stdout), [self.oid(i, 0) for i in range(1, 9)], out.stdout)
        out = self.snmp("snmpbulkget", "2c", self.PRINTER, [self.oid()], options=("-Cn0", "-Cr3"))
        self.assertEqual(persona.findall(out.stdout), [self.oid(i, 0) for i in range(1, 4)], out.stdout)

    def test_default_context_does_not_see_the_persona(self):
        out = self.snmp("snmpget", "2c", self.HOST, [self.oid(1, 0)])
        self.assertIn("No Such Object", out.stdout)
        out = self.snmp("snmpget", "2c", self.HOST, [".1.3.6.1.2.1.1.3.0"])  # the host itself is served
        self.assertIn("Timeticks", out.stdout)

    def test_printer_context_does_not_see_host_mibs(self):
        out = self.snmp("snmpget", "2c", self.PRINTER, [".1.3.6.1.2.1.1.1.0", ".1.3.6.1.2.1.25.1.1.0"])
        self.assertEqual(out.stdout.count("No Such"), 2, out.stdout)

    def test_set_is_refused_by_the_subagent(self):
        out = self.snmp("snmpset", "2c", self.WRITER, [self.oid(1, 0), "i", "5"])
        self.assertIn("notWritable", out.stdout)
        out = self.snmp("snmpget", "2c", self.PRINTER, [self.oid(1, 0)])  # session still healthy
        self.assertIn("INTEGER: -42", out.stdout)

    def test_zy_update_swaps_values_and_subtrees_in_the_same_session(self):
        """update() against net-snmp: Register, then Unregister, and no session close."""
        other = (1, 3, 6, 1, 4, 1, 2435, 99998)   # second test-only arc
        session = self.subagent.session_id
        original = self.subagent.handler

        def restore():
            generation = self.subagent.update(original, [("printer", self.SUBTREE)])
            self.assertTrue(self.subagent.wait_applied(generation, 10), self.subagent.status)

        self.addCleanup(restore)
        replacement = ax.TableHandler({"printer": {other + (1, 0): (T.INTEGER, 7),
                                                   self.SUBTREE + (1, 0): (T.INTEGER, 6)}})
        generation = self.subagent.update(replacement, [("printer", self.SUBTREE), ("printer", other)])
        self.assertTrue(self.subagent.wait_applied(generation, 10), self.subagent.status)
        out = self.snmp("snmpget", "1", self.PRINTER, [self.oid(1, 0), "." + ax.format_oid(other + (1, 0))])
        self.assertIn(f"{self.oid(1, 0)} = INTEGER: 6", out.stdout)
        self.assertIn(f".{ax.format_oid(other + (1, 0))} = INTEGER: 7", out.stdout)
        generation = self.subagent.update(registrations=[("printer", other)])
        self.assertTrue(self.subagent.wait_applied(generation, 10), self.subagent.status)
        out = self.snmp("snmpget", "1", self.PRINTER, [self.oid(1, 0)])
        self.assertIn("noSuchName", out.stdout, "the unregistered subtree is gone")
        out = self.snmp("snmpget", "1", self.PRINTER, ["." + ax.format_oid(other + (1, 0))])
        self.assertIn("INTEGER: 7", out.stdout)
        self.assertEqual(self.subagent.session_id, session, "the AgentX session stayed open")
        self.assertTrue(self.subagent.ready.is_set(), self.subagent.status)

    def test_zz_reregisters_after_snmpd_restart(self):
        cls = type(self)
        cls._stop_snmpd()
        self.assertTrue(_wait_until(lambda: not self.subagent.ready.is_set(), 10),
                        "subagent did not notice that snmpd stopped")
        cls._start_snmpd()
        self.assertTrue(self.subagent.ready.wait(20), self.subagent.status)
        out = self.snmp("snmpget", "2c", self.PRINTER, [self.oid(1, 0)])
        self.assertIn("INTEGER: -42", out.stdout)


if __name__ == "__main__":
    unittest.main()
