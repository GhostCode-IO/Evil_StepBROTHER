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
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

from tests.fake_printer import (FAKE_SERIAL, PTOUCH_TRAILER, RESYNC_REQUEST, TEST_DEVICE_ID, FakeBus,
                                PrinterModel, PtyPrinter)

from ql810wc import config, media, proxyd, rasterproto, statecache, usbio  # noqa: E402  (path set by fake_printer)
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
MONO = rasterproto.make_test_job(two_color=False, rows=150)         # black only, 62 mm
#: ESC i z: the first command of a test job's content (the bytes before it are set-up).
PRINT_INFO = b"\x1b\x69\x7a"
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


def _send_and_close(sock: socket.socket, data: bytes) -> str:
    """Send ``data`` and half-close, like CUPS does; then "rst" if the proxy
    reset the connection (while it was being sent or after), "fin" for an
    ordinary close. A send that fails counts as the reset: once a send has
    reported it, Linux returns end of file to the next read."""
    try:
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        return "rst"
    return _how_closed(sock)


def _send_refusable(h: "Harness", data: bytes) -> str:
    """A job on a new connection; see _send_and_close."""
    return _send_and_close(h.connect(), data)


def _wait_logged(h: "Harness", logs: Any, text: str) -> None:
    """Inside assertLogs: wait until a captured line contains ``text`` (the
    daemon logs from its own thread)."""
    h.wait_for(lambda: any(text in line for line in logs.output), f"a log line with {text!r}")


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


class ColorCheckTest(unittest.TestCase):
    """RAW_COLOR_CHECK: a job whose colour mode the loaded roll cannot print
    never reaches the printer; every other job goes through byte for byte."""

    def assert_nothing_of_the_job_arrived(self, h: Harness) -> None:
        self.assertNotIn(PRINT_INFO, h.session_bytes())
        self.assertEqual(h.model.jobs, [])
        self.assertEqual(h.model.violations, [])
        self.assertFalse(h.model.mute)

    def test_black_only_job_on_a_black_red_roll_is_refused(self):
        h = Harness(self).start()
        with self.assertLogs("ql810wc.proxyd", "WARNING") as logs:
            self.assertEqual(_send_refusable(h, MONO), "rst", "the client sees the job fail")
        job = h.state()["job"]
        self.assertEqual(job["last_result"], "refused")
        self.assertFalse(job["active"])
        self.assertIs(job["two_color"], False)
        self.assertEqual(job["requested_media"]["width"], 62)
        self.assertEqual(job["last_detail"], "a black-only job, but the loaded roll, 62mm continuous black/red "
                                             "(DK-22251), takes two-color jobs only")
        self.assertTrue(any("refused a print job from 127.0.0.1:" in line
                            and "None of its print data was sent to the printer" in line
                            for line in logs.output), logs.output)
        self.assert_nothing_of_the_job_arrived(h)
        start = len(h.session_bytes())
        _wait_polls(h, start, 2)
        self.assertEqual(h.session_bytes()[start:].replace(STATUS_REQUEST, b""), b"",
                         "no resync: the printer's input was never touched")
        h.send_job(JOB)
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        self.assertEqual(h.session_bytes().count(JOB), 1)

    def test_two_color_job_on_a_single_color_roll_is_refused(self):
        h = Harness(self)
        h.model.set_media("62")                     # DK-22205: one colour
        h.start()
        self.assertEqual(_send_refusable(h, JOB), "rst")
        job = h.state()["job"]
        self.assertEqual(job["last_result"], "refused")
        self.assertIs(job["two_color"], True)
        self.assertIn("a two-color job", job["last_detail"])
        self.assertIn("cannot print red", job["last_detail"])
        self.assert_nothing_of_the_job_arrived(h)
        h.send_job(MONO)
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        self.assertEqual(h.session_bytes().count(MONO), 1, "a black-only job on this roll goes through")

    def test_the_check_off_forwards_every_job(self):
        with self.assertLogs("ql810wc.proxyd", "WARNING") as logs:
            h = Harness(self, color_check=False).start()
        self.assertTrue(any("RAW_COLOR_CHECK is off" in line for line in logs.output), logs.output)
        h.send_job(MONO)
        self.assertEqual(h.session_bytes().count(MONO), 1, "forwarded byte for byte")
        job = h.state()["job"]
        self.assertEqual(job["last_result"], "error")
        self.assertIn("media does not support monochrome printing", job["last_detail"])
        self.assertTrue(h.model.jobs[-1]["failed"])
        self.assertTrue(h.model.mute, "what the check prevents: the printer no longer answers")

    def test_an_uncertain_colour_mode_goes_through(self):
        # The check does not guess. Without ESC i K the printer presumably prints in its
        # default mode, black only, and the fake printer then reports the mono error.
        k = MONO.index(b"\x1b\x69\x4b")          # ESC i K 08
        cases = {
            "no ESC i K": MONO[:k] + MONO[k + 4:],
            "ESC i K two-colour with g rows": MONO[:k] + b"\x1b\x69\x4b\x09" + MONO[k + 4:],
        }
        for name, job in cases.items():
            with self.subTest(name):
                h = Harness(self).start()
                h.send_job(job)
                self.assertEqual(h.session_bytes().count(job), 1)
                self.assertNotEqual(h.state()["job"]["last_result"], "refused")

    def test_an_uncertain_roll_goes_through(self):
        stale = Harness(self, status_max_age=0.0).start()
        stale.send_job(MONO)
        self.assertEqual(stale.session_bytes().count(MONO), 1, "no status frame recent enough")
        unknown = Harness(self)
        unknown.model.base[25] = 0x00               # neither 0x81 nor 0x01
        unknown.start()
        unknown.send_job(MONO)
        self.assertEqual(unknown.session_bytes().count(MONO), 1, "a byte 25 the check does not know")
        self.assertEqual(unknown.state()["job"]["last_result"], "ok")
        empty = Harness(self)
        empty.model.base[10] = 0                    # width 0: the roll was taken out
        empty.start()
        empty.send_job(MONO)
        self.assertEqual(empty.session_bytes().count(MONO), 1, "no roll: the printer reports it itself")

    def test_a_job_is_held_until_it_shows_its_colour_mode(self):
        h = Harness(self, poll_seconds=0.1).start()
        cut = JOB.index(b"\x77\x01")               # the first raster row
        sock = h.connect()
        sock.sendall(JOB[:cut])
        h.wait_state(lambda s: s["job"]["active"], "the job started")
        time.sleep(0.5)                            # five poll intervals
        self.assertNotIn(PRINT_INFO, h.session_bytes(), "held: none of the job is with the printer yet")
        sock.sendall(JOB[cut:])
        sock.shutdown(socket.SHUT_WR)
        _recv_until_closed(sock)
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        self.assertEqual(h.session_bytes().count(JOB), 1, "then forwarded whole, in order, unmodified")
        self.assertEqual(h.model.violations, [])

    def test_a_job_that_ends_while_held_sends_nothing(self):
        header = JOB[JOB.index(PRINT_INFO):JOB.index(b"\x77\x01")]   # job content, no raster row
        h = Harness(self).start()
        self.assertEqual(h.send_job(header), b"")
        job = h.state()["job"]
        self.assertEqual(job["last_result"], "error")
        self.assertIn("none of its print data was sent to the printer", job["last_detail"])
        sock = h.connect()
        sock.sendall(header)
        h.wait_state(lambda s: s["job"]["active"], "the second job started")
        _reset(sock)
        job = h.wait_state(lambda s: s["job"]["last_result"] == "torn", "torn job")["job"]
        self.assertIn("none of its print data was sent to the printer", job["last_detail"])
        received = h.session_bytes()
        self.assertTrue(received.startswith(RESYNC_REQUEST))
        self.assertEqual(received[len(RESYNC_REQUEST):].replace(STATUS_REQUEST, b""), b"",
                         "nothing sent, so nothing to resynchronise")

    def test_a_mode_shown_before_the_stream_turns_opaque_counts(self):
        # An unknown command right after the first g row: refused however the job is split.
        row = MONO.index(b"\x67\x00")
        cut = row + 3 + MONO[row + 2]               # just after the first g row
        job = MONO[:cut] + b"\x1b\x69\x55\x58\x00" + MONO[cut:]
        for pieces in (1, 2):
            with self.subTest(pieces=pieces):
                h = Harness(self).start()
                sock = h.connect()
                if pieces == 1:
                    how = _send_and_close(sock, job)
                else:
                    sock.sendall(job[:cut])
                    time.sleep(0.2)                    # the refusal comes with this first read
                    how = _send_and_close(sock, job[cut:])
                self.assertEqual(how, "rst")
                self.assertEqual(h.state()["job"]["last_result"], "refused")
                self.assertNotIn(PRINT_INFO, h.session_bytes())
                self.assertFalse(h.model.mute)

    def test_a_stale_roll_is_read_again_before_the_check(self):
        # The idle polls pause while set-up commands wait on a connection, so the last
        # status goes stale; the check asks for a new one instead of letting the job through.
        h = Harness(self, poll_seconds=0.1, status_max_age=1.0).start()
        setup = INVALIDATE + rasterproto.ESC_INIT
        sock = h.connect()
        sock.sendall(setup)
        h.wait_for(lambda: h.session_bytes().endswith(setup), "the set-up bytes reached the printer")
        start = len(h.session_bytes())
        time.sleep(1.5)
        self.assertEqual(h.session_bytes()[start:], b"", "no polls while a job may follow")
        self.assertEqual(_send_and_close(sock, MONO[len(setup):]), "rst")
        self.assertEqual(h.state()["job"]["last_result"], "refused")
        rest = h.session_bytes()[start:]
        self.assertTrue(rest.startswith(STATUS_REQUEST), "one status request before the decision")
        self.assertNotIn(PRINT_INFO, rest)
        self.assertEqual(h.model.violations, [])

    def test_a_second_job_cut_inside_its_first_command(self):
        """After a settled job, a read that ends inside the next job's first command
        (here half of ESC i z) keeps that command back: a refused job leaves none of
        it with the printer, and a job that passes reaches it whole."""
        for second, refused in ((MONO, True), (JOB, False)):
            with self.subTest(refused=refused):
                h = Harness(self).start()
                cut = second.index(PRINT_INFO) + 2              # 1B 69 of ESC i z
                sock = h.connect()
                sock.sendall(JOB)
                h.wait_state(lambda s: s["job"]["last_result"] == "ok" and not s["job"]["active"],
                             "the first job printed")
                sock.sendall(second[:cut])
                h.wait_for(lambda: h.session_bytes().endswith(second[:cut - 2]), "the second job's set-up arrived")
                time.sleep(0.2)
                self.assertFalse(h.session_bytes().endswith(second[:cut]), "the cut ESC i z is kept back")
                start = len(h.session_bytes())
                self.assertEqual(_send_and_close(sock, second[cut:]), "rst" if refused else "fin")
                state = h.state()["job"]
                rest = h.session_bytes()[start:]
                if refused:
                    self.assertEqual(state["last_result"], "refused")
                    self.assertEqual(rest.replace(STATUS_REQUEST, b""), b"", "none of the job, and no resync")
                else:
                    self.assertEqual(state["last_result"], "ok")
                    self.assertTrue(rest.startswith(second[cut - 2:]), "the job goes on whole, from its ESC i z")
                    self.assertEqual(state["bytes"], len(second) - (cut - 2), "its first command counts as the job's")
                self.assertEqual(h.model.violations, [])

    def test_a_second_job_is_held_from_its_first_print_data(self):
        # Brother's Windows driver sends ESC i U J (a job id) before ESC i z.
        z = MONO.index(PRINT_INFO)
        job_id = b"\x1b\x69\x55\x4a" + bytes([0x00, 0x0C] + [0] * 9 + [0x42, 0, 0])
        second = MONO[:z] + job_id + MONO[z:]
        h = Harness(self).start()
        sock = h.connect()
        sock.sendall(JOB)
        h.wait_state(lambda s: s["job"]["last_result"] == "ok" and not s["job"]["active"], "the first job printed")
        start = len(h.session_bytes())
        self.assertEqual(_send_and_close(sock, second), "rst")
        self.assertEqual(h.state()["job"]["last_result"], "refused")
        rest = h.session_bytes()[start:]
        self.assertNotIn(job_id, rest, "held from its first print data, not from ESC i z")
        self.assertNotIn(PRINT_INFO, rest)
        self.assertFalse(h.model.mute)

    def test_a_job_that_does_not_show_its_colour_mode_soon_goes_through(self):
        # ESC i K 08, then 20000 blank lines before the first g row, sent in two parts.
        k = MONO.index(b"\x1b\x69\x4b") + 4
        job = MONO[:k] + b"\x5a" * 20000 + MONO[k:]
        h = Harness(self).start()
        with mock.patch.object(proxyd, "_CHECK_HOLD_LIMIT", 1000):
            h.send_job(job, pieces=2, pause=0.3)
        self.assertEqual(h.session_bytes().count(job), 1, "past the hold limit the job goes through unchecked")
        self.assertNotEqual(h.state()["job"]["last_result"], "refused")


