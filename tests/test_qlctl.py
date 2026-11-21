"""Tests for agent/qlctl, run as a subprocess the way operators run it."""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Optional

from tests.fake_printer import (FAKE_SERIAL, IDLE_DK11201, IDLE_DK22205, IDLE_DK22251, RESYNC_REQUEST,
                                TEST_DEVICE_ID, PrinterModel, PtyPrinter)

from ql810wc import media, rasterproto, statecache  # noqa: E402  (path set by fake_printer)
from ql810wc.rasterproto import STATUS_REQUEST, Status  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
QLCTL = REPO / "agent" / "qlctl"
HAVE_PTY = os.name == "posix"


def state_doc(frame: Optional[bytes] = IDLE_DK22251, *, device_state: str = "ready",
              polled_ago: float = 1.0, pid: Optional[int] = None, job: Optional[Dict[str, Any]] = None
              ) -> Dict[str, Any]:
    """A state.json document as ql810wc-proxyd writes it."""
    now = time.time()
    doc = statecache.new_state(pid=pid, raw_port=9100, started=now - 3600)
    doc["device"].update(state=device_state, vid="04f9", pid="209c", product="QL-810W", manufacturer="Brother",
                         serial=FAKE_SERIAL, node="/dev/usb/lp0", transport="usblp", ieee1284_id=TEST_DEVICE_ID,
                         detail="no Brother QL printer found on USB" if device_state == "absent" else "ok")
    if frame is not None:
        status = Status(frame)
        doc["status"] = {"raw_hex": frame.hex(), "polled": now - polled_ago, "parsed": status.to_dict(),
                         "source": "reply"}
    if job:
        doc["job"].update(job)
    return doc


class QlctlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.config = os.path.join(self.dir, "runtime.env")
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write('STATUS_MAX_AGE_SECONDS="60"\n')
        self.state = os.path.join(self.dir, "state.json")

    def write_state(self, doc: Dict[str, Any]) -> None:
        statecache.StateCache(self.state).write(doc)

    def qlctl(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", "-B", str(QLCTL), "--config", self.config,
                               "--state", self.state, *args],
                              capture_output=True, text=True, timeout=timeout)


