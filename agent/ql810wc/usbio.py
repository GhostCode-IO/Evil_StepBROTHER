"""USB access to the Brother QL printer: discovery, transports, status requests.

Only ``ql810wc-proxyd`` talks to the printer (and ``qlctl probe`` while the
daemon is stopped); everything else reads ``state.json``
(docs/architecture.md sections 2 and 4).

Discovery (Linux sysfs, no privileges needed)
    :func:`scan_printers` and :func:`find_printer` walk ``/sys/bus/usb/devices``
    with the rules of ``qlp_detect_printer`` in ``scripts/lib/common.sh``:
    vendor ``04f9``; product id and serial only when pinned in the config;
    and, unless the product id is pinned, only products named ``QL-...``.
    The mode comes from the interface classes: a printer interface (class
    07) means printer mode; mass storage (class 08) without one means Editor
    Lite is on. Product ids and serials are never assumed: the QL-810Wc
    reports ``04f9:209c`` as a printer and ``04f9:209f`` in Editor Lite mode,
    with a different serial string in each mode.

Transports (:class:`Transport`)
    :class:`UsblpTransport` uses the kernel ``usblp`` node (``/dev/usb/lpN``),
    the normal path: non-blocking I/O with poll-based timeouts. The kernel
    lets only one process open it (``EBUSY`` otherwise).
    :class:`PyUsbTransport` (optional ``python3-pyusb``) claims the class-7
    interface itself when ``usblp`` is not bound, and gives it back on close.

Status
    :func:`request_status` sends ``ESC i S`` (optionally preceded by the
    400 x NUL + ``ESC @`` resync) and returns the printer's 32-byte reply,
    the block that is also served over SNMP. Never call it while a job is
    being forwarded: a request inside a job corrupts it.

Hardware facts used here are in ``dev/notes/usb-ground-truth.md``.
"""
from __future__ import annotations

import abc
import errno
import logging
import math
import os
import platform
import select
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, Union

from .rasterproto import RESYNC, STATUS_LEN, STATUS_REQUEST, Status, split_status_frames

try:  # POSIX only (the daemons run on Linux; the tests also run on macOS).
    import fcntl
except ImportError:  # pragma: no cover - not reached on Linux or macOS
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

BytesLike = Union[bytes, bytearray, memoryview]

#: Brother Industries USB vendor id.
BROTHER_VID = "04f9"
USB_CLASS_PRINTER = 0x07
USB_CLASS_MASS_STORAGE = 0x08
#: Printer-class protocol 4 is IPP-over-USB (an alternate setting on the
#: QL-810Wc); raster always goes through protocol 1/2.
PRINTER_PROTOCOL_IPP_USB = 0x04
PRINTER_PROTOCOL_BIDIRECTIONAL = 0x02

#: Device modes reported by discovery.
MODE_PRINTER = "printer"
MODE_MASS_STORAGE = "mass-storage"
MODE_OTHER = "other"

SYSFS_USB_DEVICES = "/sys/bus/usb/devices"
SYSFS_USBMISC = "/sys/class/usbmisc"
DEV_USB_DIR = "/dev/usb"

#: Bytes per USB IN read (the size of usblp's read buffer).
READ_SIZE = 1024
#: Default time allowed for one status request (Brother's own tools use 2 s).
STATUS_TIMEOUT = 2.0
#: Size of an IEEE-1284 device id buffer (usblp's USBLP_DEVICE_ID_SIZE).
DEVICE_ID_BUFFER = 1024

#: What to tell the operator when the printer is in Editor Lite mode.
EDITOR_LITE_HINT = ("Editor Lite is on: the printer shows up as a USB drive instead of a "
                    "printer. Press and hold the Editor Lite button until its green LED "
                    "goes out.")

_ZLP_BACKOFF = 0.01        # pause after a zero-length read, so an idle device cannot spin us
_DRAIN_TIMEOUT = 0.02      # wait per read while draining stale frames
_MAX_DRAIN_READS = 64
_READY, _TIMEOUT, _HANGUP = "ready", "timeout", "hangup"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class UsbError(Exception):
    """USB I/O with the printer failed. ``errno`` holds the OS error number, if any."""

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.errno = code


class DeviceGone(UsbError):
    """The printer is no longer there (unplugged, powered off, re-enumerated)."""


class DeviceBusy(UsbError):
    """Another process holds the printer (the kernel allows one usblp opener)."""


class TransportTimeout(UsbError):
    """The printer did not accept or deliver data in time."""

    @property
    def stream_unknown(self) -> bool:
        """True when the printer may have been left inside a command (part of
        the data may have arrived): resynchronise before the next request."""
        return True


class StatusTimeout(TransportTimeout):
    """No reply to ``ESC i S`` arrived in time.

    Raised as is when the whole request was written: the printer's input is
    then still at a command boundary (it may be busy or mute), so
    :attr:`stream_unknown` is False. A request the printer did not take in
    time raises :class:`StatusRequestNotAccepted` instead."""

    @property
    def stream_unknown(self) -> bool:
        return False