#: A job's set-up bytes: 400 x NUL, ESC @, ESC i a 01 (raster command mode).
SETUP = INVALIDATE + rasterproto.ESC_INIT + b"\x1b\x69\x61\x01"
#: One two-colour raster line (a w row for each plane) and one black-only line (a g row).
LINE_W = b"\x77\x01\x5a" + b"\xff" * 90 + b"\x77\x02\x5a" + bytes(90)
LINE_G = b"\x67\x00\x5a" + b"\xff" * 90


def _info(flags: int, kind: int, width: int, length: int = 0, lines: int = 20, page: int = 0) -> bytes:
    """The ten parameter bytes of an ``ESC i z``: n1 flags, media type, width
    and length (mm), raster lines (n5-n8) and page (n9)."""
    return bytes((flags, kind, width, length)) + lines.to_bytes(4, "little") + bytes((page, 0))


# ESC i z parameters as Brother's macOS driver (4.0.3, PPD 4.0.4) sends them on a network
# queue (local renders). Flags 0x8E ask the printer to check the media type, width and length.
MAC_62 = bytes.fromhex("8E 0A 3E 00 57 04 00 00 00 00")        # 2.4": 62 mm continuous, 1111 lines
MAC_62_748 = bytes.fromhex("8E 0A 3E 00 EC 02 00 00 00 00")    # Length 69.1 mm, Margin 3 mm: 748 lines
MAC_DC03 = bytes.fromhex("8E 0B 1D 5A DF 03 00 00 00 00")      # 1.1" x 3.5" (the queue's default): 29x90 die-cut
MAC_DC06 = bytes.fromhex("8E 0B 3E 1D 0F 01 00 00 00 00")      # 2.4" x 1.1": 62x29 die-cut
MAC_29 = bytes.fromhex("8E 0A 1D 00 57 04 00 00 00 00")        # 1.1": 29 mm continuous
MAC_HIGH_RES = bytes.fromhex("CE 0A 3E 00 AF 08 00 00 00 00")  # 2-Color + High resolution (ESC i K 49), 62 mm


def _page(info: Optional[bytes], expanded: Optional[int] = 0x09, *, lines: int = 20, last: bool = True,
          before_rows: bytes = b"") -> bytes:
    """A page laid out like make_test_job's: ``ESC i z`` + ``info`` (None: no
    ESC i z), ESC i M 40, ESC i A 01, ``ESC i K`` ``expanded`` (None: no ESC i
    K), ESC i d 23 00, ``before_rows``, ``lines`` raster lines (w rows when
    ``expanded`` asks for two colours, else g rows), then 0x1A (``last``) or 0x0C."""
    out = b"" if info is None else PRINT_INFO + info
    out += b"\x1b\x69\x4d\x40" + b"\x1b\x69\x41\x01"
    if expanded is not None:
        out += b"\x1b\x69\x4b" + bytes((expanded,))
    out += b"\x1b\x69\x64\x23\x00" + before_rows
    out += (LINE_W if expanded is not None and expanded & 0x01 else LINE_G) * lines
    return out + (b"\x1a" if last else b"\x0c")


def _job(*pages: bytes) -> bytes:
    """The set-up bytes, then ``pages``."""
    return SETUP + b"".join(pages)


#: The queue's default paper (29x90 die-cut) as a two-colour and as a black-only job.
TWO_DC03 = _job(_page(MAC_DC03))
MONO_DC03 = _job(_page(MAC_DC03, 0x08))
#: A two-colour job for 62 mm continuous tape (DK-22251 takes it).
TWO_62 = _job(_page(_info(0x86, 0x0A, 62)))
#: The fake printer's roll unless a test loads another one.
DK22251 = "62mm continuous black/red (DK-22251)"


