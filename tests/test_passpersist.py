"""Tests for the pass_persist fallback (agent/ql810wc/passpersist.py) and its entry point.

* FormatTest: reply lines per type, text vs hex, snmpd's 4095-byte line limit.
* LineReaderTest: line splitting, CRLF, timeouts, end of input, garbage.
* ProtocolTest: a PassPersistServer over real pipes: PING/PONG, get, getnext
  bounded to the subtree of the request, SET refused with and without a value
  line, unknown commands, invalid OIDs, blank line and end of input.
* ChildTranscriptTest: brother_snmp_agent.py --mode pass_persist run as snmpd
  runs it (python3 -I -B on pipes): full transcripts, nothing ever on stderr,
  logs in --log-file, a broken OID map or config still answers.
* RealSnmpdPassPersistTest: net-snmp's snmpd runs the agent from two
  pass_persist lines, as scripts/30-snmp-agent.sh configures it (Linux
  net-snmp, or Apple's /usr/libexec/snmpd on macOS).

Self-contained on purpose: the fixtures below do not depend on other test modules.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple

REPO = pathlib.Path(__file__).resolve().parent.parent
AGENT_DIR = REPO / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from ql810wc import config, passpersist, persona, statecache  # noqa: E402
from ql810wc.agentx import VarBind, VarType, parse_oid  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the interpreter
    yaml = None

AGENT_SCRIPT = AGENT_DIR / "brother_snmp_agent.py"
SHIPPED_MAP = AGENT_DIR / "oid-map.yaml"

NOW = 1_763_650_000.0
DK22251 = "802042343930040000003e0a0000230000000000000000000081000000000000"   # tested QL-810Wc, idle
DK22251_SPACED = " ".join(DK22251[i:i + 2] for i in range(0, len(DK22251), 2))
IEEE1284 = ("MFG:Brother;CMD:PT-CBP,URF;MDL:QL-810W;CLS:PRINTER;CID:Brother LabelPrinter TypeE1;"
            "URF:V1.5,W8,CP1,IS1,MT1-5,OB10,PQ4-5,RS300,FN3-11-60-63;")
SERIAL = "Z0TEST000002"              # placeholder, not a real serial
MAC_TEXT = "00:00:5E:00:53:07"       # RFC 7042 documentation MAC
COMMUNITY = "ql810wc-pp-test"

STATUS = ".1.3.6.1.4.1.2435.3.3.9.1.6.1.0"
IEEE1284_OID = ".1.3.6.1.4.1.2435.2.3.9.1.1.7.0"
SERIAL_OID = ".1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.0"
NODE_OID = ".1.3.6.1.4.1.2435.2.4.3.1240.1.1.0"
HW_TYPE_OID = ".1.3.6.1.4.1.2435.2.4.3.1240.1.3.0"
PRT_SERIAL = ".1.3.6.1.2.1.43.5.1.1.17.1"
PRT_COVER = ".1.3.6.1.2.1.43.6.1.1.3.1.1"
PRT_CONSOLE = ".1.3.6.1.2.1.43.16.5.1.2.1.1"
SYS_NAME = ".1.3.6.1.2.1.1.5.0"
PRINTER_MIB = ".1.3.6.1.2.1.43"
BROTHER = ".1.3.6.1.4.1.2435"
#: A test-only arc for the types the shipped map does not use in pass_persist.
TYPE_OIDS = {"oid": ".1.3.6.1.4.1.2435.99.1.0", "timeticks": ".1.3.6.1.4.1.2435.99.2.0",
             "counter32": ".1.3.6.1.4.1.2435.99.3.0", "gauge32": ".1.3.6.1.4.1.2435.99.4.0",
             "ipaddress": ".1.3.6.1.4.1.2435.99.5.0"}


def _entry(oid: str, name: str, type_: str, source: Dict[str, Any]) -> Dict[str, Any]:
    return {"oid": oid.lstrip("."), "name": name, "type": type_, "source": source, "context": "printer",
            "verified": False, "notes": "test entry"}


TYPE_ENTRIES = [
    _entry(TYPE_OIDS["oid"], "testOid", "oid", {"const": "1.3.6.1.4.1.2435.2.3.9.1"}),
    _entry(TYPE_OIDS["timeticks"], "testTicks", "timeticks", {"const": 123456}),
    _entry(TYPE_OIDS["counter32"], "testCounter", "counter32", {"const": 4294967295}),
    _entry(TYPE_OIDS["gauge32"], "testGauge", "gauge32", {"const": 42}),
    _entry(TYPE_OIDS["ipaddress"], "testAddress", "ipaddress", {"const": "192.0.2.10"}),
]

#: Entries under 43 and 2435 (served by pass_persist) plus sysName (never served
#: there). brInfoSerialNumber has no default: absent while no serial is known.
PP_MAP_DOC: Dict[str, Any] = {"schema_version": 1, "model": "QL-810W", "validated_firmware": [], "entries": [
    _entry(SYS_NAME, "sysName", "string", {"provider": "sysname"}),
    _entry(PRT_SERIAL, "prtGeneralSerialNumber.1", "string", {"provider": "serial", "default": ""}),
    _entry(PRT_COVER, "prtCoverStatus.1.1", "integer", {"provider": "cover_status"}),
    _entry(PRT_CONSOLE, "prtConsoleDisplayBufferText.1.1", "string", {"provider": "console_text"}),
    _entry(IEEE1284_OID, "brieee1284id", "string",
           {"provider": "ieee1284_id", "default": "MFG:Brother;CMD:PT-CBP;MDL:QL-810W;CLS:PRINTER;"}),
    _entry(SERIAL_OID, "brInfoSerialNumber", "string", {"provider": "serial"}),
    _entry(NODE_OID, "brpsNodeName", "string", {"provider": "sysname"}),
    _entry(HW_TYPE_OID, "brpsHardwareType", "integer", {"const": 0}),
    _entry(STATUS, "brStatusBlock", "octets", {"provider": "status_block"}),
] + TYPE_ENTRIES}
PP_MAP = persona.OidMap.from_dict(PP_MAP_DOC)


def make_state(frame: Optional[str] = DK22251, *, now: float = NOW, serial: Optional[str] = SERIAL,
               device: str = "ready") -> Dict[str, Any]:
    """A fresh schema-1 state.json document as ql810wc-proxyd writes it."""
    doc = statecache.new_state(pid=4321, raw_port=9100, started=now - 3600)
    doc["updated"] = now - 1
    doc["device"].update(state=device, vid="04f9", pid="209c", product="QL-810W", manufacturer="Brother",
                         serial=serial, node="/dev/usb/lp0", transport="usblp", ieee1284_id=IEEE1284)
    if frame is not None:
        doc["status"] = {"raw_hex": frame, "polled": now - 1, "parsed": None}
    return doc


def write_json(path: str, doc: Any) -> None:
    """Replace ``path`` atomically, as ql810wc-proxyd does."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def short_tempdir(prefix: str) -> str:
    """A temporary directory with a short path."""
    base = "/tmp" if len(tempfile.gettempdir()) > 40 and os.path.isdir("/tmp") else None
    return tempfile.mkdtemp(prefix=prefix, dir=base)