class StatusRequestNotAccepted(StatusTimeout):
    """The printer did not accept the whole status request in time.

    ``written`` of the request's ``total`` bytes went out. When none did, the
    stream is where it was; when only part of it did, the printer may be left
    inside a command (:attr:`stream_unknown`)."""

    def __init__(self, message: str, *, written: int, total: int) -> None:
        super().__init__(message)
        self.written = written
        self.total = total

    @property
    def stream_unknown(self) -> bool:
        return 0 < self.written < self.total


_GONE_ERRNOS = frozenset({errno.ENODEV, errno.ENXIO, errno.ENOENT, errno.ESHUTDOWN})


def _from_os_error(exc: OSError, what: str) -> UsbError:
    """Translate an OSError from a device node into the matching UsbError."""
    code = exc.errno
    reason = exc.strerror or str(exc)
    if code in _GONE_ERRNOS:
        return DeviceGone(f"{what}: {reason}", code=code)
    if code == errno.EBUSY:
        return DeviceBusy(f"{what}: device busy (another program has the printer open)", code=code)
    if code in (errno.EACCES, errno.EPERM):
        return UsbError(f"{what}: permission denied (the service user needs group lp)", code=code)
    return UsbError(f"{what}: {reason}", code=code)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrinterInfo:
    """One Brother USB device as seen in sysfs (or just a device node).

    ``mode`` is :data:`MODE_PRINTER`, :data:`MODE_MASS_STORAGE` (Editor Lite)
    or :data:`MODE_OTHER`. ``interface`` / ``alt_setting`` / ``interface_path``
    describe the raster printer interface; ``lp_node`` is its ``/dev/usb/lpN``
    when the kernel usblp driver is bound. ``serial`` is opaque (it differs
    between printer and Editor Lite mode) and never leaves the box.
    """

    sysfs_path: Optional[str] = None
    vid: Optional[str] = None
    pid: Optional[str] = None
    serial: Optional[str] = None
    product: Optional[str] = None
    manufacturer: Optional[str] = None
    mode: str = MODE_OTHER
    lp_node: Optional[str] = None
    interface: Optional[int] = None
    alt_setting: Optional[int] = None
    interface_path: Optional[str] = None
    busnum: Optional[int] = None
    devnum: Optional[int] = None

    @property
    def usb_id(self) -> str:
        """``vid:pid`` (``????`` for an unknown part)."""
        return f"{self.vid or '????'}:{self.pid or '????'}"

    def describe(self) -> str:
        """One line for logs, e.g. ``Brother QL-810W (04f9:209c, printer, /dev/usb/lp0)``."""
        name = " ".join(part for part in (self.manufacturer, self.product) if part) or "USB printer"
        where = self.lp_node or (f"bus {self.busnum} device {self.devnum}"
                                 if self.busnum is not None else "no usblp node")
        if self.vid is None and self.pid is None:
            return f"{name} ({self.mode}, {where})"
        return f"{name} ({self.usb_id}, {self.mode}, {where})"


class _Interface(NamedTuple):
    path: str
    number: int
    alt: int
    cls: int
    subclass: int
    protocol: int
    lp_node: Optional[str]


def _read_attr(directory: str, name: str) -> Optional[str]:
    """A sysfs attribute as text without surrounding blanks; None if unreadable."""
    try:
        with open(os.path.join(directory, name), "rb") as fh:
            data = fh.read(4096)
    except OSError:
        return None
    return data.decode("utf-8", "replace").replace("\x00", "").strip()


def _int_attr(directory: str, name: str, base: int) -> Optional[int]:
    text = _read_attr(directory, name)
    if not text:
        return None
    try:
        return int(text, base)
    except ValueError:
        return None


def _interfaces(device_dir: str, dev_dir: str) -> List[_Interface]:
    """Interfaces of one USB device (its ``<name>:<cfg>.<if>`` subdirectories)."""
    name = os.path.basename(os.path.normpath(device_dir))
    try:
        entries = sorted(os.listdir(device_dir))
    except OSError:
        return []
    found = []
    for entry in entries:
        if not entry.startswith(name + ":"):
            continue
        path = os.path.join(device_dir, entry)
        cls = _int_attr(path, "bInterfaceClass", 16)
        if cls is None:
            continue
        try:
            nodes = sorted(n for n in os.listdir(os.path.join(path, "usbmisc")) if n.startswith("lp"))
        except OSError:
            nodes = []
        found.append(_Interface(
            path=path,
            number=_int_attr(path, "bInterfaceNumber", 16) or 0,
            alt=_int_attr(path, "bAlternateSetting", 10) or 0,
            cls=cls,
            subclass=_int_attr(path, "bInterfaceSubClass", 16) or 0,
            protocol=_int_attr(path, "bInterfaceProtocol", 16) or 0,
            lp_node=os.path.join(dev_dir, nodes[0]) if nodes else None,
        ))
    return found


