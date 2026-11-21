"""ql810wc-proxyd: the data plane of ql810wc-print-proxy (docs/architecture.md section 4).

It is the only process that touches the printer. It forwards raw print jobs
from TCP ``RAW_PORT`` to the USB printer byte for byte, polls the printer's
32-byte status block while no job is in flight, and publishes everything in
``state.json`` (section 5) for the SNMP persona and ``qlctl``.

Connections are served one at a time; the others wait in the listen backlog,
which gives clients the same TCP back-pressure as the real printer. Each
connection goes through these states::

    IDLE ──job bytes──▶ WRITING ──0x1A at a command boundary──▶ SETTLING ──▶ IDLE
                           │                                    (printer back at
                           │                                     "receiving")
                           └── RST/error, RAW_IDLE_TIMEOUT or MAX_JOB_BYTES before
                               the 0x1A: torn, the printer gets 400 x NUL + ESC @

* **WRITING** - client bytes go to USB unmodified and in order. A
  :class:`~ql810wc.rasterproto.RasterTokenizer` watches them (job end,
  two-color bit, requested media) but never changes them; an opaque stream is
  simply forwarded. The proxy never sends ``ESC i S`` while a job is in flight.
* **SETTLING** - after the job's ``0x1A`` the proxy reads the frames the
  printer volunteers and polls ``ESC i S`` until the phase is back at
  "receiving" (capped). Bytes the client sends meanwhile belong to that job:
  P-touch for Mac sends a trailer (``1B 69 55 74 1C 00``, ``1B 69 61 FF``, on
  some paths a bare ``ESC i S``) once SNMP shows the label done, and a failed
  send fails its print. They are read into a hold buffer, so the client never
  stalls, and forwarded verbatim once the printer is idle again. They are never
  dropped, never resynchronised on and never make a job torn.
* The connection closes only after the client half-closed and the job is
  settled (delivery acknowledgement: everything is with the printer, and the
  result is in ``state.json``), or after ``RAW_IDLE_TIMEOUT`` seconds without
  data. Never on a timer after ``0x1A``. Every wait on the printer is bounded:
  a printer that takes nothing for ``RAW_IDLE_TIMEOUT`` seconds (while
  forwarding, or after a half-close without ``0x1A``), or has not taken the
  end of a job when settling ends, fails the job; the client is reset and the
  transport reopened, which drops the stuck USB transfer.
* **Torn** means an abnormal end before the job's ``0x1A``: a reset or socket
  error, ``RAW_IDLE_TIMEOUT``, ``MAX_JOB_BYTES``, or the service stopping.
  Parser state alone never makes a job torn. A clean end of stream in the middle
  of a command is reported as ``error`` and the printer is resynchronised too.

Device lifecycle: the printer is found through sysfs (rescanned every few
seconds while absent), opened through the kernel usblp node (pyusb as a
fallback) and kept open. Editor Lite mode, absence and USB errors are reported
in ``state.json``; the daemon never exits because of the printer. The
printer's input state is "unknown" at start-up, after it (re)appears, after a
torn or aborted write (or an opaque job that ended in an error) and after a
status request it took only in part; the next status request is then
preceded by 400 x NUL + ``ESC @``. A printer-reported fault at the end of a
complete job is no reason: ``ESC @`` would cancel the job the printer holds
until the fault is cleared. For the same reason the ``ESC @`` waits while the
printer reports an error or no roll, or is printing (only the NULs and
``ESC i S`` go out until two polls in a row find it idle). A printer that
stops answering status requests is closed and reopened.

systemd: ``Type=notify`` (``READY=1`` once listening and after the first
device scan, so the first ``state.json`` never calls an attached printer
absent; ``STATUS=``, watchdog pings), socket activation through
``LISTEN_FDS`` (or its own dual-stack listener for development), ``SIGTERM``
lets the current job finish within a grace period, then aborts it cleanly and
exits 0.
"""
from __future__ import annotations

import argparse
import errno
import ipaddress
import logging
import math
import os
import select
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from . import __version__, media, statecache, usbio
from . import config as qconfig
from .rasterproto import (
    INVALIDATE,
    PHASE_PRINTING,
    PHASE_RECEIVING,
    PHASE_TYPES,
    RESYNC,
    ST_PRINTING_COMPLETED,
    ST_REPLY,
    RasterTokenizer,
    Status,
)
from .usbio import (
    MODE_MASS_STORAGE,
    MODE_PRINTER,
    DeviceGone,
    FrameReader,
    PrinterInfo,
    Transport,
    TransportTimeout,
    UsbError,
)

log = logging.getLogger(__name__)

# Exit codes (sysexits.h). The service unit does not restart on 64 and 78.
EX_OK = 0
EX_USAGE = 64
EX_SOFTWARE = 70
EX_OSERR = 71
EX_CONFIG = 78

#: ``RAW_IDLE_TIMEOUT`` default (from ``config.SETTINGS``, like its bounds):
#: seconds without client data before the connection is closed.
DEFAULT_RAW_IDLE_TIMEOUT = int(qconfig.SETTINGS["RAW_IDLE_TIMEOUT"].default)
#: ``MAX_JOB_BYTES`` default: a larger job is torn (a 1 m two-color label is about 4.4 MB).
DEFAULT_MAX_JOB_BYTES = int(qconfig.SETTINGS["MAX_JOB_BYTES"].default)
MIN_MAX_JOB_BYTES = int(qconfig.SETTINGS["MAX_JOB_BYTES"].minimum or 0)
#: First file descriptor passed by systemd socket activation.
SD_LISTEN_FDS_START = 3

#: ``ESC i !`` value that silences the printer's volunteered frames.
_AUTO_STATUS_OFF = 0x01
#: ``ESC i a`` value of ``ESC i a FF``, the end of P-touch for Mac's job trailer.
_COMMAND_MODE_RESTORE = 0xFF
#: Repeated identical log lines (a printer stuck in a fault) are shown at most this often.
_LOG_REPEAT_SECONDS = 600.0
#: Longest wait between reopen attempts of a printer that stopped answering.
_MAX_REOPEN_BACKOFF = 60.0
_JOURNAL_PRIORITY = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4,
                     logging.ERROR: 3, logging.CRITICAL: 2}


def _mono() -> float:
    return time.monotonic()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class ProxySettings:
    """How the daemon behaves. :meth:`from_config` builds it from runtime.env;
    the timing fields (seconds) have production defaults, and tests shrink them."""

    listen: Tuple[str, int] = ("", 9100)     # host "" = every IPv4 and IPv6 address
    state_path: str = statecache.DEFAULT_PATH
    bidirectional: bool = False
    poll_seconds: float = 3.0                # idle ESC i S cadence (STATUS_POLL_SECONDS)
    status_timeout: float = usbio.STATUS_TIMEOUT
    raw_idle_timeout: float = float(DEFAULT_RAW_IDLE_TIMEOUT)
    max_job_bytes: int = DEFAULT_MAX_JOB_BYTES
    post_job_timeout: float = 60.0           # longest wait for the printer after a job
    post_job_poll: float = 1.0               # ESC i S cadence while settling
    post_job_error_grace: float = 5.0        # a printer error lasting this long ends settling
    settle_min: float = 1.0                  # "receiving" right after a job counts only after this
    write_stall_timeout: float = float(DEFAULT_RAW_IDLE_TIMEOUT)
    device_wait: float = 5.0                 # how long a new connection waits for a missing printer
    rescan_seconds: float = 2.0
    heartbeat_seconds: float = 3.0           # state.json is rewritten at least this often
    stop_grace: float = 10.0                 # SIGTERM: time the current job gets to finish
    io_slice: float = 0.05                   # USB read cadence during a job (no usable descriptor)
    idle_io_slice: float = 0.25              # USB read cadence while idle (volatile frames only)
    unanswered_limit: int = 3                # failed idle status requests before a reopen
    hold_limit: int = 64 * 1024              # client bytes read ahead while settling
    recv_size: int = 64 * 1024
    keepalive: Tuple[int, int, int] = (30, 10, 3)   # idle s, interval s, probes
    vendor_id: str = usbio.BROTHER_VID
    product_id: str = ""
    serial: str = ""
    device: str = "auto"                     # "auto" (sysfs) or a /dev/usb/lpN path
    allow_pyusb: bool = True

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any], *, listen: Optional[Tuple[str, int]] = None,
                    device: str = "auto", state_path: Optional[str] = None) -> "ProxySettings":
        """Settings from a runtime config (``config.load_runtime_config``).

        Also reads ``RAW_IDLE_TIMEOUT`` (seconds, 10-86400, default 300) and
        ``MAX_JOB_BYTES`` (64 KiB - 1 GiB, default 16 MiB). ``listen`` overrides
        ``RAW_BIND``/``RAW_PORT``. Raises ``config.ConfigError``.
        """
        if device != "auto" and not device.startswith("/"):
            raise qconfig.ConfigError(f"device {device!r}: use 'auto' or an absolute path such as /dev/usb/lp0")
        poll = qconfig.get_int(cfg, "STATUS_POLL_SECONDS")
        # Defaults and bounds come from config.SETTINGS, so the installer's check and the
        # daemon judge every value alike (tests/test_config.py DataPlaneLimitsTest).
        idle = qconfig.get_int(cfg, "RAW_IDLE_TIMEOUT")
        max_bytes = qconfig.get_int(cfg, "MAX_JOB_BYTES")
        if listen is None:
            listen = (str(cfg.get("RAW_BIND") or "").strip(), qconfig.get_int(cfg, "RAW_PORT"))
        return cls(
            listen=listen,
            state_path=state_path or str(cfg.get("STATE_FILE") or statecache.DEFAULT_PATH),
            bidirectional=qconfig.get_bool(cfg, "RAW_BIDIRECTIONAL"),
            poll_seconds=float(poll),
            raw_idle_timeout=float(idle),
            max_job_bytes=max_bytes,
            write_stall_timeout=float(idle),
            heartbeat_seconds=float(min(30, poll)),
            vendor_id=str(cfg.get("USB_VENDOR_ID") or usbio.BROTHER_VID).strip().lower(),
            product_id=str(cfg.get("USB_PRODUCT_ID") or "").strip().lower(),
            serial=str(cfg.get("USB_SERIAL") or "").strip(),
            device=device,
        )