class _Clock:
    def __init__(self) -> None:
        self.wall, self.mono = NOW, 1000.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


class _Net:
    def mac(self) -> Optional[bytes]:
        return bytes.fromhex(MAC_TEXT.replace(":", ""))


def make_tree(directory: str, state: Optional[Dict[str, Any]], subtrees=persona.PASS_PERSIST_SUBTREES,
              oid_map: persona.OidMap = PP_MAP) -> persona.PersonaTree:
    """A pass_persist PersonaTree over ``state`` (None: no state.json) with a frozen clock."""
    path = os.path.join(directory, "state.json")
    if state is not None:
        write_json(path, state)
    clock = _Clock()
    the_persona = persona.Persona(oid_map, config.load_runtime_config(None), path, clock=clock.time,
                                  monotonic=clock.monotonic, netinfo=_Net())
    return persona.PersonaTree(the_persona, context="", subtrees=subtrees)


# ---------------------------------------------------------------------------
# Formatting and line reading
# ---------------------------------------------------------------------------
def fake_entry(type_: str, name: str = "testEntry") -> persona.Entry:
    return persona.Entry(oid=(1, 3, 6, 1, 4, 1, 2435, 99, 9, 0), name=name, type=type_,
                         source=persona.Source("const", None))


def reply(type_: str, value: Any) -> List[str]:
    entry = fake_entry(type_)
    text = passpersist.format_reply(entry, VarBind(entry.oid, entry.vtype, value))
    assert text.endswith("\n"), text
    return text[:-1].split("\n")