def _classify(interfaces: List[_Interface]) -> Tuple[str, Optional[_Interface]]:
    """(mode, raster printer interface) from a device's interfaces."""
    printers = [i for i in interfaces if i.cls == USB_CLASS_PRINTER]
    if printers:
        raster = [i for i in printers if i.protocol != PRINTER_PROTOCOL_IPP_USB] or printers
        best = min(raster, key=lambda i: (i.protocol != PRINTER_PROTOCOL_BIDIRECTIONAL,
                                          i.lp_node is None, i.number))
        return MODE_PRINTER, best
    if any(i.cls == USB_CLASS_MASS_STORAGE for i in interfaces):
        return MODE_MASS_STORAGE, None
    return MODE_OTHER, None


def scan_printers(vendor_id: str = BROTHER_VID, product_id: Optional[str] = None,
                  serial: Optional[str] = None, *, sysfs_root: str = SYSFS_USB_DEVICES,
                  dev_dir: str = DEV_USB_DIR) -> List[PrinterInfo]:
    """Every attached USB device that matches, in sysfs name order.

    ``vendor_id`` / ``product_id`` are 4 hex digits (any case); empty or None
    ``product_id`` / ``serial`` match anything. Without a pinned product id
    only products whose name contains ``QL-`` are accepted, as in
    ``qlp_detect_printer``. Never raises: an unreadable sysfs gives ``[]``.
    """
    want_vid = (vendor_id or BROTHER_VID).lower()
    want_pid = (product_id or "").lower()
    try:
        names = sorted(os.listdir(sysfs_root))
    except OSError:
        return []
    found = []
    for name in names:
        if ":" in name:                       # interfaces are listed here too
            continue
        device_dir = os.path.join(sysfs_root, name)
        vid = (_read_attr(device_dir, "idVendor") or "").lower()
        if vid != want_vid:
            continue
        pid = (_read_attr(device_dir, "idProduct") or "").lower()
        if want_pid and pid != want_pid:
            continue
        device_serial = _read_attr(device_dir, "serial")
        if serial and device_serial != serial:
            continue
        product = _read_attr(device_dir, "product")
        if not want_pid and "QL-" not in (product or ""):
            continue
        mode, iface = _classify(_interfaces(device_dir, dev_dir))
        found.append(PrinterInfo(
            sysfs_path=device_dir,
            vid=vid,
            pid=pid or None,
            serial=device_serial,
            product=product,
            manufacturer=_read_attr(device_dir, "manufacturer"),
            mode=mode,
            lp_node=iface.lp_node if iface else None,
            interface=iface.number if iface else None,
            alt_setting=iface.alt if iface else None,
            interface_path=iface.path if iface else None,
            busnum=_int_attr(device_dir, "busnum", 10),
            devnum=_int_attr(device_dir, "devnum", 10),
        ))
    return found


_MODE_RANK = {MODE_PRINTER: 0, MODE_MASS_STORAGE: 1, MODE_OTHER: 2}


def find_printer(vendor_id: str = BROTHER_VID, product_id: Optional[str] = None,
                 serial: Optional[str] = None, *, sysfs_root: str = SYSFS_USB_DEVICES,
                 dev_dir: str = DEV_USB_DIR) -> Optional[PrinterInfo]:
    """The device to use: the first match in printer mode, else the first in
    Editor Lite (mass storage) mode, else any match; None when nothing matches."""
    found = scan_printers(vendor_id, product_id, serial, sysfs_root=sysfs_root, dev_dir=dev_dir)
    if not found:
        return None
    return min(found, key=lambda info: _MODE_RANK.get(info.mode, len(_MODE_RANK)))


def find_editor_lite(vendor_id: str = BROTHER_VID, *, sysfs_root: str = SYSFS_USB_DEVICES,
                     dev_dir: str = DEV_USB_DIR) -> Optional[PrinterInfo]:
    """A QL printer in Editor Lite mode, ignoring any pinned product id or serial.

    Editor Lite changes both (``209c`` -> ``209f`` on the QL-810Wc), so a
    printer pinned in the config would otherwise just look absent.
    """
    for info in scan_printers(vendor_id, None, None, sysfs_root=sysfs_root, dev_dir=dev_dir):
        if info.mode == MODE_MASS_STORAGE:
            return info
    return None


def info_for_node(node: str, *, usbmisc_root: str = SYSFS_USBMISC) -> PrinterInfo:
    """Describe an explicitly configured ``/dev/usb/lpN`` node.

    Identity comes from sysfs (``/sys/class/usbmisc/lpN/device``) when it is
    there; otherwise only the node is known. The mode is always "printer"
    (the node exists only while usblp is bound to a printer interface).
    """
    link = os.path.join(usbmisc_root, os.path.basename(node), "device")
    if not os.path.isdir(link):
        return PrinterInfo(mode=MODE_PRINTER, lp_node=node)
    iface_dir = os.path.realpath(link)
    device_dir = os.path.dirname(iface_dir)
    return PrinterInfo(
        sysfs_path=device_dir,
        vid=(_read_attr(device_dir, "idVendor") or "").lower() or None,
        pid=(_read_attr(device_dir, "idProduct") or "").lower() or None,
        serial=_read_attr(device_dir, "serial"),
        product=_read_attr(device_dir, "product"),
        manufacturer=_read_attr(device_dir, "manufacturer"),
        mode=MODE_PRINTER,
        lp_node=node,
        interface=_int_attr(iface_dir, "bInterfaceNumber", 16),
        alt_setting=_int_attr(iface_dir, "bAlternateSetting", 10),
        interface_path=iface_dir,
        busnum=_int_attr(device_dir, "busnum", 10),
        devnum=_int_attr(device_dir, "devnum", 10),
    )


