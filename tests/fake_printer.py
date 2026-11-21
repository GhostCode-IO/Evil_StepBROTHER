"""Fake Brother QL-810W printers for the tests (no hardware needed).

* :class:`PrinterModel` - the printer's protocol behaviour without any I/O.
  It parses what it is sent with the project's own tokenizer, answers
  ``ESC i S`` at command boundaries with a 32-byte status (DK-22251 loaded,
  two-colour, by default: byte 14 = 0x23, byte 25 = 0x81), volunteers the
  phase-change / printing-completed / error frames a real QL sends after a
  job's ``0x1A``, and records everything for assertions: every byte, every
  status request and every ``ESC i S`` that arrived while a job was in
  flight (:attr:`PrinterModel.violations`). It rejects a job whose colour
  mode the roll cannot print, and after a black-only job on DK-22251 it
  answers nothing until :meth:`PrinterModel.power_cycle`, as the tested unit
  did.
* :class:`FakePrinter` - an in-process :class:`ql810wc.usbio.Transport` on
  top of a model, with slow writes, stalls, a last transfer that never
  completes, early empty reads and unplugging.
* :class:`FakeBus` - a device source for ``ProxyDaemon``: the printer
  present, absent, or in Editor Lite (mass storage) mode.
* :class:`PtyPrinter` - the model behind a pseudo-terminal, for code that
  opens a device node (``UsblpTransport``, ``qlctl probe``, the daemon run
  as a subprocess). POSIX only.

Frames are timed with ``time.monotonic()``; scheduled frames become readable
once due.
"""
from __future__ import annotations

import os
import pathlib
import select
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

AGENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from ql810wc import media, usbio  # noqa: E402
from ql810wc.rasterproto import (  # noqa: E402
    PHASE_PRINTING, PHASE_RECEIVING, RESYNC, STATUS_REQUEST, ST_ERROR, ST_PHASE_CHANGE,
    ST_PRINTING_COMPLETED, ST_REPLY, RasterTokenizer)

#: The tested unit's idle frame with DK-22251 loaded (dev/notes/usb-ground-truth.md).
IDLE_DK22251 = media.synthesize_status(media.lookup_label("62red")).raw
#: A mono 62 mm roll (DK-22205) and the 29x90 die-cut roll (DK-11201).
IDLE_DK22205 = media.synthesize_status(media.lookup_label("62")).raw
IDLE_DK11201 = media.synthesize_status(media.lookup_label("29x90")).raw
#: The tested unit's IEEE-1284 device id.
TEST_DEVICE_ID = ("MFG:Brother;CMD:PT-CBP,URF;MDL:QL-810W;CLS:PRINTER;CID:Brother LabelPrinter TypeE1;"
                  "URF:V1.5,W8,CP1,IS1,MT1-5,OB10,PQ4-5,RS300,FN3-11-60-63;")
#: Placeholder USB serial (real serials never enter the repository).
FAKE_SERIAL = "FAKE0SERIAL1"
#: The trailer P-touch Editor for Mac sends after a job, once SNMP shows completion.
PTOUCH_TRAILER = b"\x1b\x69\x55\x74\x1c\x00" + b"\x1b\x69\x61\xff"
#: What the proxy sends to resynchronise the printer before a status request.
RESYNC_REQUEST = RESYNC + STATUS_REQUEST

#: Commands that open a job on the printer. ESC i a 01/FF and ESC i ! only set a
#: mode (another ESC i a value opens a job: what follows is not raster).
_JOB_START_EVENTS = frozenset({"print_info", "various_mode", "cut_every", "expanded_mode", "margin",
                               "esc_i_j", "job_id", "media_block", "compression", "print"})