class FormatTest(unittest.TestCase):
    OID = ".1.3.6.1.4.1.2435.99.9.0"

    def test_each_type(self):
        cases = [
            ("integer", -2, ["integer", "-2"]),
            ("octets", bytes.fromhex(DK22251), ["octet", DK22251_SPACED]),
            ("string", "READY           ", ["string", "READY           "]),
            ("string", "Büro 2", ["string", "Büro 2"]),
            ("oid", (1, 3, 6, 1, 4, 1, 2435, 2, 3, 9, 1), ["objectid", ".1.3.6.1.4.1.2435.2.3.9.1"]),
            ("oid", "1.3.6.1.2.1.25.3.1.5", ["objectid", ".1.3.6.1.2.1.25.3.1.5"]),
            ("timeticks", 2 ** 32 + 5, ["timeticks", "5"]),
            ("counter32", 2 ** 32 + 7, ["counter", "7"]),
            ("gauge32", 2 ** 40, ["gauge", "4294967295"]),
            ("ipaddress", "192.0.2.10", ["ipaddress", "192.0.2.10"]),
            ("string", "", ["octet", ""]),     # never an empty "string" line (net-snmp reads before its buffer)
            ("octets", b"", ["octet", ""]),
        ]
        for type_, value, expected in cases:
            with self.subTest(type=type_, value=value):
                self.assertEqual(reply(type_, value), [self.OID] + expected)

    def test_text_that_cannot_travel_as_a_string_is_sent_as_hex(self):
        self.assertEqual(reply("string", "two\nlines"), [self.OID, "octet", "74 77 6f 0a 6c 69 6e 65 73"])
        self.assertEqual(reply("string", b"\xff\xfe"), [self.OID, "octet", "ff fe"])
        self.assertEqual(reply("string", "nul\0"), [self.OID, "octet", "6e 75 6c 00"])
        self.assertEqual(reply("octets", "AB"), [self.OID, "octet", "41 42"])   # octets are always hex

    def test_line_limit(self):
        with self.assertLogs("ql810wc.passpersist", logging.WARNING):
            lines = reply("string", "a" * 5000)
        self.assertEqual(len(lines[2]), passpersist.MAX_STRING_BYTES)
        with self.assertLogs("ql810wc.passpersist", logging.WARNING):
            lines = reply("string", "é" * 3000)   # 6000 bytes: cut at a character boundary
        self.assertLessEqual(len(lines[2].encode("utf-8")), passpersist.MAX_STRING_BYTES)
        self.assertEqual(set(lines[2]), {"é"})
        with self.assertLogs("ql810wc.passpersist", logging.WARNING):
            lines = reply("octets", bytes(2000))
        self.assertEqual(len(lines[2].split()), passpersist.MAX_OCTET_BYTES)
        for line in lines:
            self.assertLessEqual(len(line.encode("utf-8")) + 1, passpersist.MAX_LINE)
        self.assertEqual((passpersist.MAX_STRING_BYTES, passpersist.MAX_OCTET_BYTES), (4094, 1365))

    def test_unknown_type(self):
        entry = fake_entry("counter64")
        with self.assertRaises(ValueError):
            passpersist.format_reply(entry, VarBind(entry.oid, VarType.COUNTER64, 1))


class LineReaderTest(unittest.TestCase):
    def setUp(self):
        self.r, self.w = os.pipe()
        self.addCleanup(self._close)
        self.reader = passpersist.LineReader(self.r)

    def _close(self):
        for fd in (self.r, self.w):
            try:
                os.close(fd)
            except OSError:
                pass

    def test_lines_crlf_partial_and_eof(self):
        os.write(self.w, b"a\nb\r\nc")
        self.assertEqual(self.reader.readline(), b"a")
        self.assertEqual(self.reader.readline(), b"b")
        started = time.monotonic()
        self.assertIsNone(self.reader.readline(timeout=0.2))   # "c" has no newline yet
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        self.assertFalse(self.reader.eof)
        os.write(self.w, b"\n\nd")
        self.assertEqual(self.reader.readline(), b"c")
        self.assertEqual(self.reader.readline(), b"")           # a blank line is a line
        os.close(self.w)
        self.assertIsNone(self.reader.readline())               # "d" without newline is discarded
        self.assertTrue(self.reader.eof)
        self.assertIsNone(self.reader.readline(timeout=0.1))

    def test_discard_partial_keeps_complete_lines(self):
        os.write(self.w, b"string ")
        self.assertIsNone(self.reader.readline(timeout=0.2))
        self.assertEqual(self.reader.discard_partial(), b"string ")
        os.write(self.w, b"PING\n")
        self.assertEqual(self.reader.readline(timeout=2), b"PING")
        os.write(self.w, b"a\nb")
        self.assertEqual(self.reader.readline(timeout=2), b"a")
        self.assertEqual(self.reader.discard_partial(), b"b")
        os.write(self.w, b"c\nd\ne")
        self.assertEqual(self.reader.readline(timeout=2), b"c")   # "d\ne" stays buffered
        self.assertEqual(self.reader.discard_partial(), b"")       # a complete line is kept
        self.assertEqual(self.reader.readline(timeout=2), b"d")
        self.assertEqual(self.reader.discard_partial(), b"e")
        self.assertIsNone(self.reader.readline(timeout=0.1))

    def test_overlong_garbage_is_dropped(self):
        reader = passpersist.LineReader(self.r, max_line=8)
        os.write(self.w, b"0123456789ABCDEF")
        with self.assertLogs("ql810wc.passpersist", logging.ERROR):
            self.assertIsNone(reader.readline(timeout=0.3))
        os.write(self.w, b"\nok\n")
        self.assertEqual(reader.readline(timeout=2), b"")
        self.assertEqual(reader.readline(timeout=2), b"ok")