# ---------------------------------------------------------------------------
# IEEE-1284 device id
# ---------------------------------------------------------------------------

def decode_device_id(raw: BytesLike) -> str:
    """Text of a GET_DEVICE_ID reply: strips the 2-byte length prefix.

    The prefix counts itself and is big-endian by the standard; some devices
    send it little-endian, which is accepted when the big-endian reading is
    impossible. Text stops at the first NUL.
    """
    data = bytes(raw)
    if len(data) < 2:
        return ""
    body = data[2:]
    for order in ("big", "little"):
        length = int.from_bytes(data[:2], order)  # type: ignore[arg-type]
        if 2 < length <= len(data):
            body = data[2:length]
            break
    return body.split(b"\x00", 1)[0].decode("latin-1").strip()


_DEVICE_ID_KEYS = {"MANUFACTURER": "MFG", "MODEL": "MDL", "COMMAND SET": "CMD", "CLASS": "CLS"}


def parse_device_id(text: str) -> Dict[str, str]:
    """``MFG:Brother;MDL:QL-810W;...`` as a dict with upper-case keys; the long
    key spellings (``MANUFACTURER``, ``MODEL``, ...) map to the short ones."""
    fields: Dict[str, str] = {}
    for part in (text or "").split(";"):
        key, sep, value = part.partition(":")
        key = key.strip().upper()
        if sep and key:
            fields[_DEVICE_ID_KEYS.get(key, key)] = value.strip()
    return fields


def _lpioc_get_device_id(size: int) -> int:
    """``LPIOC_GET_DEVICE_ID(size)`` = ``_IOC(_IOC_READ, 'P', 1, size)`` for this CPU."""
    machine = platform.machine().lower()
    size_bits = 13 if machine.startswith(("ppc", "powerpc", "mips", "sparc", "alpha")) else 14
    ioc_read = 2
    return (ioc_read << (16 + size_bits)) | ((size & ((1 << size_bits) - 1)) << 16) | (ord("P") << 8) | 1


def read_ieee1284_id(info: Optional[PrinterInfo], transport: Optional["Transport"] = None,
                     timeout: float = 2.0) -> Optional[str]:
    """The printer's IEEE-1284 device id, e.g. ``MFG:Brother;...;MDL:QL-810W;...``.

    Tries, in order: usblp's sysfs attribute ``ieee1284_id`` (cached by the
    kernel at probe time, so it costs no USB traffic); then the open
    transport (``LPIOC_GET_DEVICE_ID`` on the usblp node, or the GET_DEVICE_ID
    class request through pyusb). None when no source answers. Never raises.
    """
    if info is not None and info.interface_path:
        text = _read_attr(info.interface_path, "ieee1284_id")
        if text:
            return text
    if transport is not None and not transport.closed:
        try:
            return transport.device_id(timeout)
        except UsbError as exc:
            log.debug("device id from %s failed: %s", transport.kind, exc)
    return None


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class Transport(abc.ABC):
    """A byte pipe to the printer's raster interface. Every call has a timeout.

    Implementations: :class:`UsblpTransport`, :class:`PyUsbTransport` (and a
    fake printer in the tests). Not thread-safe: one owner at a time.
    """

    #: Short name stored in ``state.json`` (``device.transport``).
    kind = "transport"
    #: Device node or bus path, for logs and ``state.json`` (``device.node``).
    node: Optional[str] = None
    #: Shortest ``write`` timeout worth asking for. A transport that cannot
    #: tell how much of a timed-out transfer went through (pyusb) needs
    #: generous timeouts, because its timeouts are fatal for the stream.
    min_write_timeout = 0.0

    @abc.abstractmethod
    def write(self, data: BytesLike, timeout: float) -> int:
        """Send ``data`` in order, waiting at most ``timeout`` seconds in total.

        Returns how many leading bytes the printer accepted: fewer than
        ``len(data)`` when it stopped accepting in time (the caller resends
        the rest later). A transport that cannot tell how much of a
        timed-out transfer was delivered raises :class:`TransportTimeout`
        instead: the stream position is then unknown. Raises
        :class:`DeviceGone` or :class:`UsbError` on failure.
        """

    @abc.abstractmethod
    def read(self, max_len: int, timeout: float) -> bytes:
        """Up to ``max_len`` bytes from the printer, waiting at most ``timeout``
        seconds for the first of them; ``b""`` when nothing arrived.

        Zero-length USB packets (sent by an idle or cooling printer) are
        absorbed. Raises :class:`DeviceGone` or :class:`UsbError`.
        """

    def flush(self, timeout: float) -> bool:
        """Wait until every byte accepted by :meth:`write` has been handed to
        the printer; True when done within ``timeout``."""
        return True

    def device_id(self, timeout: float = 2.0) -> Optional[str]:
        """The IEEE-1284 device id read from the device itself, or None."""
        return None

    def fileno(self) -> Optional[int]:
        """A pollable file descriptor, or None."""
        return None

    @property
    @abc.abstractmethod
    def closed(self) -> bool:
        """True once :meth:`close` has run."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release the device. Idempotent; never raises. Call :meth:`flush`
        first if the last bytes written matter."""

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _wait_fd(fd: int, for_write: bool, timeout: float) -> str:
    """Wait until ``fd`` is readable/writable: ``_READY``, ``_TIMEOUT`` or
    ``_HANGUP`` (usblp reports POLLHUP once the device is gone)."""
    timeout = max(0.0, timeout)
    if hasattr(select, "poll"):
        poller = select.poll()
        poller.register(fd, (select.POLLOUT if for_write else select.POLLIN)
                        | select.POLLERR | select.POLLHUP)
        events = poller.poll(math.ceil(timeout * 1000))
        for _fd, mask in events:
            if mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                return _HANGUP
            return _READY
        return _TIMEOUT
    readers, writers, _ = select.select([] if for_write else [fd], [fd] if for_write else [], [], timeout)
    return _READY if (readers or writers) else _TIMEOUT


