"""Tests for agent/ql810wc/statecache.py (atomic state.json writer/reader)."""
from __future__ import annotations

import json
import math
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time
import unittest

AGENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import ql810wc  # noqa: E402
from ql810wc import statecache  # noqa: E402
from ql810wc.statecache import StateCache  # noqa: E402

DK22251 = "802042343930040000003e0a0000230000000000000000000081000000000000"
# Tested unit (dev/notes/usb-ground-truth.md): the cover-open reply to ESC i S is an
# "error occurred" frame (type 02, err2 10) that still describes the DK-22251 roll; then
# the same with the roll removed (width 0, type 0, err1 clear, byte 25 kept at 0x81).
DK22251_COVER_OPEN = "802042343930040000103e0a0000230000000200000000000081000000000000"
ROLL_REMOVED = "8020423439300400001000000000230000000200000000000081000000000000"


class StateCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.cache = StateCache(self.path)

    def listing(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_round_trip(self):
        state = statecache.new_state(pid=4321, raw_port=9100, started=1763640000.0)
        state["status"]["raw_hex"] = DK22251
        before = time.time()
        self.cache.write(state)
        got = self.cache.read()
        self.assertEqual(got["status"]["raw_hex"], DK22251)
        self.assertEqual(got["proxy"], {"version": ql810wc.__version__, "pid": 4321,
                                        "started": 1763640000.0, "raw_port": 9100})
        self.assertEqual(got["schema"], 1)
        self.assertGreaterEqual(got["updated"], before)
        self.assertIsNone(state["updated"], "the caller's document is not modified")
        self.assertEqual(self.listing(), ["state.json"], "no temporary files left behind")

    def test_mode_is_0644_whatever_the_umask(self):
        old = os.umask(0o077)
        try:
            self.cache.write(statecache.new_state())
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_overwrite_replaces_the_file(self):
        self.cache.write({"job": {"bytes": 1}})
        first_inode = self.path.stat().st_ino
        self.cache.write({"job": {"bytes": 2}})
        self.assertEqual(self.cache.read()["job"], {"bytes": 2})
        self.assertNotEqual(self.path.stat().st_ino, first_inode, "replaced by rename, not rewritten")

    def test_bad_documents_leave_the_old_file(self):
        self.cache.write({"job": {"bytes": 7}})
        for bad in ({"x": object()}, {"x": float("nan")}, {"x": math.inf}, {"schema": 2}, {"schema": True}):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    self.cache.write(bad)
                self.assertEqual(self.cache.read()["job"], {"bytes": 7})
                self.assertEqual(self.listing(), ["state.json"])

    def test_missing_directory(self):
        cache = StateCache(self.dir / "missing" / "state.json")
        with self.assertRaises(OSError):
            cache.write(statecache.new_state())
        self.assertIsNone(cache.read())

    def test_read_never_raises(self):
        self.assertIsNone(self.cache.read(), "missing file")
        cases = {
            "corrupt": b"{not json",
            "array": b"[1, 2]",
            "wrong schema": b'{"schema": 2}',
            "bool schema": b'{"schema": true}',
            "no schema": b'{"job": {}}',
            "not utf-8": b'{"schema": 1, "x": "\xff"}',
            "deep": b"[" * 100000,
            "empty": b"",
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.path.write_bytes(data)
                self.assertIsNone(self.cache.read())
        self.path.write_bytes(b'{"schema": 1, "pad": "' + b"x" * statecache.MAX_STATE_BYTES + b'"}')
        self.assertIsNone(self.cache.read(), "oversized")
        self.assertIsNone(StateCache(self.dir).read(), "a directory")
        self.assertIsNone(StateCache(str(self.path) + "\0x").read(), "an invalid path")

    def test_readers_never_see_a_partial_document(self):
        self.cache.write({"seq": -1, "pad": "x" * 4000})
        stop = threading.Event()
        failures = []

        def reader():
            while not stop.is_set():
                doc = self.cache.read()
                if doc is None or len(doc["pad"]) != 4000:
                    failures.append(doc)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for seq in range(200):
                self.cache.write({"seq": seq, "pad": "x" * 4000})
        finally:
            stop.set()
            thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.cache.read()["seq"], 199)

    def test_default_path(self):
        self.assertEqual(StateCache().path, pathlib.Path("/run/ql810wc-proxy/state.json"))
        self.assertEqual(statecache.DEFAULT_PATH, "/run/ql810wc-proxy/state.json")


class NewStateTest(unittest.TestCase):
    def test_schema_1_skeleton(self):
        state = statecache.new_state()
        self.assertEqual(set(state), {"schema", "updated", "proxy", "device", "status", "job"})
        self.assertEqual(state["schema"], 1)
        self.assertEqual(set(state["proxy"]), {"version", "pid", "started", "raw_port"})
        self.assertEqual(set(state["device"]), {"state", "vid", "pid", "product", "manufacturer",
                                                "serial", "node", "transport", "ieee1284_id"})
        self.assertIn(state["device"]["state"], statecache.DEVICE_STATES)
        self.assertEqual(state["status"], {"raw_hex": None, "polled": None, "parsed": None})
        self.assertEqual(state["job"], {"active": False, "peer": None, "started": None, "bytes": 0,
                                        "two_color": None, "last_result": None, "last_finished": None})
        self.assertEqual(json.loads(json.dumps(state)), state)
        self.assertIsNot(statecache.new_state()["job"], state["job"], "fresh objects every call")


class HelpersTest(unittest.TestCase):
    def setUp(self):
        self.state = statecache.new_state()
        self.state.update(updated=1000.0)
        self.state["status"].update(polled=990.5, raw_hex=DK22251)

    def test_get_path(self):
        self.assertEqual(statecache.get_path(self.state, "status.polled"), 990.5)
        self.assertEqual(statecache.get_path(self.state, "device.state"), "absent")
        self.assertIsNone(statecache.get_path(self.state, "status.polled.x"))
        self.assertEqual(statecache.get_path(self.state, "nope", "d"), "d")
        self.assertIsNone(statecache.get_path(None, "updated"))

    def test_age_seconds(self):
        self.assertEqual(statecache.age_seconds(self.state, now=1010.0), 10.0)
        self.assertEqual(statecache.age_seconds(self.state, "status.polled", now=1000.0), 9.5)
        self.assertEqual(statecache.age_seconds(self.state, "updated", now=900.0), 0.0, "clock stepped back")
        self.assertIsNone(statecache.age_seconds(self.state, "job.started", now=1000.0))
        self.assertIsNone(statecache.age_seconds(None, now=1000.0))
        for bad in (True, "1000", float("nan"), math.inf, [1]):
            self.state["updated"] = bad
            self.assertIsNone(statecache.age_seconds(self.state, now=1000.0), repr(bad))
        self.state["updated"] = time.time() - 5
        self.assertAlmostEqual(statecache.age_seconds(self.state), 5, delta=2)

    def test_status_from_state(self):
        status = statecache.status_from_state(self.state)
        self.assertTrue(status.two_color)
        self.assertEqual(status.media_width_mm, 62)
        for bad in (None, "zz", DK22251[:-2], 42):
            self.state["status"]["raw_hex"] = bad
            self.assertIsNone(statecache.status_from_state(self.state), repr(bad))
        self.assertIsNone(statecache.status_from_state(None))

    def test_error_replies_round_trip(self):
        # Readers get the frame exactly as the printer sent it, error replies included.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = StateCache(pathlib.Path(tmp.name) / "state.json")
        for raw_hex, has_media, two_color in ((DK22251_COVER_OPEN, True, True), (ROLL_REMOVED, False, False)):
            with self.subTest(raw_hex=raw_hex):
                self.state["status"]["raw_hex"] = raw_hex
                cache.write(self.state)
                status = statecache.status_from_state(cache.read())
                self.assertEqual(status.raw.hex(), raw_hex)
                self.assertEqual(status.status_type, 0x02, "an 'error occurred' reply")
                self.assertEqual(status.errors(), ["cover open"])
                self.assertEqual((status.has_media, status.two_color), (has_media, two_color))


if __name__ == "__main__":
    unittest.main()
