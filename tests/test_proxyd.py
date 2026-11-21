"""Tests for agent/ql810wc/proxyd.py (ql810wc-proxyd).

The real daemon runs in a thread (or, for socket activation and SIGTERM, as
a subprocess) and is driven over real localhost TCP sockets. The printer is
tests/fake_printer.py: it records every byte the daemon sends it, so the
tests can check ordering, resyncs and that no ``ESC i S`` ever lands inside
a job.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Callable, Dict, List, Optional

from tests.fake_printer import (FAKE_SERIAL, PTOUCH_TRAILER, RESYNC_REQUEST, TEST_DEVICE_ID, FakeBus,
                                PrinterModel, PtyPrinter)

from ql810wc import config, proxyd, rasterproto, statecache, usbio  # noqa: E402  (path set by fake_printer)
from ql810wc.proxyd import ProxyDaemon, ProxySettings, SdNotifier  # noqa: E402
from ql810wc.rasterproto import (INVALIDATE, PHASE_PRINTING, RESYNC, STATUS_REQUEST,  # noqa: E402
                                 ST_PHASE_CHANGE)
from tests.fake_printer import FakePrinter  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
PROXYD = REPO / "agent" / "ql810wc-proxyd"
# The daemon logs warnings on purpose in several tests (torn jobs, unplugging);
# keep them out of the test report. assertLogs still sees them.
logging.getLogger("ql810wc").addHandler(logging.NullHandler())
JOB = rasterproto.make_test_job()                                   # two-colour bars, 600 rows
JOB_B = rasterproto.make_test_job(rows=300, pattern="black")
HAVE_PTY = os.name == "posix" and sys.platform != "cygwin"


class RecordingNotifier(SdNotifier):
    """Collects sd_notify messages instead of sending them."""

    def __init__(self, watchdog_usec: Optional[int] = None) -> None:
        super().__init__(None, watchdog_usec)
        self.messages: List[Dict[str, object]] = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return True

    def notify(self, **fields: object) -> bool:
        with self._lock:
            self.messages.append(dict(fields))
        return True

    def values(self, key: str) -> List[object]:
        with self._lock:
            return [m[key] for m in self.messages if key in m]


def _reset(sock: socket.socket) -> None:
    """Close with a TCP RST (what a crashed or killed client produces)."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def _how_closed(sock: socket.socket, timeout: float = 15.0) -> str:
    """"fin" or "rst": how the proxy ended the connection (bytes it sent are skipped)."""
    sock.settimeout(timeout)
    try:
        while sock.recv(65536):
            pass
    except ConnectionResetError:
        return "rst"
    return "fin"


def _recv_until_closed(sock: socket.socket, timeout: float = 15.0) -> bytes:
    """Everything the peer sends until it closes (b"" on a reset)."""
    sock.settimeout(timeout)
    data = bytearray()
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return bytes(data)
            data += chunk
    except ConnectionResetError:
        return bytes(data)