class UsblpTransport(Transport):
    """The kernel usblp node (``/dev/usb/lpN``), opened non-blocking.

    usblp accepts up to 8 KiB per write and completes it in the background;
    :meth:`flush` waits for that last transfer. Its read side returns one
    USB IN transfer per call (a 32-byte status frame, or a zero-length packet).
    """

    kind = "usblp"

    def __init__(self, path: str) -> None:
        flags = os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOCTTY", 0)
        try:
            self._fd = os.open(path, flags)
        except OSError as exc:
            raise _from_os_error(exc, f"cannot open {path}") from exc
        self.node = path
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def fileno(self) -> Optional[int]:
        return None if self._closed else self._fd

    def _fd_or_raise(self) -> int:
        if self._closed:
            raise UsbError(f"{self.node}: transport is closed")
        return self._fd

    def write(self, data: BytesLike, timeout: float) -> int:
        fd = self._fd_or_raise()
        view = memoryview(data)
        total, done = len(view), 0
        deadline = time.monotonic() + max(0.0, timeout)
        while done < total:
            try:
                count = os.write(fd, view[done:])
            except BlockingIOError:
                count = 0
            except OSError as exc:
                raise _from_os_error(exc, f"write to {self.node}") from exc
            if count > 0:
                done += count
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if _wait_fd(fd, True, remaining) == _HANGUP:
                raise DeviceGone(f"{self.node}: the printer disconnected")
        return done

    def read(self, max_len: int, timeout: float) -> bytes:
        fd = self._fd_or_raise()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                data: Optional[bytes] = os.read(fd, max(1, max_len))
            except BlockingIOError:
                data = None
            except OSError as exc:
                raise _from_os_error(exc, f"read from {self.node}") from exc
            if data:
                return data
            if data == b"" and _wait_fd(fd, False, 0.0) == _HANGUP:
                raise DeviceGone(f"{self.node}: the printer disconnected")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b""
            if data == b"":                      # zero-length packet
                time.sleep(min(_ZLP_BACKOFF, remaining))
                continue
            if _wait_fd(fd, False, remaining) == _HANGUP:
                raise DeviceGone(f"{self.node}: the printer disconnected")

    def flush(self, timeout: float) -> bool:
        state = _wait_fd(self._fd_or_raise(), True, timeout)
        if state == _HANGUP:
            raise DeviceGone(f"{self.node}: the printer disconnected")
        return state == _READY

    def device_id(self, timeout: float = 2.0) -> Optional[str]:
        """Device id via ``LPIOC_GET_DEVICE_ID`` (Linux usblp only). The kernel
        bounds the USB request itself (5 s); ``timeout`` is not used here."""
        if fcntl is None or not sys.platform.startswith("linux"):
            return None
        buf = bytearray(DEVICE_ID_BUFFER)
        try:
            fcntl.ioctl(self._fd_or_raise(), _lpioc_get_device_id(len(buf)), buf, True)
        except OSError as exc:
            log.debug("LPIOC_GET_DEVICE_ID on %s failed: %s", self.node, exc)
            return None
        return decode_device_id(buf) or None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._fd)
        except OSError as exc:
            log.debug("closing %s: %s", self.node, exc)


def _ms(seconds: float) -> int:
    """A libusb timeout in milliseconds: at least 1 (0 would mean "forever")."""
    return max(1, int(math.ceil(seconds * 1000)))


def _pyusb_is_timeout(exc: BaseException) -> bool:
    return (type(exc).__name__ == "USBTimeoutError"
            or getattr(exc, "errno", None) == errno.ETIMEDOUT
            or getattr(exc, "backend_error_code", None) == -7)       # LIBUSB_ERROR_TIMEOUT