# ---------------------------------------------------------------------------
# systemd integration (standard library only)
# ---------------------------------------------------------------------------

class SdNotifier:
    """``sd_notify(3)`` over ``$NOTIFY_SOCKET``, plus watchdog pings.

    A socket name starting with ``@`` is in the Linux abstract namespace.
    Every method is a no-op without a socket, and none of them raises.
    """

    def __init__(self, address: Optional[str] = None, watchdog_usec: Optional[int] = None) -> None:
        self.address = address or None
        #: Seconds between ``WATCHDOG=1`` pings (half the systemd interval), or None.
        self.watchdog_interval: Optional[float] = watchdog_usec / 2e6 if watchdog_usec else None
        self._next_ping = 0.0
        self._status: Optional[str] = None

    @classmethod
    def from_environ(cls, environ: Optional[Mapping[str, str]] = None,
                     pid: Optional[int] = None) -> "SdNotifier":
        """From ``NOTIFY_SOCKET``, ``WATCHDOG_USEC`` and ``WATCHDOG_PID``."""
        env = os.environ if environ is None else environ
        usec: Optional[int]
        try:
            usec = int(env.get("WATCHDOG_USEC", "") or 0) or None
            watchdog_pid = env.get("WATCHDOG_PID", "")
            if usec and watchdog_pid and int(watchdog_pid) != (os.getpid() if pid is None else pid):
                usec = None
        except ValueError:
            usec = None
        return cls(env.get("NOTIFY_SOCKET") or None, usec)

    @property
    def enabled(self) -> bool:
        """True when there is a notification socket."""
        return self.address is not None

    def notify(self, **fields: object) -> bool:
        """Send ``KEY=value`` lines in one datagram; False if not delivered."""
        if not self.enabled or not fields:
            return False
        assert self.address is not None
        target = "\0" + self.address[1:] if self.address.startswith("@") else self.address
        payload = "".join(f"{key}={str(value).replace(chr(10), ' ')}\n" for key, value in fields.items())
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.sendto(payload.encode("utf-8", "replace"), target)
        except OSError as exc:
            log.debug("sd_notify to %s failed: %s", self.address, exc)
            return False
        return True

    def ready(self, status: str) -> None:
        """``READY=1`` with the first ``STATUS=``."""
        self._status = status
        self.notify(READY=1, STATUS=status, MAINPID=os.getpid())

    def status(self, text: str) -> None:
        """``STATUS=text`` when it differs from the last one sent."""
        if text != self._status:
            self._status = text
            self.notify(STATUS=text)

    def stopping(self, text: str = "stopping") -> None:
        """``STOPPING=1``."""
        self._status = text
        self.notify(STOPPING=1, STATUS=text)

    def watchdog(self, now: Optional[float] = None) -> None:
        """``WATCHDOG=1`` once the interval has passed (no-op without a watchdog)."""
        if not self.watchdog_interval:
            return
        current = _mono() if now is None else now
        if current >= self._next_ping:
            self._next_ping = current + self.watchdog_interval
            self.notify(WATCHDOG=1)

    def next_ping(self) -> float:
        """Monotonic time of the next due watchdog ping (infinity without one)."""
        return self._next_ping if self.watchdog_interval else math.inf


def systemd_listeners(environ: Optional[Dict[str, str]] = None,
                      pid: Optional[int] = None) -> List[socket.socket]:
    """Listening sockets passed by systemd (``LISTEN_FDS``/``LISTEN_PID``).

    ``LISTEN_PID``, ``LISTEN_FDS`` and ``LISTEN_FDNAMES`` are always removed
    from ``environ`` (default ``os.environ``), like ``sd_listen_fds(1)``.
    Returns ``[]`` when the descriptors are not meant for this process.
    Descriptors that are not listening stream sockets are closed and skipped.
    """
    env = os.environ if environ is None else environ
    raw_pid, raw_count = env.get("LISTEN_PID", ""), env.get("LISTEN_FDS", "")
    for key in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        env.pop(key, None)
    try:
        listen_pid, count = int(raw_pid), int(raw_count)
    except ValueError:
        return []
    if listen_pid != (os.getpid() if pid is None else pid) or count <= 0:
        return []
    sockets = []
    for fd in range(SD_LISTEN_FDS_START, SD_LISTEN_FDS_START + count):
        try:
            os.set_inheritable(fd, False)
            sock = socket.socket(fileno=fd)
        except OSError as exc:
            log.warning("ignoring inherited file descriptor %d: %s", fd, exc)
            continue
        try:
            listening = bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
        except (OSError, AttributeError):
            listening = True          # SO_ACCEPTCONN not supported (macOS): trust systemd
        if sock.type != socket.SOCK_STREAM or not listening:
            log.warning("ignoring inherited file descriptor %d: not a listening TCP socket", fd)
            sock.close()
            continue
        sockets.append(sock)
    return sockets


def parse_listen(text: str) -> Tuple[str, int]:
    """``HOST:PORT``, ``[IPV6]:PORT``, ``:PORT`` or ``PORT`` -> (host, port).

    An empty host (or ``*``) means every address, dual-stack. Raises ValueError.
    """
    value = text.strip()
    if value.startswith("["):
        host, sep, rest = value[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise ValueError(f"bad listen address {text!r} (use [IPV6]:PORT)")
        port_text = rest[1:]
    elif value.count(":") == 1:
        host, port_text = value.split(":")
    elif ":" not in value:
        host, port_text = "", value
    else:
        raise ValueError(f"bad listen address {text!r} (write IPv6 addresses as [IPV6]:PORT)")
    host = host.strip()
    if host == "*":
        host = ""
    if host:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(f"bad listen address {text!r}: {host!r} is not an IP address") from None
    if not port_text.isdigit() or not 0 <= int(port_text) <= 65535:
        raise ValueError(f"bad listen address {text!r}: the port must be 0-65535")
    return host, int(port_text)


def bind_listener(host: str, port: int, backlog: int = 32) -> socket.socket:
    """A listening TCP socket. An empty host (or ``::``) listens on every IPv4
    and IPv6 address (``[::]`` with ``IPV6_V6ONLY=0``), falling back to IPv4
    only when the host has no IPv6. Raises OSError."""
    if host in ("", "::"):
        if socket.has_dualstack_ipv6():
            try:
                return socket.create_server(("::", port), family=socket.AF_INET6,
                                            backlog=backlog, dualstack_ipv6=True)
            except OSError as exc:
                if exc.errno not in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                    raise
        return socket.create_server(("0.0.0.0", port), family=socket.AF_INET, backlog=backlog)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host, port), family=family, backlog=backlog)


def format_peer(address: Any) -> str:
    """``ip:port`` (``[ip]:port`` for IPv6; IPv4-mapped IPv6 shown as IPv4)."""
    if isinstance(address, tuple) and len(address) >= 2:
        host, port = str(address[0]), address[1]
        try:
            ip = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return f"{host}:{port}"
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None:
                return f"{ip.ipv4_mapped}:{port}"
            return f"[{host}]:{port}"
        return f"{host}:{port}"
    return str(address) or "local"


def mask_serial(serial: Optional[str]) -> str:
    """The first three characters of a serial number, for logs."""
    if not serial:
        return "unknown"
    return serial[:3] + "***" if len(serial) > 3 else serial