class VersionAndStatusTest(QlctlTestCase):
    def test_version(self):
        done = self.qlctl("version")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "qlctl (ql810wc-print-proxy) 0.1.0")

    def test_status_ready(self):
        self.write_state(state_doc(pid=os.getpid(), job={"last_result": "ok", "peer": "192.0.2.10:50000",
                                                          "bytes": 1234, "two_color": True,
                                                          "last_finished": time.time() - 90}))
        done = self.qlctl("status")
        self.assertEqual(done.returncode, 0, done.stderr)
        out = done.stdout
        self.assertIn("Printer   ready: Brother QL-810W (04f9:209c via usblp /dev/usb/lp0)", out)
        self.assertIn("62mm continuous black/red (DK-22251)  [two-color]", out)
        self.assertIn("Errors    none", out)
        self.assertIn("Job       last: ok, from 192.0.2.10:50000, 1234 bytes, two-color", out)
        self.assertNotIn("STALE", out)

    def test_status_shows_why_the_last_job_failed(self):
        detail = ("a black-only job, but the loaded roll, 62mm continuous black/red (DK-22251), "
                  "takes two-color jobs only")
        self.write_state(state_doc(pid=os.getpid(), job={"last_result": "refused", "peer": "192.0.2.10:50001",
                                                          "bytes": 10945, "two_color": False,
                                                          "last_finished": time.time() - 30,
                                                          "last_detail": detail}))
        done = self.qlctl("status")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Job       last: refused, from 192.0.2.10:50001, 10945 bytes, mono", done.stdout)
        self.assertIn(f"\n          {detail}\n", done.stdout)

    def test_status_json(self):
        self.write_state(state_doc())
        done = self.qlctl("status", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertTrue(report["available"])
        self.assertTrue(report["summary"]["ok"])
        self.assertEqual(report["summary"]["media"]["label_id"], "62red")
        self.assertEqual(report["summary"]["media"]["dk_parts"], ["DK-22251"])
        self.assertEqual(report["state"]["schema"], 1)

    def test_status_json_masks_the_serial(self):
        self.write_state(state_doc())
        done = self.qlctl("status", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["state"]["device"]["serial"], FAKE_SERIAL[:3] + "***")
        self.assertNotIn(FAKE_SERIAL, done.stdout)
        done = self.qlctl("status", "--json", "--show-serial")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["state"]["device"]["serial"], FAKE_SERIAL)
        self.assertEqual(statecache.StateCache(self.state).read()["device"]["serial"], FAKE_SERIAL,
                         "state.json itself is left alone")

    def test_status_problems(self):
        self.write_state(state_doc(device_state="absent"))
        done = self.qlctl("status")
        self.assertEqual(done.returncode, 3)
        self.assertIn("absent", done.stdout)
        self.assertIn("no Brother QL printer found on USB", done.stdout)
        self.write_state(state_doc(polled_ago=600))
        done = self.qlctl("status")
        self.assertEqual(done.returncode, 3)
        self.assertIn("[STALE]", done.stdout)
        cover_open = bytearray(IDLE_DK22251)
        cover_open[9], cover_open[18] = 0x10, 0x02
        self.write_state(state_doc(bytes(cover_open)))
        done = self.qlctl("status")
        self.assertEqual(done.returncode, 3)
        self.assertIn("Errors    cover open", done.stdout)

    def test_status_while_printing_is_not_stale(self):
        self.write_state(state_doc(polled_ago=600, job={"active": True, "peer": "192.0.2.10:50000",
                                                         "bytes": 4096}))
        done = self.qlctl("status")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("Job       printing: from 192.0.2.10:50000, 4096 bytes so far", done.stdout)

    def test_status_without_state(self):
        done = self.qlctl("status")
        self.assertEqual(done.returncode, 1)
        self.assertIn("is ql810wc-proxyd running?", done.stderr)
        done = self.qlctl("status", "--json")
        self.assertEqual(json.loads(done.stdout)["available"], False)

    def test_usage_error(self):
        self.assertEqual(self.qlctl().returncode, 2)


class FakeProxy:
    """Accepts one connection, reads it to EOF, optionally records a job
    result in state.json (as ql810wc-proxyd would), then closes."""

    def __init__(self, test: unittest.TestCase, state_path: Optional[str] = None) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test.addCleanup(self.sock.close)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(30)
        self.port = self.sock.getsockname()[1]
        self.state_path = state_path
        self.received: List[bytes] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            conn, address = self.sock.accept()
        except OSError:
            return
        with conn:
            data = bytearray()
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            self.received.append(bytes(data))
            if self.state_path:
                cache = statecache.StateCache(self.state_path)
                doc = cache.read()
                doc["job"].update(last_result="ok", last_finished=time.time(), bytes=len(data),
                                  peer=f"{address[0]}:{address[1]}")
                cache.write(doc)