def _from_pyusb(exc: BaseException, what: str) -> UsbError:
    """Translate a pyusb USBError into the matching UsbError."""
    code = getattr(exc, "errno", None)
    backend = getattr(exc, "backend_error_code", None)
    if code == errno.ENODEV or backend == -4:                       # LIBUSB_ERROR_NO_DEVICE
        return DeviceGone(f"{what}: {exc}", code=code)
    if code == errno.EBUSY or backend == -6:                        # LIBUSB_ERROR_BUSY
        return DeviceBusy(f"{what}: {exc} (another program has claimed the printer)", code=code)
    if code in (errno.EACCES, errno.EPERM) or backend == -3:        # LIBUSB_ERROR_ACCESS
        return UsbError(f"{what}: permission denied (the service user needs group lp, "
                        f"see /etc/udev/rules.d/60-ql810wc-proxy.rules)", code=code)
    return UsbError(f"{what}: {exc}", code=code)


def _load_pyusb(module: Any = None) -> Tuple[Any, Any]:
    """(usb.core, usb.util), or UsbError when python3-pyusb is missing."""
    if module is not None:
        return module.core, module.util
    try:
        import usb.core  # type: ignore[import-not-found]
        import usb.util  # type: ignore[import-not-found]
    except ImportError as exc:
        raise UsbError("the printer has no /dev/usb/lp* node (usblp is not bound) and "
                       "python3-pyusb is not installed for the libusb fallback") from exc
    return usb.core, usb.util