def _close_socket(sock: socket.socket, *, abort: bool) -> None:
    """Close ``sock``; ``abort`` sends a TCP RST instead of a FIN."""
    if abort:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
    try:
        sock.close()
    except OSError:
        pass


def media_summary(frame: Optional[Status]) -> Optional[Dict[str, Any]]:
    """The loaded roll as P-touch would recognise it (``media.describe``), or
    None when there is no frame or no media."""
    if frame is None or not frame.has_media:
        return None
    found = media.describe(frame)
    if found is None:
        return {"label_id": None, "name": None, "two_color": frame.two_color, "dk_parts": [],
                "description": f"unsupported roll (width {frame.media_width_mm} mm, type "
                               f"0x{frame.media_type_raw:02x}, length {frame.media_length_mm} mm)"}
    return {"label_id": found.label_id, "name": found.name, "two_color": found.needs_two_color,
            "dk_parts": list(media.dk_parts_for(frame)), "description": found.description}


# ---------------------------------------------------------------------------
# The printer
# ---------------------------------------------------------------------------

class UsbProvider:
    """Finds and opens the printer on USB: sysfs discovery, usblp or pyusb.

    The interface the daemon expects of a provider: ``scan()`` returns the
    printer's :class:`~ql810wc.usbio.PrinterInfo` (its ``mode`` says printer,
    Editor Lite or other) or None; ``open(info)`` returns a
    :class:`~ql810wc.usbio.Transport`; ``present(info)`` (optional) is a cheap
    check that an opened printer is still attached.
    """

    def __init__(self, settings: ProxySettings) -> None:
        self.settings = settings

    def scan(self) -> Optional[PrinterInfo]:
        """The configured printer (preferring printer mode), else a QL in
        Editor Lite mode, else None."""
        s = self.settings
        if s.device != "auto":
            return usbio.info_for_node(s.device) if os.path.exists(s.device) else None
        found = usbio.find_printer(s.vendor_id, s.product_id or None, s.serial or None)
        return found if found is not None else usbio.find_editor_lite(s.vendor_id)

    def open(self, info: PrinterInfo) -> Transport:
        """Open the printer's raster interface (raises usbio.UsbError)."""
        return usbio.open_transport(info, allow_pyusb=self.settings.allow_pyusb)

    def present(self, info: PrinterInfo) -> bool:
        """The opened device is still attached (sysfs and device node exist)."""
        if info.sysfs_path and not os.path.isdir(info.sysfs_path):
            return False
        return not info.lp_node or os.path.exists(info.lp_node)


FrameCallback = Callable[[Status, float, bool], None]

#: Clean probe replies in a row before a deferred ``ESC @`` is sent (see PrinterLink.request).
_CLEAN_PROBES_BEFORE_INIT = 2


def _busy(frame: Optional[Status]) -> bool:
    """The frame shows a printer that ``ESC @`` would disturb: an error or no
    roll (it may hold a job until the fault is cleared; the tested unit
    reports a removed roll without any error bit), the printing phase or the
    "printer in use" flag."""
    return frame is not None and (frame.is_error or not frame.has_media or frame.is_printing
                                  or frame.in_use)


class PrinterLink:
    """The printer as the daemon sees it: discovery, the open transport,
    status requests and volunteered frames.

    ``state`` is one of :data:`statecache.DEVICE_STATES`. Every frame read is
    passed to ``on_frame(frame, wall_time, solicited)``; ``on_change()`` runs
    when ``state``, ``detail`` or the device changes. The I/O methods let
    usbio's exceptions through after updating ``state``: a failing device is
    closed and found again by the next scan.

    :attr:`unknown` means the printer may be left inside a command or a job
    (a new or reopened transport, a torn or aborted write, a status request
    written only in part); the next status request then resynchronises it
    (see :meth:`request`). A status request that was written completely but
    got no reply leaves the stream at a command boundary and changes nothing.
    """

    def __init__(self, provider: Any, settings: ProxySettings, on_frame: FrameCallback,
                 on_change: Callable[[], None], log_once: Callable[[int, str], None]) -> None:
        self.provider = provider
        self.settings = settings
        self._on_frame = on_frame
        self._on_change = on_change
        self._log = log_once
        self.state = "absent"
        self.detail = "looking for the printer"
        self.info: Optional[PrinterInfo] = None
        #: The last printer-mode device opened: its identity is kept while it is away.
        self.identity: Optional[PrinterInfo] = None
        self.transport: Optional[Transport] = None
        self.ieee1284_id: Optional[str] = None
        self.reader = FrameReader()
        #: The printer's input state is unknown: resynchronise before the next request.
        self.unknown = True
        #: The last frame read from the open transport (None until the first one), and when.
        self.last_frame: Optional[Status] = None
        self.last_frame_at = 0.0
        #: While an ESC @ waits for an idle printer: clean probe replies in a row (see request()).
        self._probes: Optional[int] = None
        self._next_scan = 0.0
        self._next_presence = 0.0

    @property
    def ready(self) -> bool:
        """A transport is open."""
        return self.transport is not None and not self.transport.closed

    def fileno(self) -> Optional[int]:
        """Pollable descriptor of the open transport, if its readiness means data.

        usblp's readiness is useless for a QL: the printer answers every bulk-IN
        request with a zero-length packet, so the kernel marks the node readable
        again immediately and each read returns 0 bytes. Selecting on it spins a
        core (measured on the tested unit: ~1 CPU-second per second). usblp is
        therefore treated like libusb, as having no descriptor, and is read on a
        timer instead.
        """
        if not self.ready or self.transport is None:
            return None
        if getattr(self.transport, "kind", None) == "usblp":
            return None
        return self.transport.fileno()

    @property
    def write_slice(self) -> float:
        """Longest single write call, so the daemon stays responsive."""
        minimum = self.transport.min_write_timeout if self.transport is not None else 0.0
        return max(1.0, minimum)

    def next_scan(self) -> float:
        """Monotonic time of the next discovery attempt (infinity while open)."""
        return math.inf if self.ready else self._next_scan

    # -- lifecycle -------------------------------------------------------------
    def maintain(self, force: bool = False) -> None:
        """Look for the printer while it is not open (every ``rescan_seconds``,
        or now with ``force``); check that an open one is still attached."""
        now = _mono()
        if self.ready:
            present = getattr(self.provider, "present", None)
            if present is not None and (force or now >= self._next_presence):
                self._next_presence = now + self.settings.rescan_seconds
                assert self.info is not None
                if not present(self.info):
                    self.lost("the printer was disconnected")
            return
        if not force and now < self._next_scan:
            return
        self._next_scan = now + self.settings.rescan_seconds
        try:
            info = self.provider.scan()
        except OSError as exc:
            self._set_state("error", None, f"USB discovery failed: {exc}")
            return
        if info is None:
            self._set_state("absent", None, "no Brother QL printer on USB")
        elif info.mode == MODE_PRINTER:
            self._open(info)
        elif info.mode == MODE_MASS_STORAGE:
            self._set_state("mass-storage", info, usbio.EDITOR_LITE_HINT)
        else:
            self._set_state("error", info, f"{info.describe()} has no printer interface")

    def _open(self, info: PrinterInfo) -> None:
        try:
            transport = self.provider.open(info)
        except DeviceGone as exc:
            self._set_state("absent", None, str(exc))
            return
        except (UsbError, OSError) as exc:
            self._set_state("error", info, str(exc))
            return
        self.transport = transport
        self.identity = info
        self._forget_stream()
        self._next_presence = _mono() + self.settings.rescan_seconds
        self.ieee1284_id = usbio.read_ieee1284_id(info, transport)
        self._set_state("ready", info, "")

    def _set_state(self, state: str, info: Optional[PrinterInfo], detail: str) -> None:
        changed = (state, detail) != (self.state, self.detail) or _identity(info) != _identity(self.info)
        self.state, self.info, self.detail = state, info, detail
        if not changed:
            return
        if state == "ready":
            assert info is not None and self.transport is not None
            self._log(logging.INFO, f"printer ready: {info.describe()} via {self.transport.kind} "
                                    f"{self.transport.node}, serial {mask_serial(info.serial)}")
        elif state == "mass-storage":
            name = info.describe() if info is not None else "the printer"
            self._log(logging.WARNING, f"{name} is in Editor Lite mode. {detail}")
        elif state == "absent":
            self._log(logging.INFO, f"waiting for the printer: {detail}")
        else:
            self._log(logging.WARNING, f"printer unusable: {detail}")
        self._on_change()

    def lost(self, reason: str, state: str = "absent", retry_in: Optional[float] = None) -> None:
        """Close the transport after a failure; look for the printer again in
        ``retry_in`` seconds (default: soon)."""
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self._forget_stream()
        self.ieee1284_id = None
        delay = min(0.5, self.settings.rescan_seconds) if retry_in is None else retry_in
        self._next_scan = _mono() + delay
        self._set_state(state, self.info if state != "absent" else None, reason)

    def shutdown(self, flush_timeout: float = 1.0) -> None:
        """Flush and close the transport (daemon exit)."""
        if self.transport is not None and not self.transport.closed:
            try:
                self.transport.flush(flush_timeout)
            except UsbError:
                pass
            self.transport.close()
        self.transport = None

    # -- I/O ---------------------------------------------------------------------
    def _forget_stream(self) -> None:
        """A new or closed transport: nothing is known about the printer's input."""
        self.reader.reset()
        self.unknown = True
        self.last_frame = None
        self.last_frame_at = 0.0
        self._probes = None

    def _known(self) -> None:
        """A reply showed the printer at a command boundary with nothing owed."""
        self.unknown = False
        self._probes = None

    def _seen(self, frame: Status, solicited: bool) -> None:
        self.last_frame = frame
        self.last_frame_at = _mono()
        if self.unknown and _busy(frame):
            self._probes = 0                 # an ESC @ now waits for two clean probes in a row
        self._on_frame(frame, time.time(), solicited)

    def _io(self, operation: Callable[[Transport], Any]) -> Any:
        transport = self.transport
        if transport is None or transport.closed:
            raise DeviceGone(f"the printer is not available ({self.detail or self.state})")
        try:
            return operation(transport)
        except DeviceGone as exc:
            self.lost(f"the printer disappeared ({exc})")
            raise
        except TransportTimeout as exc:
            # Only a write that may have arrived in part leaves the stream position unknown;
            # a status request that was written but not answered does not.
            if exc.stream_unknown:
                self.unknown = True
            raise
        except UsbError as exc:
            self.lost(f"USB error: {exc}", state="error")
            raise

    def write(self, data: memoryview, timeout: float) -> int:
        """Bytes the printer accepted within ``timeout`` (see Transport.write)."""
        return int(self._io(lambda t: t.write(data, timeout)))

    def flush(self, timeout: float) -> bool:
        """True once every written byte has been handed to the printer."""
        return bool(self._io(lambda t: t.flush(timeout)))

    def collect(self, timeout: float = 0.0) -> List[Status]:
        """Frames the printer volunteers: one read, waiting at most ``timeout``."""
        frames: List[Status] = self._io(lambda t: self.reader.read(t, timeout))
        for frame in frames:
            self._seen(frame, False)
        return frames

    def request(self, resync: Optional[bool] = None, timeout: Optional[float] = None) -> Status:
        """``ESC i S``; returns the reply.

        ``resync`` None: resynchronise first if :attr:`unknown`. True: mark
        the input state unknown first. False: a plain request (settling after
        a job, when the stream is at a command boundary).

        Resynchronising is 400 x NUL + ``ESC @``. ``ESC @`` cancels printing
        and any job the printer holds until a fault is cleared, so while the
        last frame shows an error, no roll, the printing phase or the busy
        flag, the request is only a probe: 400 x NUL (they complete a partial
        command) and ``ESC i S``. The ``ESC @`` goes out right behind the
        second probe in a row that finds the printer idle, with no busy frame
        in between: a held job that starts printing once its fault is cleared
        shows up between the two. :attr:`unknown` stays True until a full
        resync is answered. With no frame read since the transport was opened
        (start-up, reopen) the full resync goes out at once.
        """
        if resync:
            self.unknown = True
        wait = self.settings.status_timeout if timeout is None else timeout
        if resync is False or not self.unknown:
            reply = self._ask(b"", wait)
            self._known()
            return reply
        for frame in self._io(lambda t: self.reader.drain(t)):
            self._seen(frame, False)          # frames already waiting are part of the decision
        if self._probes is None and _busy(self.last_frame):
            self._probes = 0
        if self._probes is not None:
            reply = self._ask(INVALIDATE, wait)
            if _busy(reply):
                return reply                  # _seen() has restarted the count
            self._probes += 1
            if self._probes < _CLEAN_PROBES_BEFORE_INIT:
                return reply
        reply = self._ask(RESYNC, wait)
        self._known()
        return reply

    def _ask(self, prefix: bytes, wait: float) -> Status:
        reply: Status = self._io(lambda t: usbio.request_status(
            t, timeout=wait, reader=self.reader, prefix=prefix,
            on_frame=lambda frame: self._seen(frame, False)))
        self._seen(reply, True)
        return reply