class PrinterModel:
    """A QL-810W's protocol behaviour. Thread-safe; all attributes are public
    so tests can inspect and steer it.

    ``notify``: volunteer status frames (the printer default; ``ESC i ! 01``
    in the stream turns it off, ``ESC i ! 00`` on). ``print_seconds``: how
    long a label takes to print. ``mute``: never answer ``ESC i S``.
    ``fail_next_job = (err1, err2)``: the next job ends in an error frame.
    ``error_detail``: byte 31 of the frames (a colour-mismatch code; see
    :meth:`_color_mismatch`).
    """

    def __init__(self, status: bytes = IDLE_DK22251, *, notify: bool = True,
                 print_seconds: float = 0.05) -> None:
        self._lock = threading.RLock()
        self.base = bytearray(status)
        self.err1 = 0
        self.err2 = 0
        self.notify = notify
        self.print_seconds = print_seconds
        self.reply_delay = 0.0
        self.mute = False
        self.error_detail = 0
        self.after_reply: Optional[bytes] = None           # a frame sent right behind the next reply
        self.fail_next_job: Optional[Tuple[int, int]] = None
        self.received = bytearray()
        self.receive_log: List[Tuple[float, int]] = []     # (time, stream offset of the chunk)
        self.status_requests: List[int] = []               # stream offsets of ESC i S
        self.violations: List[int] = []                    # ESC i S while a job was in flight
        self.inits: List[int] = []                         # stream offsets of ESC @
        self.jobs: List[Dict[str, Any]] = []               # one dict per 0x1A
        self._tok = RasterTokenizer()
        self._tok_base = 0
        self._job_start: Optional[int] = None
        self._two_color = False
        self._printing_until = 0.0
        self._seq = 0
        self._out: List[Tuple[float, int, bytes, bool]] = []   # (due, seq, frame, cancellable)

    # -- frames --------------------------------------------------------------
    @property
    def printing(self) -> bool:
        """A label is being printed right now."""
        return time.monotonic() < self._printing_until

    def frame(self, status_type: int, phase: Optional[int] = None) -> bytes:
        """A status block for the current roll and error bits."""
        with self._lock:
            out = bytearray(self.base)
            out[8], out[9] = self.err1, self.err2
            out[31] = self.error_detail
            out[18] = status_type
            if phase is None:
                phase = PHASE_PRINTING if self.printing else PHASE_RECEIVING
            out[19] = phase
            return bytes(out)

    def _schedule(self, frame: bytes, due: float, cancellable: bool = False) -> None:
        self._seq += 1
        self._out.append((due, self._seq, frame, cancellable))
        self._out.sort(key=lambda item: (item[0], item[1]))

    def inject(self, frame: bytes, delay: float = 0.0) -> None:
        """Queue an unsolicited frame (``delay`` s from now)."""
        with self._lock:
            self._schedule(bytes(frame), time.monotonic() + delay)

    def pending(self) -> bytes:
        """Every frame that is due, removed from the queue."""
        with self._lock:
            now = time.monotonic()
            due = [item for item in self._out if item[0] <= now]
            self._out = [item for item in self._out if item[0] > now]
            return b"".join(item[2] for item in due)

    def next_due(self) -> Optional[float]:
        """When the next queued frame becomes due, or None."""
        with self._lock:
            return self._out[0][0] if self._out else None

    # -- steering ------------------------------------------------------------
    def set_media(self, label_id: str) -> None:
        """Load another roll (a brother_ql label id such as ``62red`` or ``29x90``)."""
        found = media.lookup_label(label_id)
        if found is None:
            raise ValueError(label_id)
        with self._lock:
            self.base = bytearray(media.synthesize_status(found).raw)

    def set_cover_open(self, is_open: bool) -> None:
        """Open/close the roll cover (err2 bit 0x10); replies become type 0x02."""
        with self._lock:
            self.err2 = (self.err2 | 0x10) if is_open else (self.err2 & ~0x10)

    def clear_errors(self) -> None:
        with self._lock:
            self.err1 = self.err2 = 0
            self.error_detail = 0

    def power_cycle(self) -> None:
        """Switch the printer off and on: errors, a held job and muteness are gone."""
        with self._lock:
            self.clear_errors()
            self.mute = False
            self._job_start = None
            self._two_color = False
            self._printing_until = 0.0
            self._out.clear()

    # -- receiving -----------------------------------------------------------
    def feed(self, data: bytes) -> None:
        """Bytes written to the printer, in order."""
        with self._lock:
            start = len(self.received)
            self.receive_log.append((time.monotonic(), start))
            self.received += data
            self._parse(bytes(data), start)

    def _parse(self, data: bytes, start: int) -> None:
        while data:
            before = self._tok.bytes_seen
            resume = None
            for ev in self._tok.feed(data):
                self._event(ev.kind, ev.params, self._tok_base + ev.offset)
                if ev.kind == "opaque":           # skip the unknown command, parse on
                    resume = max(0, ev.offset + len(ev.params) - before)
            if resume is None:
                return
            data, start = data[resume:], start + resume
            self._tok = RasterTokenizer()
            self._tok_base = start

    def _event(self, kind: str, params: bytes, offset: int) -> None:
        now = time.monotonic()
        if kind == "status_request":
            self.status_requests.append(offset)
            if self._job_start is not None:
                self.violations.append(offset)
            if not self.mute:
                status_type = ST_ERROR if (self.err1 or self.err2 or self.error_detail) else ST_REPLY
                self._schedule(self.frame(status_type), now + self.reply_delay)
                if self.after_reply is not None:
                    self._schedule(self.after_reply, now + self.reply_delay)
                    self.after_reply = None
        elif kind == "init":                       # ESC @ cancels a job and any printing
            self.inits.append(offset)
            self._job_start = None
            self._two_color = False
            self._printing_until = 0.0
            self._out = [item for item in self._out if not item[3]]
        elif kind == "print_last":
            self._end_job(offset, now)
        elif kind == "auto_status":
            if params:
                self.notify = params[0] == 0x00
        elif kind == "command_mode":
            if params not in (b"\x01", b"\xff") and self._job_start is None:
                self._job_start = offset           # not raster: what follows is a job of its own kind
        elif kind in _JOB_START_EVENTS:
            if kind == "expanded_mode" and params:
                self._two_color = bool(params[0] & 0x01)
            if self._job_start is None:
                self._job_start = offset

    def _color_mismatch(self) -> int:
        """The error code the printer reports for the job that just ended:
        ``0x24`` ("media does not support monochrome printing") for a
        black-only job on a black/red roll (byte 25 ``0x81``), ``0x23`` for a
        two-colour job on a single-colour roll (``0x01``), else 0."""
        roll = self.base[25]
        if roll == 0x81 and not self._two_color:
            return 0x24
        if roll == 0x01 and self._two_color:
            return 0x23
        return 0

    def _end_job(self, offset: int, now: float) -> None:
        start = self._job_start if self._job_start is not None else offset
        self._job_start = None
        done = now + self.print_seconds
        job: Dict[str, Any] = {"start": start, "end": offset + 1, "two_color": self._two_color,
                               "ended_at": now, "done_at": done, "failed": False}
        self.jobs.append(job)
        mismatch = self._color_mismatch()
        if mismatch:
            # Nothing prints. The tested unit, after a black-only job on DK-22251,
            # answers no status request until it is switched off and on.
            job["failed"] = True
            self.error_detail = mismatch
            self._schedule(self.frame(ST_ERROR, PHASE_RECEIVING), now)
            if mismatch == 0x24:
                self.mute = True
            return
        self._printing_until = done
        if self.notify:
            self._schedule(self.frame(ST_PHASE_CHANGE, PHASE_PRINTING), now, cancellable=True)
        if self.fail_next_job is not None:
            err1, err2 = self.fail_next_job
            self.fail_next_job = None
            job["failed"] = True
            self.err1 |= err1
            self.err2 |= err2
            self._schedule(self.frame(ST_ERROR, PHASE_RECEIVING), done)
            return
        if self.notify:
            self._schedule(self.frame(ST_PRINTING_COMPLETED, PHASE_PRINTING), done, cancellable=True)
            self._schedule(self.frame(ST_PHASE_CHANGE, PHASE_RECEIVING), done, cancellable=True)

    # -- inspection ----------------------------------------------------------
    def time_of(self, offset: int) -> Optional[float]:
        """When the byte at stream ``offset`` arrived."""
        with self._lock:
            found = None
            for when, start in self.receive_log:
                if start <= offset:
                    found = when
                else:
                    break
            return found if offset < len(self.received) else None