class PyUsbTransport(Transport):
    """libusb access through python3-pyusb, for when usblp is not bound.

    Claims the raster interface (class 07, never the IPP-over-USB protocol
    04), detaching a kernel driver if one holds it, selects its alternate
    setting and finds the bulk endpoints by direction. :meth:`close` releases
    the interface and re-attaches the kernel driver.
    """

    kind = "pyusb"
    min_write_timeout = 5.0
    _WRITE_CHUNK = 16384

    def __init__(self, info: PrinterInfo, *, usb_module: Any = None) -> None:
        core, util = _load_pyusb(usb_module)
        self._core, self._util = core, util
        self._closed = True
        self._reattach = False
        self._rbuf = b""
        if info.busnum is not None and info.devnum is not None:
            self.node = f"/dev/bus/usb/{info.busnum:03d}/{info.devnum:03d}"
        else:
            self.node = f"usb:{info.usb_id}"
        criteria: Dict[str, Any] = {}
        if info.vid:
            criteria["idVendor"] = int(info.vid, 16)
        if info.pid:
            criteria["idProduct"] = int(info.pid, 16)

        def same_device(dev: Any) -> bool:
            return ((info.busnum is None or getattr(dev, "bus", None) == info.busnum)
                    and (info.devnum is None or getattr(dev, "address", None) == info.devnum))

        no_backend = getattr(core, "NoBackendError", ())
        try:
            dev = core.find(custom_match=same_device, **criteria)
        except core.USBError as exc:
            raise _from_pyusb(exc, "USB device lookup") from exc
        except no_backend as exc:   # type: ignore[misc]
            raise UsbError(f"pyusb found no libusb backend: {exc}") from exc
        if dev is None:
            raise DeviceGone(f"USB device {info.usb_id} not found")
        self._dev = dev
        try:
            cfg = dev.get_active_configuration()
        except core.USBError as exc:
            raise _from_pyusb(exc, "reading the USB configuration") from exc
        candidates = [i for i in cfg if i.bInterfaceClass == USB_CLASS_PRINTER
                      and i.bInterfaceProtocol != PRINTER_PROTOCOL_IPP_USB]
        if not candidates:
            raise UsbError(f"{info.usb_id} has no raster printer interface (class 07)")
        intf = min(candidates, key=lambda i: (i.bInterfaceProtocol != PRINTER_PROTOCOL_BIDIRECTIONAL,
                                              i.bAlternateSetting, i.bInterfaceNumber))
        self._ifnum = int(intf.bInterfaceNumber)
        self._alt = int(intf.bAlternateSetting)
        try:
            if dev.is_kernel_driver_active(self._ifnum):
                dev.detach_kernel_driver(self._ifnum)
                self._reattach = True
        except NotImplementedError:
            pass                                  # not Linux: no kernel drivers to detach
        except core.USBError as exc:
            raise _from_pyusb(exc, "detaching the kernel driver") from exc
        try:
            util.claim_interface(dev, self._ifnum)
        except core.USBError as exc:
            self._give_back()
            raise _from_pyusb(exc, "claiming the printer interface") from exc
        self._closed = False
        try:
            dev.set_interface_altsetting(interface=self._ifnum, alternate_setting=self._alt)
        except core.USBError as exc:              # some devices stall SET_INTERFACE; harmless
            log.debug("SET_INTERFACE %d/%d: %s", self._ifnum, self._alt, exc)

        def bulk(direction: int) -> Callable[[Any], bool]:
            return lambda ep: (util.endpoint_direction(ep.bEndpointAddress) == direction
                               and util.endpoint_type(ep.bmAttributes) == util.ENDPOINT_TYPE_BULK)

        self._ep_out = util.find_descriptor(intf, custom_match=bulk(util.ENDPOINT_OUT))
        self._ep_in = util.find_descriptor(intf, custom_match=bulk(util.ENDPOINT_IN))
        if self._ep_out is None or self._ep_in is None:
            self.close()
            raise UsbError(f"{info.usb_id}: the printer interface has no bulk IN/OUT endpoints")
        self._packet = max(1, int(getattr(self._ep_in, "wMaxPacketSize", 64) or 64))
        self._out_packet = max(1, int(getattr(self._ep_out, "wMaxPacketSize", 64) or 64))

    @property
    def closed(self) -> bool:
        return self._closed

    def _check_open(self) -> None:
        if self._closed:
            raise UsbError(f"{self.node}: transport is closed")

    def write(self, data: BytesLike, timeout: float) -> int:
        """See :meth:`Transport.write`. libusb does not report how much of a
        timed-out multi-packet transfer was delivered, so such a timeout
        raises :class:`TransportTimeout`; a timed-out single packet (atomic
        on the bus) just ends the write early."""
        self._check_open()
        view = memoryview(data)
        total, done = len(view), 0
        deadline = time.monotonic() + max(0.0, timeout)
        while done < total:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = bytes(view[done:done + self._WRITE_CHUNK])
            try:
                count = int(self._ep_out.write(chunk, timeout=_ms(remaining)))
            except self._core.USBError as exc:
                if not _pyusb_is_timeout(exc):
                    raise _from_pyusb(exc, "USB write") from exc
                if len(chunk) <= self._out_packet:
                    break
                raise TransportTimeout(f"USB write timed out after {done} confirmed bytes; how much "
                                       f"of the last {len(chunk)}-byte transfer arrived is unknown") from exc
            if count <= 0:
                break
            done += count
        return done

    def read(self, max_len: int, timeout: float) -> bytes:
        self._check_open()
        max_len = max(1, max_len)
        if self._rbuf:
            data, self._rbuf = self._rbuf[:max_len], self._rbuf[max_len:]
            return data
        size = -(-max_len // self._packet) * self._packet    # whole packets: no overflow
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            try:
                data = bytes(self._ep_in.read(size, timeout=_ms(max(remaining, 0.0))))
            except self._core.USBError as exc:
                if _pyusb_is_timeout(exc):
                    return b""
                raise _from_pyusb(exc, "USB read") from exc
            if data:
                self._rbuf = data[max_len:]
                return data[:max_len]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b""
            time.sleep(min(_ZLP_BACKOFF, remaining))

    def device_id(self, timeout: float = 2.0) -> Optional[str]:
        """Device id via the printer-class GET_DEVICE_ID request."""
        self._check_open()
        try:
            raw = self._dev.ctrl_transfer(0xA1, 0x00, 0, (self._ifnum << 8) | self._alt,
                                          DEVICE_ID_BUFFER - 1, timeout=_ms(timeout))
        except self._core.USBError as exc:
            log.debug("GET_DEVICE_ID failed: %s", exc)
            return None
        return decode_device_id(bytes(raw)) or None

    def _give_back(self) -> None:
        if self._reattach:
            self._reattach = False
            try:
                self._dev.attach_kernel_driver(self._ifnum)
            except Exception as exc:   # cleanup must not raise; pyusb raises several types
                log.debug("re-attaching the kernel driver: %s", exc)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._util.release_interface(self._dev, self._ifnum)
        except Exception as exc:       # cleanup must not raise; pyusb raises several types
            log.debug("releasing interface %d: %s", self._ifnum, exc)
        self._give_back()
        try:
            self._util.dispose_resources(self._dev)
        except Exception as exc:       # cleanup must not raise; pyusb raises several types
            log.debug("disposing USB resources: %s", exc)


def open_transport(info: PrinterInfo, *, allow_pyusb: bool = True) -> Transport:
    """Open the printer described by ``info``.

    Uses its usblp node when the kernel driver is bound (the normal case), else
    pyusb when ``allow_pyusb``. A busy usblp node is never taken over through
    libusb: that would pull the printer from under its current user.
    Raises :class:`DeviceBusy`, :class:`DeviceGone` or :class:`UsbError`.
    """
    if info.mode == MODE_MASS_STORAGE:
        raise UsbError(EDITOR_LITE_HINT)
    if info.mode != MODE_PRINTER:
        raise UsbError(f"{info.usb_id} has no printer interface")
    if info.lp_node:
        return UsblpTransport(info.lp_node)
    if not allow_pyusb:
        raise UsbError("the printer has no /dev/usb/lp* node (usblp is not bound)")
    return PyUsbTransport(info)


def write_all(transport: Transport, data: BytesLike, timeout: float) -> None:
    """Write all of ``data`` within ``timeout`` seconds or raise :class:`TransportTimeout`."""
    view = memoryview(data)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        count = transport.write(view, max(0.0, deadline - time.monotonic()))
        view = view[count:]
        if not len(view):
            return
        if time.monotonic() >= deadline:
            raise TransportTimeout(f"the printer accepted {len(data) - len(view)} of {len(data)} "
                                   f"bytes within {timeout:.1f} s")


# ---------------------------------------------------------------------------
# Status frames
# ---------------------------------------------------------------------------

class FrameReader:
    """Reassembles 32-byte status frames from USB IN reads.

    Frames may arrive split across reads; bytes that are not part of a frame
    are skipped and counted in :attr:`skipped`. Frames handed back with
    :meth:`unread` come out first on the next :meth:`read` / :meth:`drain`.
    """

    def __init__(self) -> None:
        self._pending = b""
        self._ready: List[Status] = []
        self.skipped = 0

    @property
    def pending(self) -> int:
        """Bytes held back as the possible start of a frame."""
        return len(self._pending)

    def feed(self, data: BytesLike) -> List[Status]:
        """Add bytes read from the printer; returns the frames completed by them."""
        if not data:
            return []
        buf = self._pending + bytes(data)
        frames, rest = split_status_frames(buf)
        self.skipped += len(buf) - len(rest) - STATUS_LEN * len(frames)
        self._pending = rest
        return frames

    def unread(self, frames: List[Status]) -> None:
        """Queue ``frames`` (oldest first) to be returned before anything new."""
        self._ready = list(frames) + self._ready

    def take_ready(self) -> List[Status]:
        """Frames queued by :meth:`unread`."""
        ready, self._ready = self._ready, []
        return ready

    def discard_partial(self) -> int:
        """Drop a half-received frame (stale bytes); returns how many bytes."""
        count, self._pending = len(self._pending), b""
        self.skipped += count
        return count

    def reset(self) -> None:
        """Forget everything (a new device or transport)."""
        self._pending = b""
        self._ready = []

    def read(self, transport: Transport, timeout: float, max_len: int = READ_SIZE) -> List[Status]:
        """Frames from one read of ``transport`` (waiting at most ``timeout``
        seconds); frames queued by :meth:`unread` are returned first, without
        reading."""
        ready = self.take_ready()
        if ready:
            return ready
        return self.feed(transport.read(max_len, timeout))

    def drain(self, transport: Transport, timeout: float = _DRAIN_TIMEOUT,
              max_reads: int = _MAX_DRAIN_READS) -> List[Status]:
        """Every frame already waiting: reads until one read brings nothing
        (each read waits at most ``timeout`` seconds)."""
        frames = self.take_ready()
        for _ in range(max_reads):
            data = transport.read(READ_SIZE, timeout)
            if not data:
                break
            frames.extend(self.feed(data))
        return frames


def request_status(transport: Transport, resync: bool = False, timeout: float = STATUS_TIMEOUT, *,
                   reader: Optional[FrameReader] = None,
                   on_frame: Optional[Callable[[Status], None]] = None,
                   prefix: Optional[BytesLike] = None) -> Status:
    """Ask the printer for its 32-byte status block and return the reply.

    1. Frames already waiting on the IN endpoint (stale replies, unsolicited
       "printing completed" / phase-change frames) are drained and handed to
       ``on_frame``; a half-received frame is dropped.
    2. ``ESC i S`` is sent, preceded by 400 x NUL + ``ESC @`` when ``resync``
       is true, or by ``prefix`` when one is given (for example
       :data:`~ql810wc.rasterproto.INVALIDATE` alone, which completes a
       partial command without the ``ESC @``). Resync only when the printer's
       input state is unknown (start-up, after a torn or aborted write):
       ``ESC @`` also cancels printing and any job the printer holds.
    3. The **first complete frame** that arrives after the request is the
       reply, whatever its status type: with the cover open the printer
       answers with an "error occurred" frame (type 0x02), not 0x00
       (verified on the tested unit). Frames that arrive in the same read
       after it are queued in ``reader`` for the caller's next read.
       Zero-length reads are tolerated.

    ``reader`` keeps partial frames between calls (pass the caller's reader).
    Raises :class:`StatusRequestNotAccepted` when the printer did not take
    the whole request within ``timeout`` seconds (its ``stream_unknown`` says
    whether part of it went out), :class:`StatusTimeout` when the request was
    written but no frame arrived in time (the printer's input is then still
    at a command boundary), :class:`TransportTimeout` (a transport that
    cannot tell how much of a write arrived), :class:`DeviceGone` or
    :class:`UsbError`. Never call this while a job is being forwarded.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    reader = reader if reader is not None else FrameReader()
    for frame in reader.drain(transport, timeout=min(_DRAIN_TIMEOUT, max(0.0, timeout))):
        if on_frame is not None:
            on_frame(frame)
    reader.discard_partial()
    head = (RESYNC if resync else b"") if prefix is None else bytes(prefix)
    request = head + STATUS_REQUEST
    view = memoryview(request)
    while len(view):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            written = len(request) - len(view)
            if written:
                message = (f"the printer accepted only {written} of the {len(request)} bytes of the "
                           f"status request within {timeout:.1f} s")
            else:
                message = f"the printer did not accept the status request within {timeout:.1f} s"
            raise StatusRequestNotAccepted(message, written=written, total=len(request))
        view = view[transport.write(view, remaining):]
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StatusTimeout(f"no reply to the status request within {timeout:.1f} s")
        frames = reader.read(transport, remaining)
        if frames:
            reader.unread(frames[1:])
            return frames[0]