def _identity(info: Optional[PrinterInfo]) -> Tuple[Any, ...]:
    if info is None:
        return ()
    return (info.sysfs_path, info.vid, info.pid, info.serial, info.lp_node, info.mode)


# ---------------------------------------------------------------------------
# Jobs and connections
# ---------------------------------------------------------------------------

@dataclass
class JobRecord:
    """One print job, from its first command to the printer being back at
    "receiving". ``bytes`` counts the job's data; bytes that followed its
    ``0x1A`` on the same connection (P-touch's trailer) are ``trailer_bytes``."""

    peer: str
    started: float                           # wall clock
    started_mono: float
    bytes: int = 0
    trailer_bytes: int = 0
    two_color: Optional[bool] = None
    requested_media: Optional[Dict[str, int]] = None
    pages: int = 0
    opaque: Optional[str] = None
    printing_seen: bool = False              # a frame showed the printing phase
    completed_seen: bool = False             # a "printing completed" frame arrived
    settled: bool = False
    result: Optional[str] = None
    detail: str = ""
    finished: Optional[float] = None         # wall clock
    finished_mono: Optional[float] = None

    def summary(self) -> str:
        """The per-job log line."""
        end = self.finished_mono if self.finished_mono is not None else _mono()
        parts = [f"job from {self.peer}: {self.bytes} bytes in {max(0.0, end - self.started_mono):.1f} s"]
        if self.two_color is not None:
            parts.append("two-color" if self.two_color else "black only")
        if self.requested_media:
            parts.append(describe_requested_media(self.requested_media))
        if self.trailer_bytes:
            parts.append(f"{self.trailer_bytes}-byte trailer")
        if self.opaque:
            parts.append(f"stream not fully parsed ({self.opaque})")
        parts.append(f"result {self.result or 'unknown'}" + (f" ({self.detail})" if self.detail else ""))
        return ", ".join(parts)


def describe_requested_media(requested: Mapping[str, int]) -> str:
    """``ESC i z`` contents as text, e.g. ``requested 62 mm continuous``."""
    width, length = requested.get("width", 0), requested.get("length", 0)
    if requested.get("type") == 0x0B or length:
        return f"requested {width}x{length} mm die-cut"
    return f"requested {width} mm continuous"


class _Abort(Exception):
    """Stops serving a connection. ``kind``: client | device | usb | stop | internal."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class _Segment:
    """The bytes of one connection since it opened, or since a job on it settled."""

    def __init__(self, after_job: Optional[JobRecord] = None) -> None:
        self.tok = RasterTokenizer()
        self.bytes = 0
        self.job: Optional[JobRecord] = None
        self.after_job = after_job

    def observe(self, data: bytes) -> Tuple[int, bool]:
        """Feed ``data`` to the tokenizer, stopping right after the first job end
        (``0x1A`` at a command position). Returns (bytes consumed, job ended)."""
        tok = self.tok
        total = len(data)
        view = memoryview(data)
        pos = 0
        while pos < total:
            if tok.opaque is not None:
                tok.feed(view[pos:])
                return total, False
            hit = data.find(b"\x1a", pos)
            end = total if hit < 0 else hit + 1
            events = tok.feed(view[pos:end])
            pos = end
            if hit >= 0 and events and events[-1].kind == "print_last" \
                    and events[-1].offset == tok.bytes_seen - 1:
                return pos, True
        return total, False

    def has_job_content(self) -> bool:
        """True once the segment holds a job. After a settled job only print
        data starts a new one; anything else is that job's trailer."""
        tok = self.tok
        if self.after_job is None:
            return tok.in_job or tok.jobs > 0 or tok.opaque is not None or not tok.at_command_boundary
        return tok.requested_media is not None or tok.raster_lines > 0 or tok.pages > 0

    @property
    def at_rest(self) -> bool:
        """No job here and the stream is at a known command boundary: status
        requests are safe.

        That includes a settled job's trailer that ended with ``ESC i a FF``
        (P-touch for Mac), for as long as nothing has followed it: the
        tokenizer stops parsing at that command, but the stream still stands
        right behind it. Without this, a client that keeps the connection open
        after its trailer would stop the idle polls, and the persona would
        soon report a printer that is idle and fine as switched off. Once
        anything follows the trailer, it cannot be judged any more, and the
        polls wait until the connection closes."""
        tok = self.tok
        if self.job is not None or tok.in_job:
            return False
        if tok.opaque is None:
            return tok.at_command_boundary
        return (self.after_job is not None and tok.command_mode == _COMMAND_MODE_RESTORE
                and tok.opaque_end is not None and tok.bytes_seen == tok.opaque_end)