class FakePrinter(usbio.Transport):
    """An in-process printer behind the Transport interface.

    ``attach()`` opens a session (what a device source's ``open`` returns);
    ``sessions`` keeps the bytes of each. Knobs: ``write_limit`` (bytes per
    write call), ``write_rate`` (bytes/s), ``stall_after`` (accept no more
    once this many bytes arrived), ``stall_after_jobs`` (accept no more once
    the model has seen this many job ends), ``stalled``, ``flush_blocked``
    (the last write transfer never completes, like a usblp URB the printer
    does not take: ``flush()`` waits out its timeout and returns False;
    writes are not affected, and ``attach()`` clears it, as closing the node
    drops the transfer), ``empty_reads`` (reads that return b"" at once),
    ``open_error`` (raised by ``attach``).
    """

    kind = "fake"

    def __init__(self, model: Optional[PrinterModel] = None, *, node: str = "/dev/usb/lp0") -> None:
        self.model = model or PrinterModel()
        self.node = node
        self._cv = threading.Condition()
        self.present = True
        self._closed = True
        self.sessions: List[bytearray] = []
        self.total_written = 0
        self.write_limit: Optional[int] = None
        self.write_rate: Optional[float] = None
        self.stall_after: Optional[int] = None
        self.stall_after_jobs: Optional[int] = None
        self.stalled = False
        self.flush_blocked = False
        self.empty_reads = 0
        self.open_error: Optional[BaseException] = None
        self.device_id_text: Optional[str] = TEST_DEVICE_ID
        self._outbuf = bytearray()

    # -- bus side --------------------------------------------------------------
    def attach(self) -> "FakePrinter":
        with self._cv:
            if not self.present:
                raise usbio.DeviceGone("fake printer is unplugged")
            if self.open_error is not None:
                raise self.open_error
            self._closed = False
            self.flush_blocked = False            # a reopened node has no transfer pending
            self._outbuf.clear()
            self.sessions.append(bytearray())
            return self

    def unplug(self) -> None:
        with self._cv:
            self.present = False
            self._cv.notify_all()

    def replug(self) -> None:
        with self._cv:
            self.present = True
            self._cv.notify_all()

    def set_stalled(self, stalled: bool) -> None:
        with self._cv:
            self.stalled = stalled
            if not stalled:
                self.stall_after = None
                self.stall_after_jobs = None
            self._cv.notify_all()

    def set_flush_blocked(self, blocked: bool) -> None:
        with self._cv:
            self.flush_blocked = blocked
            self._cv.notify_all()

    # -- Transport -------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    def _check(self) -> None:
        if self._closed:
            raise usbio.UsbError("fake transport is closed")
        if not self.present:
            raise usbio.DeviceGone("fake printer unplugged")

    def write(self, data: usbio.BytesLike, timeout: float) -> int:
        payload = bytes(data)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while True:
                self._check()
                allowed = 0 if self.stalled else len(payload)
                if self.stall_after is not None:
                    allowed = min(allowed, max(0, self.stall_after - self.total_written))
                if self.stall_after_jobs is not None and len(self.model.jobs) >= self.stall_after_jobs:
                    allowed = 0
                if self.write_limit is not None:
                    allowed = min(allowed, self.write_limit)
                if self.write_rate:
                    allowed = min(allowed, max(1, int(self.write_rate * max(timeout, 0.001))))
                if allowed > 0 or not payload:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 0
                self._cv.wait(min(remaining, 0.01))
            chunk = payload[:allowed]
            self.sessions[-1] += chunk
            self.total_written += len(chunk)
        if self.write_rate and chunk:
            time.sleep(len(chunk) / self.write_rate)
        self.model.feed(chunk)
        return len(chunk)

    def read(self, max_len: int, timeout: float) -> bytes:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._cv:
                self._check()
                if self.empty_reads > 0:
                    self.empty_reads -= 1
                    return b""
                self._outbuf += self.model.pending()
                if self._outbuf:
                    out = bytes(self._outbuf[:max_len])
                    del self._outbuf[:max_len]
                    return out
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cv.wait(min(remaining, 0.005))

    def flush(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while True:
                self._check()
                if not self.flush_blocked:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(min(remaining, 0.01))

    def device_id(self, timeout: float = 2.0) -> Optional[str]:
        with self._cv:
            self._check()
            return self.device_id_text

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()


class FakeBus:
    """A device source (``scan``/``open``) with one fake printer."""

    def __init__(self, printer: Optional[FakePrinter] = None) -> None:
        self.printer = printer or FakePrinter()
        self.mode = usbio.MODE_PRINTER
        self.scans = 0

    def info(self) -> usbio.PrinterInfo:
        """What sysfs would show for the printer in its current mode."""
        printer_mode = self.mode == usbio.MODE_PRINTER
        return usbio.PrinterInfo(
            sysfs_path="/sys/bus/usb/devices/1-1", vid="04f9", pid="209c" if printer_mode else "209f",
            serial=FAKE_SERIAL if printer_mode else "FAKE0SERIAL2", product="QL-810W",
            manufacturer="Brother", mode=self.mode, lp_node=self.printer.node if printer_mode else None,
            interface=0 if printer_mode else None, alt_setting=0 if printer_mode else None,
            busnum=1, devnum=4)

    def scan(self) -> Optional[usbio.PrinterInfo]:
        self.scans += 1
        if self.mode == usbio.MODE_PRINTER and not self.printer.present:
            return None
        return self.info()

    def open(self, info: usbio.PrinterInfo) -> usbio.Transport:
        if self.mode != usbio.MODE_PRINTER:
            raise usbio.UsbError(usbio.EDITOR_LITE_HINT)
        return self.printer.attach()

    def set_editor_lite(self, enabled: bool) -> None:
        """Switch Editor Lite on (the printer re-enumerates as a USB drive) or off."""
        if enabled:
            self.mode = usbio.MODE_MASS_STORAGE
            self.printer.unplug()
        else:
            self.mode = usbio.MODE_PRINTER
            self.printer.replug()


class PtyPrinter:
    """A :class:`PrinterModel` behind a pseudo-terminal: open :attr:`path`
    like ``/dev/usb/lp0``. :meth:`pause` stops reading (back-pressure);
    :meth:`unplug` hangs the terminal up. POSIX only."""

    def __init__(self, model: Optional[PrinterModel] = None) -> None:
        import pty
        import tty

        self.model = model or PrinterModel()
        self._master, self._slave = pty.openpty()
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        os.set_blocking(self._master, False)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._out = bytearray()
        self._master_open = True
        self._thread = threading.Thread(target=self._run, name="pty-printer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            readers = [] if self._paused.is_set() else [self._master]
            writers = [self._master] if self._out else []
            try:
                ready_r, ready_w, _ = select.select(readers, writers, [], 0.005)
            except (OSError, ValueError):
                return
            if ready_r:
                try:
                    data = os.read(self._master, 65536)
                except BlockingIOError:
                    data = b""
                except OSError:
                    return
                if data:
                    self.model.feed(data)
            self._out += self.model.pending()
            if self._out and (ready_w or not writers):
                try:
                    count = os.write(self._master, bytes(self._out))
                    del self._out[:count]
                except BlockingIOError:
                    pass
                except OSError:
                    return

    def pause(self) -> None:
        """Stop reading what the host writes (its writes then block)."""
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def _stop_thread(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def unplug(self) -> None:
        """Hang up: the device side sees the printer disappear."""
        self._stop_thread()
        if self._master_open:
            self._master_open = False
            os.close(self._master)

    def close(self) -> None:
        self.unplug()
        try:
            os.close(self._slave)
        except OSError:
            pass