class TestPrintTest(QlctlTestCase):
    def test_two_color_by_default(self):
        self.write_state(state_doc())
        proxy = FakeProxy(self, self.state)
        done = self.qlctl("test-print", "--port", str(proxy.port))
        self.assertEqual(done.returncode, 0, done.stderr)
        proxy.thread.join(10)
        self.assertEqual(proxy.received, [rasterproto.make_test_job(62, True, "bars")])
        self.assertIn("two-color, bars, 62 mm, 600 rows", done.stdout)
        self.assertIn("printer result: ok", done.stdout)

    def test_black_pattern_and_rows(self):
        self.write_state(state_doc())
        proxy = FakeProxy(self)
        done = self.qlctl("test-print", "--host", "127.0.0.1", "--port", str(proxy.port),
                          "--pattern", "black", "--rows", "200")
        self.assertEqual(done.returncode, 0, done.stderr)
        proxy.thread.join(10)
        self.assertEqual(proxy.received, [rasterproto.make_test_job(62, True, "black", 200)])

    def test_mono_on_a_mono_roll(self):
        self.write_state(state_doc(IDLE_DK22205))
        proxy = FakeProxy(self)
        done = self.qlctl("test-print", "--mono", "--port", str(proxy.port))
        self.assertEqual(done.returncode, 0, done.stderr)
        proxy.thread.join(10)
        self.assertEqual(proxy.received, [rasterproto.make_test_job(62, False, "bars")])

    def test_mono_uses_the_loaded_width(self):
        self.write_state(state_doc(media.synthesize_status(media.lookup_label("29")).raw))
        proxy = FakeProxy(self)
        done = self.qlctl("test-print", "--mono", "--port", str(proxy.port))
        self.assertEqual(done.returncode, 0, done.stderr)
        proxy.thread.join(10)
        self.assertEqual(proxy.received, [rasterproto.make_test_job(29, False, "bars")])

    def test_refuses_a_mismatched_roll(self):
        cases = [(IDLE_DK22251, ("--mono",), "DK-22251"), (IDLE_DK22205, (), "not black/red"),
                 (IDLE_DK11201, (), "die-cut")]
        for frame, args, reason in cases:
            with self.subTest(reason=reason):
                self.write_state(state_doc(frame))
                done = self.qlctl("test-print", "--port", "9", *args)
                self.assertEqual(done.returncode, 3, done.stderr)
                self.assertIn(reason, done.stderr)

    def test_force_sends_anyway(self):
        self.write_state(state_doc(IDLE_DK22205))
        proxy = FakeProxy(self)
        done = self.qlctl("test-print", "--force", "--port", str(proxy.port))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("sending anyway", done.stderr)

    def test_refuses_a_width_the_loaded_roll_does_not_have(self):
        # The job's ESC i z sets "width valid": a 62 mm job on a 29 mm roll makes the
        # printer report "replace media" until the roll cover is opened and closed.
        self.write_state(state_doc(media.synthesize_status(media.lookup_label("29")).raw))
        done = self.qlctl("test-print", "--mono", "--width", "62", "--port", "9")
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("29 mm wide but --width 62", done.stderr)
        self.assertIn("replace media", done.stderr)
        proxy = FakeProxy(self)
        done = self.qlctl("test-print", "--mono", "--width", "62", "--force", "--port", str(proxy.port))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("sending anyway", done.stderr)
        proxy.thread.join(10)
        self.assertEqual(proxy.received, [rasterproto.make_test_job(62, False, "bars")])

    def test_matching_width_is_sent(self):
        self.write_state(state_doc(media.synthesize_status(media.lookup_label("29")).raw))
        proxy = FakeProxy(self)
        done = self.qlctl("test-print", "--mono", "--width", "29", "--port", str(proxy.port))
        self.assertEqual(done.returncode, 0, done.stderr)
        proxy.thread.join(10)
        self.assertEqual(proxy.received, [rasterproto.make_test_job(29, False, "bars")])

    def test_impossible_job_is_a_usage_error(self):
        # Two-color needs 62 mm tape: exit 2 before any roll check, with or without --force.
        self.write_state(state_doc())
        for extra in ((), ("--force",)):
            with self.subTest(extra=extra):
                done = self.qlctl("test-print", "--width", "29", "--port", "9", *extra)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("62 mm", done.stderr)

    def test_unknown_status_still_sends(self):
        proxy = FakeProxy(self)
        done = self.qlctl("test-print", "--port", str(proxy.port))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("status unknown", done.stderr)

    def test_nothing_listening(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.write_state(state_doc())
        done = self.qlctl("test-print", "--port", str(port))
        self.assertEqual(done.returncode, 1)
        self.assertIn("cannot send the test label", done.stderr)


class EndToEndTest(QlctlTestCase):
    def test_test_print_through_the_daemon(self):
        from tests.test_proxyd import Harness      # the real ql810wc-proxyd with a fake printer
        h = Harness(self).start()
        done = subprocess.run([sys.executable, "-I", "-B", str(QLCTL), "--config", self.config,
                               "--state", h.state_path, "test-print", "--port", str(h.port)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("printer result: ok", done.stdout)
        self.assertEqual(h.session_bytes().count(rasterproto.make_test_job(62, True, "bars")), 1)
        self.assertEqual(h.model.violations, [])
        done = subprocess.run([sys.executable, "-I", "-B", str(QLCTL), "--config", self.config,
                               "--state", h.state_path, "status"], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Job       last: ok", done.stdout)

    def test_a_test_label_the_daemon_refuses(self):
        from tests.test_proxyd import Harness      # the real ql810wc-proxyd with a fake printer
        h = Harness(self)
        h.model.set_media("62")                    # a single-colour roll; the label is two-colour
        h.start()
        done = subprocess.run([sys.executable, "-I", "-B", str(QLCTL), "--config", self.config,
                               "--state", h.state_path, "test-print", "--force", "--port", str(h.port)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("refused", done.stdout + done.stderr)
        self.assertIn("a two-color job, but the loaded roll, 62mm continuous", done.stdout + done.stderr)
        self.assertNotIn("Connection reset", done.stderr)
        self.assertEqual(h.model.jobs, [])

    def test_stale_state_does_not_block_probe(self):
        doc = state_doc(pid=os.getpid())
        self.write_state(doc)
        stale = statecache.StateCache(self.state).read()
        stale["updated"] = time.time() - 3600
        with open(self.state, "w", encoding="utf-8") as fh:
            json.dump(stale, fh)
        done = self.qlctl("probe", "--device", "/nonexistent/usb/lp0")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("no such device", done.stderr)


class ProbeTest(QlctlTestCase):
    def test_refused_while_the_daemon_runs(self):
        self.write_state(state_doc(pid=os.getpid()))
        done = self.qlctl("probe", "--device", "/nonexistent/usb/lp0")
        self.assertEqual(done.returncode, 3)
        self.assertIn("refusing", done.stderr)
        done = self.qlctl("probe", "--device", "/nonexistent/usb/lp0", "--force")
        self.assertEqual(done.returncode, 1)
        self.assertIn("no such device", done.stderr)

    @unittest.skipUnless(HAVE_PTY, "needs a POSIX pseudo-terminal")
    def test_probe_a_printer(self):
        printer = PtyPrinter()
        self.addCleanup(printer.close)
        done = self.qlctl("probe", "--device", printer.path)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("62mm continuous black/red (DK-22251)  [two-color]", done.stdout)
        self.assertIn(IDLE_DK22251.hex(" "), done.stdout)
        self.assertIn("model QL-810W", done.stdout)
        self.assertEqual(bytes(printer.model.received), STATUS_REQUEST, "no resync unless asked")
        done = self.qlctl("probe", "--device", printer.path, "--json", "--resync")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual(report["status"]["raw_hex"], IDLE_DK22251.hex())
        self.assertEqual(report["media"]["label_id"], "62red")
        self.assertEqual(report["device"]["transport"], "usblp")
        self.assertTrue(bytes(printer.model.received).endswith(RESYNC_REQUEST))

    @unittest.skipUnless(HAVE_PTY, "needs a POSIX pseudo-terminal")
    def test_probe_without_reply(self):
        printer = PtyPrinter(PrinterModel())
        self.addCleanup(printer.close)
        printer.model.mute = True
        done = self.qlctl("probe", "--device", printer.path, "--timeout", "0.5")
        self.assertEqual(done.returncode, 1)
        self.assertIn("try --resync", done.stderr)


if __name__ == "__main__":
    unittest.main()