class _Session:
    """One client connection."""

    def __init__(self, sock: socket.socket, peer: str) -> None:
        self.sock = sock
        self.peer = peer
        self.held = bytearray()
        self.eof = False
        self.error: Optional[str] = None
        self.idle_timed_out = False
        self.closed = False
        self.idle_since = _mono()
        self.bytes_in = 0

    @property
    def finished(self) -> bool:
        """The client has nothing more to send."""
        return self.eof or self.error is not None or self.closed

    def close(self, abort: bool = False) -> None:
        """Close the connection (FIN, or RST with ``abort``). Idempotent."""
        if not self.closed:
            self.closed = True
            _close_socket(self.sock, abort=abort)

    def recv_now(self, limit: int) -> bytes:
        """What the client sent, without waiting; sets ``eof`` / ``error``."""
        if self.finished or limit <= 0:
            return b""
        try:
            data = self.sock.recv(limit)
        except (BlockingIOError, InterruptedError):
            return b""
        except ConnectionResetError:
            self.error = "the client reset the connection"
            return b""
        except OSError as exc:
            self.error = f"connection error: {exc.strerror or exc}"
            return b""
        if not data:
            self.eof = True
            return b""
        self.idle_since = _mono()
        self.bytes_in += len(data)
        return data

    def check_reset(self) -> bool:
        """True (and ``error`` set) if the socket reports that the client is gone."""
        if self.finished:
            return self.error is not None
        try:
            code = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        except OSError as exc:
            code = exc.errno or errno.EBADF
        if code:
            self.error = f"connection error: {os.strerror(code)}"
            return True
        return False


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------