# ---------------------------------------------------------------------------
# The protocol over pipes
# ---------------------------------------------------------------------------
class Session:
    """A PassPersistServer on a thread, driven like snmpd drives it."""

    def __init__(self, tree: Optional[persona.PersonaTree], **kwargs: Any) -> None:
        self.in_r, self.in_w = os.pipe()
        self.out_r, self.out_w = os.pipe()
        self.server = passpersist.PassPersistServer(tree, self.in_r, self.out_w, **kwargs)
        self.result: List[int] = []
        self.thread = threading.Thread(target=lambda: self.result.append(self.server.serve()), daemon=True)
        self.thread.start()
        self.reader = passpersist.LineReader(self.out_r)

    def send(self, *lines: str) -> None:
        os.write(self.in_w, "".join(line + "\n" for line in lines).encode("utf-8"))

    def line(self, timeout: float = 5.0) -> str:
        data = self.reader.readline(timeout)
        if data is None:
            raise AssertionError("no reply from the pass_persist server")
        return data.decode("utf-8")

    def reply(self, timeout: float = 5.0) -> List[str]:
        first = self.line(timeout)
        return [first, self.line(), self.line()] if first.startswith(".") else [first]

    def ask(self, *lines: str) -> List[str]:
        self.send(*lines)
        return self.reply()

    def _close(self, name: str) -> None:
        """Close one pipe end once: a second os.close() of the number could hit
        a descriptor that something else has opened since."""
        fd = getattr(self, name)
        if fd >= 0:
            setattr(self, name, -1)
            os.close(fd)

    def finish(self, timeout: float = 5.0) -> int:
        """Close the server's input and return its exit status."""
        self._close("in_w")
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise AssertionError("the pass_persist server did not exit")
        return self.result[0]

    def close(self) -> None:
        # Stop the server before its descriptors go. Closed under a thread that
        # still waits on them, their numbers are free for the next test's
        # os.pipe(), and this server (with another tree) would answer that
        # test's requests.
        self._close("in_w")
        self.thread.join(5.0)
        for name in ("in_r", "out_r", "out_w"):
            self._close(name)


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qlpp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def session(self, state: Optional[Dict[str, Any]] = None, *, tree: Any = "default", **kwargs: Any) -> Session:
        if tree == "default":
            tree = make_tree(self.tmp, make_state() if state is None else state)
        s = Session(tree, **kwargs)
        self.addCleanup(s.close)
        return s

    def test_ping_get_and_types(self):
        s = self.session()
        self.assertEqual(s.ask("PING"), ["PONG"])
        self.assertEqual(s.ask("get", STATUS), [STATUS, "octet", DK22251_SPACED])
        self.assertEqual(s.ask("get", IEEE1284_OID), [IEEE1284_OID, "string", IEEE1284])
        self.assertEqual(s.ask("get", PRT_COVER), [PRT_COVER, "integer", "4"])
        self.assertEqual(s.ask("get", PRT_CONSOLE), [PRT_CONSOLE, "string", "READY           "])
        self.assertEqual(s.ask("get", TYPE_OIDS["oid"]), [TYPE_OIDS["oid"], "objectid", ".1.3.6.1.4.1.2435.2.3.9.1"])
        self.assertEqual(s.ask("get", TYPE_OIDS["ipaddress"]), [TYPE_OIDS["ipaddress"], "ipaddress", "192.0.2.10"])
        self.assertEqual(s.finish(), 0)

    def test_get_of_absent_or_unserved_oids(self):
        s = self.session(make_state(serial=None))
        self.assertEqual(s.ask("get", SERIAL_OID), ["NONE"])            # no serial known, no default
        self.assertEqual(s.ask("get", PRT_SERIAL), [PRT_SERIAL, "octet", ""])    # the empty default
        self.assertEqual(s.ask("get", ".1.3.6.1.4.1.2435.3.3.9.1.6.3.0"), ["NONE"])
        self.assertEqual(s.ask("get", SYS_NAME), ["NONE"])               # outside the served subtrees
        self.assertEqual(s.ask("get", STATUS + ".0"), ["NONE"])

    def test_getnext_stays_in_the_subtree_of_the_request(self):
        s = self.session()
        self.assertEqual(s.ask("getnext", PRINTER_MIB)[0], PRT_SERIAL)       # snmpd sends the root
        self.assertEqual(s.ask("getnext", PRT_SERIAL)[0], PRT_COVER)
        self.assertEqual(s.ask("getnext", PRT_CONSOLE), ["NONE"])          # last 43 entry: no jump to 2435
        self.assertEqual(s.ask("getnext", BROTHER)[0], IEEE1284_OID)
        self.assertEqual(s.ask("getnext", ".1.3.6.1.4.1.2435.3.3.9.1.6")[0], STATUS)
        self.assertEqual(s.ask("getnext", TYPE_OIDS["ipaddress"]), ["NONE"])
        self.assertEqual(s.ask("getnext", ".1.3.6.1.4.1")[0], IEEE1284_OID)  # registered above us

    def test_getnext_skips_absent_values(self):
        s = self.session(make_state(serial=None))
        self.assertEqual(s.ask("getnext", IEEE1284_OID)[0], NODE_OID)

    def test_requests_outside_the_served_subtrees(self):
        s = self.session()
        with self.assertLogs("ql810wc.passpersist", logging.WARNING) as logs:
            self.assertEqual(s.ask("getnext", ".1.3.6.1.2.1.1"), ["NONE"])
            self.assertEqual(s.ask("getnext", ".1.3.6.1.2.1.1.5.0"), ["NONE"])
        self.assertEqual(len([m for m in logs.output if "outside the served subtrees" in m]), 1)

    def test_narrowed_subtrees(self):
        tree = make_tree(self.tmp, make_state(), subtrees=[parse_oid(BROTHER)])
        s = self.session(tree=tree)
        self.assertEqual(s.ask("get", PRT_COVER), ["NONE"])
        self.assertEqual(s.ask("get", STATUS)[1], "octet")

    def test_set_is_refused_and_the_pipe_stays_in_sync(self):
        s = self.session()
        with self.assertLogs("ql810wc.passpersist", logging.INFO) as logs:
            self.assertEqual(s.ask("set", STATUS, 'octet "00 00"'), ["not-writable"])
            self.assertEqual(s.ask("PING"), ["PONG"])
            # A value type snmpd cannot format leaves the value line out entirely.
            started = time.monotonic()
            self.assertEqual(s.ask("set", PRT_COVER), ["not-writable"])
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertEqual(s.ask("PING"), ["PONG"])
            self.assertEqual(s.ask("get", PRT_COVER), [PRT_COVER, "integer", "4"])
            # net-snmp 5.6 (macOS) cuts the value line short, without a newline.
            os.write(s.in_w, f"set\n{STATUS}\nstring ".encode())
            self.assertEqual(s.reply(), ["not-writable"])
            self.assertEqual(s.ask("PING"), ["PONG"])
            self.assertEqual(s.ask("get", PRT_COVER), [PRT_COVER, "integer", "4"])
        self.assertTrue(any("refusing SET" in m for m in logs.output))

    def test_odd_input(self):
        s = self.session()
        with self.assertLogs("ql810wc.passpersist", logging.WARNING):
            self.assertEqual(s.ask("frobnicate"), ["NONE"])
            self.assertEqual(s.ask("get", "1.3.x"), ["NONE"])
        self.assertEqual(s.ask("GET", STATUS.lstrip("."))[0], STATUS)     # case and leading dot
        os.write(s.in_w, b"get\r\n" + STATUS.encode() + b"\r\n")         # CRLF
        self.assertEqual(s.reply()[0], STATUS)
        self.assertEqual(s.ask("PING"), ["PONG"])

    def test_blank_line_and_end_of_input_end_the_session(self):
        s = self.session()
        self.assertEqual(s.ask("PING"), ["PONG"])
        s.send("")
        s.thread.join(5)
        self.assertEqual(s.result, [0])
        t = self.session()
        t.send("get")              # end of input in the middle of a request
        self.assertEqual(t.finish(), 0)

    def test_without_an_oid_map_everything_is_none(self):
        s = self.session(tree=None)
        self.assertEqual(s.ask("PING"), ["PONG"])
        self.assertEqual(s.ask("get", STATUS), ["NONE"])
        self.assertEqual(s.ask("getnext", BROTHER), ["NONE"])
        self.assertEqual(s.ask("set", STATUS, 'string "x"'), ["not-writable"])
        self.assertEqual(s.finish(), 0)

    def test_a_failing_request_is_answered_none(self):
        s = self.session()
        original = passpersist.format_reply

        def broken(entry, vb):
            if entry.name == "prtCoverStatus.1.1":
                raise RuntimeError("formatter bug")
            return original(entry, vb)

        passpersist.format_reply = broken
        self.addCleanup(setattr, passpersist, "format_reply", original)
        with self.assertLogs("ql810wc.passpersist", logging.ERROR):
            self.assertEqual(s.ask("get", PRT_COVER), ["NONE"])
        self.assertEqual(s.ask("get", STATUS)[1], "octet")

    def test_status_block_without_state(self):
        s = self.session(tree=make_tree(self.tmp, None))
        oid, kind, value = s.ask("get", STATUS)
        self.assertEqual(bytes.fromhex(value.replace(" ", "")), persona.offline_block().raw)

    def test_the_server_writes_nothing_unasked(self):
        s = self.session()
        self.assertEqual(s.ask("PING"), ["PONG"])
        self.assertIsNone(s.reader.readline(timeout=0.3))