class Harness:
    """A ProxyDaemon on 127.0.0.1:<ephemeral> with a fake printer, in a thread."""

    def __init__(self, test: unittest.TestCase, *, bus: Optional[FakeBus] = None,
                 notifier: Optional[SdNotifier] = None, **overrides: Any) -> None:
        self.test = test
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.state_path = os.path.join(tmp.name, "state.json")
        self.bus = bus or FakeBus()
        self.listener = proxyd.bind_listener("127.0.0.1", 0)
        test.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]
        params: Dict[str, Any] = dict(
            state_path=self.state_path, listen=("127.0.0.1", 0), poll_seconds=0.3, status_timeout=0.5,
            raw_idle_timeout=5.0, post_job_timeout=5.0, post_job_poll=0.1, post_job_error_grace=0.5,
            settle_min=0.1, write_stall_timeout=5.0, device_wait=1.0, rescan_seconds=0.1,
            heartbeat_seconds=1.0, stop_grace=2.0, io_slice=0.05)
        params.update(overrides)
        self.settings = ProxySettings(**params)
        self.notifier = notifier if notifier is not None else RecordingNotifier()
        self.daemon = ProxyDaemon(self.settings, [self.listener], self.bus,
                                  statecache.StateCache(self.state_path), self.notifier)
        self.exit_code: Optional[int] = None
        self.thread = threading.Thread(target=self._run, name="proxyd", daemon=True)
        test.addCleanup(self.stop)

    @property
    def printer(self):
        return self.bus.printer

    @property
    def model(self) -> PrinterModel:
        return self.bus.printer.model

    def session_bytes(self, index: int = -1) -> bytes:
        return bytes(self.printer.sessions[index])

    def _run(self) -> None:
        self.exit_code = self.daemon.run()

    def start(self, wait_ready: bool = True) -> "Harness":
        self.thread.start()
        if wait_ready:
            self.wait_state(lambda s: s["device"]["state"] == "ready" and s["status"]["raw_hex"],
                            "printer ready")
        return self

    def stop(self) -> None:
        if self.thread.is_alive():
            self.daemon.request_stop()
            self.thread.join(15)
            self.test.assertFalse(self.thread.is_alive(), "daemon did not stop")

    def state(self) -> Optional[Dict[str, Any]]:
        return statecache.StateCache(self.state_path).read()

    def wait_state(self, predicate: Callable[[Dict[str, Any]], Any], what: str,
                   timeout: float = 10.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        doc = None
        while time.monotonic() < deadline:
            doc = self.state()
            if doc is not None and predicate(doc):
                return doc
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {what}; last state: {json.dumps(doc, indent=1)}")

    def wait_for(self, predicate: Callable[[], Any], what: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {what}")

    def connect(self) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.test.addCleanup(sock.close)
        return sock

    def send_job(self, data: bytes, *, pieces: int = 1, pause: float = 0.0) -> bytes:
        """Send like CUPS does: the job, half-close, wait for the proxy to close."""
        sock = self.connect()
        step = -(-len(data) // pieces)
        for start in range(0, len(data), step):
            sock.sendall(data[start:start + step])
            if pause and start + step < len(data):
                time.sleep(pause)
        sock.shutdown(socket.SHUT_WR)
        return _recv_until_closed(sock)


class ZlpStormPrinter(FakePrinter):
    """A usblp node as the real QL presents it: the descriptor is always readable
    (every bulk-IN request is answered by a zero-length packet), while reads
    return nothing between status replies. Counts read() calls."""

    kind = "usblp"

    def __init__(self) -> None:
        super().__init__()
        self._rfd, self._wfd = os.pipe()
        os.write(self._wfd, b"x")                 # never drained: always "readable"
        self.reads = 0

    def fileno(self) -> Optional[int]:
        return self._rfd

    def read(self, max_len: int, timeout: float) -> bytes:
        self.reads += 1
        return super().read(max_len, 0.0)          # like a ZLP: returns at once

    def cleanup(self) -> None:
        os.close(self._rfd)
        os.close(self._wfd)


class IdleCpuTest(unittest.TestCase):
    def test_always_readable_usblp_does_not_spin(self):
        """Regression (tested unit): selecting on the usblp node spun a core."""
        printer = ZlpStormPrinter()
        self.addCleanup(printer.cleanup)
        h = Harness(self, bus=FakeBus(printer), poll_seconds=0.5).start()
        before = printer.reads
        time.sleep(1.5)
        reads = printer.reads - before
        # idle_io_slice (0.25 s) plus the status polls: a handful per second, not thousands.
        self.assertLess(reads, 40, f"{reads} USB reads in 1.5 s while idle: the loop is spinning")
        self.assertEqual(h.state()["device"]["state"], "ready")


class ForwardingTest(unittest.TestCase):
    def test_single_two_color_job(self):
        h = Harness(self).start()
        back = h.send_job(JOB)
        self.assertEqual(back, b"", "nothing is sent back with RAW_BIDIRECTIONAL=no")
        # Delivery acknowledgement: the result is published before the proxy closes.
        state = h.state()
        job = state["job"]
        self.assertEqual(job["last_result"], "ok")
        self.assertFalse(job["active"])
        self.assertEqual(job["bytes"], len(JOB))
        self.assertIs(job["two_color"], True)
        self.assertEqual(job["peer"].split(":")[0], "127.0.0.1")
        self.assertEqual(job["requested_media"]["width"], 62)
        self.assertEqual(job["requested_media"]["type"], 0x0A)
        self.assertEqual(job["requested_media"]["lines"], 600)
        received = h.session_bytes()
        self.assertEqual(received.count(JOB), 1, "job forwarded once, unmodified")
        start = received.index(JOB)
        prefix, suffix = received[:start], received[start + len(JOB):]
        self.assertTrue(prefix.startswith(RESYNC_REQUEST), "first contact resynchronises")
        self.assertEqual(prefix[len(RESYNC_REQUEST):].replace(STATUS_REQUEST, b""), b"")
        self.assertEqual(suffix.replace(STATUS_REQUEST, b""), b"", "after the job: status requests only")
        self.assertEqual(h.model.violations, [])
        self.assertEqual(len(h.model.jobs), 1)
        self.assertTrue(h.model.jobs[0]["two_color"])
        # The state document (schema 1).
        self.assertEqual(state["schema"], 1)
        self.assertEqual(state["proxy"]["pid"], os.getpid())
        self.assertEqual(state["proxy"]["raw_port"], h.port)
        device = state["device"]
        self.assertEqual((device["state"], device["vid"], device["pid"]), ("ready", "04f9", "209c"))
        self.assertEqual((device["product"], device["manufacturer"]), ("QL-810W", "Brother"))
        self.assertEqual(device["serial"], FAKE_SERIAL)
        self.assertEqual((device["transport"], device["node"]), ("fake", "/dev/usb/lp0"))
        self.assertEqual(device["ieee1284_id"], TEST_DEVICE_ID)
        status = state["status"]
        self.assertEqual(status["parsed"]["text_color"], 0x81)
        self.assertEqual(status["parsed"]["status_type"], 0x00)
        self.assertEqual(status["parsed"]["phase_type"], 0x00)
        self.assertEqual(status["media"]["label_id"], "62red")
        self.assertEqual(status["source"], "reply")

    def test_two_clients_are_served_one_at_a_time(self):
        h = Harness(self).start()
        done: Dict[str, float] = {}

        def client_a() -> None:
            sock = h.connect()
            half = len(JOB) // 2
            sock.sendall(JOB[:half])
            time.sleep(0.6)
            sock.sendall(JOB[half:])
            sock.shutdown(socket.SHUT_WR)
            _recv_until_closed(sock)
            done["a"] = time.monotonic()

        thread = threading.Thread(target=client_a)
        thread.start()
        time.sleep(0.2)                            # A is connected and mid-job
        h.send_job(JOB_B)
        done["b"] = time.monotonic()
        thread.join(15)
        received = h.session_bytes()
        a, b = received.index(JOB), received.index(JOB_B)
        self.assertLessEqual(a + len(JOB), b, "B starts only after A is complete")
        self.assertLess(done["a"], done["b"])
        self.assertEqual(h.model.violations, [])
        self.assertEqual(len(h.model.jobs), 2)

    def test_no_status_request_inside_a_slow_job(self):
        h = Harness(self, poll_seconds=0.1).start()
        h.send_job(JOB, pieces=6, pause=0.25)
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        self.assertEqual(h.model.violations, [])
        job = h.model.jobs[-1]
        before = [o for o in h.model.status_requests if o < job["start"]]
        after = [o for o in h.model.status_requests if o >= job["end"]]
        self.assertTrue(before and after, "polled before and after the job, never during it")

    def test_torn_job_resynchronises_the_printer(self):
        h = Harness(self).start()
        partial = JOB[:len(JOB) // 2 + 7]          # ends in the middle of a raster row
        sock = h.connect()
        sock.sendall(partial)
        h.wait_for(lambda: partial in h.session_bytes(), "the partial job reached the printer")
        _reset(sock)                               # the client dies: RST before the 0x1A
        state = h.wait_state(lambda s: s["job"]["last_result"] == "torn", "torn job")
        self.assertFalse(state["job"]["active"])
        self.assertIn("reset", state["job"]["last_detail"])
        received = h.session_bytes()
        end = received.index(partial) + len(partial)
        self.assertTrue(received[end:].startswith(RESYNC_REQUEST), received[end:end + 8].hex())
        self.assertEqual(h.model.violations, [])
        self.assertGreater(h.model.inits[-1], end - 1, "ESC @ after the partial job")
        self.assertEqual(received[end + len(RESYNC_REQUEST):].replace(STATUS_REQUEST, b""), b"",
                         "resynchronised once, then plain status requests")

    def test_idle_client_mid_job_is_torn(self):
        h = Harness(self, raw_idle_timeout=0.5).start()
        sock = h.connect()
        sock.sendall(JOB[:5000])
        self.assertEqual(_how_closed(sock, timeout=10), "rst", "a torn job is aborted")
        state = h.wait_state(lambda s: s["job"]["last_result"] == "torn", "torn job after the idle timeout")
        self.assertIn("no data from the client", state["job"]["last_detail"])
        received = h.session_bytes()
        self.assertIn(RESYNC_REQUEST, received[received.index(JOB[:5000]) + 5000:])

    def test_clean_close_mid_job_is_not_torn(self):
        # Only an abnormal end (RST/error, RAW_IDLE_TIMEOUT, MAX_JOB_BYTES) tears a
        # job. A half-close before the 0x1A is reported as an error, and the
        # printer, left in the middle of a raster row, is resynchronised.
        h = Harness(self).start()
        partial = JOB[:len(JOB) // 2 + 7]
        self.assertEqual(h.send_job(partial), b"")
        state = h.state()
        self.assertEqual(state["job"]["last_result"], "error")
        self.assertIn("before the end of the job", state["job"]["last_detail"])
        received = h.session_bytes()
        end = received.index(partial) + len(partial)
        self.assertTrue(received[end:].startswith(RESYNC_REQUEST))
        self.assertEqual(h.model.violations, [])

    def test_opaque_stream_is_never_torn(self):
        # ESC i a 00 switches to ESC/P: the tokenizer cannot follow, the bytes are
        # forwarded anyway, and the client's EOF is trusted (no resync).
        h = Harness(self).start()
        payload = b"\x00" * 400 + b"\x1b\x40" + b"\x1b\x69\x61\x00" + b"Hello from ESC/P\r\n" * 20
        self.assertEqual(h.send_job(payload), b"")
        state = h.state()
        self.assertEqual(state["job"]["last_result"], "ok")
        self.assertEqual(state["job"]["bytes"], len(payload))
        received = h.session_bytes()
        end = received.index(payload) + len(payload)
        self.assertNotIn(RESYNC, received[end:])

    def test_max_job_bytes_tears_the_job(self):
        h = Harness(self, max_job_bytes=100_000).start()
        sock = h.connect()
        try:
            sock.sendall(JOB)                      # about 111 kB
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass                                   # the proxy may reset us mid-send
        self.assertEqual(_how_closed(sock), "rst")
        state = h.wait_state(lambda s: s["job"]["last_result"] == "torn", "torn job")
        self.assertIn("MAX_JOB_BYTES", state["job"]["last_detail"])
        received = h.session_bytes()
        start = received.index(JOB[:1000])
        resync = received.index(RESYNC_REQUEST, start)
        self.assertLessEqual(resync - start, 100_000, "never more than MAX_JOB_BYTES of one job")
        self.assertEqual(h.model.violations, [])

    def test_connection_stays_open_after_the_job_until_the_client_closes(self):
        # Never close on a timer after 0x1A: P-touch sends its trailer only after
        # SNMP shows the label done. A bare ESC i S (its PRN path) is forwarded too.
        h = Harness(self).start()
        sock = h.connect()
        sock.sendall(JOB)
        h.wait_state(lambda s: s["job"]["last_result"] == "ok" and not s["job"]["active"], "job settled")
        sock.settimeout(1.0)
        with self.assertRaises(socket.timeout):
            sock.recv(1)                           # still open, nothing sent back
        requests_before = len(h.model.status_requests)
        sock.sendall(STATUS_REQUEST)
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(_how_closed(sock), "fin")
        self.assertGreater(len(h.model.status_requests), requests_before)
        received = h.session_bytes()
        self.assertNotIn(RESYNC, received[received.index(JOB) + len(JOB):])
        self.assertEqual(h.state()["job"]["bytes"], len(JOB))

    def test_bare_status_request_trailer_is_held_while_printing(self):
        model = PrinterModel(print_seconds=0.5)
        h = Harness(self, bus=FakeBus(FakePrinter(model))).start()
        self.assertEqual(h.send_job(JOB + STATUS_REQUEST), b"")
        received = h.session_bytes()
        end = received.index(JOB) + len(JOB)
        done = model.jobs[0]["done_at"]
        late = [o for o in model.status_requests if o >= end]
        self.assertTrue(late)
        for offset in late:
            self.assertGreaterEqual(model.time_of(offset), done, "no ESC i S reached a printing printer")
        self.assertEqual(model.violations, [])
        self.assertEqual(h.state()["job"]["last_result"], "ok")

    def test_idle_timeout_after_a_settled_job_closes_normally(self):
        h = Harness(self, raw_idle_timeout=0.6).start()
        sock = h.connect()
        sock.sendall(JOB)
        h.wait_state(lambda s: s["job"]["last_result"] == "ok" and not s["job"]["active"], "job settled")
        self.assertEqual(_how_closed(sock), "fin", "not a torn job: an ordinary close")
        time.sleep(0.2)
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        received = h.session_bytes()
        self.assertNotIn(RESYNC, received[received.index(JOB) + len(JOB):])

    def test_trailer_after_job_is_held_until_printing_completes(self):
        model = PrinterModel(print_seconds=0.5)
        h = Harness(self, bus=FakeBus(_printer(model))).start()
        back = h.send_job(JOB + PTOUCH_TRAILER)
        self.assertEqual(back, b"")
        received = h.session_bytes()
        trailer_at = received.index(PTOUCH_TRAILER)
        self.assertGreater(trailer_at, received.index(JOB))
        self.assertGreaterEqual(model.time_of(trailer_at), model.jobs[0]["done_at"],
                                "the trailer reached the printer only after it finished printing")
        self.assertNotIn(RESYNC, received[trailer_at:], "a trailer is not a torn job")
        state = h.state()
        self.assertEqual(state["job"]["last_result"], "ok")
        self.assertEqual(state["job"]["bytes"], len(JOB), "the trailer is not counted as a job")

    def test_ptouch_style_session(self):
        # P-touch for Mac: job, then SNMP polling until the printer is idle, then
        # the trailer on the same connection, then close.
        model = PrinterModel(print_seconds=0.3)
        h = Harness(self, bus=FakeBus(_printer(model))).start()
        sock = h.connect()
        sock.sendall(JOB)
        h.wait_state(lambda s: s["job"]["active"], "job in flight")
        state = h.wait_state(lambda s: s["job"]["last_result"] == "ok" and not s["job"]["active"],
                             "job finished while the connection is still open")
        self.assertEqual(state["status"]["parsed"]["phase_type"], 0)
        sock.sendall(PTOUCH_TRAILER)
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(_recv_until_closed(sock), b"")
        received = h.session_bytes()
        self.assertIn(PTOUCH_TRAILER, received)
        self.assertNotIn(RESYNC, received[received.index(PTOUCH_TRAILER):])
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        self.assertEqual(model.violations, [])

    def test_idle_polls_go_on_while_the_client_keeps_the_connection_after_its_trailer(self):
        # P-touch for Mac closes about 1 s after its trailer. A client that keeps the
        # connection open must not freeze state.json: the persona would soon report
        # the idle printer as switched off (STATUS_MAX_AGE_SECONDS).
        model = PrinterModel(print_seconds=0.2)
        h = Harness(self, bus=FakeBus(_printer(model))).start()
        sock = h.connect()
        sock.sendall(JOB + PTOUCH_TRAILER)
        h.wait_state(lambda s: s["job"]["last_result"] == "ok" and not s["job"]["active"], "job settled")
        h.wait_for(lambda: PTOUCH_TRAILER in h.session_bytes(), "trailer forwarded")
        trailer_end = h.session_bytes().index(PTOUCH_TRAILER) + len(PTOUCH_TRAILER)
        polled = h.state()["status"]["polled"]
        _wait_polls(h, trailer_end, 2)
        h.wait_state(lambda s: s["status"]["polled"] > polled, "state.json keeps a current status")
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        # Anything after the trailer can no longer be judged: no status request may land in it.
        sock.sendall(JOB_B)
        time.sleep(1.0)
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(_how_closed(sock), "fin")
        self.assertEqual(model.violations, [], "no ESC i S inside what followed the trailer")
        received = h.session_bytes()
        second = received.index(JOB_B, trailer_end)
        self.assertNotIn(RESYNC, received[trailer_end:second], "plain ESC i S polls behind the trailer")

    def test_job_without_volunteered_frames_is_confirmed_by_polling(self):
        model = PrinterModel(notify=False, print_seconds=0.3)
        h = Harness(self, bus=FakeBus(_printer(model))).start()
        h.send_job(JOB)
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        job = model.jobs[-1]
        polls_after = [o for o in model.status_requests if o >= job["end"]]
        self.assertTrue(polls_after)
        self.assertEqual(model.violations, [])

    def test_printer_error_after_a_job(self):
        # A fault reported at the end of a complete job leaves the stream at a
        # command boundary: plain ESC i S polls only. ESC @ would cancel the job a
        # real printer holds until the fault is cleared.
        model = PrinterModel()
        model.fail_next_job = (0x00, 0x01)        # "replace media"
        h = Harness(self, bus=FakeBus(_printer(model))).start()
        h.send_job(JOB)
        state = h.state()
        self.assertEqual(state["job"]["last_result"], "error")
        self.assertIn("replace media", state["status"]["parsed"]["errors"])
        end = model.jobs[-1]["end"]
        _wait_polls(h, end, 3)
        after = h.session_bytes()[end:]
        self.assertNotIn(RESYNC, after)
        self.assertEqual(after.replace(STATUS_REQUEST, b""), b"", "plain status requests only")
        self.assertEqual([o for o in model.inits if o >= end], [])

    def test_job_sent_with_the_cover_open_is_not_cancelled(self):
        h = Harness(self).start()
        h.model.set_cover_open(True)
        h.wait_state(lambda s: s["status"]["parsed"]["err2"] == 0x10, "cover open")
        h.send_job(JOB)
        state = h.state()
        self.assertEqual(state["job"]["last_result"], "error")
        self.assertIn("cover open", state["job"]["last_detail"])
        end = h.model.jobs[-1]["end"]
        _wait_polls(h, end, 3)
        h.model.set_cover_open(False)
        h.wait_state(lambda s: s["status"]["parsed"]["errors"] == [], "cover closed")
        _wait_polls(h, end, 2)
        self.assertEqual([o for o in h.model.inits if o >= end], [], "no ESC @ after the job")
        self.assertEqual(h.session_bytes()[end:].replace(STATUS_REQUEST, b""), b"")
        self.assertEqual(h.model.violations, [])

    def test_resync_waits_while_the_printer_reports_an_error(self):
        # A torn job needs ESC @, but not while the printer reports a fault: only
        # 400 x NUL + ESC i S go out until two polls in a row find it idle.
        h = Harness(self).start()
        h.model.set_cover_open(True)
        h.wait_state(lambda s: s["status"]["parsed"]["err2"] == 0x10, "cover open")
        partial = JOB[:len(JOB) // 2 + 7]          # ends in the middle of a raster row
        sock = h.connect()
        sock.sendall(partial)
        h.wait_for(lambda: partial in h.session_bytes(), "the partial job reached the printer")
        _reset(sock)
        state = h.wait_state(lambda s: s["job"]["last_result"] == "torn", "torn job")
        self.assertIn("resynchronisation pending", state["job"]["last_detail"])
        end = h.session_bytes().index(partial) + len(partial)
        probe = INVALIDATE + STATUS_REQUEST
        h.wait_for(lambda: h.session_bytes()[end:].count(probe) >= 3, "probes while the cover is open")
        self.assertEqual([o for o in h.model.inits if o >= end], [], "no ESC @ while the cover is open")
        h.model.set_cover_open(False)
        h.wait_for(lambda: any(o >= end for o in h.model.inits), "ESC @ once the printer is idle")
        _wait_polls(h, end, 1)
        head, found, tail = h.session_bytes()[end:].partition(RESYNC_REQUEST)
        self.assertTrue(found)
        self.assertEqual(head.replace(probe, b""), b"", "probes only before the resync")
        self.assertGreaterEqual(head.count(probe), 5, "three or more with the cover open, then two clean ones")
        self.assertEqual(tail.replace(STATUS_REQUEST, b""), b"", "plain status requests after it")

    def test_usb_stall_aborts_the_job(self):
        h = Harness(self, write_stall_timeout=0.5).start()
        h.printer.stall_after = h.printer.total_written + 20000
        sock = h.connect()
        try:
            sock.sendall(JOB)
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass                                   # the proxy may reset us mid-send
        self.assertEqual(_recv_until_closed(sock), b"")
        h.wait_state(lambda s: s["job"]["last_result"] == "error", "job aborted after the stall")
        written = h.printer.total_written
        h.printer.set_stalled(False)
        h.wait_for(lambda: RESYNC_REQUEST in h.session_bytes()[written - 20000:],
                   "resync once the printer accepts data again")

    def test_half_close_waits_for_delivery_within_the_stall_bound(self):
        # P-touch Template mode (ESC i a 03) is opaque: no 0x1A is seen, so after
        # the half-close the proxy waits for the printer to take the last of the
        # data. A printer that never does fails the job after write_stall_timeout,
        # and the next client is served.
        h = Harness(self, write_stall_timeout=1.0).start()
        sessions = len(h.printer.sessions)
        h.printer.set_flush_blocked(True)          # the last USB transfer never completes
        template = b"\x00" * 400 + b"\x1b\x40" + b"\x1b\x69\x61\x03" + b"^II" + b"\x40" * 64
        started = time.monotonic()
        self.assertEqual(h.send_job(template), b"")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 4.0, "bounded by write_stall_timeout")
        state = h.state()
        self.assertEqual(state["job"]["last_result"], "error")
        self.assertIn("accepted no data for 1 s", state["job"]["last_detail"])
        h.wait_state(lambda s: s["device"]["state"] == "ready" and s["status"]["raw_hex"], "reopened")
        self.assertGreater(len(h.printer.sessions), sessions, "closing the node dropped the transfer")
        self.assertTrue(h.session_bytes().startswith(RESYNC_REQUEST), "the reopened printer is resynced")
        self.assertEqual(h.send_job(JOB_B), b"")
        self.assertEqual(h.state()["job"]["last_result"], "ok", "the next client is served")

    def test_half_close_records_frames_while_waiting_for_delivery(self):
        h = Harness(self, write_stall_timeout=1.5).start()
        h.printer.set_flush_blocked(True)
        template = b"\x00" * 400 + b"\x1b\x69\x61\x03" + b"^II"
        sock = h.connect()
        sock.sendall(template)
        sock.shutdown(socket.SHUT_WR)
        h.wait_state(lambda s: s["job"]["active"], "job in flight")
        h.model.set_cover_open(True)
        h.model.inject(h.model.frame(0x02))        # the printer volunteers "error occurred"
        state = h.wait_state(lambda s: s["status"]["parsed"]["err2"] == 0x10, "the error frame in state.json")
        self.assertTrue(state["job"]["active"])
        _recv_until_closed(sock)
        self.assertEqual(h.state()["job"]["last_result"], "error")

    def test_undelivered_end_of_job_fails_after_a_reported_error(self):
        # The job's last USB transfer (with its 0x1A) is never taken and the printer
        # reports an error: settling ends after post_job_error_grace, not at
        # post_job_timeout, and the transport is reopened to drop the transfer.
        model = PrinterModel()
        model.fail_next_job = (0x02, 0x00)        # "end of media"
        h = Harness(self, bus=FakeBus(_printer(model)), post_job_timeout=8.0).start()
        sessions = len(h.printer.sessions)
        h.printer.set_flush_blocked(True)
        started = time.monotonic()
        self.assertEqual(h.send_job(JOB), b"")
        self.assertLess(time.monotonic() - started, 4.0)
        state = h.state()
        self.assertEqual(state["job"]["last_result"], "error")
        self.assertIn("end of media", state["job"]["last_detail"])
        self.assertIn("did not accept the end of the job", state["job"]["last_detail"])
        h.wait_for(lambda: len(h.printer.sessions) > sessions, "the transport is reopened")
        h.wait_state(lambda s: s["device"]["state"] == "ready" and s["status"]["raw_hex"], "ready again")
        self.assertTrue(h.session_bytes().startswith(RESYNC_REQUEST))
        self.assertEqual(model.violations, [])

    def test_undelivered_end_of_job_is_never_ok(self):
        # No frame, no completed transfer: at post_job_timeout the job is an error
        # (it was reported "ok" before), and no status request queued behind it.
        model = PrinterModel(notify=False)
        h = Harness(self, bus=FakeBus(_printer(model)), post_job_timeout=1.5).start()
        sessions = len(h.printer.sessions)
        h.printer.set_flush_blocked(True)
        self.assertEqual(h.send_job(JOB), b"")
        state = h.state()
        self.assertEqual(state["job"]["last_result"], "error")
        self.assertIn("did not accept the end of the job within 1.5 s", state["job"]["last_detail"])
        first_session = bytes(h.printer.sessions[sessions - 1])
        self.assertEqual(first_session[first_session.index(JOB) + len(JOB):], b"",
                         "no status request was written behind the undelivered data")
        h.wait_for(lambda: len(h.printer.sessions) > sessions, "the transport is reopened")
        self.assertEqual(model.violations, [])

    def test_trailer_of_a_failed_job_does_not_wait_long_on_a_stalled_printer(self):
        model = PrinterModel()
        model.fail_next_job = (0x00, 0x01)        # "replace media"
        h = Harness(self, bus=FakeBus(_printer(model)), write_stall_timeout=8.0).start()
        h.printer.stall_after_jobs = len(model.jobs) + 1   # takes the job, then nothing more
        started = time.monotonic()
        self.assertEqual(h.send_job(JOB + PTOUCH_TRAILER), b"")
        self.assertLess(time.monotonic() - started, 5.0, "post_job_error_grace, not write_stall_timeout")
        self.assertEqual(h.state()["job"]["last_result"], "error")
        self.assertNotIn(PTOUCH_TRAILER, h.session_bytes())

    def test_second_job_on_a_connection_counts_only_its_own_bytes(self):
        h = Harness(self).start()
        sock = h.connect()
        sock.sendall(JOB)
        h.wait_state(lambda s: s["job"]["last_result"] == "ok" and not s["job"]["active"], "first job settled")
        sock.sendall(STATUS_REQUEST)               # a bare ESC i S between the jobs, read on its own
        time.sleep(0.5)
        sock.sendall(JOB_B)
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(_recv_until_closed(sock), b"")
        state = h.state()
        self.assertEqual(state["job"]["last_result"], "ok")
        self.assertEqual(state["job"]["bytes"], len(JOB_B), "the ESC i S was the first job's trailer")
        self.assertEqual(len(h.model.jobs), 2)

    def test_bidirectional_relays_printer_frames(self):
        h = Harness(self, bidirectional=True).start()
        sock = h.connect()
        sock.sendall(JOB)
        sock.settimeout(10)
        data = b""
        while len(data) < 32:
            data += sock.recv(4096)
        frames, _rest = rasterproto.split_status_frames(data)
        self.assertTrue(frames)
        self.assertIn(ST_PHASE_CHANGE, [f.status_type for f in frames])
        sock.shutdown(socket.SHUT_WR)
        _recv_until_closed(sock)
        self.assertEqual(h.state()["job"]["last_result"], "ok")

    def test_internal_error_is_contained(self):
        h = Harness(self).start()

        def broken(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("injected failure")

        original = h.daemon._forward_data
        h.daemon._forward_data = broken            # type: ignore[assignment]
        with self.assertLogs("ql810wc.proxyd", "ERROR"):
            back = h.send_job(JOB)
        self.assertEqual(back, b"")
        h.daemon._forward_data = original          # type: ignore[assignment]
        self.assertTrue(h.thread.is_alive(), "the daemon survives")
        h.send_job(JOB_B)
        self.assertEqual(h.state()["job"]["last_result"], "ok")


def _printer(model: PrinterModel) -> FakePrinter:
    return FakePrinter(model)


def _wait_polls(h: Harness, since: int, count: int) -> None:
    """Wait until the printer has parsed ``count`` more ``ESC i S`` at stream
    offsets from ``since`` on."""
    def polls() -> int:
        return len([o for o in h.model.status_requests if o >= since])

    target = polls() + count
    h.wait_for(lambda: polls() >= target, f"{count} more status requests")


class StatusTest(unittest.TestCase):
    def test_idle_polling_updates_the_state(self):
        h = Harness(self, poll_seconds=0.2).start()
        first = h.state()["status"]["polled"]
        h.wait_state(lambda s: s["status"]["polled"] > first + 0.1, "a second poll")
        h.model.set_media("29x90")
        state = h.wait_state(lambda s: s["status"]["parsed"]["media_width_mm"] == 29, "die-cut roll")
        self.assertEqual(state["status"]["media"]["label_id"], "29x90")
        self.assertFalse(state["status"]["parsed"]["two_color"])
        received = h.session_bytes()
        self.assertTrue(received.startswith(RESYNC_REQUEST))
        rest = received[len(RESYNC_REQUEST):]
        self.assertEqual(rest.replace(STATUS_REQUEST, b""), b"", "resync only when the state is unknown")
        self.assertGreaterEqual(rest.count(STATUS_REQUEST), 2)

    def test_cover_open_reply_is_recorded(self):
        h = Harness(self, poll_seconds=0.2).start()
        h.model.set_cover_open(True)
        state = h.wait_state(lambda s: s["status"]["parsed"]["err2"] == 0x10, "cover open")
        self.assertEqual(state["status"]["parsed"]["status_type"], 0x02)
        self.assertIn("cover open", state["status"]["parsed"]["errors"])
        self.assertEqual(state["device"]["state"], "ready", "a printer error is not a USB error")
        h.model.set_cover_open(False)
        h.wait_state(lambda s: s["status"]["parsed"]["errors"] == [], "cover closed")

    def test_unanswered_status_requests_mark_the_device(self):
        h = Harness(self, poll_seconds=0.1, status_timeout=0.2).start()
        sessions = len(h.printer.sessions)
        h.model.mute = True
        state = h.wait_state(lambda s: s["device"]["state"] == "error", "device error")
        self.assertIn("status requests", state["device"]["detail"])
        h.wait_for(lambda: len(h.printer.sessions) > sessions, "the printer is reopened")
        h.model.mute = False
        h.wait_state(lambda s: s["device"]["state"] == "ready", "ready again")

    def test_printing_printer_that_does_not_answer_is_not_reopened_at_once(self):
        # A reopen resynchronises at once, and its ESC @ would cut a label short:
        # while the last frame showed printing, the reopen waits (post_job_timeout).
        h = Harness(self, poll_seconds=0.1, status_timeout=0.2, post_job_timeout=2.5).start()
        sessions = len(h.printer.sessions)
        h.model.mute = True
        h.model.inject(h.model.frame(ST_PHASE_CHANGE, PHASE_PRINTING))   # a long label is printing
        h.wait_state(lambda s: s["status"]["parsed"]["phase_type"] == PHASE_PRINTING, "printing frame")
        time.sleep(1.5)                            # three unanswered polls take about 0.9 s
        self.assertEqual(len(h.printer.sessions), sessions, "no reopen while it prints")
        h.wait_for(lambda: len(h.printer.sessions) > sessions, "reopened once the bound has passed")
        h.model.mute = False
        h.wait_state(lambda s: s["device"]["state"] == "ready" and s["status"]["raw_hex"], "ready again")

    def test_unplug_and_replug(self):
        h = Harness(self).start()
        h.printer.unplug()
        state = h.wait_state(lambda s: s["device"]["state"] == "absent", "printer absent")
        self.assertIsNone(state["device"]["transport"])
        self.assertEqual(state["device"]["serial"], FAKE_SERIAL, "the identity of the last printer is kept")
        h.printer.replug()
        h.wait_state(lambda s: s["device"]["state"] == "ready" and s["device"]["transport"], "ready again")
        self.assertGreaterEqual(len(h.printer.sessions), 2)
        self.assertTrue(h.session_bytes().startswith(RESYNC_REQUEST), "a reconnected printer is resynced")

    def test_editor_lite_mode(self):
        h = Harness(self, device_wait=0.3).start()
        h.bus.set_editor_lite(True)
        state = h.wait_state(lambda s: s["device"]["state"] == "mass-storage", "Editor Lite")
        self.assertIn("Editor Lite", state["device"]["detail"])
        self.assertEqual(state["device"]["pid"], "209c", "identity stays the printer's")
        written = h.printer.total_written
        sock = h.connect()
        try:
            sock.sendall(JOB_B)
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self.assertEqual(_recv_until_closed(sock), b"")
        h.wait_state(lambda s: s["job"]["last_result"] == "error", "job refused")
        self.assertEqual(h.printer.total_written, written, "nothing reaches the printer")
        h.bus.set_editor_lite(False)
        h.wait_state(lambda s: s["device"]["state"] == "ready", "printer mode again")

    def test_busy_device_is_reported_and_retried(self):
        bus = FakeBus()
        bus.printer.open_error = usbio.DeviceBusy("device busy (another program has the printer open)")
        h = Harness(self, bus=bus).start(wait_ready=False)
        state = h.wait_state(lambda s: s["device"]["state"] == "error", "busy printer")
        self.assertIn("busy", state["device"]["detail"])
        bus.printer.open_error = None
        h.wait_state(lambda s: s["device"]["state"] == "ready", "ready once free")


class PrinterLinkTest(unittest.TestCase):
    """When the printer's input counts as unknown, and what a resync sends."""

    PROBE = INVALIDATE + STATUS_REQUEST

    def setUp(self) -> None:
        self.bus = FakeBus()
        self.model = self.bus.printer.model
        settings = ProxySettings(status_timeout=0.3, rescan_seconds=0.1)
        self.link = proxyd.PrinterLink(self.bus, settings, lambda *_args: None, lambda: None,
                                       lambda *_args: None)
        self.link.maintain(force=True)
        self.assertTrue(self.link.ready)
        self.addCleanup(self.link.shutdown)

    def sent(self) -> bytes:
        return bytes(self.bus.printer.sessions[-1])

    def make_unknown_while_the_cover_is_open(self) -> int:
        self.link.request()                        # first contact: resync
        self.model.set_cover_open(True)
        self.assertTrue(self.link.request().is_error)
        self.link.unknown = True                   # as a torn job leaves it
        return len(self.sent())

    def test_first_request_resynchronises(self):
        self.assertTrue(self.link.unknown)
        self.link.request()
        self.assertFalse(self.link.unknown)
        self.assertEqual(self.sent(), RESYNC_REQUEST)

    def test_unanswered_request_keeps_the_stream_position(self):
        self.link.request()
        self.model.mute = True
        with self.assertRaises(usbio.StatusTimeout) as caught:
            self.link.request()
        self.assertNotIsInstance(caught.exception, usbio.StatusRequestNotAccepted)
        self.assertFalse(self.link.unknown, "a request written but not answered leaves a command boundary")
        self.model.mute = False
        self.link.request()
        self.assertEqual(self.sent(), RESYNC_REQUEST + STATUS_REQUEST * 2)

    def test_request_not_taken_at_all_changes_nothing(self):
        self.link.request()
        self.bus.printer.set_stalled(True)
        with self.assertRaises(usbio.StatusRequestNotAccepted) as caught:
            self.link.request()
        self.assertEqual(caught.exception.written, 0)
        self.assertFalse(self.link.unknown)

    def test_request_written_in_part_makes_the_stream_unknown(self):
        self.link.request()
        printer = self.bus.printer
        printer.stall_after = printer.total_written + 1
        with self.assertRaises(usbio.StatusRequestNotAccepted) as caught:
            self.link.request()
        self.assertTrue(caught.exception.stream_unknown)
        self.assertTrue(self.link.unknown)
        printer.set_stalled(False)
        self.link.request()
        self.assertFalse(self.link.unknown)
        self.assertTrue(self.sent().endswith(STATUS_REQUEST[:1] + RESYNC_REQUEST))

    def test_esc_at_waits_for_two_clean_probes(self):
        start = self.make_unknown_while_the_cover_is_open()
        self.assertTrue(self.link.request().is_error)      # probe: still open
        self.assertTrue(self.link.unknown)
        self.model.set_cover_open(False)
        self.assertFalse(self.link.request().is_error)     # first clean probe
        self.assertTrue(self.link.unknown)
        self.link.request()                                # second clean probe, then the resync
        self.assertFalse(self.link.unknown)
        self.assertEqual(self.sent()[start:], self.PROBE * 3 + RESYNC_REQUEST)
        self.link.request()
        self.assertTrue(self.sent().endswith(RESYNC_REQUEST + STATUS_REQUEST))

    def test_printing_frame_between_probes_restarts_the_count(self):
        start = self.make_unknown_while_the_cover_is_open()
        self.model.set_cover_open(False)
        self.link.request()                                # clean probe 1
        self.model.inject(self.model.frame(ST_PHASE_CHANGE, PHASE_PRINTING))
        self.assertTrue(self.link.collect(1.0))            # a held job starts printing
        self.link.request()                                # clean probe 1 again
        self.assertTrue(self.link.unknown)
        self.link.request()                                # clean probe 2, then the resync
        self.assertFalse(self.link.unknown)
        self.assertEqual(self.sent()[start:], self.PROBE * 3 + RESYNC_REQUEST)

    def test_no_roll_defers_esc_at(self):
        # The tested unit reports a removed roll as width 0 / media type 0 with no
        # error bit; a printer without media holds jobs like one with the cover open.
        self.link.request()
        self.model.base[10] = self.model.base[11] = 0
        self.assertFalse(self.link.request().has_media)
        self.link.unknown = True
        start = len(self.sent())
        self.link.request()
        self.assertEqual(self.sent()[start:], self.PROBE)
        self.assertTrue(self.link.unknown)

    def test_waiting_frames_count_before_the_decision(self):
        self.link.request()
        self.link.unknown = True
        self.model.inject(self.model.frame(ST_PHASE_CHANGE, PHASE_PRINTING))
        time.sleep(0.05)
        start = len(self.sent())
        self.link.request()
        self.assertEqual(self.sent()[start:], self.PROBE, "the printing frame waiting on USB defers ESC @")

    def test_a_reopened_printer_is_resynchronised_at_once(self):
        self.make_unknown_while_the_cover_is_open()
        self.link.lost("test reopen", state="error", retry_in=0.0)
        self.link.maintain(force=True)
        self.assertTrue(self.link.ready)
        self.assertIsNone(self.link.last_frame)
        self.link.request()
        self.assertEqual(self.sent(), RESYNC_REQUEST, "nothing known after a reopen: full resync")


class LifecycleTest(unittest.TestCase):
    def test_ready_follows_the_first_device_scan(self):
        notifier = RecordingNotifier()
        Harness(self, notifier=notifier).start()
        first = notifier.messages[0]
        self.assertEqual(first.get("READY"), 1)
        self.assertIn("printer attached", str(first.get("STATUS")), "not 'looking for the printer'")

    def test_stop_while_idle(self):
        notifier = RecordingNotifier()
        h = Harness(self, notifier=notifier).start()
        h.stop()
        self.assertEqual(h.exit_code, 0)
        state = h.state()
        self.assertIsNone(state["proxy"]["pid"], "a stopped daemon is marked as such")
        self.assertFalse(state["job"]["active"])
        self.assertEqual(notifier.messages[0].get("READY"), 1)
        self.assertEqual(notifier.values("STOPPING"), [1])
        self.assertTrue(h.printer.closed)

    def test_stop_lets_the_current_job_finish(self):
        h = Harness(self, stop_grace=5.0).start()
        sock = h.connect()
        half = len(JOB) // 2
        sock.sendall(JOB[:half])
        h.wait_state(lambda s: s["job"]["active"], "job in flight")
        h.daemon.request_stop()
        time.sleep(0.3)
        sock.sendall(JOB[half:])
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(_recv_until_closed(sock), b"")
        h.thread.join(15)
        self.assertEqual(h.exit_code, 0)
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        self.assertEqual(h.session_bytes().count(JOB), 1)

    def test_stop_aborts_a_stuck_job_after_the_grace_period(self):
        h = Harness(self, stop_grace=0.3).start()
        sock = h.connect()
        sock.sendall(JOB[:3000])
        h.wait_state(lambda s: s["job"]["active"], "job in flight")
        h.daemon.request_stop()
        self.assertEqual(_recv_until_closed(sock), b"")
        h.thread.join(15)
        self.assertEqual(h.exit_code, 0)
        self.assertEqual(h.state()["job"]["last_result"], "torn")
        received = h.session_bytes()
        self.assertIn(RESYNC_REQUEST, received[received.index(JOB[:3000]):])

    def test_sd_notify_ready_status_and_watchdog(self):
        notifier = RecordingNotifier(watchdog_usec=200_000)      # ping every 0.1 s
        h = Harness(self, notifier=notifier).start()
        h.wait_for(lambda: len(notifier.values("WATCHDOG")) >= 3, "watchdog pings")
        self.assertEqual(notifier.messages[0].get("READY"), 1)
        statuses = [str(s) for s in notifier.values("STATUS")]
        self.assertTrue(any(s.startswith("ready: 62mm continuous black/red") for s in statuses), statuses)
        h.send_job(JOB_B)
        self.assertTrue(any(str(s).startswith("printing job from") for s in notifier.values("STATUS"))
                        or h.state()["job"]["last_result"] == "ok")


class SdNotifierTest(unittest.TestCase):
    def _datagram_socket(self) -> socket.socket:
        directory = tempfile.mkdtemp(prefix="qlp")
        self.addCleanup(lambda: os.rmdir(directory))
        path = os.path.join(directory, "n")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(path)
        self.addCleanup(os.unlink, path)
        self.addCleanup(sock.close)
        sock.settimeout(5)
        return sock

    def test_sends_to_a_path_socket(self):
        sock = self._datagram_socket()
        notifier = SdNotifier.from_environ({"NOTIFY_SOCKET": sock.getsockname()})
        self.assertTrue(notifier.enabled)
        self.assertTrue(notifier.notify(READY=1, STATUS="ready: line one\nline two"))
        self.assertEqual(sock.recv(4096), b"READY=1\nSTATUS=ready: line one line two\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "abstract sockets are Linux-only")
    def test_sends_to_an_abstract_socket(self):
        name = f"qlp-test-{os.getpid()}-{time.monotonic_ns()}"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind("\0" + name)
        sock.settimeout(5)
        notifier = SdNotifier.from_environ({"NOTIFY_SOCKET": "@" + name})
        self.assertTrue(notifier.notify(WATCHDOG=1))
        self.assertEqual(sock.recv(4096), b"WATCHDOG=1\n")

    def test_disabled_without_a_socket(self):
        notifier = SdNotifier.from_environ({})
        self.assertFalse(notifier.enabled)
        self.assertFalse(notifier.notify(READY=1))
        self.assertIsNone(notifier.watchdog_interval)

    def test_watchdog_interval_and_pid(self):
        env = {"NOTIFY_SOCKET": "/run/systemd/notify", "WATCHDOG_USEC": "60000000"}
        self.assertEqual(SdNotifier.from_environ(env).watchdog_interval, 30.0)
        env["WATCHDOG_PID"] = str(os.getpid())
        self.assertEqual(SdNotifier.from_environ(env).watchdog_interval, 30.0)
        env["WATCHDOG_PID"] = str(os.getpid() + 1)
        self.assertIsNone(SdNotifier.from_environ(env).watchdog_interval, "meant for another process")

    def test_unreachable_socket_does_not_raise(self):
        notifier = SdNotifier("/nonexistent/notify")
        self.assertFalse(notifier.notify(READY=1))


class SegmentTest(unittest.TestCase):
    """The job splitter: stops after a 0x1A at a command position, never at a
    0x1A byte inside raster data."""

    ROW = b"\x77\x01\x5a" + b"\x1a" * 90 + b"\x77\x02\x5a" + b"\x1a" * 90
    JOB = b"\x00" * 400 + b"\x1b\x40" + b"\x1b\x69\x61\x01" + ROW * 3 + b"\x1a"

    def test_split_at_the_job_end(self):
        seg = proxyd._Segment()
        self.assertEqual(seg.observe(self.JOB + PTOUCH_TRAILER), (len(self.JOB), True))
        self.assertTrue(seg.has_job_content())

    def test_split_across_reads(self):
        seg = proxyd._Segment()
        self.assertEqual(seg.observe(self.JOB[:450]), (450, False))
        self.assertEqual(seg.observe(self.JOB[450:] + b"\x1b"), (len(self.JOB) - 450, True))

    def test_trailer_segment_is_not_a_job(self):
        seg = proxyd._Segment(after_job=proxyd.JobRecord(peer="192.0.2.5:1", started=0.0, started_mono=0.0))
        self.assertEqual(seg.observe(PTOUCH_TRAILER), (len(PTOUCH_TRAILER), False))
        self.assertFalse(seg.has_job_content())
        follow = proxyd._Segment(after_job=seg.after_job)
        follow.observe(rasterproto.make_test_job(rows=150))
        self.assertTrue(follow.has_job_content(), "print data after a settled job is a new job")

    def test_status_request_alone_is_not_a_job(self):
        seg = proxyd._Segment()
        self.assertEqual(seg.observe(b"\x00" * 10 + STATUS_REQUEST), (13, False))
        self.assertFalse(seg.has_job_content())
        self.assertTrue(seg.at_rest)

    def test_a_trailer_that_ends_in_esc_i_a_ff_leaves_the_stream_at_rest(self):
        """Behind P-touch's trailer the stream stands at a command boundary until
        anything follows it; then it can no longer be judged."""
        job = proxyd.JobRecord(peer="192.0.2.5:1", started=0.0, started_mono=0.0)
        for pieces in ([PTOUCH_TRAILER], [PTOUCH_TRAILER[:7], PTOUCH_TRAILER[7:]]):
            with self.subTest(pieces=len(pieces)):
                seg = proxyd._Segment(after_job=job)
                self.assertTrue(seg.at_rest, "a settled job, nothing after it yet")
                for piece in pieces:
                    seg.observe(piece)
                self.assertIsNotNone(seg.tok.opaque)
                self.assertTrue(seg.at_rest)
                seg.observe(b"\x00")
                self.assertFalse(seg.at_rest, "a byte after ESC i a FF: unknown from here on")
        fresh = proxyd._Segment()
        fresh.observe(b"\x1b\x69\x61\xff")
        self.assertFalse(fresh.at_rest, "only the end of a settled job's trailer counts")
        other = proxyd._Segment(after_job=job)
        other.observe(b"\x1b\x69\x61\x00")    # ESC/P mode: not P-touch's trailer
        self.assertFalse(other.at_rest)


class FakeModelTest(unittest.TestCase):
    """The test double itself: what it counts as a job."""

    def test_ptouch_trailer_starts_no_job(self):
        model = PrinterModel()
        model.feed(JOB + PTOUCH_TRAILER + STATUS_REQUEST)
        self.assertEqual(len(model.jobs), 1)
        self.assertEqual(len(model.status_requests), 1)
        self.assertEqual(model.violations, [], "ESC i a FF ends the trailer; it does not open a job")

    def test_status_request_inside_a_job_is_a_violation(self):
        model = PrinterModel()
        first_row = JOB.index(b"\x77\x01\x5a")
        cut = first_row + 2 * (3 + 90)             # after one complete two-colour line
        model.feed(JOB[:cut] + STATUS_REQUEST)
        self.assertEqual(len(model.violations), 1)


class ListenerTest(unittest.TestCase):
    def test_parse_listen(self):
        self.assertEqual(proxyd.parse_listen("0.0.0.0:9100"), ("0.0.0.0", 9100))
        self.assertEqual(proxyd.parse_listen("192.0.2.10:9100"), ("192.0.2.10", 9100))
        self.assertEqual(proxyd.parse_listen("[2001:db8::1]:9100"), ("2001:db8::1", 9100))
        self.assertEqual(proxyd.parse_listen(":9101"), ("", 9101))
        self.assertEqual(proxyd.parse_listen("9102"), ("", 9102))
        for bad in ("2001:db8::1:9100", "host:port", "[::1]9100", "192.0.2.1:70000"):
            with self.assertRaises(ValueError, msg=bad):
                proxyd.parse_listen(bad)

    def test_bind_listener_any_address(self):
        sock = proxyd.bind_listener("", 0)
        self.addCleanup(sock.close)
        port = sock.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass

    @unittest.skipUnless(socket.has_dualstack_ipv6(), "no dual-stack IPv6 on this host")
    def test_bind_listener_is_dual_stack(self):
        sock = proxyd.bind_listener("", 0)
        self.addCleanup(sock.close)
        if sock.family != socket.AF_INET6:
            self.skipTest("IPv6 is disabled here; bind_listener fell back to IPv4")
        self.assertEqual(sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY), 0)
        port = sock.getsockname()[1]
        try:
            client = socket.create_connection(("::1", port), timeout=5)
        except OSError as exc:
            self.skipTest(f"no IPv6 loopback: {exc}")
        client.close()

    def test_format_peer(self):
        self.assertEqual(proxyd.format_peer(("::ffff:192.0.2.7", 51234, 0, 0)), "192.0.2.7:51234")
        self.assertEqual(proxyd.format_peer(("2001:db8::5", 9100, 0, 0)), "[2001:db8::5]:9100")
        self.assertEqual(proxyd.format_peer(("192.0.2.7", 1)), "192.0.2.7:1")

    def test_systemd_listeners_ignore_other_processes(self):
        env = {"LISTEN_PID": str(os.getpid() + 1), "LISTEN_FDS": "1", "LISTEN_FDNAMES": "x"}
        self.assertEqual(proxyd.systemd_listeners(env), [])
        self.assertEqual(env, {}, "the variables are consumed")
        self.assertEqual(proxyd.systemd_listeners({}), [])


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        cfg = config.load_runtime_config(None)
        settings = ProxySettings.from_config(cfg)
        self.assertEqual(settings.listen, ("", 9100))
        self.assertEqual(settings.poll_seconds, 3.0)
        self.assertEqual(settings.raw_idle_timeout, 300.0)
        self.assertEqual(settings.max_job_bytes, 16 * 1024 * 1024)
        self.assertEqual(settings.post_job_timeout, 60.0)
        self.assertEqual(settings.heartbeat_seconds, 3.0)
        self.assertEqual(settings.state_path, statecache.DEFAULT_PATH)
        self.assertFalse(settings.bidirectional)
        self.assertEqual(settings.device, "auto")

    def test_values_and_overrides(self):
        cfg = config.load_runtime_config(None)
        cfg.update(RAW_PORT="9101", RAW_BIND="192.0.2.10", RAW_BIDIRECTIONAL="yes", STATUS_POLL_SECONDS="5",
                   RAW_IDLE_TIMEOUT="120", MAX_JOB_BYTES="1048576", USB_PRODUCT_ID="209C",
                   STATE_FILE="/tmp/x.json")
        settings = ProxySettings.from_config(cfg, listen=("127.0.0.1", 0), device="/dev/usb/lp1")
        self.assertEqual(settings.listen, ("127.0.0.1", 0))
        self.assertTrue(settings.bidirectional)
        self.assertEqual(settings.poll_seconds, 5.0)
        self.assertEqual(settings.raw_idle_timeout, 120.0)
        self.assertEqual(settings.write_stall_timeout, 120.0)
        self.assertEqual(settings.max_job_bytes, 1048576)
        self.assertEqual(settings.product_id, "209c")
        self.assertEqual(settings.state_path, "/tmp/x.json")
        self.assertEqual(settings.device, "/dev/usb/lp1")
        self.assertEqual(ProxySettings.from_config(cfg).listen, ("192.0.2.10", 9101))

    def test_invalid_values(self):
        for key, value in (("RAW_IDLE_TIMEOUT", "1"), ("RAW_IDLE_TIMEOUT", "soon"), ("MAX_JOB_BYTES", "100")):
            with self.subTest(key=key, value=value):
                cfg = config.load_runtime_config(None)
                cfg[key] = value
                with self.assertRaises(config.ConfigError):
                    ProxySettings.from_config(cfg)
        with self.assertRaises(config.ConfigError):
            ProxySettings.from_config(config.load_runtime_config(None), device="lp0")


class MainTest(unittest.TestCase):
    def test_bad_config_exits_78(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runtime.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('STATUS_POLL_SECONDS="0"\n')
            done = subprocess.run([sys.executable, "-I", "-B", str(PROXYD), "--config", path],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, proxyd.EX_CONFIG, done.stderr)
        self.assertIn("configuration error", done.stderr)

    def test_version(self):
        done = subprocess.run([sys.executable, "-I", "-B", str(PROXYD), "--version"],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "ql810wc-proxyd 0.1.0")

    def test_usage_errors_exit_64(self):
        for args in (["--listen", "nowhere:9100"], ["--bogus"]):
            with self.subTest(args=args):
                done = subprocess.run([sys.executable, "-I", "-B", str(PROXYD), *args],
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, proxyd.EX_USAGE, done.stderr)

    def test_bad_limits_exit_78(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runtime.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('RAW_IDLE_TIMEOUT="1"\n')
            done = subprocess.run([sys.executable, "-I", "-B", str(PROXYD), "--config", path],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, proxyd.EX_CONFIG, done.stderr)
        self.assertIn("RAW_IDLE_TIMEOUT", done.stderr)


_ACTIVATE = """
import os, sys
fd = int(os.environ.pop("TEST_LISTEN_FD"))
if fd != 3:
    os.dup2(fd, 3)
    os.close(fd)
os.environ["LISTEN_FDS"] = "1"
os.environ["LISTEN_PID"] = str(os.getpid())
os.execv(sys.executable, [sys.executable, "-I", "-B"] + sys.argv[1:])
"""


@unittest.skipUnless(HAVE_PTY, "needs a POSIX pseudo-terminal")
class SubprocessTest(unittest.TestCase):
    """The installed entry point: socket activation, sd_notify, a real
    UsblpTransport (on a pty standing in for /dev/usb/lp0) and SIGTERM."""

    def test_socket_activation_job_and_sigterm(self):
        printer = PtyPrinter()
        self.addCleanup(printer.close)
        tmp = tempfile.mkdtemp(prefix="qlp")
        self.addCleanup(_rmtree, tmp)
        runtime = os.path.join(tmp, "runtime.env")
        with open(runtime, "w", encoding="utf-8") as fh:
            fh.write('STATUS_POLL_SECONDS="1"\nSTATUS_MAX_AGE_SECONDS="10"\nLOG_LEVEL="DEBUG"\n')
        state_path = os.path.join(tmp, "state.json")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(notify.close)
        notify_path = os.path.join(tmp, "notify")
        notify.bind(notify_path)
        notify.settimeout(0.2)
        env = dict(os.environ, TEST_LISTEN_FD=str(listener.fileno()), NOTIFY_SOCKET=notify_path,
                   WATCHDOG_USEC="1000000")
        env.pop("JOURNAL_STREAM", None)
        log_path = os.path.join(tmp, "proxyd.log")
        with open(log_path, "wb") as log_file:
            proc = subprocess.Popen(
                [sys.executable, "-c", _ACTIVATE, str(PROXYD), "--config", runtime, "--state", state_path,
                 "--device", printer.path],
                env=env, pass_fds=(listener.fileno(),), stdout=log_file, stderr=subprocess.STDOUT)
        self.addCleanup(_kill, proc)
        messages: List[str] = []

        def collect(until: Callable[[], bool], timeout: float, what: str) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if until():
                    return
                try:
                    messages.extend(notify.recv(4096).decode().splitlines())
                except socket.timeout:
                    if proc.poll() is not None:     # exited and nothing more queued
                        break
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                raise AssertionError(f"timed out waiting for {what}; messages {messages}\n{fh.read()}")

        collect(lambda: "READY=1" in messages, 20, "READY=1")
        cache = statecache.StateCache(state_path)
        collect(lambda: (cache.read() or {}).get("device", {}).get("state") == "ready", 20, "printer ready")
        self.assertEqual(cache.read()["proxy"]["pid"], proc.pid)
        self.assertEqual(cache.read()["device"]["transport"], "usblp")
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(JOB)
            sock.shutdown(socket.SHUT_WR)
            self.assertEqual(_recv_until_closed(sock, timeout=30), b"")
        self.assertEqual(cache.read()["job"]["last_result"], "ok")
        self.assertEqual(printer.model.received.count(JOB), 1)
        self.assertEqual(printer.model.violations, [])
        collect(lambda: "WATCHDOG=1" in messages, 10, "WATCHDOG=1")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 0)
        collect(lambda: "STOPPING=1" in messages, 5, "STOPPING=1")
        self.assertIsNone(cache.read()["proxy"]["pid"])


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def _rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