class ProxyDaemon:
    """The data-plane daemon: one listener loop, one printer, one job at a time.

    ``listeners``: bound, listening TCP sockets (closed on exit). ``provider``:
    finds and opens the printer (:class:`UsbProvider`, or a fake in tests).
    ``cache``: where ``state.json`` goes (default: ``settings.state_path``).
    :meth:`run` serves until :meth:`request_stop`.
    """

    def __init__(self, settings: ProxySettings, listeners: Sequence[socket.socket], provider: Any,
                 cache: Optional[statecache.StateCache] = None, notifier: Optional[SdNotifier] = None) -> None:
        if not listeners:
            raise ValueError("no listening socket")
        self.settings = settings
        self.listeners = list(listeners)
        for sock in self.listeners:
            sock.setblocking(False)
        self.cache = cache if cache is not None else statecache.StateCache(settings.state_path)
        self.notifier = notifier if notifier is not None else SdNotifier()
        self._log_times: Dict[str, float] = {}
        self.link = PrinterLink(provider, settings, self._on_frame, self._on_device_change, self._log_once)
        self.port = self.listeners[0].getsockname()[1]
        self._doc = statecache.new_state(pid=os.getpid(), raw_port=self.port, started=time.time())
        self._doc["job"].update(requested_media=None, last_detail=None)
        self._doc["device"]["detail"] = self.link.detail
        self._dirty: Optional[str] = None
        self._last_save = -math.inf
        self._next_heartbeat = 0.0
        self._save_failed = False
        self._ready_sent = False            # READY=1 goes first; STATUS= updates only after it
        self._stop = False
        self._stop_reason = ""
        self._stop_deadline = math.inf
        self._stop_logged = False
        self._session: Optional[_Session] = None
        self._job: Optional[JobRecord] = None
        self._next_poll = 0.0
        self._unanswered = 0
        self._reopen_backoff = settings.rescan_seconds
        self._last_frame_wall: Optional[float] = None

    # -- control -----------------------------------------------------------------
    def request_stop(self, reason: str = "stop requested") -> None:
        """Ask :meth:`run` to finish (safe from a signal handler or another
        thread). A job in flight gets ``stop_grace`` seconds before it is aborted."""
        if not self._stop:
            self._stop_reason = reason
            self._stop_deadline = _mono() + self.settings.stop_grace
            self._stop = True

    def _stop_expired(self) -> bool:
        return self._stop and _mono() >= self._stop_deadline

    def run(self) -> int:
        """Serve until stopped; returns the exit code (0)."""
        addresses = ", ".join(format_peer(s.getsockname()) for s in self.listeners)
        log.info("ql810wc-proxyd %s listening on %s", __version__, addresses)
        try:
            # The first device scan comes before the first state.json and READY=1, so
            # neither reports a printer that is attached as absent.
            self.link.maintain(force=True)
            self._save(force=True)
            self.notifier.ready(f"listening; {self._status_text()}")
            self._ready_sent = True
            while not self._stop:
                self._loop_once()
        finally:
            self._shutdown()
        return EX_OK

    def _shutdown(self) -> None:
        log.info("stopping (%s)", self._stop_reason or "stop requested")
        self.notifier.stopping()
        self.link.shutdown()
        self._doc["proxy"]["pid"] = None
        self._doc["job"]["active"] = False
        self._save(force=True)
        for sock in self.listeners:
            try:
                sock.close()
            except OSError:
                pass
        log.info("stopped")

    # -- housekeeping ----------------------------------------------------------------
    def _log_once(self, level: int, message: str) -> None:
        """Log ``message``; an identical repeat within 10 minutes goes to debug."""
        now = _mono()
        last = self._log_times.get(message)
        if last is not None and now - last < _LOG_REPEAT_SECONDS:
            log.debug("%s", message)
            return
        if len(self._log_times) >= 256:
            self._log_times.clear()
        self._log_times[message] = now
        log.log(level, "%s", message)

    def _tick(self) -> None:
        if self._stop and not self._stop_logged:
            self._stop_logged = True
            log.info("stop requested (%s)", self._stop_reason)
        self.notifier.watchdog()
        self._save()

    def _mark(self, level: str) -> None:
        if level == "now" or self._dirty is None:
            self._dirty = level

    def _save(self, force: bool = False) -> None:
        now = _mono()
        due = (force or now >= self._next_heartbeat or self._dirty == "now"
               or (self._dirty == "progress" and now - self._last_save >= 1.0))
        if not due:
            return
        try:
            self.cache.write(self._doc)
        except (OSError, ValueError) as exc:
            if not self._save_failed:
                log.error("cannot write %s: %s", self.cache.path, exc)
                self._save_failed = True
        else:
            if self._save_failed:
                log.info("%s is writable again", self.cache.path)
                self._save_failed = False
        self._dirty = None
        self._last_save = now
        self._next_heartbeat = now + self.settings.heartbeat_seconds

    def _next_wakeup(self, now: float) -> float:
        """Seconds until the next timer of the idle loop (at most 1 s)."""
        due = [now + 1.0, self._next_heartbeat, self.notifier.next_ping()]
        if self.link.ready:
            due.append(self._next_poll)
        else:
            due.append(self.link.next_scan())
        return max(0.0, min(due) - now)

    def _status_text(self) -> str:
        link = self.link
        if link.state == "mass-storage":
            return "printer in Editor Lite mode: press and hold its Editor Lite button until the LED goes out"
        if not link.ready:
            return f"waiting for the printer: {link.detail or link.state}"
        job = self._job
        if job is not None and not job.settled:
            return f"printing job from {job.peer} ({job.bytes} bytes)"
        frame = statecache.status_from_state(self._doc)
        if frame is None:
            return "printer attached; reading its status"
        found = media_summary(frame)
        roll = found["description"] if found else "no media"
        errors = frame.errors()
        if errors:
            return f"printer error: {', '.join(errors)} ({roll})"
        if frame.is_printing:
            return f"printing ({roll})"
        return f"ready: {roll}"

    def _notify_status(self) -> None:
        """``STATUS=`` for systemd (once ``READY=1`` has gone out)."""
        if self._ready_sent:
            self.notifier.status(self._status_text())

    def _on_device_change(self) -> None:
        link = self.link
        ident = link.identity or link.info
        transport = link.transport if link.ready else None
        self._doc["device"] = {
            "state": link.state,
            "vid": ident.vid if ident else None,
            "pid": ident.pid if ident else None,
            "product": ident.product if ident else None,
            "manufacturer": ident.manufacturer if ident else None,
            "serial": ident.serial if ident else None,
            "node": transport.node if transport is not None else None,
            "transport": transport.kind if transport is not None else None,
            "ieee1284_id": link.ieee1284_id if transport is not None else None,
            "detail": link.detail or None,
        }
        if transport is None or link.unknown:
            # A new or missing device: the previous status block no longer applies.
            self._doc["status"] = {"raw_hex": None, "polled": None, "parsed": None, "media": None,
                                   "source": None}
        if transport is not None:
            self._next_poll = 0.0           # a printer that just appeared: read its status at once
        self._mark("now")
        self._notify_status()

    def _on_frame(self, frame: Status, wall: float, solicited: bool) -> None:
        self._doc["status"] = {"raw_hex": frame.raw.hex(), "polled": wall, "parsed": frame.to_dict(),
                               "media": media_summary(frame),
                               "source": "reply" if solicited else "unsolicited"}
        self._last_frame_wall = wall
        job = self._job
        if job is not None and not job.settled:
            if frame.phase_type == PHASE_PRINTING:
                job.printing_seen = True
            if frame.status_type == ST_PRINTING_COMPLETED:
                job.completed_seen = True
        if not solicited:
            log.debug("the printer volunteered: %s, phase %s", frame.to_dict()["status_type_name"],
                      PHASE_TYPES.get(frame.phase_type, frame.phase_type))
            self._relay(frame)
        self._mark("now")
        self._notify_status()

    def _relay(self, frame: Status) -> None:
        """RAW_BIDIRECTIONAL: pass a frame from the printer to the client."""
        sess = self._session
        if not self.settings.bidirectional or sess is None or sess.closed:
            return
        try:
            sess.sock.send(frame.raw)
        except BlockingIOError:
            log.debug("client %s is not reading; a status frame was not relayed", sess.peer)
        except OSError as exc:
            if sess.error is None:
                sess.error = f"connection error: {exc.strerror or exc}"

    def _select(self, items: Sequence[Union[socket.socket, int]], timeout: float) -> List[Any]:
        try:
            readable, _, _ = select.select(list(items), [], [], max(0.0, timeout))
        except (OSError, ValueError) as exc:
            log.debug("select failed: %s", exc)
            time.sleep(min(max(0.0, timeout), 0.1))
            return []
        return readable

    def _collect(self) -> None:
        if self.link.ready:
            try:
                self.link.collect(0.0)
            except UsbError:
                pass                     # the link has updated its state

    # -- idle loop ---------------------------------------------------------------------
    def _loop_once(self) -> None:
        self._tick()
        self.link.maintain()
        if self.link.ready and _mono() >= self._next_poll:
            self._poll_status()
        timeout = self._next_wakeup(_mono())
        usb_fd = self.link.fileno()
        if self.link.ready and usb_fd is None:
            timeout = min(timeout, self.settings.idle_io_slice)  # no usable descriptor: read on a timer
        items: List[Union[socket.socket, int]] = list(self.listeners)
        if usb_fd is not None:
            items.append(usb_fd)
        readable = self._select(items, timeout)
        if self.link.ready and (usb_fd is None or usb_fd in readable):
            self._collect()
        for listener in self.listeners:
            if listener in readable and not self._stop:
                self._accept(listener)
                break

    def _poll_status(self) -> None:
        """One idle status poll (never during a job)."""
        self._next_poll = _mono() + self.settings.poll_seconds
        try:
            self.link.request()
        except TransportTimeout as exc:
            self._unanswered += 1
            if self._unanswered == 1:
                self._log_once(logging.WARNING, f"printer status request failed: {exc}" + (
                    "; resynchronising before the next one" if self.link.unknown else ""))
            if self._unanswered >= self.settings.unanswered_limit:
                frame = self.link.last_frame
                if (frame is not None and frame.is_printing
                        and _mono() - self.link.last_frame_at < self.settings.post_job_timeout):
                    # It may just not answer while printing: a reopen resynchronises it at once,
                    # and that ESC @ would cut the label short. Wait for it (bounded).
                    return
                self._unanswered = 0
                retry = self._reopen_backoff
                self._reopen_backoff = min(_MAX_REOPEN_BACKOFF, self._reopen_backoff * 2)
                self.link.lost(f"the printer does not answer status requests "
                               f"({self.settings.unanswered_limit} in a row); reopening it", state="error",
                               retry_in=retry)
        except UsbError as exc:
            log.debug("status poll: %s", exc)
        else:
            if self._unanswered:
                log.info("the printer answers status requests again")
            self._unanswered = 0
            self._reopen_backoff = self.settings.rescan_seconds

    def _accept(self, listener: socket.socket) -> None:
        try:
            sock, address = listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            log.warning("accept failed: %s", exc)
            return
        peer = format_peer(address)
        if not self.link.ready:
            deadline = _mono() + self.settings.device_wait
            while not self.link.ready and _mono() < deadline and not self._stop:
                self._tick()
                self.link.maintain(force=True)
                if not self.link.ready:
                    time.sleep(min(0.2, max(0.0, deadline - _mono())))
            if not self.link.ready:
                self._refuse(sock, peer)
                return
        try:
            self._configure_client(sock)
        except OSError as exc:
            log.warning("connection from %s failed: %s", peer, exc)
            _close_socket(sock, abort=True)
            return
        sess = _Session(sock, peer)
        self._session = sess
        log.debug("connection from %s", peer)
        try:
            self._serve(sess)
        finally:
            self._session = None
            sess.close(abort=True)          # no-op unless something went badly wrong
            self._next_poll = _mono() + self.settings.poll_seconds

    def _refuse(self, sock: socket.socket, peer: str) -> None:
        """Reject a connection while the printer is unusable: the client sees a
        reset at once, and state.json records the refused job."""
        reason = f"printer not available: {self._status_text()}"
        _close_socket(sock, abort=True)
        job = JobRecord(peer=peer, started=time.time(), started_mono=_mono())
        self._finish_job(job, "error", reason, stream_unknown=False)    # nothing reached the printer
        log.warning("refused a print job from %s (%s)", peer, reason)

    def _configure_client(self, sock: socket.socket) -> None:
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle, interval, count = self.settings.keepalive
        for name, value in (("TCP_KEEPIDLE", idle), ("TCP_KEEPALIVE", idle),
                            ("TCP_KEEPINTVL", interval), ("TCP_KEEPCNT", count)):
            option = getattr(socket, name, None)
            if option is None:
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError:
                pass

    # -- one connection ------------------------------------------------------------------
    def _serve(self, sess: _Session) -> None:
        seg = _Segment()
        jobs: List[JobRecord] = []
        abort: Optional[_Abort] = None
        try:
            while True:
                self._tick()
                if self._stop_expired():
                    raise _Abort("stop", "the service is stopping")
                if sess.held:
                    data = bytes(sess.held)
                    sess.held.clear()
                else:
                    if sess.finished:
                        break
                    data = self._receive(sess, seg)
                    if not data:
                        if sess.finished:
                            break
                        if _mono() - sess.idle_since >= self.settings.raw_idle_timeout:
                            sess.error = f"no data from the client for {self.settings.raw_idle_timeout:g} s"
                            sess.idle_timed_out = True
                            break
                        continue
                consumed, ended = seg.observe(data)
                if seg.bytes + consumed > self.settings.max_job_bytes:
                    sess.error = f"the job exceeds MAX_JOB_BYTES ({self.settings.max_job_bytes} bytes)"
                    break
                if seg.job is None and seg.has_job_content():
                    seg.job = self._begin_job(sess, seg)
                    jobs.append(seg.job)
                stall = None
                if seg.job is None and seg.after_job is not None and seg.after_job.result != "ok":
                    # The trailer of a failed job: do not hold the connection for minutes
                    # on a printer that has stopped accepting data.
                    stall = self.settings.post_job_error_grace
                self._forward_data(sess, memoryview(data)[:consumed], stall)
                self._account(seg, consumed)
                if ended:
                    job = seg.job
                    assert job is not None      # a 0x1A at a command position is job content
                    sess.held[0:0] = data[consumed:]
                    self._settle_job(sess, seg, job)
                    seg = _Segment(after_job=job)
                    sess.idle_since = _mono()
        except _Abort as exc:
            abort = exc
        except Exception as exc:   # one bad connection must not take the print server down
            log.exception("internal error while serving %s", sess.peer)
            abort = _Abort("internal", f"internal error: {exc}")
        self._end_connection(sess, seg, jobs, abort)

    def _receive(self, sess: _Session, seg: _Segment) -> bytes:
        """Wait briefly for client data while serving the printer: volunteered
        frames are read, and status polls run while no job is in flight."""
        if seg.at_rest and self.link.ready and _mono() >= self._next_poll:
            self._poll_status()
        usb_fd = self.link.fileno()
        items: List[Union[socket.socket, int]] = [sess.sock]
        if usb_fd is not None:
            items.append(usb_fd)
        wait = 0.5 if usb_fd is not None else self.settings.io_slice
        if seg.at_rest and self.link.ready:
            wait = min(wait, max(0.0, self._next_poll - _mono()))
        readable = self._select(items, wait)
        if self.link.ready and (usb_fd is None or usb_fd in readable):
            self._collect()
        if sess.sock in readable:
            return sess.recv_now(self.settings.recv_size)
        return b""

    def _begin_job(self, sess: _Session, seg: _Segment) -> JobRecord:
        # The bytes this segment held before the job content: on a fresh connection they
        # belong to the job (its NUL padding); after a settled job they were counted as
        # that job's trailer already (_account), so the new job starts from zero.
        held_before = seg.bytes if seg.after_job is None else 0
        if seg.after_job is not None:
            log.debug("a new job follows on the connection from %s", sess.peer)
        job = JobRecord(peer=sess.peer, started=time.time(), started_mono=_mono(), bytes=held_before)
        self._job = job
        self._publish_job(job)
        self._mark("now")
        self._notify_status()
        return job

    def _account(self, seg: _Segment, count: int) -> None:
        seg.bytes += count
        job = seg.job
        if job is None:
            if seg.after_job is not None:
                seg.after_job.trailer_bytes += count
            return
        tok = seg.tok
        job.bytes += count
        if tok.expanded_mode is not None:
            job.two_color = tok.two_color
        if tok.requested_media is not None:
            job.requested_media = dict(tok.requested_media)
        job.pages = tok.pages
        job.opaque = tok.opaque
        self._publish_job(job)
        self._mark("progress")

    def _forward_data(self, sess: _Session, view: memoryview, stall_timeout: Optional[float] = None) -> None:
        """Write client bytes to the printer, in order; back-pressure the client
        while the printer is slow, give up once it has accepted nothing for
        ``stall_timeout`` seconds (default ``write_stall_timeout``)."""
        stall = self.settings.write_stall_timeout if stall_timeout is None else stall_timeout
        stall_deadline = _mono() + stall
        while len(view):
            self._tick()
            if self._stop_expired():
                raise _Abort("stop", "the service is stopping")
            if not self.link.ready:
                raise _Abort("device", f"the printer is not available ({self.link.detail})")
            try:
                count = self.link.write(view, self.link.write_slice)
            except TransportTimeout as exc:
                raise _Abort("usb", str(exc)) from None
            except UsbError as exc:
                raise _Abort("device", str(exc)) from None
            if count:
                view = view[count:]
                stall_deadline = _mono() + stall
                continue
            self._collect()
            if sess.check_reset():
                raise _Abort("client", sess.error or "the client reset the connection")
            if _mono() >= stall_deadline:
                raise _Abort("usb", f"the printer accepted no data for {stall:g} s")

    # -- settling --------------------------------------------------------------------------
    def _settle_job(self, sess: _Session, seg: _Segment, job: JobRecord) -> None:
        """The job's 0x1A was forwarded: wait for the printer, then record the
        result. The stream ended at a command boundary, so a fault the printer
        reports (cover open, no roll) never makes it resynchronise: ``ESC @``
        would cancel the job the printer holds until the fault is cleared."""
        self._account(seg, 0)
        result, detail = self._settle(sess, job, expect_print=True, auto_status=seg.tok.auto_status)
        self._finish_job(job, result, detail, stream_unknown=False)

    def _settle(self, sess: Optional[_Session], job: JobRecord, *, expect_print: bool,
                auto_status: Optional[int]) -> Tuple[str, str]:
        """Wait until the printer is back at "receiving" after a job's last byte.

        The printer must first take the job's last USB transfer (usblp
        completes writes in the background); only then is ``ESC i S`` polled,
        every ``post_job_poll`` seconds except while the printer itself reports
        that it is printing. Volunteered frames are read throughout, client
        bytes go to the hold buffer, and nothing here resynchronises. Ends:

        * "ok" when a reply shows "receiving" (once ``settle_min`` has passed,
          unless the print was seen), or at ``post_job_timeout`` with no error;
        * "error" when a printer error has lasted ``post_job_error_grace``
          seconds, at ``post_job_timeout`` after one, or after three
          unanswered polls;
        * "error" when the printer has not taken the end of the job by then
          (see :meth:`_undelivered`): never "ok" for data it did not receive.

        Returns (result, detail).
        """
        s = self.settings
        start = _mono()
        deadline = start + s.post_job_timeout
        notify = auto_status != _AUTO_STATUS_OFF
        last_poll: Optional[float] = None
        last_volunteered: Optional[float] = None
        error_detail = ""
        error_since: Optional[float] = None
        timeouts = 0
        delivered = False
        while True:
            self._settle_service(sess)
            if not self.link.ready:
                return "error", f"the printer went away ({self.link.detail})"
            try:
                delivered = delivered or self.link.flush(0.0)
            except UsbError:
                continue                      # the link has updated its state
            now = _mono()
            if self._stop_expired():
                if not delivered:
                    return "error", "the service stopped before the printer accepted the end of the job"
                return ("error", error_detail) if error_detail else (
                    "ok", "the service stopped before the printer reported completion")
            if now >= deadline:
                if not delivered:
                    return self._undelivered(sess, f"the printer did not accept the end of the job within "
                                                   f"{s.post_job_timeout:g} s")
                if error_detail:
                    return "error", error_detail
                return "ok", f"the printer did not report completion within {s.post_job_timeout:g} s"
            try:
                frames = self.link.collect(min(0.1, deadline - now))
            except UsbError:
                continue
            for frame in frames:
                last_volunteered = _mono()
                if frame.is_error:
                    error_detail = ", ".join(frame.errors())
                    error_since = error_since or _mono()
            now = _mono()
            if error_since is not None and now - error_since >= s.post_job_error_grace:
                if not delivered:
                    return self._undelivered(sess, f"{error_detail}; the printer did not accept the end "
                                                   f"of the job")
                return "error", error_detail
            if not delivered:
                continue                      # a status request would only queue behind that data
            busy = (notify and error_since is None and job.printing_seen and not job.completed_seen
                    and last_volunteered is not None and now - last_volunteered < 10.0)
            if busy or (last_poll is not None and now - last_poll < s.post_job_poll):
                continue
            last_poll = now
            try:
                reply = self.link.request(resync=False, timeout=min(s.status_timeout, max(0.1, deadline - now)))
            except TransportTimeout:
                timeouts += 1
                if timeouts >= 3:
                    return "error", "the printer did not answer status requests after the job"
                continue
            except UsbError:
                continue
            timeouts = 0
            if reply.is_error:
                error_detail = ", ".join(reply.errors())
                error_since = error_since or _mono()
                continue
            error_since = None
            if reply.phase_type == PHASE_RECEIVING and reply.status_type in (ST_REPLY, ST_PRINTING_COMPLETED):
                if (not expect_print or job.printing_seen or job.completed_seen
                        or _mono() - start >= s.settle_min):
                    return "ok", (f"recovered after: {error_detail}" if error_detail else "")

    def _undelivered(self, sess: Optional[_Session], detail: str) -> Tuple[str, str]:
        """The end of the job is stuck in a USB transfer the printer does not
        take. Close the transport, which drops that transfer, and reopen it
        (the reopened printer is resynchronised): otherwise the data would go
        out whenever the printer recovers, behind whatever the next status
        request or job brings. The client is reset once the result is
        recorded, since the job was not delivered. Returns the job's result."""
        if sess is not None and sess.error is None:
            sess.error = detail
        self.link.lost(f"{detail}; reopening it", state="error")
        return "error", detail

    def _settle_service(self, sess: Optional[_Session]) -> None:
        """While settling: housekeeping, and read the client into the hold buffer."""
        self._tick()
        if sess is None or sess.finished:
            return
        room = self.settings.hold_limit - len(sess.held)
        if room > 0:
            data = sess.recv_now(min(room, self.settings.recv_size))
            if data:
                sess.held.extend(data)

    def _publish_job(self, job: JobRecord) -> None:
        doc = self._doc["job"]
        doc.update(active=not job.settled, peer=job.peer, started=job.started, bytes=job.bytes,
                   two_color=job.two_color, requested_media=job.requested_media)
        if job.settled:
            doc.update(last_result=job.result, last_finished=job.finished, last_detail=job.detail or None)

    def _finish_job(self, job: JobRecord, result: str, detail: str, *, stream_unknown: bool) -> None:
        """Record the job's result in state.json (written before the client is let go).

        ``stream_unknown``: the printer may be left inside a command or a job
        (an aborted or torn write not resynchronised yet, an opaque stream that
        ended in an error), so its next status request resynchronises it. A
        job whose 0x1A was delivered leaves the stream at a command boundary
        whatever the printer reports; resynchronising after a fault there
        would cancel the job the printer holds until the fault is cleared."""
        job.settled = True
        job.result = result
        job.detail = detail
        job.finished_mono = _mono()
        frame_time = self._last_frame_wall
        job.finished = frame_time if frame_time is not None and frame_time >= job.started else time.time()
        if stream_unknown:
            self.link.unknown = True
        self._publish_job(job)
        self._mark("now")
        self._save()
        self._notify_status()

    def _resync_printer(self) -> bool:
        """Resynchronise the printer now (400 x NUL + ESC @ + ESC i S); True once
        that is done. While the printer reports an error or is printing, the
        ESC @ waits for later status requests (PrinterLink.request): False."""
        if not self.link.ready:
            return False
        try:
            self.link.request(resync=True)
        except UsbError:
            return False
        return not self.link.unknown

    def _end_connection(self, sess: _Session, seg: _Segment, jobs: List[JobRecord],
                        abort: Optional[_Abort]) -> None:
        job = seg.job
        if job is not None and not job.settled:
            self._end_unsettled_job(sess, seg, job, abort)
        graceful = abort is None and sess.error is None
        if job is None and (sess.idle_timed_out or (abort is not None and abort.kind == "stop")):
            graceful = True                 # nothing pending: an ordinary close
        sess.close(abort=not graceful)
        for record in jobs:
            log.info("%s", record.summary())
        problem = abort.detail if abort is not None else sess.error
        if not jobs:
            if sess.bytes_in:
                log.info("connection from %s: %d bytes, no print job%s", sess.peer, sess.bytes_in,
                         f" ({problem})" if problem else "")
            else:
                log.debug("connection from %s closed without data", sess.peer)
        elif job is None and abort is not None:
            # Ended while forwarding what followed a settled job (a trailer).
            log.log(logging.INFO if abort.kind == "stop" else logging.WARNING,
                    "connection from %s ended after its job: %s", sess.peer, abort.detail)
        if self._job is not None and self._job.settled:
            self._job = None
        self._notify_status()

    def _end_unsettled_job(self, sess: _Session, seg: _Segment, job: JobRecord,
                           abort: Optional[_Abort]) -> None:
        """The connection ended before this job's 0x1A was forwarded and settled."""
        kind = abort.kind if abort is not None else None
        if kind in ("device", "usb", "internal"):
            # The write was aborted: where the printer's input stands is unknown.
            sess.close(abort=True)
            assert abort is not None
            self._finish_job(job, "error", abort.detail, stream_unknown=True)
            return
        reason = abort.detail if abort is not None else sess.error
        if reason is not None:
            # Abnormal end before the job's 0x1A: torn. Resynchronise the printer.
            sess.close(abort=True)
            fixed = self._resync_printer()
            self._finish_job(job, "torn", f"{reason}; printer " + (
                "resynchronised" if fixed else "resynchronisation pending"), stream_unknown=not fixed)
            return
        # The client half-closed before its 0x1A: the printer takes the last of the data first.
        if not self._await_delivery(sess, job):
            return
        if seg.tok.needs_resync:
            fixed = self._resync_printer()
            log.warning("%s closed the connection in the middle of a job (no 0x1A); %s", sess.peer,
                        "printer resynchronised" if fixed else "printer resynchronisation pending")
            self._finish_job(job, "error", "the client closed the connection before the end of the job",
                             stream_unknown=not fixed)
            return
        if self.link.ready:
            result, detail = self._settle(sess, job, expect_print=True, auto_status=seg.tok.auto_status)
            # Only an opaque stream gets here (needs_resync is never true for one). Its end
            # cannot be judged: after an error the printer may be stuck inside it.
            unknown = result != "ok" and seg.tok.opaque is not None
        else:
            result, detail, unknown = "error", f"the printer is not available ({self.link.detail})", True
        self._finish_job(job, result, detail, stream_unknown=unknown)

    def _await_delivery(self, sess: _Session, job: JobRecord) -> bool:
        """After the client's half-close: wait until the printer has taken every
        byte written to it (usblp completes the last transfer in the
        background), reading the frames it volunteers meanwhile.

        Bounded like :meth:`_forward_data`: once the printer has accepted
        nothing for ``write_stall_timeout`` seconds the job ends as an error,
        the transport is closed (which drops the stuck transfer) and reopened
        (the reopened printer is resynchronised), and the client is reset
        after the result is recorded. A printer that holds the job during a
        fault keeps it for that long. False when the job was finished here."""
        s = self.settings
        deadline = _mono() + s.write_stall_timeout
        while self.link.ready and not self._stop_expired():
            try:
                if self.link.flush(0.25):
                    return True
            except UsbError:
                return True                   # the link has updated its state
            self._tick()
            self._collect()
            if _mono() >= deadline:
                detail = (f"the printer accepted no data for {s.write_stall_timeout:g} s after the client "
                          f"finished")
                self.link.lost(f"{detail}; reopening it", state="error")
                self._finish_job(job, "error", detail, stream_unknown=True)
                if sess.error is None:
                    sess.error = detail       # _end_connection resets the client
                return False
        return True


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