# ---------------------------------------------------------------------------
# The entry point as snmpd runs it
# ---------------------------------------------------------------------------
def child_map(directory: str, *, shipped: bool) -> Tuple[str, persona.OidMap]:
    """An OID map a ``python3 -I`` child can load, and the map it will serve.

    ``shipped``: the shipped map plus the test-only type entries, as
    oid-map.yaml with an identical oid-map.json sibling (read when the child
    has no PyYAML). Without PyYAML here, or with ``shipped=False``: the test map.
    """
    if shipped and yaml is not None:
        doc = yaml.safe_load(SHIPPED_MAP.read_text(encoding="utf-8"))
        doc["entries"] = list(doc["entries"]) + TYPE_ENTRIES
        path = os.path.join(directory, "oid-map.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False)
        with open(os.path.join(directory, "oid-map.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path, persona.OidMap.from_dict(doc)
    path = os.path.join(directory, "oid-map.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(PP_MAP_DOC, fh)
    return path, PP_MAP


def write_runtime(directory: str) -> Tuple[str, str]:
    """runtime.env with generous status limits (the fixture stays fresh) and identity.env."""
    runtime = os.path.join(directory, "runtime.env")
    with open(runtime, "w", encoding="utf-8") as fh:
        fh.write(f'STATUS_POLL_SECONDS="60"\nSTATUS_MAX_AGE_SECONDS="600"\nPERSONA_MAC="{MAC_TEXT}"\n'
                 'PERSONA_LOCATION="Label bench"\n')
    identity = os.path.join(directory, "identity.env")
    with open(identity, "w", encoding="utf-8") as fh:
        fh.write('PRINTER_UUID="e3248000-80ce-11db-8000-00005e005307"\n')
    return runtime, identity


class ChildTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = short_tempdir("qlppc")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.map_path, self.oid_map = child_map(self.tmp, shipped=False)
        self.runtime, self.identity = write_runtime(self.tmp)
        self.state = os.path.join(self.tmp, "state.json")
        write_json(self.state, make_state(now=time.time()))
        self.log = os.path.join(self.tmp, "pp.log")

    def spawn(self, *extra: str, **overrides: str) -> subprocess.Popen:
        options = {"--config": self.runtime, "--identity": self.identity, "--state": self.state,
                   "--oid-map": self.map_path, "--log-file": self.log}
        options.update(overrides)
        args = [item for pair in options.items() for item in pair]
        proc = subprocess.Popen([sys.executable, "-I", "-B", str(AGENT_SCRIPT), "--mode", "pass_persist",
                                 *args, *extra],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    def converse(self, proc: subprocess.Popen, *requests: Tuple[str, ...]) -> List[List[str]]:
        """Send each request, read its reply (one or three lines)."""
        reader = passpersist.LineReader(proc.stdout.fileno())
        replies = []
        for request in requests:
            os.write(proc.stdin.fileno(), "".join(line + "\n" for line in request).encode("utf-8"))
            first = reader.readline(30)   # the first reply includes interpreter start-up
            self.assertIsNotNone(first, f"no reply to {request}: {self.log_text()}")
            lines = [first.decode()]
            if lines[0].startswith("."):
                lines += [reader.readline(5).decode(), reader.readline(5).decode()]
            replies.append(lines)
        return replies

    def finish(self, proc: subprocess.Popen) -> Tuple[int, bytes]:
        proc.stdin.close()
        status = proc.wait(15)
        return status, proc.stderr.read()

    def log_text(self) -> str:
        try:
            with open(self.log, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def test_transcript(self):
        proc = self.spawn()
        replies = self.converse(proc, ("PING",), ("get", STATUS), ("PING",), ("getnext", PRINTER_MIB),
                                ("get", ".1.3.6.1.4.1.2435.9.9.9.0"), ("set", STATUS, 'string "x"'),
                                ("getnext", PRT_CONSOLE), ("get", IEEE1284_OID))
        self.assertEqual(replies, [
            ["PONG"], [STATUS, "octet", DK22251_SPACED], ["PONG"], [PRT_SERIAL, "string", SERIAL], ["NONE"],
            ["not-writable"], ["NONE"], [IEEE1284_OID, "string", IEEE1284]])
        status, stderr = self.finish(proc)
        self.assertEqual((status, stderr), (0, b""))
        log_text = self.log_text()
        self.assertIn("serving .1.3.6.1.2.1.43, .1.3.6.1.4.1.2435", log_text)
        self.assertIn("refusing SET", log_text)
        self.assertNotIn("Traceback", log_text)

    def test_blank_line_ends_the_session(self):
        proc = self.spawn()
        self.assertEqual(self.converse(proc, ("PING",)), [["PONG"]])
        os.write(proc.stdin.fileno(), b"\n")
        self.assertEqual(proc.wait(15), 0)

    def test_subtree_arguments_narrow_what_is_served(self):
        proc = self.spawn(BROTHER)
        replies = self.converse(proc, ("get", PRT_COVER), ("get", STATUS), ("getnext", PRINTER_MIB))
        self.assertEqual(replies[0], ["NONE"])
        self.assertEqual(replies[1][:2], [STATUS, "octet"])
        self.assertEqual(replies[2], ["NONE"])
        self.assertEqual(self.finish(proc), (0, b""))

    def test_broken_oid_map_still_answers(self):
        broken = os.path.join(self.tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write('{"schema_version": 2}')
        proc = self.spawn(**{"--oid-map": broken})
        self.assertEqual(self.converse(proc, ("PING",), ("get", STATUS), ("getnext", BROTHER)),
                         [["PONG"], ["NONE"], ["NONE"]])
        self.assertEqual(self.finish(proc), (0, b""))
        self.assertIn("answering NONE to every request", self.log_text())

    def test_missing_config_falls_back_to_defaults(self):
        proc = self.spawn(**{"--config": os.path.join(self.tmp, "missing.env")})
        replies = self.converse(proc, ("PING",), ("get", STATUS))
        self.assertEqual(replies[0], ["PONG"])
        self.assertEqual(replies[1][:2], [STATUS, "octet"])
        self.assertEqual(self.finish(proc), (0, b""))
        self.assertIn("serving with built-in defaults", self.log_text())

    def test_usage_error_writes_nothing_to_the_pipes(self):
        proc = self.spawn("--no-such-option")
        status, stderr = self.finish(proc)
        self.assertEqual((status, stderr, proc.stdout.read()), (64, b"", b""))
        self.assertIn("usage", self.log_text())


# ---------------------------------------------------------------------------
# Real net-snmp
# ---------------------------------------------------------------------------
def _which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for folder in ("/usr/sbin", "/usr/bin", "/usr/local/sbin", "/sbin"):
        candidate = os.path.join(folder, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_SNMP_LINE = re.compile(r"(?m)^(\.1\.3\.6\.1(?:\.\d+)+) = (.*)$")


def snmp_values(output: str) -> Dict[str, str]:
    """``{oid: value}`` from numeric net-snmp output. Wrapped hex lines are joined; a walk's
    closing "No more variables" line does not replace the last value."""
    values: Dict[str, str] = {}
    current = None
    for line in output.splitlines():
        match = _SNMP_LINE.match(line)
        if match:
            if match.group(1) in values and match.group(2).startswith("No more variables"):
                current = None
                continue
            current = match.group(1)
            values[current] = match.group(2)
        elif current is not None and re.fullmatch(r"[0-9A-Fa-f ]+", line.strip() or "-"):
            values[current] += " " + line.strip()
        else:
            current = None
    return values


class RealSnmpdPassPersistTest(unittest.TestCase):
    """snmpd runs the agent from the same two pass_persist lines scripts/30-snmp-agent.sh writes."""

    @classmethod
    def setUpClass(cls):
        if sys.platform == "darwin":
            snmpd = "/usr/libexec/snmpd" if os.access("/usr/libexec/snmpd", os.X_OK) else None  # not the wrapper
        else:
            snmpd = _which("snmpd")
        cls.tools = {"snmpd": snmpd}
        for name in ("snmpget", "snmpgetnext", "snmpwalk", "snmpset"):
            cls.tools[name] = _which(name)
        if None in cls.tools.values():
            raise unittest.SkipTest("net-snmp (snmpd and its client tools) is not installed")
        if " " in sys.executable or " " in str(AGENT_SCRIPT):
            raise unittest.SkipTest("pass_persist splits its command line on spaces")
        cls.tmp = short_tempdir("qlpps")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.map_path, cls.oid_map = child_map(cls.tmp, shipped=True)
        runtime, identity = write_runtime(cls.tmp)
        state = os.path.join(cls.tmp, "state.json")
        write_json(state, make_state(now=time.time()))
        cls.pp_log = os.path.join(cls.tmp, "pp.log")
        command = (f"{sys.executable} -I -B {AGENT_SCRIPT} --mode pass_persist --config {runtime} "
                   f"--identity {identity} --state {state} --oid-map {cls.map_path} --log-file {cls.pp_log}")
        usage = subprocess.run([snmpd, "-C", "-H"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=30,
                               check=False)
        lines = ["# Private test instance written by tests/test_passpersist.py"]
        if re.search(r"(?m)^\s+rocommunity\s", usage.stdout):   # VACM build (Linux); Apple's has none
            lines.append(f"rwcommunity {COMMUNITY} 127.0.0.1")
        lines += [f"pass_persist {BROTHER} {command}", f"pass_persist {PRINTER_MIB} {command}", ""]
        for sub in ("clientconf", "persist"):
            os.makedirs(os.path.join(cls.tmp, sub), exist_ok=True)
        cls.conf = os.path.join(cls.tmp, "snmpd.conf")
        with open(cls.conf, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        cls.env = dict(os.environ, HOME=cls.tmp, LC_ALL="C", SNMPCONFPATH=os.path.join(cls.tmp, "clientconf"),
                       SNMP_PERSISTENT_DIR=os.path.join(cls.tmp, "persist"))
        cls.port = _free_udp_port()
        cls.out = open(os.path.join(cls.tmp, "snmpd.out"), "ab")
        cls.addClassCleanup(cls.out.close)
        cls.proc = subprocess.Popen([snmpd, "-f", "-C", "-c", cls.conf, "-Lf", os.path.join(cls.tmp, "snmpd.log"),
                                     f"udp:127.0.0.1:{cls.port}"],
                                    env=cls.env, stdin=subprocess.DEVNULL, stdout=cls.out, stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls._stop)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                break
            probe = cls.snmp("snmpget", "2c", ".1.3.6.1.2.1.1.3.0", timeout=1, retries=0)
            if "Timeticks" in probe.stdout:
                return
            time.sleep(0.2)
        problem = f"snmpd did not answer on 127.0.0.1:{cls.port}: {cls.tail()}"
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        if not is_root or sys.platform == "darwin":
            raise unittest.SkipTest(f"cannot run a private snmpd here: {problem}")
        raise AssertionError(problem)

    @classmethod
    def _stop(cls):
        if cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(5)
        pgrep = _which("pgrep")
        if pgrep is not None:   # snmpd kills its pass_persist children; make sure nothing of ours survives
            out = subprocess.run([pgrep, "-f", cls.tmp], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 encoding="ascii", errors="replace", timeout=30, check=False)
            for pid in out.stdout.split():
                if pid.isdigit() and int(pid) != os.getpid():
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except OSError:
                        pass

    @classmethod
    def tail(cls) -> str:
        parts = []
        for name in ("snmpd.log", "snmpd.out", "pp.log"):
            try:
                with open(os.path.join(cls.tmp, name), encoding="utf-8", errors="replace") as fh:
                    parts.append(f"--- {name} ---\n{fh.read()[-3000:]}")
            except OSError:
                continue
        return "\n".join(parts)

    @classmethod
    def snmp(cls, tool: str, version: str, *args: str, timeout: float = 5,
             retries: int = 1) -> subprocess.CompletedProcess:
        return subprocess.run([cls.tools[tool], "-v", version, "-c", COMMUNITY, "-On", "-t", str(timeout),
                               "-r", str(retries), f"127.0.0.1:{cls.port}", *args],
                              env=cls.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=60,
                              check=False)

    def under(self, root: str) -> List[str]:
        return ["." + e.dotted for e in self.oid_map.within([parse_oid(root)])]

    def test_status_block_v1_and_v2c(self):
        for version in ("1", "2c"):
            with self.subTest(version=version):
                out = self.snmp("snmpget", version, STATUS)
                value = snmp_values(out.stdout).get(STATUS, "")
                self.assertTrue(value.startswith("Hex-STRING:"), out.stdout + self.tail())
                self.assertEqual(bytes.fromhex(value.split(":", 1)[1].replace(" ", "")), bytes.fromhex(DK22251))

    def test_ieee1284_id(self):
        out = self.snmp("snmpget", "1", IEEE1284_OID)
        self.assertIn('STRING: "MFG:Brother;CMD:PT-CBP,URF;MDL:QL-810W;', out.stdout)

    def test_walks_stay_in_their_subtree(self):
        for root in (PRINTER_MIB, BROTHER):
            with self.subTest(root=root):
                out = self.snmp("snmpwalk", "2c", root)
                self.assertEqual(list(snmp_values(out.stdout)), self.under(root), out.stdout + self.tail())

    def test_types_through_net_snmp(self):
        out = self.snmp("snmpget", "2c", *TYPE_OIDS.values())
        values = snmp_values(out.stdout)
        self.assertEqual(values[TYPE_OIDS["oid"]], "OID: .1.3.6.1.4.1.2435.2.3.9.1")
        self.assertEqual(values[TYPE_OIDS["timeticks"]].split(")")[0], "Timeticks: (123456")
        self.assertEqual(values[TYPE_OIDS["counter32"]], "Counter32: 4294967295")
        self.assertEqual(values[TYPE_OIDS["gauge32"]], "Gauge32: 42")
        self.assertEqual(values[TYPE_OIDS["ipaddress"]], "IpAddress: 192.0.2.10")

    def test_multi_varbind_v1_get(self):
        """Brother's discovery bundles mix OIDs in one SNMPv1 GET; snmpd PINGs us per varbind."""
        wanted = [oid for oid in (IEEE1284_OID, SERIAL_OID, HW_TYPE_OID, STATUS)
                  if self.oid_map.get(oid) is not None]
        out = self.snmp("snmpget", "1", *wanted)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertEqual(sorted(snmp_values(out.stdout)), sorted(wanted))

    def test_missing_objects(self):
        out = self.snmp("snmpget", "1", ".1.3.6.1.4.1.2435.9.9.9.0")
        self.assertIn("noSuchName", out.stdout)
        out = self.snmp("snmpget", "2c", ".1.3.6.1.4.1.2435.9.9.9.0")
        self.assertIn("No Such Instance", out.stdout)

    def test_set_is_refused(self):
        out = self.snmp("snmpset", "2c", STATUS, "s", "x")
        self.assertIn("notWritable", out.stdout)
        self.assertIn("Hex-STRING", self.snmp("snmpget", "2c", STATUS).stdout)   # still in sync


if __name__ == "__main__":
    unittest.main()