def _send_bytewise(sock: socket.socket, data: bytes) -> str:
    """Send ``data`` one byte per TCP segment and half-close; then how the
    proxy ended the connection (see _send_and_close)."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        for i in range(len(data)):
            sock.sendall(data[i:i + 1])
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        return "rst"
    return _how_closed(sock)


def _requests_before_the_job(h: Harness, start: int) -> int:
    """``ESC i S`` the printer got from stream offset ``start`` up to the next ``ESC i z``."""
    received = bytes(h.model.received)
    return received[start:received.index(PRINT_INFO, start)].count(STATUS_REQUEST)


class _GoneOnRequest(FakePrinter):
    """A fake printer that is unplugged by the next status request once ``armed``."""

    armed = False

    def write(self, data: usbio.BytesLike, timeout: float) -> int:
        if self.armed and bytes(data).endswith(STATUS_REQUEST):
            self.armed = False
            self.unplug()
        return super().write(data, timeout)


class MediaCheckTest(unittest.TestCase):
    """RAW_MEDIA_CHECK: a job whose first page asks the printer to check media
    that the loaded roll does not match, or a two-colour job in high
    resolution on a black/red roll, never reaches the printer. Brother
    documents that the printer rejects such a job ("replace media"), and
    owners of the QL-800 and QL-810W report that it then stops until the roll
    cover is opened and closed; the fake printer models that (it is not seen
    on the tested unit). Every other job goes through byte for byte."""

    def assert_refused(self, h: Harness, job: bytes, detail: str) -> List[str]:
        """Send ``job`` on a new connection: the client sees a reset, the job is
        recorded and logged as refused with ``detail``, and none of its print
        data reaches the printer, which still answers. Returns the log lines."""
        start = len(h.model.received)
        jobs = len(h.model.jobs)
        with self.assertLogs("ql810wc.proxyd", "INFO") as logs:
            self.assertEqual(_send_refusable(h, job), "rst", "the client sees the job fail")
            _wait_logged(h, logs, f", result refused ({detail})")       # the job line
        state = h.state()["job"]
        self.assertEqual(state["last_result"], "refused")
        self.assertFalse(state["active"])
        self.assertEqual(state["last_detail"], detail)
        self.assertTrue(any(line.startswith("WARNING:ql810wc.proxyd:refused a print job from 127.0.0.1:")
                            and line.endswith(f": {detail}. None of its print data was sent to the printer")
                            for line in logs.output), logs.output)
        rest = bytes(h.model.received[start:])
        self.assertNotIn(PRINT_INFO, rest, "none of the job's print data reached the printer")
        self.assertTrue(SETUP.startswith(rest.replace(STATUS_REQUEST, b"")),
                        "status requests only (and the job's set-up bytes, had they come in a read of their own)")
        self.assertEqual(len(h.model.jobs), jobs)
        self.assertEqual(h.model.violations, [])
        self.assertFalse(h.model.mute, "the printer still answers")
        return logs.output

    def assert_through(self, h: Harness, job: bytes) -> Tuple[Dict[str, Any], List[str]]:
        """Send ``job``: it reaches the printer once, whole and unmodified, and is
        not refused. Returns the job's state and the log lines."""
        before = bytes(h.model.received).count(job)
        with self.assertLogs("ql810wc.proxyd", "INFO") as logs:
            self.assertEqual(h.send_job(job), b"")
            _wait_logged(h, logs, ", result ")                          # the job line
        self.assertEqual(bytes(h.model.received).count(job), before + 1, "forwarded byte for byte")
        state = h.state()["job"]
        self.assertNotEqual(state["last_result"], "refused")
        self.assertEqual(h.model.violations, [])
        return state, logs.output

    # -- refused -------------------------------------------------------------------
    def test_die_cut_labels_on_continuous_tape_are_refused(self):
        # The queue's default paper on DK-22251, two-colour: only the media is wrong.
        h = Harness(self).start()
        detail = f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}"
        output = self.assert_refused(h, TWO_DC03, detail)
        self.assertTrue(any("two-color, requested 29x90 mm die-cut, 991 lines (83.9 mm of print), flags 0x8e, "
                            f"result refused ({detail})" in line for line in output), output)
        job = h.state()["job"]
        self.assertIs(job["two_color"], True)
        self.assertEqual(job["requested_media"], {"flags": 0x8E, "type": 0x0B, "width": 29, "length": 90,
                                                  "lines": 991, "page": 0})
        self.assertNotIn("expanded_mode", job, "not published")
        start = len(h.model.received)
        _wait_polls(h, start, 2)
        self.assertEqual(bytes(h.model.received[start:]).replace(STATUS_REQUEST, b""), b"",
                         "no resync: the printer's input was never touched")
        self.assertEqual(self.assert_through(h, TWO_62)[0]["last_result"], "ok")

    def test_another_width_is_refused(self):
        h = Harness(self).start()
        self.assert_refused(h, _job(_page(MAC_29)),
                            f"a job for 29 mm continuous tape, but the loaded roll is {DK22251}")

    def test_continuous_tape_on_die_cut_labels_is_refused(self):
        # MONO (make_test_job) asks the printer to check the type and width (n1 0x86).
        h = Harness(self)
        h.model.set_media("62x100")                 # DK-11202, one colour
        h.start()
        self.assert_refused(h, MONO, "a job for 62 mm continuous tape, but the loaded roll is 62mm x 100mm die-cut "
                                     "(DK-11202)")
        self.assertEqual(self.assert_through(h, _job(_page(_info(0x8E, 0x0B, 62, 100), 0x08)))[0]["last_result"],
                         "ok", "a job for the roll's own labels")

    def test_a_die_cut_label_length_may_be_1_mm_off(self):
        # Status byte 17 is the printer's rounded label length (60x86 reads 87).
        h = Harness(self)
        h.model.set_media("29x90")                  # DK-11201, one colour
        h.start()
        for length in (88, 92):
            with self.subTest(length=length):
                self.assert_refused(h, _job(_page(_info(0x8E, 0x0B, 29, length), 0x08)),
                                    f"a job for 29x{length} mm die-cut labels, but the loaded roll is 29mm x 90mm "
                                    "die-cut (DK-11201)")
        for length in (89, 90, 91):
            with self.subTest(length=length):
                state, _ = self.assert_through(h, _job(_page(_info(0x8E, 0x0B, 29, length), 0x08)))
                self.assertEqual(state["last_result"], "ok")

    def test_a_black_only_job_for_die_cut_labels_names_both_problems(self):
        h = Harness(self).start()
        self.assert_refused(h, MONO_DC03, f"a black-only job for 29x90 mm die-cut labels, but the loaded roll is "
                                          f"{DK22251}, which takes two-color jobs only")

    def test_a_job_without_a_colour_mode_is_still_checked_for_its_media(self):
        h = Harness(self).start()
        self.assert_refused(h, _job(_page(MAC_DC03, None)),
                            f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}")

    def test_two_colors_in_high_resolution_are_refused_on_a_black_red_roll(self):
        h = Harness(self).start()
        detail = (f"a two-color job in high resolution mode, but the loaded roll, {DK22251}, prints two colors in "
                  "standard resolution only")
        output = self.assert_refused(h, _job(_page(MAC_HIGH_RES, 0x49)), detail)
        self.assertTrue(any("two-color, requested 62 mm continuous, 2223 lines (94.1 mm of print), flags 0xce, "
                            "result refused" in line for line in output), output)      # 600-dpi lines
        self.assert_refused(h, _job(_page(_info(0xCE, 0x0A, 29, 0, 2223), 0x49)),
                            f"a two-color job for 29 mm continuous tape in high resolution mode, but the loaded "
                            f"roll is {DK22251}, which prints two colors in standard resolution only")

    def test_headers_of_brothers_mac_driver(self):
        colour = f"a black-only job, but the loaded roll, {DK22251}, takes two-color jobs only"
        cases = [
            ('2.4" 2-Color', _page(MAC_62, 0x0B), None),
            ('2.4" Monochrome', _page(MAC_62, 0x0A), colour),
            ("Length 69.1 mm 2-Color", _page(MAC_62_748, 0x09), None),
            ("Length 69.1 mm Monochrome", _page(MAC_62_748, 0x08), colour),
            ('1.1" x 3.5" 2-Color', _page(MAC_DC03, 0x09),
             f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}"),
            ('2.4" x 1.1" 2-Color', _page(MAC_DC06, 0x09),
             f"a job for 62x29 mm die-cut labels, but the loaded roll is {DK22251}"),
            ('1.1" 2-Color', _page(MAC_29, 0x09),
             f"a job for 29 mm continuous tape, but the loaded roll is {DK22251}"),
            ('2.4" 2-Color + High resolution', _page(MAC_HIGH_RES, 0x49),
             f"a two-color job in high resolution mode, but the loaded roll, {DK22251}, prints two colors in "
             "standard resolution only"),
        ]
        h = Harness(self).start()
        for name, page, refusal in cases:
            with self.subTest(name):
                if refusal is None:
                    self.assertEqual(self.assert_through(h, _job(page))[0]["last_result"], "ok")
                else:
                    self.assert_refused(h, _job(page), refusal)
        own = Harness(self)
        own.model.set_media("29x90")
        own.start()
        self.assertEqual(self.assert_through(own, _job(_page(MAC_DC03, 0x08)))[0]["last_result"], "ok",
                         "the queue's default paper on its own roll")

    def test_a_roll_known_only_within_the_length_tolerance_is_named(self):
        """A 60x86 roll that reports a label length of 86 (Brother's raster
        reference; P-touch's table says 87) is unknown to P-touch's exact rule
        but judged by the lenient lookup: the texts name it as judged, not as
        an unsupported roll."""
        h = Harness(self)
        h.model.set_media("60x86")                  # DK-11234, one colour
        h.model.base[17] = 86
        h.start()
        self.assertIsNone(h.state()["status"]["media"]["label_id"], "P-touch would not know it")
        roll = "60mm x 86mm die-cut (DK-11234)"
        self.assert_refused(h, MONO_DC03, f"a job for 29x90 mm die-cut labels, but the loaded roll is {roll}")
        state, output = self.assert_through(h, _job(_page(_info(0x80, 0x0B, 29, 90), 0x08)))
        self.assertEqual(state["last_result"], "ok")
        self.assertTrue(any(line.endswith(f" for 29x90 mm die-cut labels goes to the printer unchecked: the loaded "
                                          f"roll is {roll}, but the job does not ask the printer to check its width "
                                          "or length") for line in output), output)

    def test_a_refusal_names_the_media_type_the_job_asks_for(self):
        """A header's media type decides how it reads (media.describe_request),
        as it decides the type compare_media judges: a continuous-tape header
        with a length, or a die-cut one without, is not named as the roll's
        own media."""
        cases = [
            ("62x100", {"flags": 0x8E, "type": 0x0A, "width": 62, "length": 100},
             "a job for 62 mm continuous tape, but the loaded roll is 62mm x 100mm die-cut (DK-11202)"),
            ("62red", {"flags": 0x86, "type": 0x0B, "width": 29, "length": 0},
             f"a job for 29 mm die-cut labels, but the loaded roll is {DK22251}"),
        ]
        for roll, requested, detail in cases:
            with self.subTest(detail):
                frame = media.synthesize_status(media.lookup_label(roll))
                self.assertEqual(proxyd._refusal_detail(frame, None, requested, False), (detail, ()))

    def test_the_colour_rule_refuses_through_a_fault(self):
        """Only the media rules wait out a fault: a black-only job on DK-22251
        is refused while the printer reports one (after such a job the tested
        unit needed a power cycle). The fault seen at the last poll is read
        again once, and that reply decides."""
        faults: Dict[str, Callable[[PrinterModel], None]] = {
            "cover open": lambda m: m.set_cover_open(True),
            "end of media": lambda m: setattr(m, "err1", 0x02),
        }
        for name, fault in faults.items():
            with self.subTest(name):
                h = Harness(self, poll_seconds=30.0)   # no idle poll after the first one
                fault(h.model)
                h.start()
                start = len(h.model.received)
                self.assert_refused(h, MONO, f"a black-only job, but the loaded roll, {DK22251}, takes two-color "
                                             "jobs only")
                self.assertEqual(bytes(h.model.received[start:]).count(STATUS_REQUEST), 1)

    # -- passes ----------------------------------------------------------------------
    def test_62_mm_continuous_jobs_go_through(self):
        h = Harness(self).start()
        for flags, lines, expanded in ((0x8E, 748, 0x09), (0x8E, 1111, 0x0B), (0x8E, 2402, 0x09),
                                       (0xCE, 748, 0x09), (0x86, 600, 0x09)):     # 0xCE: the quality bit
            with self.subTest(flags=flags, lines=lines):
                state, output = self.assert_through(h, _job(_page(_info(flags, 0x0A, 62, 0, lines), expanded)))
                self.assertEqual(state["last_result"], "ok")
                mm = f"{lines * 25.4 / 300:.1f}"
                self.assertTrue(any(f"two-color, requested 62 mm continuous, {lines} lines ({mm} mm of print), "
                                    f"flags 0x{flags:02x}, result ok" in line for line in output), output)
                self.assertFalse(any("unchecked" in line for line in output), output)

    def test_fields_the_job_does_not_ask_to_check_go_through_with_a_note(self):
        h = Harness(self).start()
        for flags in (0x80, 0x00):
            with self.subTest(flags=flags):
                state, output = self.assert_through(h, _job(_page(_info(flags, 0x0B, 29, 90))))
                self.assertEqual(state["last_result"], "ok", "Brother: the printer checks only the flagged fields")
                self.assertTrue(any(line.startswith("INFO:ql810wc.proxyd:a job from 127.0.0.1:") and line.endswith(
                    f" for 29x90 mm die-cut labels goes to the printer unchecked: the loaded roll is {DK22251}, "
                    "but the job does not ask the printer to check its type or width") for line in output), output)
        die_cut = Harness(self)
        die_cut.model.set_media("29x90")
        die_cut.start()
        state, output = self.assert_through(die_cut, _job(_page(_info(0x86, 0x0B, 29, 88), 0x08)))
        self.assertEqual(state["last_result"], "ok")
        self.assertTrue(any(line.endswith(
            " for 29x88 mm die-cut labels goes to the printer unchecked: the loaded roll is 29mm x 90mm die-cut "
            "(DK-11201), but the job does not ask the printer to check its length") for line in output), output)

    def test_black_only_high_resolution_on_a_single_color_roll_goes_through(self):
        h = Harness(self)
        h.model.set_media("62")                     # DK-22205
        h.start()
        state, output = self.assert_through(h, _job(_page(_info(0xCE, 0x0A, 62, 0, 2223), 0x48)))
        self.assertEqual(state["last_result"], "ok")
        self.assertTrue(any("black only, requested 62 mm continuous, 2223 lines (94.1 mm of print), flags 0xce, "
                            "result ok" in line for line in output), output)

    def test_two_colors_in_high_resolution_are_judged_on_a_black_red_roll_only(self):
        """Brother's high-resolution rule is about the black/red roll: on a
        single-colour roll only the colour rule speaks."""
        job = _job(_page(MAC_HIGH_RES, 0x49))
        h = Harness(self, color_check=False)
        h.model.set_media("62")                     # DK-22205
        h.start()
        state, _ = self.assert_through(h, job)
        self.assertIn("media does not support two-color printing", state["last_detail"])
        checked = Harness(self)
        checked.model.set_media("62")
        checked.start()
        self.assert_refused(checked, job, "a two-color job, but the loaded roll, 62mm continuous (DK-22205, DK-44205, "
                                          "DK-44605), cannot print red")

    def test_the_job_line_shows_the_print_header(self):
        h = Harness(self).start()
        state, output = self.assert_through(h, JOB)
        self.assertEqual(state["last_result"], "ok")
        self.assertTrue(any("two-color, requested 62 mm continuous, 600 lines (50.8 mm of print), flags 0x86, "
                            "result ok" in line for line in output), output)

    # -- uncertain: through ------------------------------------------------------------
    def test_an_uncertain_request_goes_through(self):
        """Two-colour jobs for 29x90 die-cut labels, refused on DK-22251 when their
        media is certain, go through when it is not; so does a high-resolution
        job whose first row contradicts its two-colour ESC i K."""
        page = _page(None)
        row_end = page.index(LINE_W) + len(LINE_W)
        jobs = {
            "no ESC i z": _job(page),
            "ESC @ after its ESC i z": _job(PRINT_INFO + MAC_DC03 + rasterproto.ESC_INIT + page),
            "ESC i z only after the first row": _job(page[:row_end] + PRINT_INFO + MAC_DC03 + page[row_end:]),
            "an unknown command before the first row": _job(_page(MAC_DC03, before_rows=b"\x1b\x69\x55\x58\x00")),
            "ESC i K 49 with g rows": _job(_page(MAC_HIGH_RES, 0x49).replace(LINE_W, LINE_G)),
        }
        for name, job in jobs.items():
            with self.subTest(name):
                h = Harness(self).start()
                self.assert_through(h, job)

    def test_an_uncertain_roll_goes_through(self):
        def no_roll(model: PrinterModel) -> None:
            model.base[10] = model.base[11] = 0     # as the tested unit reports a removed roll

        cases: Dict[str, Tuple[Dict[str, Any], Optional[Callable[[PrinterModel], None]]]] = {
            "no recent status": ({"status_max_age": 0.0}, None),
            "an unknown roll": ({}, lambda m: m.base.__setitem__(10, 61)),
            "no roll": ({}, no_roll),
            "cover open": ({}, lambda m: m.set_cover_open(True)),
            "end of media": ({}, lambda m: setattr(m, "err1", 0x02)),
        }
        for name, (settings, change) in cases.items():
            with self.subTest(name):
                h = Harness(self, **settings)
                if change is not None:
                    change(h.model)
                h.start()
                self.assert_through(h, TWO_DC03)

    def test_a_job_that_does_not_show_its_media_soon_goes_through(self):
        job = _job(_page(MAC_DC03, before_rows=b"\x5a" * 20000))   # 20000 blank lines before the first row
        cut = job.index(LINE_W)
        h = Harness(self).start()
        sock = h.connect()
        with mock.patch.object(proxyd, "_CHECK_HOLD_LIMIT", 1000):
            sock.sendall(job[:cut])
            h.wait_for(lambda: PRINT_INFO in bytes(h.model.received), "past the hold limit the job goes on")
        _send_and_close(sock, job[cut:])
        self.assertEqual(bytes(h.model.received).count(job), 1, "unchecked, byte for byte")
        self.assertNotEqual(h.state()["job"]["last_result"], "refused")

    # -- the setting -------------------------------------------------------------------
    def test_the_check_off_forwards_every_job(self):
        with self.assertLogs("ql810wc.proxyd", "WARNING") as logs:
            h = Harness(self, media_check=False).start()
        self.assertIn("WARNING:ql810wc.proxyd:RAW_MEDIA_CHECK is off: jobs go to the printer without the media check",
                      logs.output)
        self.assertFalse(any("RAW_COLOR_CHECK" in line for line in logs.output), logs.output)
        state, _ = self.assert_through(h, TWO_DC03)
        self.assertEqual(state["last_result"], "error")
        self.assertIn("replace media", state["last_detail"])
        self.assertEqual(h.model.jobs[-1]["media_mismatch"], ("type", "width"))
        self.assertTrue(h.model.mute, "what the check prevents: the printer stops answering")
        h.model.set_cover_open(True)                # what owners report clears it (Brother's first step)
        h.model.set_cover_open(False)
        h.wait_state(lambda s: s["device"]["state"] == "ready" and (s["status"]["parsed"] or {}).get("errors") == [],
                     "the printer answers again")
        self.assertEqual(self.assert_through(h, TWO_62)[0]["last_result"], "ok")

    def test_the_check_off_forwards_two_colors_in_high_resolution(self):
        h = Harness(self, media_check=False).start()
        state, _ = self.assert_through(h, _job(_page(MAC_HIGH_RES, 0x49)))
        self.assertEqual(state["last_result"], "error")
        self.assertIn("replace media", state["last_detail"])
        self.assertTrue(h.model.jobs[-1]["high_resolution"])

    def test_each_check_works_alone(self):
        with self.subTest("RAW_COLOR_CHECK off"):
            with self.assertLogs("ql810wc.proxyd", "WARNING") as logs:
                h = Harness(self, color_check=False).start()
            self.assertIn("WARNING:ql810wc.proxyd:RAW_COLOR_CHECK is off: jobs go to the printer without the colour "
                          "check", logs.output)
            self.assertFalse(any("RAW_MEDIA_CHECK" in line for line in logs.output), logs.output)
            self.assert_refused(h, MONO_DC03, f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}")
            state, _ = self.assert_through(h, MONO)
            self.assertIn("media does not support monochrome printing", state["last_detail"])
        with self.subTest("RAW_MEDIA_CHECK off"):
            h = Harness(self, media_check=False).start()
            self.assert_refused(h, MONO_DC03, f"a black-only job, but the loaded roll, {DK22251}, takes two-color "
                                              "jobs only")
            state, _ = self.assert_through(h, TWO_DC03)
            self.assertIn("replace media", state["last_detail"])
        with self.subTest("both off"):
            h = Harness(self, color_check=False, media_check=False).start()
            self.assert_through(h, MONO_DC03)

    # -- how the job is judged -------------------------------------------------------------
    def test_a_multi_page_job_is_judged_by_its_first_page(self):
        """Sent at once, a multi-page job may have all its headers parsed when
        the verdict comes: the verdict and the job line follow page 1's ESC i z."""
        tape = _page(_info(0x86, 0x0A, 62, 0, 10), lines=10, last=False)
        labels = _page(_info(0x8E, 0x0B, 29, 90, 10), lines=10, last=False)
        cases = {
            "page 2 asks for other media": (
                _job(tape, _page(_info(0x8E, 0x0B, 29, 90, 10, page=1), lines=10)), None,
                "two-color, requested 62 mm continuous, 10 lines (0.8 mm of print), flags 0x86, result ok"),
            "page 1 asks for other media": (
                _job(labels, _page(_info(0x86, 0x0A, 62, 0, 10, page=1), lines=10)),
                f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}",
                "two-color, requested 29x90 mm die-cut, 10 lines (0.8 mm of print), flags 0x8e, result refused"),
        }
        for name, (job, refusal, job_line) in cases.items():
            for bytewise in (False, True):
                with self.subTest(name, bytewise=bytewise):
                    # No idle poll after start-up: a slow bytewise send would get one among
                    # the job's leading NULs (legal, but then the job is not contiguous).
                    h = Harness(self, poll_seconds=30.0).start()
                    with self.assertLogs("ql810wc.proxyd", "INFO") as logs:
                        sock = h.connect()
                        how = _send_bytewise(sock, job) if bytewise else _send_and_close(sock, job)
                        _wait_logged(h, logs, ", result ")
                    self.assertTrue(any(job_line in line for line in logs.output), logs.output)
                    state = h.state()["job"]
                    self.assertEqual(state["requested_media"]["page"], 0, "page 1's header is published")
                    if refusal is None:
                        self.assertEqual(how, "fin")
                        self.assertEqual(state["last_result"], "ok")
                        self.assertEqual(bytes(h.model.received).count(job), 1)
                    else:
                        self.assertEqual(how, "rst")
                        self.assertEqual((state["last_result"], state["last_detail"]), ("refused", refusal))
                        self.assertNotIn(PRINT_INFO, bytes(h.model.received))
                        self.assertFalse(h.model.mute)
                    self.assertEqual(h.model.violations, [])

    def test_media_shown_before_the_stream_turns_opaque_counts(self):
        # An unknown command right after the first row: refused however the job is split.
        cut = TWO_DC03.index(LINE_W) + len(LINE_W)
        job = TWO_DC03[:cut] + b"\x1b\x69\x55\x58\x00" + TWO_DC03[cut:]
        for pieces in (1, 2):
            with self.subTest(pieces=pieces):
                h = Harness(self).start()
                sock = h.connect()
                if pieces == 1:
                    how = _send_and_close(sock, job)
                else:
                    sock.sendall(job[:cut])
                    h.wait_state(lambda s: s["job"]["last_result"] == "refused", "refused with the first piece")
                    how = _send_and_close(sock, job[cut:])
                self.assertEqual(how, "rst")
                self.assertEqual(h.state()["job"]["last_detail"],
                                 f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}")
                self.assertNotIn(PRINT_INFO, bytes(h.model.received))
                self.assertFalse(h.model.mute)

    def test_a_job_with_nothing_to_judge_asks_the_printer_nothing(self):
        """Without an ESC i z and an ESC i K for the checks that are on, the
        verdict needs no status: none is asked for, even when the last is stale."""
        cases = [
            ("no ESC i z, no ESC i K", {}, _job(_page(None, None)), 0),
            ("no ESC i z, the colour check off", {"color_check": False}, _job(_page(None)), 0),
            ("no ESC i K, the media check off", {"media_check": False}, _job(_page(MAC_DC03, None)), 0),
            ("an ESC i K to judge", {}, _job(_page(None)), 1),
            ("an ESC i z to judge", {}, _job(_page(MAC_DC03, None)), 1),
        ]
        for name, settings, job, requests in cases:
            with self.subTest(name):
                h = Harness(self, poll_seconds=30.0, status_max_age=0.0, **settings).start()
                start = len(h.model.received)
                self.assert_through(h, job)
                received = bytes(h.model.received)
                first = received.index(b"\x1b\x69\x4d\x40", start)        # ESC i M 40: in every job here
                self.assertEqual(received[start:first].count(STATUS_REQUEST), requests)

    def test_a_job_is_held_until_its_first_raster_row(self):
        h = Harness(self).start()
        cut = TWO_DC03.index(LINE_W)
        sock = h.connect()
        sock.sendall(TWO_DC03[:cut])
        h.wait_state(lambda s: s["job"]["active"] and s["job"]["bytes"] == cut, "the daemon read the job's commands")
        self.assertNotIn(PRINT_INFO, bytes(h.model.received), "held: its ESC i z alone decides nothing")
        self.assertEqual(_send_and_close(sock, TWO_DC03[cut:]), "rst")
        self.assertEqual(h.state()["job"]["last_result"], "refused")
        self.assertNotIn(PRINT_INFO, bytes(h.model.received))
        self.assertFalse(h.model.mute)

    def test_a_second_job_on_a_connection_is_checked_too(self):
        h = Harness(self).start()
        sock = h.connect()
        sock.sendall(TWO_62)
        h.wait_state(lambda s: s["job"]["last_result"] == "ok" and not s["job"]["active"], "the first job printed")
        start = len(h.model.received)
        self.assertEqual(_send_and_close(sock, TWO_DC03), "rst")
        state = h.state()["job"]
        self.assertEqual((state["last_result"], state["last_detail"]),
                         ("refused", f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}"))
        rest = bytes(h.model.received[start:])
        self.assertNotIn(PRINT_INFO, rest)
        self.assertTrue(SETUP.startswith(rest.replace(STATUS_REQUEST, b"")))
        self.assertEqual(len(h.model.jobs), 1)
        self.assertFalse(h.model.mute)

    # -- the status the verdict is judged on --------------------------------------------------
    def test_a_refusal_judged_on_an_earlier_status_is_confirmed(self):
        """The roll may have changed since the last idle poll: before a refusal
        the printer is asked once more, and its reply decides."""
        def no_roll(model: PrinterModel) -> None:
            model.base[10] = model.base[11] = 0

        cases: Dict[str, Tuple[Optional[Callable[[PrinterModel], None]], bytes, Optional[str]]] = {
            "the reply agrees": (None, TWO_DC03,
                                 f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}"),
            "another roll": (lambda m: m.set_media("29x90"), MONO_DC03, None),
            # The reply's roll still warrants a refusal: the reasons are its own.
            "another roll, still refused": (lambda m: m.set_media("62"), TWO_DC03,
                                            "a two-color job for 29x90 mm die-cut labels, but the loaded roll is "
                                            "62mm continuous (DK-22205, DK-44205, DK-44605), which cannot print red"),
            "no roll": (no_roll, TWO_DC03, None),
            "cover open": (lambda m: m.set_cover_open(True), TWO_DC03, None),
            # The colour check's refusals are confirmed the same way.
            "the reply agrees, colour only": (None, MONO, f"a black-only job, but the loaded roll, {DK22251}, takes "
                                                          "two-color jobs only"),
            "another roll, colour only": (lambda m: m.set_media("62"), MONO, None),
        }
        for name, (change, job, refusal) in cases.items():
            with self.subTest(name):
                h = Harness(self, poll_seconds=30.0).start()   # no idle poll after the first one
                if change is not None:
                    change(h.model)
                start = len(h.model.received)
                if refusal is not None:
                    self.assert_refused(h, job, refusal)
                    self.assertEqual(bytes(h.model.received[start:]).count(STATUS_REQUEST), 1,
                                     "one status request before the refusal")
                    continue
                state, _ = self.assert_through(h, job)
                self.assertEqual(_requests_before_the_job(h, start), 1, "one status request; its reply decides")
                if name.startswith("another roll"):
                    self.assertEqual(state["last_result"], "ok")
                    self.assertEqual(h.state()["status"]["media"]["label_id"], "29x90" if job is MONO_DC03 else "62")

    def test_a_status_read_for_the_verdict_is_not_confirmed_again(self):
        # The idle polls pause while set-up commands wait on a connection: the status goes stale,
        # the check asks for a new one, and that reply decides at once. (No idle poll follows
        # the refusal within the test: the next one is 30 s away.)
        h = Harness(self, poll_seconds=30.0, status_max_age=1.0).start()
        sock = h.connect()
        sock.sendall(SETUP)
        h.wait_for(lambda: h.session_bytes().endswith(SETUP), "the set-up bytes reached the printer")
        start = len(h.model.received)
        h.wait_state(lambda s: time.time() - s["status"]["polled"] > 1.5, "the last status went stale")
        self.assertEqual(_send_and_close(sock, TWO_DC03[len(SETUP):]), "rst")
        self.assertEqual(h.state()["job"]["last_result"], "refused")
        self.assertEqual(bytes(h.model.received[start:]), STATUS_REQUEST, "one status request, none of the job")

    def test_without_an_answer_the_last_status_decides(self):
        with self.subTest("a refusal to confirm: the earlier status stands"):
            h = Harness(self, poll_seconds=30.0).start()
            h.model.mute = True
            start = len(h.model.received)
            self.assertEqual(_send_refusable(h, TWO_DC03), "rst")
            state = h.state()["job"]
            self.assertEqual((state["last_result"], state["last_detail"]),
                             ("refused", f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}"))
            rest = bytes(h.model.received[start:])
            self.assertEqual(rest.count(STATUS_REQUEST), 1, "asked once, in vain")
            self.assertNotIn(PRINT_INFO, rest)
        for name, requests in (("a stale status, no answer", 1), ("a stale status, the input state unknown", 0)):
            with self.subTest(name):
                h = Harness(self, poll_seconds=0.1, status_max_age=1.0).start()
                sock = h.connect()
                sock.sendall(SETUP)
                h.wait_for(lambda: h.session_bytes().endswith(SETUP), "the set-up bytes reached the printer")
                start = len(h.model.received)
                h.wait_state(lambda s: time.time() - s["status"]["polled"] > 1.5, "the last status went stale")
                if requests:
                    h.model.mute = True
                else:
                    h.daemon.link.unknown = True    # as a torn write leaves it: no status request then
                _send_and_close(sock, TWO_DC03[len(SETUP):])
                self.assertEqual(bytes(h.model.received).count(TWO_DC03[len(SETUP):]), 1, "unchecked, byte for byte")
                self.assertNotEqual(h.state()["job"]["last_result"], "refused")
                self.assertEqual(_requests_before_the_job(h, start), requests)

    def test_no_roll_or_a_fault_seen_earlier_is_read_again(self):
        """Closing the roll cover sends no status, so a roll just loaded shows
        only at the next poll: a status read earlier that shows no roll or a
        fault is read again before the job is judged. The reply decides, and
        no confirm read follows it."""
        def no_roll(model: PrinterModel) -> None:
            model.base[10] = model.base[11] = 0     # as the tested unit reports a removed roll

        size = f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}"
        colour = f"a black-only job, but the loaded roll, {DK22251}, takes two-color jobs only"
        cases: Dict[str, Tuple[Callable[[PrinterModel], None], Callable[[PrinterModel], None], bytes, str]] = {
            "cover open at the last poll, then closed": (
                lambda m: m.set_cover_open(True), lambda m: m.set_cover_open(False), TWO_DC03, size),
            "no roll at the last poll, then DK-22251": (no_roll, lambda m: m.set_media("62red"), TWO_DC03, size),
            "no roll at the last poll, then DK-22251, colour": (no_roll, lambda m: m.set_media("62red"), MONO,
                                                                 colour),
        }
        for name, (earlier, now, job, refusal) in cases.items():
            with self.subTest(name):
                h = Harness(self, poll_seconds=30.0)   # no idle poll after the first one
                earlier(h.model)
                h.start()
                now(h.model)
                start = len(h.model.received)
                self.assert_refused(h, job, refusal)
                self.assertEqual(bytes(h.model.received[start:]).count(STATUS_REQUEST), 1,
                                 "one status request; its reply decides")

    def test_a_fault_still_in_the_reply_lets_the_job_through(self):
        """Through a fault the media rules are not judged (the printer holds jobs
        then): asked again, the cover is still open, and the job goes through."""
        h = Harness(self, poll_seconds=30.0)
        h.model.set_cover_open(True)
        h.start()
        start = len(h.model.received)
        self.assert_through(h, TWO_DC03)
        self.assertEqual(_requests_before_the_job(h, start), 1, "read again once")

    def test_a_fault_seen_earlier_is_read_again_for_the_colour_rule_too(self):
        """The colour rule judges a frame with a fault, but a fault seen earlier
        is read again for it too: a roll swap passes through cover open, and
        the single-colour roll the last poll saw with the cover open may be
        DK-22251 by now (after a black-only job on DK-22251 the tested unit
        needed a power cycle). Here the media rules have nothing to judge."""
        cases: Dict[str, Tuple[Dict[str, Any], bytes]] = {
            "media check off": ({"media_check": False}, MONO),
            "no ESC i z": ({}, _job(_page(None, 0x08))),
        }
        for name, (overrides, job) in cases.items():
            with self.subTest(name):
                h = Harness(self, poll_seconds=30.0, **overrides)   # no idle poll after the first one
                h.model.set_media("62")                 # DK-22205: one colour
                h.model.set_cover_open(True)
                h.start()                               # the last poll: DK-22205, cover open
                h.model.set_media("62red")              # DK-22251 goes in and the cover closes;
                h.model.set_cover_open(False)           # no frame says so
                start = len(h.model.received)
                self.assert_refused(h, job, f"a black-only job, but the loaded roll, {DK22251}, takes "
                                            "two-color jobs only")
                self.assertEqual(bytes(h.model.received[start:]).count(STATUS_REQUEST), 1, "read again once")

    def test_a_printer_that_does_not_answer_delays_the_verdict_once(self):
        """A fault seen earlier is read again; when the printer does not answer,
        the earlier frame decides at once, with no confirm read (no second
        wait). Through the fault only the colour rule refuses."""
        h = Harness(self, poll_seconds=30.0)
        h.model.set_cover_open(True)
        h.start()
        h.model.mute = True
        start = len(h.model.received)
        self.assertEqual(_send_refusable(h, MONO), "rst")
        state = h.state()["job"]
        self.assertEqual((state["last_result"], state["last_detail"]),
                         ("refused", f"a black-only job, but the loaded roll, {DK22251}, takes two-color jobs only"))
        rest = bytes(h.model.received[start:])
        self.assertEqual(rest.count(STATUS_REQUEST), 1, "asked once, in vain, and not again")
        self.assertNotIn(PRINT_INFO, rest)
        self.assertEqual(h.model.violations, [])

    def test_no_confirm_read_while_the_input_state_is_unknown(self):
        """While the printer's input state is unknown (a torn write), a refusal
        judged on an earlier frame stands: a bare ESC i S could land inside a
        command. The status request after the connection resynchronises the
        printer; had the verdict asked, its reply would have cleared the
        unknown state, and no resync would follow."""
        h = Harness(self, poll_seconds=30.0).start()   # no idle poll after the first one
        h.daemon.link.unknown = True                   # as a torn write leaves it
        start = len(h.model.received)
        self.assert_refused(h, TWO_DC03, f"a job for 29x90 mm die-cut labels, but the loaded roll is {DK22251}")
        h.wait_for(lambda: bytes(h.model.received[start:]).endswith(RESYNC_REQUEST), "the resync")
        rest = bytes(h.model.received[start:])
        if rest.startswith(SETUP):
            rest = rest[len(SETUP):]                   # the job's set-up bytes, had they come in a read of their own
        self.assertEqual(rest, RESYNC_REQUEST, "no status request for the verdict, then the resync")
        self.assertEqual(h.model.violations, [])

    def test_a_status_request_the_printer_takes_in_part_tears_the_job(self):
        """The job is never sent behind a status request of the check that the
        printer took only in part (it may wait inside that command): the job
        is torn with none of it sent, and the next request resynchronises."""
        cases = {
            "the request that confirms a refusal": {"poll_seconds": 0.2},
            "the request that reads a stale status again": {"poll_seconds": 0.1, "status_max_age": 1.0},
        }
        for name, settings in cases.items():
            with self.subTest(name):
                h = Harness(self, **settings).start()
                sock = h.connect()
                sock.sendall(SETUP)
                h.wait_for(lambda: h.session_bytes().endswith(SETUP), "the set-up bytes reached the printer")
                start = len(h.model.received)
                if "status_max_age" in settings:
                    h.wait_state(lambda s: time.time() - s["status"]["polled"] > 1.5, "the last status went stale")
                h.printer.stall_after = h.printer.total_written + 1    # one more byte, then nothing
                self.assertEqual(_send_and_close(sock, TWO_DC03[len(SETUP):]), "rst")
                state = h.state()["job"]
                self.assertEqual(state["last_result"], "torn")
                self.assertEqual(state["last_detail"], "the printer took the check's status request only in part; "
                                                       "none of its print data was sent to the printer")
                self.assertEqual(bytes(h.model.received[start:]), STATUS_REQUEST[:1],
                                 "a third of the request, none of the job")
                h.printer.set_stalled(False)
                h.wait_for(lambda: len(h.model.received) >= start + 1 + len(RESYNC_REQUEST), "the next request")
                rest = bytes(h.model.received[start:])
                self.assertTrue(rest.startswith(STATUS_REQUEST[:1] + RESYNC_REQUEST), rest[:8].hex(" "))
                self.assertNotIn(PRINT_INFO, rest)
                self.assertEqual(h.model.violations, [])
                self.assertEqual(self.assert_through(h, TWO_62)[0]["last_result"], "ok")

    def test_a_queued_connection_after_a_status_request_taken_in_part_follows_a_resync(self):
        """After the check's status request went out only in part, a connection
        already waiting in the listen backlog is served only after the next
        idle poll has resynchronised the printer, even with a job that does not
        start with NULs and ESC @ of its own."""
        h = Harness(self, poll_seconds=30.0, status_timeout=2.0).start()
        sock = h.connect()
        sock.sendall(SETUP)
        h.wait_for(lambda: h.session_bytes().endswith(SETUP), "the set-up bytes reached the printer")
        second = _page(_info(0x86, 0x0A, 62))       # starts at ESC i z
        queued = h.connect()                        # waits in the listen backlog
        queued.sendall(second)
        queued.shutdown(socket.SHUT_WR)
        start = len(h.model.received)
        h.printer.stall_after = h.printer.total_written + 1    # one more byte, then nothing
        self.assertEqual(_send_and_close(sock, TWO_DC03[len(SETUP):]), "rst")
        self.assertEqual(h.state()["job"]["last_result"], "torn")
        h.printer.set_stalled(False)
        self.assertEqual(_recv_until_closed(queued), b"")
        state = h.wait_state(lambda s: not s["job"]["active"] and s["job"]["bytes"] == len(second),
                             "the queued job settled")
        self.assertEqual(state["job"]["last_result"], "ok")
        rest = bytes(h.model.received[start:])
        self.assertTrue(rest.startswith(STATUS_REQUEST[:1] + RESYNC_REQUEST + second), rest[:8].hex(" "))
        self.assertFalse(h.model.jobs[-1]["failed"])
        self.assertEqual(h.model.violations, [])

    def test_a_printer_lost_during_the_check_tears_the_job(self):
        printer = _GoneOnRequest()
        h = Harness(self, bus=FakeBus(printer), poll_seconds=30.0).start()
        printer.armed = True                        # the check's status request unplugs it
        start = len(h.model.received)
        self.assertEqual(_send_refusable(h, TWO_DC03), "rst")
        detail = h.state()["job"]["last_detail"]
        self.assertEqual(h.state()["job"]["last_result"], "torn")
        self.assertTrue(detail.startswith("the printer is not available (")
                        and detail.endswith("; none of its print data was sent to the printer"), detail)
        self.assertNotIn(PRINT_INFO, bytes(h.model.received[start:]))
        printer.replug()
        h.wait_state(lambda s: s["device"]["state"] == "ready" and s["status"]["raw_hex"], "the printer is back")
        self.assertEqual(self.assert_through(h, TWO_62)[0]["last_result"], "ok")


class JobLineTest(unittest.TestCase):
    def test_describe_print_header(self):
        cases = [
            ({"flags": 0x86, "lines": 600}, None, "600 lines (50.8 mm of print), flags 0x86"),
            ({"flags": 0x8E, "lines": 748}, 0x09, "748 lines (63.3 mm of print), flags 0x8e"),
            ({"flags": 0x8E, "lines": 1111}, 0x0B, "1111 lines (94.1 mm of print), flags 0x8e"),
            ({"flags": 0xCE, "lines": 2223}, 0x49, "2223 lines (94.1 mm of print), flags 0xce"),   # 600 dpi
            ({"flags": 0xCE, "lines": 2223}, 0x48, "2223 lines (94.1 mm of print), flags 0xce"),
            ({"flags": 0xCE, "lines": 2223}, None, "2223 lines (188.2 mm of print), flags 0xce"),
            ({"flags": 0x80, "lines": 0}, 0x09, "flags 0x80"),
            ({}, None, "flags 0x00"),
        ]
        for requested, expanded, text in cases:
            with self.subTest(text):
                self.assertEqual(proxyd.describe_print_header(requested, expanded), text)
        self.assertEqual(proxyd.describe_print_header({"flags": 0x8E, "lines": 748}), "748 lines (63.3 mm of print), "
                                                                                       "flags 0x8e")
        self.assertIn("describe_print_header", proxyd.__all__)

    def test_describe_requested_media(self):
        """The media type decides first, as in media.describe_request (and the
        refusal texts): a length decides only for other types."""
        cases = [
            ({"type": 0x0A, "width": 62}, "requested 62 mm continuous"),
            ({"type": 0x0A, "width": 62, "length": 100}, "requested 62 mm continuous"),
            ({"type": 0x0B, "width": 29, "length": 90}, "requested 29x90 mm die-cut"),
            ({"type": 0x0B, "width": 29}, "requested 29 mm die-cut"),
            ({"type": 0x4B, "width": 29, "length": 90}, "requested 29x90 mm die-cut"),
            ({"type": 0x4A, "width": 29}, "requested 29 mm continuous"),
            ({"width": 29, "length": 90}, "requested 29x90 mm die-cut"),
            ({"width": 29}, "requested 29 mm continuous"),
        ]
        for requested, text in cases:
            with self.subTest(text, requested=requested):
                self.assertEqual(proxyd.describe_requested_media(requested), text)

    def test_summary(self):
        job = proxyd.JobRecord(peer="192.0.2.5:1", started=0.0, started_mono=0.0, bytes=4179, two_color=False,
                               requested_media={"flags": 0x8E, "type": 0x0A, "width": 62, "length": 0, "lines": 748,
                                                "page": 0},
                               expanded_mode=0x08, result="refused", detail="a black-only job", finished_mono=0.0)
        self.assertEqual(job.summary(), "job from 192.0.2.5:1: 4179 bytes in 0.0 s, black only, requested 62 mm "
                                        "continuous, 748 lines (63.3 mm of print), flags 0x8e, result refused "
                                        "(a black-only job)")


class SetupConnectionTest(unittest.TestCase):
    """A connection with nothing but NULs and set-up commands is no job:
    iPrint&Label for iOS opens such connections; the two the tested unit
    counted held 400 NULs and ESC @, with the counts the first case expects."""

    def test_iprint_402_byte_connection_is_not_a_job(self):
        cases = {
            "iPrint&Label": (INVALIDATE + rasterproto.ESC_INIT,        # 402 bytes, like the app's
                             "(NUL bytes 400, set-up commands 1, status requests 0)"),
            "more set-up": (INVALIDATE + rasterproto.ESC_INIT + b"\x1b\x69\x61\x01\x1b\x69\x21\x00"
                            + STATUS_REQUEST, "(NUL bytes 400, set-up commands 3, status requests 1)"),
        }
        for name, (sent, contents) in cases.items():
            with self.subTest(name):
                h = Harness(self).start()
                h.send_job(JOB)
                before = h.state()["job"]
                start = len(h.session_bytes())
                with self.assertLogs("ql810wc.proxyd", "INFO") as logs:
                    sock = h.connect()
                    sock.sendall(sent)
                    sock.shutdown(socket.SHUT_WR)
                    self.assertEqual(_how_closed(sock), "fin", "an ordinary close")
                    _wait_logged(h, logs, f"{len(sent)} bytes, no print job")
                self.assertEqual(h.state()["job"], before, "the last job stays the last job")
                self.assertTrue(any(f"{len(sent)} bytes, no print job {contents}" in line for line in logs.output),
                                logs.output)
                self.assertFalse(any("middle of a job" in line for line in logs.output), logs.output)
                _wait_polls(h, start + len(sent), 1)
                self.assertEqual(h.session_bytes()[start:].replace(STATUS_REQUEST, b""),
                                 sent.replace(STATUS_REQUEST, b""),
                                 "forwarded as sent, and no resync after it")
                self.assertEqual(h.model.violations, [], "the polls after it are no status requests inside a job")

    def test_a_set_up_command_cut_between_two_reads_is_still_no_job(self):
        h = Harness(self).start()
        h.send_job(JOB)
        before = h.state()["job"]
        sent = INVALIDATE + rasterproto.ESC_INIT
        start = len(h.session_bytes())
        sock = h.connect()
        sock.sendall(sent[:-1])                            # ... 00 1B
        h.wait_for(lambda: h.session_bytes().endswith(INVALIDATE), "the NULs reached the printer")
        time.sleep(0.2)
        self.assertFalse(h.session_bytes().endswith(sent[:-1]), "half an ESC @ is kept back")
        sock.sendall(sent[-1:])                            # 40
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(_how_closed(sock), "fin")
        self.assertEqual(h.state()["job"], before, "no job recorded")
        _wait_polls(h, start + len(sent), 1)
        self.assertEqual(h.session_bytes()[start:].replace(STATUS_REQUEST, b""), sent, "then forwarded whole")
        self.assertEqual(h.model.violations, [])

    def test_an_incomplete_command_at_the_end_is_not_sent(self):
        h = Harness(self).start()
        h.send_job(JOB)
        before = h.state()["job"]
        start = len(h.session_bytes())
        with self.assertLogs("ql810wc.proxyd", "INFO") as logs:
            self.assertEqual(h.send_job(INVALIDATE + b"\x1b\x69"), b"")
            _wait_logged(h, logs, "no print job")
        self.assertTrue(any("402 bytes, no print job" in line
                            and "the last 2 bytes, an incomplete command, were not sent" in line
                            for line in logs.output), logs.output)
        self.assertEqual(h.state()["job"], before)
        _wait_polls(h, start + len(INVALIDATE), 1)
        self.assertEqual(h.session_bytes()[start:].replace(STATUS_REQUEST, b""), INVALIDATE,
                         "no half command, so no resync")

    def test_an_incomplete_command_after_a_job_is_not_sent(self):
        h = Harness(self).start()
        sock = h.connect()
        sock.sendall(JOB)
        h.wait_state(lambda s: s["job"]["last_result"] == "ok" and not s["job"]["active"], "the job printed")
        start = len(h.session_bytes())
        with self.assertLogs("ql810wc.proxyd", "INFO") as logs:
            sock.sendall(PTOUCH_TRAILER[:5])               # ESC i U t, one byte short
            sock.shutdown(socket.SHUT_WR)
            self.assertEqual(_how_closed(sock), "fin")
            _wait_logged(h, logs, "after its job, an incomplete command, were not sent")
        self.assertNotIn(PTOUCH_TRAILER[:5], h.session_bytes()[start:])
        self.assertTrue(any("the last 5 bytes after its job, an incomplete command, were not sent" in line
                            for line in logs.output), logs.output)
        self.assertFalse(any("5-byte trailer" in line for line in logs.output), logs.output)

    def test_a_trailer_cut_inside_a_command_is_forwarded_whole(self):
        h = Harness(self).start()
        sock = h.connect()
        sock.sendall(JOB)
        h.wait_state(lambda s: s["job"]["last_result"] == "ok" and not s["job"]["active"], "the job printed")
        sock.sendall(PTOUCH_TRAILER[:3])                   # 1B 69 55
        time.sleep(0.2)
        self.assertFalse(h.session_bytes().endswith(PTOUCH_TRAILER[:3]), "kept back until it is complete")
        sock.sendall(PTOUCH_TRAILER[3:])
        h.wait_for(lambda: PTOUCH_TRAILER in h.session_bytes(), "the whole trailer reached the printer")
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(_how_closed(sock), "fin")
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        self.assertEqual(h.model.violations, [])

    def test_no_status_poll_while_set_up_commands_wait_for_a_job(self):
        h = Harness(self, poll_seconds=0.1).start()
        setup = INVALIDATE + rasterproto.ESC_INIT
        sock = h.connect()
        sock.sendall(setup)
        h.wait_for(lambda: h.session_bytes().endswith(setup), "the set-up bytes reached the printer")
        start = len(h.session_bytes())
        time.sleep(0.5)
        self.assertEqual(h.session_bytes()[start:], b"", "no status request while a job may follow")
        sock.sendall(JOB[len(setup):])
        sock.shutdown(socket.SHUT_WR)
        _recv_until_closed(sock)
        self.assertEqual(h.state()["job"]["last_result"], "ok")
        self.assertEqual(h.state()["job"]["bytes"], len(JOB), "the set-up bytes count as the job's")
        self.assertEqual(h.session_bytes().count(JOB), 1)
        self.assertEqual(h.model.violations, [])


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
        with self.assertLogs("ql810wc.proxyd", "WARNING") as logs:
            sock = h.connect()
            try:
                sock.sendall(JOB_B)
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            self.assertEqual(_recv_until_closed(sock), b"")
            h.wait_state(lambda s: s["job"]["last_result"] == "error", "job turned away")
            _wait_logged(h, logs, "turned away a print job from 127.0.0.1:")
        self.assertTrue(any("turned away a print job from 127.0.0.1:" in line and "printer not available" in line
                            for line in logs.output), logs.output)
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

    def test_set_up_commands_alone_are_not_a_job(self):
        seg = proxyd._Segment()
        seg.observe(INVALIDATE + rasterproto.ESC_INIT + b"\x1b\x69\x61\x01")
        self.assertFalse(seg.has_job_content())
        self.assertFalse(seg.at_rest, "a job may follow: no status requests in between")
        seg.observe(PRINT_INFO + b"\x86\x0a\x3e\x00" + bytes(6))
        self.assertTrue(seg.has_job_content())

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

    def test_a_media_mismatch_stops_the_printer_until_the_cover_is_opened_and_closed(self):
        model = PrinterModel()
        model.feed(TWO_DC03)
        job = model.jobs[-1]
        self.assertTrue(job["failed"])
        self.assertEqual(job["media"], {"flags": 0x8E, "type": 0x0B, "width": 29, "length": 90})
        self.assertEqual(job["media_mismatch"], ("type", "width"))
        frames = rasterproto.iter_status_frames(model.pending())
        self.assertEqual([(f.status_type, f.errors()) for f in frames], [(rasterproto.ST_ERROR, ["replace media"])])
        self.assertTrue(model.mute and model.replace_media)
        model.feed(STATUS_REQUEST)
        self.assertEqual(model.pending(), b"", "no answer")
        model.set_cover_open(True)
        model.feed(STATUS_REQUEST)
        self.assertEqual(model.pending(), b"", "no answer with the cover open either")
        model.set_cover_open(False)
        self.assertFalse(model.mute or model.replace_media)
        model.feed(STATUS_REQUEST)
        self.assertEqual([f.errors() for f in rasterproto.iter_status_frames(model.pending())], [[]])
        model.feed(TWO_62)
        self.assertFalse(model.jobs[-1]["failed"])
        model.feed(TWO_DC03)
        self.assertTrue(model.mute)
        model.power_cycle()
        self.assertFalse(model.mute or model.replace_media or model.err2)

    def test_a_cover_cycle_does_not_end_the_colour_lock_up(self):
        """After a black-only job on DK-22251 the tested unit answered nothing
        until it was switched off and on. A media mismatch before or after it
        does not let a cover cycle end that."""
        for name, jobs in (("black-only, then die-cut labels", (MONO, TWO_DC03)),
                           ("die-cut labels, then black-only", (TWO_DC03, MONO))):
            with self.subTest(name):
                model = PrinterModel()
                for job in jobs:
                    model.feed(job)
                self.assertEqual([j["failed"] for j in model.jobs], [True, True])
                self.assertTrue(model.mute and model.replace_media)
                self.assertEqual(model.error_detail, 0x24)
                model.set_cover_open(True)
                model.set_cover_open(False)
                self.assertFalse(model.replace_media, "the cover cycle clears 'replace media'")
                self.assertTrue(model.mute, "but not the colour lock-up")
                model.pending()
                model.feed(STATUS_REQUEST)
                self.assertEqual(model.pending(), b"", "no answer")
                model.power_cycle()
                model.feed(STATUS_REQUEST)
                self.assertEqual([f.errors() for f in rasterproto.iter_status_frames(model.pending())], [[]])

    def test_the_media_rule_checks_what_the_job_asks_for(self):
        unflagged = _job(_page(_info(0x80, 0x0B, 29, 90)))
        model = PrinterModel()
        model.feed(unflagged)
        self.assertFalse(model.jobs[-1]["failed"], "Brother: n1 says which fields the printer checks")
        strict = PrinterModel()
        strict.checks_unflagged = True
        strict.feed(unflagged)
        self.assertEqual(strict.jobs[-1]["media_mismatch"], ("type", "width"))
        die_cut = PrinterModel()
        die_cut.set_media("29x90")
        for length, failed in ((88, True), (89, False), (90, False), (91, False), (92, True)):
            die_cut.power_cycle()
            die_cut.feed(_job(_page(_info(0x8E, 0x0B, 29, length), 0x08)))
            self.assertEqual(die_cut.jobs[-1]["failed"], failed, length)
        die_cut.power_cycle()
        die_cut.length_tolerance = 0
        die_cut.feed(_job(_page(_info(0x8E, 0x0B, 29, 89), 0x08)))
        self.assertEqual(die_cut.jobs[-1]["media_mismatch"], ("length",))
        tape = PrinterModel()
        tape.feed(_job(_page(_info(0x8E, 0x0A, 62, 100))))
        self.assertFalse(tape.jobs[-1]["failed"], "continuous tape takes any length")
        no_roll = PrinterModel()
        no_roll.base[10] = no_roll.base[11] = 0
        no_roll.feed(TWO_DC03)
        self.assertEqual(no_roll.jobs[-1]["media_mismatch"], (), "a printer without a roll is not modelled")

    def test_a_job_is_judged_by_its_first_print_information(self):
        model = PrinterModel()
        model.feed(_job(_page(_info(0x86, 0x0A, 62), last=False), _page(_info(0x8E, 0x0B, 29, 90, page=1))))
        self.assertFalse(model.jobs[-1]["failed"])
        self.assertEqual(model.jobs[-1]["media"]["width"], 62)
        model.feed(_job(_page(_info(0x8E, 0x0B, 29, 90), last=False), _page(_info(0x86, 0x0A, 62, page=1))))
        self.assertEqual(model.jobs[-1]["media_mismatch"], ("type", "width"))
        cleared = PrinterModel()
        cleared.feed(_job(PRINT_INFO + MAC_DC03 + rasterproto.ESC_INIT + _page(None)))
        self.assertIsNone(cleared.jobs[-1]["media"], "ESC @ cancels what came before it")
        self.assertFalse(cleared.jobs[-1]["failed"])

    def test_two_colors_in_high_resolution_fail_on_a_black_red_roll(self):
        model = PrinterModel()
        model.feed(_job(_page(MAC_HIGH_RES, 0x49)))
        job = model.jobs[-1]
        self.assertTrue(job["failed"] and job["high_resolution"])
        self.assertEqual(job["media_mismatch"], ())
        self.assertTrue(model.replace_media)
        self.assertFalse(model.mute, "an assumption: the printer keeps answering")
        model.pending()
        model.feed(STATUS_REQUEST)
        self.assertEqual(rasterproto.iter_status_frames(model.pending())[-1].errors(), ["replace media"])
        model.set_cover_open(True)
        model.set_cover_open(False)
        self.assertEqual(model.err2, 0)
        single = PrinterModel()
        single.set_media("62")
        single.feed(_job(_page(_info(0xCE, 0x0A, 62), 0x48)))
        self.assertFalse(single.jobs[-1]["failed"], "black only in high resolution")
        self.assertTrue(single.jobs[-1]["high_resolution"])


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
        self.assertTrue(settings.color_check)
        self.assertTrue(settings.media_check)
        self.assertEqual(settings.status_max_age, 60.0)
        self.assertEqual((ProxySettings().color_check, ProxySettings().media_check, ProxySettings().status_max_age),
                         (True, True, 60.0))

    def test_values_and_overrides(self):
        cfg = config.load_runtime_config(None)
        cfg.update(RAW_PORT="9101", RAW_BIND="192.0.2.10", RAW_BIDIRECTIONAL="yes", STATUS_POLL_SECONDS="5",
                   RAW_IDLE_TIMEOUT="120", MAX_JOB_BYTES="1048576", USB_PRODUCT_ID="209C",
                   STATE_FILE="/tmp/x.json", RAW_COLOR_CHECK="no", RAW_MEDIA_CHECK="no", STATUS_MAX_AGE_SECONDS="90")
        settings = ProxySettings.from_config(cfg, listen=("127.0.0.1", 0), device="/dev/usb/lp1")
        self.assertEqual(settings.listen, ("127.0.0.1", 0))
        self.assertTrue(settings.bidirectional)
        self.assertFalse(settings.color_check)
        self.assertFalse(settings.media_check)
        for key, expected in (("RAW_COLOR_CHECK", (False, True)), ("RAW_MEDIA_CHECK", (True, False))):
            with self.subTest(key=key):
                alone = config.load_runtime_config(None)
                alone[key] = "no"
                checks = ProxySettings.from_config(alone)
                self.assertEqual((checks.color_check, checks.media_check), expected, "each setting on its own")
        self.assertEqual(settings.status_max_age, 90.0)
        self.assertEqual(settings.poll_seconds, 5.0)
        self.assertEqual(settings.raw_idle_timeout, 120.0)
        self.assertEqual(settings.write_stall_timeout, 120.0)
        self.assertEqual(settings.max_job_bytes, 1048576)
        self.assertEqual(settings.product_id, "209c")
        self.assertEqual(settings.state_path, "/tmp/x.json")
        self.assertEqual(settings.device, "/dev/usb/lp1")
        self.assertEqual(ProxySettings.from_config(cfg).listen, ("192.0.2.10", 9101))

    def test_invalid_values(self):
        for key, value in (("RAW_IDLE_TIMEOUT", "1"), ("RAW_IDLE_TIMEOUT", "soon"), ("MAX_JOB_BYTES", "100"),
                           ("RAW_COLOR_CHECK", "maybe"), ("RAW_MEDIA_CHECK", "maybe")):
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