class _ArgumentParser(argparse.ArgumentParser):
    """argparse that exits with EX_USAGE (systemd does not restart on it)."""

    def error(self, message: str) -> "NoReturn":  # type: ignore[name-defined]  # noqa: F821
        self.print_usage(sys.stderr)
        self.exit(EX_USAGE, f"{self.prog}: error: {message}\n")


class _JournalFormatter(logging.Formatter):
    """``<priority>message`` lines, so journald keeps the log level."""

    def format(self, record: logging.LogRecord) -> str:
        return f"<{_JOURNAL_PRIORITY.get(record.levelno, 6)}>{super().format(record)}"


def setup_logging(level: str) -> None:
    """Log to stderr: priority-prefixed lines under systemd, timestamped otherwise."""
    handler = logging.StreamHandler(sys.stderr)
    if os.environ.get("JOURNAL_STREAM") or os.environ.get("INVOCATION_ID"):
        handler.setFormatter(_JournalFormatter("%(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def build_parser() -> argparse.ArgumentParser:
    """The ``ql810wc-proxyd`` command line."""
    parser = _ArgumentParser(
        prog="ql810wc-proxyd",
        description="Forward raw print jobs from TCP to a USB Brother QL-810W(c) and publish its "
                    "status in state.json (docs/architecture.md section 4).")
    parser.add_argument("--config", metavar="FILE",
                        help=f"runtime settings (default: {qconfig.RUNTIME_ENV} if it exists, "
                             "else built-in defaults)")
    parser.add_argument("--state", metavar="FILE", help="state.json to write (default: STATE_FILE)")
    parser.add_argument("--listen", metavar="HOST:PORT",
                        help="where to listen when not socket-activated (default: RAW_BIND:RAW_PORT; "
                             "an empty host means every IPv4 and IPv6 address)")
    parser.add_argument("--device", metavar="auto|PATH", default="auto",
                        help="the printer: 'auto' (found on USB) or a /dev/usb/lpN node")
    parser.add_argument("--log-level", metavar="LEVEL", type=str.upper,
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help="debug, info, warning or error (default: LOG_LEVEL)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _install_signal_handlers(daemon: ProxyDaemon) -> None:
    def handler(signum: int, _frame: Any) -> None:
        daemon.request_stop(signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point of ``ql810wc-proxyd``; returns the exit code."""
    args = build_parser().parse_args(argv)
    config_path = args.config
    if config_path is None and os.path.exists(qconfig.RUNTIME_ENV):
        config_path = qconfig.RUNTIME_ENV
    try:
        listen = parse_listen(args.listen) if args.listen else None
    except ValueError as exc:
        print(f"ql810wc-proxyd: {exc}", file=sys.stderr)
        return EX_USAGE
    try:
        cfg = qconfig.load_runtime_config(config_path)
        settings = ProxySettings.from_config(cfg, listen=listen, device=args.device, state_path=args.state)
    except qconfig.ConfigError as exc:
        print(f"ql810wc-proxyd: configuration error: {exc}", file=sys.stderr)
        return EX_CONFIG
    setup_logging(args.log_level or str(cfg.get("LOG_LEVEL") or "INFO"))
    if config_path is None:
        log.warning("no %s: using built-in defaults", qconfig.RUNTIME_ENV)
    listeners = systemd_listeners()
    if listeners:
        log.debug("socket-activated with %d listener(s)", len(listeners))
    else:
        host, port = settings.listen
        try:
            listeners = [bind_listener(host, port)]
        except OSError as exc:
            log.error("cannot listen on %s: %s", format_peer((host or "*", port)), exc.strerror or exc)
            return EX_OSERR
    daemon = ProxyDaemon(settings, listeners, UsbProvider(settings),
                         statecache.StateCache(settings.state_path), SdNotifier.from_environ())
    _install_signal_handlers(daemon)
    try:
        return daemon.run()
    except Exception:
        log.exception("fatal error")
        return EX_SOFTWARE


__all__ = [
    "DEFAULT_MAX_JOB_BYTES", "DEFAULT_RAW_IDLE_TIMEOUT", "EX_CONFIG", "EX_OK", "EX_OSERR", "EX_SOFTWARE",
    "EX_USAGE", "MIN_MAX_JOB_BYTES", "JobRecord", "PrinterLink", "ProxyDaemon", "ProxySettings",
    "SdNotifier", "UsbProvider", "bind_listener", "build_parser", "describe_requested_media",
    "format_peer", "main", "mask_serial", "media_summary", "parse_listen", "setup_logging",
    "systemd_listeners",
]
