"""Tests for agent/ql810wc/usbio.py: sysfs discovery, transports, status requests.

Discovery runs against a fake sysfs tree; UsblpTransport against a
pseudo-terminal standing in for /dev/usb/lp0; PyUsbTransport against a fake
``usb`` (pyusb) module; request_status against the in-process fake printer.
"""
from __future__ import annotations

import errno
import os
import pathlib
import platform
import sys
import tempfile
import time
import types
import unittest
from typing import Any, List, Optional, Sequence, Tuple
from unittest import mock

from tests.fake_printer import (FAKE_SERIAL, IDLE_DK22251, RESYNC_REQUEST, TEST_DEVICE_ID, FakePrinter,
                                PrinterModel, PtyPrinter)

from ql810wc import usbio  # noqa: E402  (path set by fake_printer)
from ql810wc.rasterproto import (INVALIDATE, PHASE_PRINTING, PHASE_RECEIVING, STATUS_REQUEST,  # noqa: E402
                                 ST_ERROR, ST_PHASE_CHANGE, ST_PRINTING_COMPLETED, ST_REPLY, Status)

HAVE_PTY = os.name == "posix"
PRINTER_IFACES = ((0, 0, 0x07, 0x01, 0x02, "lp0"), (1, 0, 0xFF, 0x01, 0x00, None))
EDITOR_LITE_IFACES = ((0, 0, 0x08, 0x06, 0x50, None),)


class FakeSysfs:
    """A minimal /sys + /dev layout: device dirs with interface subdirs,
    symlinks in sys/bus/usb/devices, and sys/class/usbmisc/lpN/device."""

    def __init__(self, test: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.devices = root / "sys" / "bus" / "usb" / "devices"
        self.real = root / "sys" / "devices" / "platform" / "usb1"
        self.usbmisc = root / "sys" / "class" / "usbmisc"
        self.dev = root / "dev" / "usb"
        for path in (self.devices, self.real, self.usbmisc, self.dev):
            path.mkdir(parents=True)
        self._next_lp = 0

    @staticmethod
    def _write(path: pathlib.Path, text: str) -> None:
        path.write_text(text + "\n", encoding="utf-8")

    def add(self, name: str, *, vid: str = "04f9", pid: str = "209c", serial: Optional[str] = FAKE_SERIAL,
            product: str = "QL-810W", manufacturer: str = "Brother", busnum: int = 1, devnum: int = 4,
            interfaces: Sequence[Tuple[int, int, int, int, int, Optional[str]]] = PRINTER_IFACES,
            device_id: Optional[str] = TEST_DEVICE_ID) -> pathlib.Path:
        device = self.real / name
        device.mkdir()
        attrs = {"idVendor": vid, "idProduct": pid, "product": product, "manufacturer": manufacturer,
                 "busnum": str(busnum), "devnum": str(devnum)}
        if serial is not None:
            attrs["serial"] = serial
        for key, value in attrs.items():
            self._write(device / key, value)
        (self.devices / name).symlink_to(device)
        for number, alt, cls, subclass, protocol, lp in interfaces:
            iname = f"{name}:1.{number}"
            iface = device / iname
            iface.mkdir()
            self._write(iface / "bInterfaceNumber", f"{number:02x}")
            self._write(iface / "bAlternateSetting", f"{alt:2d}")
            self._write(iface / "bInterfaceClass", f"{cls:02x}")
            self._write(iface / "bInterfaceSubClass", f"{subclass:02x}")
            self._write(iface / "bInterfaceProtocol", f"{protocol:02x}")
            (self.devices / iname).symlink_to(iface)       # sysfs lists interfaces here too
            if lp:                                          # usblp bound: next free lpN, as the kernel does
                lp = f"lp{self._next_lp}"
                self._next_lp += 1
                (iface / "usbmisc" / lp).mkdir(parents=True)
                (self.usbmisc / lp).mkdir()
                (self.usbmisc / lp / "device").symlink_to(iface)
                if device_id is not None:
                    self._write(iface / "ieee1284_id", device_id)
        return device

    def find(self, *args: Any, **kwargs: Any) -> Optional[usbio.PrinterInfo]:
        return usbio.find_printer(*args, sysfs_root=str(self.devices), dev_dir=str(self.dev), **kwargs)

    def scan(self, *args: Any, **kwargs: Any) -> List[usbio.PrinterInfo]:
        return usbio.scan_printers(*args, sysfs_root=str(self.devices), dev_dir=str(self.dev), **kwargs)


class DiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sysfs = FakeSysfs(self)

    def test_printer_mode(self):
        self.sysfs.add("1-1")
        info = self.sysfs.find()
        self.assertIsNotNone(info)
        self.assertEqual(info.mode, usbio.MODE_PRINTER)
        self.assertEqual((info.vid, info.pid, info.serial), ("04f9", "209c", FAKE_SERIAL))
        self.assertEqual((info.product, info.manufacturer), ("QL-810W", "Brother"))
        self.assertEqual(info.lp_node, os.path.join(str(self.sysfs.dev), "lp0"))
        self.assertEqual((info.interface, info.alt_setting), (0, 0))
        self.assertEqual((info.busnum, info.devnum), (1, 4))
        self.assertTrue(info.interface_path.endswith("1-1:1.0"))
        self.assertEqual(usbio.read_ieee1284_id(info), TEST_DEVICE_ID)
        self.assertIn("04f9:209c", info.describe())

    def test_editor_lite_is_mass_storage(self):
        self.sysfs.add("1-1", pid="209f", serial="FAKE0SERIAL2", interfaces=EDITOR_LITE_IFACES)
        info = self.sysfs.find()
        self.assertEqual(info.mode, usbio.MODE_MASS_STORAGE)
        self.assertIsNone(info.lp_node)
        self.assertIsNone(info.interface)

    def test_printer_mode_preferred(self):
        self.sysfs.add("1-1", pid="209f", interfaces=EDITOR_LITE_IFACES, devnum=3)
        self.sysfs.add("1-2", devnum=5)
        self.assertEqual(len(self.sysfs.scan()), 2)
        info = self.sysfs.find()
        self.assertEqual((info.mode, info.devnum), (usbio.MODE_PRINTER, 5))

    def test_filters(self):
        self.sysfs.add("1-1", product="MFC-L2750DW", pid="0393", serial="OTHER")
        self.sysfs.add("1-2", serial="FAKE0SERIAL3", devnum=6)
        self.sysfs.add("2-1", vid="1d6b", product="QL-810W", busnum=2)           # not Brother
        self.assertEqual([i.serial for i in self.sysfs.scan()], ["FAKE0SERIAL3"], "QL products only")
        self.assertEqual(self.sysfs.find(product_id="0393").product, "MFC-L2750DW", "a pinned PID wins")
        self.assertEqual(self.sysfs.find("04F9", "209C").serial, "FAKE0SERIAL3", "ids in any case")
        self.assertIsNone(self.sysfs.find(serial="NOPE"))
        self.assertEqual(self.sysfs.find(serial="FAKE0SERIAL3").devnum, 6)

    def test_editor_lite_found_despite_pinned_identity(self):
        self.sysfs.add("1-1", pid="209f", serial="FAKE0SERIAL2", interfaces=EDITOR_LITE_IFACES)
        self.assertIsNone(self.sysfs.find(product_id="209c", serial=FAKE_SERIAL))
        info = usbio.find_editor_lite(sysfs_root=str(self.sysfs.devices), dev_dir=str(self.sysfs.dev))
        self.assertEqual(info.pid, "209f")

    def test_ipp_usb_alternate_still_counts_as_printer(self):
        # ipp-usb selected alternate setting 1 (7/1/4) and usblp is unbound.
        self.sysfs.add("1-1", interfaces=((0, 1, 0x07, 0x01, 0x04, None), (1, 1, 0x07, 0x01, 0x04, None)))
        info = self.sysfs.find()
        self.assertEqual(info.mode, usbio.MODE_PRINTER)
        self.assertIsNone(info.lp_node)
        self.assertEqual(info.interface, 0)

    def test_missing_sysfs(self):
        self.assertEqual(usbio.scan_printers(sysfs_root="/nonexistent/sysfs"), [])
        self.assertIsNone(usbio.find_printer(sysfs_root="/nonexistent/sysfs"))

    def test_info_for_node(self):
        self.sysfs.add("1-1")
        info = usbio.info_for_node("/dev/usb/lp0", usbmisc_root=str(self.sysfs.usbmisc))
        self.assertEqual((info.vid, info.pid, info.serial, info.lp_node), ("04f9", "209c", FAKE_SERIAL,
                                                                           "/dev/usb/lp0"))
        self.assertEqual(usbio.read_ieee1284_id(info), TEST_DEVICE_ID)
        bare = usbio.info_for_node("/dev/usb/lp7", usbmisc_root=str(self.sysfs.usbmisc))
        self.assertEqual((bare.mode, bare.lp_node, bare.vid), (usbio.MODE_PRINTER, "/dev/usb/lp7", None))
        self.assertEqual(bare.describe(), "USB printer (printer, /dev/usb/lp7)")


class DeviceIdTest(unittest.TestCase):
    def test_decode(self):
        text = TEST_DEVICE_ID.encode()
        big = (len(text) + 2).to_bytes(2, "big") + text
        self.assertEqual(usbio.decode_device_id(big + b"\x00" * 50), TEST_DEVICE_ID)
        little = (len(text) + 2).to_bytes(2, "little") + text
        self.assertEqual(usbio.decode_device_id(little), TEST_DEVICE_ID)
        self.assertEqual(usbio.decode_device_id(b"\xff\xff" + text + b"\x00junk"), TEST_DEVICE_ID)
        self.assertEqual(usbio.decode_device_id(b"\x00"), "")

    def test_parse(self):
        fields = usbio.parse_device_id("MANUFACTURER:Brother;COMMAND SET:PT-CBP;MDL:QL-810W;cls:PRINTER;")
        self.assertEqual(fields, {"MFG": "Brother", "CMD": "PT-CBP", "MDL": "QL-810W", "CLS": "PRINTER"})
        self.assertEqual(usbio.parse_device_id(TEST_DEVICE_ID)["MDL"], "QL-810W")

    @unittest.skipIf(platform.machine().lower().startswith(("ppc", "powerpc", "mips", "sparc", "alpha")),
                     "13-bit ioctl size field")
    def test_ioctl_number(self):
        self.assertEqual(usbio._lpioc_get_device_id(1024), 0x84005001)

    def test_sources_in_order(self):
        printer = FakePrinter().attach()
        printer.device_id_text = "MFG:Other;MDL:X;"
        self.assertEqual(usbio.read_ieee1284_id(usbio.PrinterInfo(), printer), "MFG:Other;MDL:X;")
        printer.device_id_text = None
        self.assertIsNone(usbio.read_ieee1284_id(usbio.PrinterInfo(), printer))
        printer.close()
        self.assertIsNone(usbio.read_ieee1284_id(None, printer), "a closed transport is not asked")


class ErrorMappingTest(unittest.TestCase):
    def test_os_errors(self):
        def mapped(code: int) -> usbio.UsbError:
            return usbio._from_os_error(OSError(code, os.strerror(code)), "write to /dev/usb/lp0")

        self.assertIsInstance(mapped(errno.ENODEV), usbio.DeviceGone)
        self.assertIsInstance(mapped(errno.ENOENT), usbio.DeviceGone)
        self.assertIsInstance(mapped(errno.EBUSY), usbio.DeviceBusy)
        self.assertIn("group lp", str(mapped(errno.EACCES)))
        err = mapped(errno.EIO)
        self.assertIs(type(err), usbio.UsbError)
        self.assertEqual(err.errno, errno.EIO)


class FrameReaderTest(unittest.TestCase):
    def test_split_frames_and_garbage(self):
        reader = usbio.FrameReader()
        self.assertEqual(reader.feed(b"\x00\x01" + IDLE_DK22251[:10]), [])
        self.assertEqual(reader.pending, 10)
        frames = reader.feed(IDLE_DK22251[10:] + IDLE_DK22251 + b"\x80")
        self.assertEqual([f.raw for f in frames], [IDLE_DK22251, IDLE_DK22251])
        self.assertEqual(reader.skipped, 2)
        self.assertEqual(reader.discard_partial(), 1)
        self.assertEqual(reader.pending, 0)

    def test_unread_comes_first(self):
        reader = usbio.FrameReader()
        first, second = Status(IDLE_DK22251), Status(IDLE_DK22251).with_overrides(status_type=1)
        reader.unread([second])
        reader.unread([first])
        printer = FakePrinter().attach()
        self.assertEqual(reader.read(printer, 0.0), [first, second])
        self.assertEqual(reader.take_ready(), [])
        self.assertEqual(reader.read(printer, 0.0), [], "nothing more to read")


def _frame(status_type: int, phase: int = PHASE_RECEIVING) -> bytes:
    raw = bytearray(IDLE_DK22251)
    raw[18], raw[19] = status_type, phase
    return bytes(raw)


class RequestStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.model = PrinterModel()
        self.printer = FakePrinter(self.model).attach()
        self.addCleanup(self.printer.close)

    def test_plain_request(self):
        frame = usbio.request_status(self.printer)
        self.assertEqual(frame.raw, IDLE_DK22251)
        self.assertEqual(bytes(self.model.received), STATUS_REQUEST)

    def test_resync(self):
        usbio.request_status(self.printer, resync=True)
        self.assertEqual(bytes(self.model.received), RESYNC_REQUEST)

    def test_stale_frames_are_drained_first(self):
        stale = [_frame(ST_PRINTING_COMPLETED, PHASE_PRINTING), _frame(ST_PHASE_CHANGE)]
        for raw in stale:
            self.model.inject(raw)
        seen: List[bytes] = []
        frame = usbio.request_status(self.printer, on_frame=lambda f: seen.append(f.raw))
        self.assertEqual(seen, stale)
        self.assertEqual(frame.status_type, ST_REPLY)

    def test_cover_open_reply_is_an_error_frame(self):
        self.model.set_cover_open(True)
        frame = usbio.request_status(self.printer)
        self.assertEqual(frame.status_type, ST_ERROR)
        self.assertEqual(frame.err2, 0x10)

    def test_first_frame_after_the_request_is_the_reply(self):
        # docs/architecture.md section 4: the first complete frame after ESC i S
        # is its reply, whatever its status type. The printer's own answer that
        # follows is read later like any other frame.
        reader = usbio.FrameReader()
        self.model.reply_delay = 0.15
        self.model.inject(_frame(ST_PHASE_CHANGE, PHASE_PRINTING), delay=0.1)
        seen: List[int] = []
        frame = usbio.request_status(self.printer, reader=reader, on_frame=lambda f: seen.append(f.status_type))
        self.assertEqual(seen, [], "nothing was waiting before the request")
        self.assertEqual((frame.status_type, frame.phase_type), (ST_PHASE_CHANGE, PHASE_PRINTING))
        later = reader.read(self.printer, 1.0)
        self.assertEqual([f.status_type for f in later], [ST_REPLY])

    def test_frames_behind_the_reply_are_kept(self):
        reader = usbio.FrameReader()
        trailing = _frame(ST_PRINTING_COMPLETED, PHASE_PRINTING)
        self.model.after_reply = trailing
        frame = usbio.request_status(self.printer, reader=reader)
        self.assertEqual(frame.status_type, ST_REPLY)
        self.assertEqual([f.raw for f in reader.take_ready()], [trailing])

    def test_empty_reads_are_tolerated(self):
        self.printer.empty_reads = 20
        self.assertEqual(usbio.request_status(self.printer).raw, IDLE_DK22251)

    def test_no_reply(self):
        self.model.mute = True
        started = time.monotonic()
        with self.assertRaises(usbio.StatusTimeout) as caught:
            usbio.request_status(self.printer, timeout=0.3)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn("no reply", str(caught.exception))
        self.assertNotIsInstance(caught.exception, usbio.StatusRequestNotAccepted)
        self.assertFalse(caught.exception.stream_unknown, "the request was written: a command boundary")

    def test_prefix_replaces_the_resync(self):
        usbio.request_status(self.printer, resync=True, prefix=INVALIDATE)
        self.assertEqual(bytes(self.model.received), INVALIDATE + STATUS_REQUEST)

    def test_request_accepted_in_part(self):
        self.printer.stall_after = self.printer.total_written + 2
        with self.assertRaises(usbio.StatusRequestNotAccepted) as caught:
            usbio.request_status(self.printer, timeout=0.2)
        exc = caught.exception
        self.assertEqual((exc.written, exc.total), (2, 3))
        self.assertTrue(exc.stream_unknown, "the printer is left inside ESC i S")
        self.assertIn("accepted only 2 of the 3 bytes", str(exc))

    def test_ambiguous_write_timeout_leaves_the_stream_unknown(self):
        self.assertTrue(usbio.TransportTimeout("how much arrived is unknown").stream_unknown)

    def test_any_frame_type_counts_as_the_reply(self):
        # A printing-completed frame that arrives after the request answers it,
        # even when the printer itself never replies.
        self.model.mute = True
        self.model.inject(_frame(ST_PRINTING_COMPLETED, PHASE_PRINTING), delay=0.12)
        frame = usbio.request_status(self.printer, timeout=1.0)
        self.assertEqual(frame.status_type, ST_PRINTING_COMPLETED)

    def test_request_not_accepted(self):
        self.printer.set_stalled(True)
        with self.assertRaises(usbio.StatusTimeout) as caught:
            usbio.request_status(self.printer, timeout=0.2)
        self.assertIn("did not accept", str(caught.exception))
        exc = caught.exception
        self.assertIsInstance(exc, usbio.StatusRequestNotAccepted)
        self.assertEqual((exc.written, exc.total), (0, 3))
        self.assertFalse(exc.stream_unknown, "nothing went out: the stream is where it was")
        self.printer.set_stalled(False)
        self.printer.stall_after = self.printer.total_written + 100
        with self.assertRaises(usbio.StatusRequestNotAccepted) as caught:
            usbio.request_status(self.printer, resync=True, timeout=0.2)
        self.assertEqual((caught.exception.written, caught.exception.total), (100, len(RESYNC_REQUEST)))

    def test_device_gone(self):
        self.printer.unplug()
        with self.assertRaises(usbio.DeviceGone):
            usbio.request_status(self.printer, timeout=0.2)

    def test_write_all(self):
        self.printer.write_limit = 7
        usbio.write_all(self.printer, b"\x00" * 100, 1.0)
        self.assertEqual(len(self.model.received), 100)
        self.printer.set_stalled(True)
        with self.assertRaises(usbio.TransportTimeout):
            usbio.write_all(self.printer, b"\x00" * 10, 0.1)


@unittest.skipUnless(HAVE_PTY, "needs a POSIX pseudo-terminal")
class UsblpTransportTest(unittest.TestCase):
    """UsblpTransport on a pty: the same non-blocking + poll code paths as usblp."""

    def setUp(self) -> None:
        self.pty = PtyPrinter()
        self.addCleanup(self.pty.close)
        self.transport = usbio.UsblpTransport(self.pty.path)
        self.addCleanup(self.transport.close)

    def test_status_round_trip(self):
        frame = usbio.request_status(self.transport, resync=True, timeout=3.0)
        self.assertEqual(frame.raw, IDLE_DK22251)
        self.assertEqual(bytes(self.pty.model.received), RESYNC_REQUEST)
        self.assertTrue(self.transport.flush(1.0))
        self.assertEqual(self.transport.kind, "usblp")
        self.assertEqual(self.transport.node, self.pty.path)
        self.assertIsNotNone(self.transport.fileno())

    def test_write_back_pressure_and_timeout(self):
        self.pty.pause()
        data = b"\x00" * 65536
        started = time.monotonic()
        written = self.transport.write(data, 0.3)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertGreater(written, 0)
        self.assertLess(written, len(data), "a printer that stops reading stops the writer")
        self.pty.resume()
        rest = data[written:]
        total = written
        deadline = time.monotonic() + 10
        while rest and time.monotonic() < deadline:
            count = self.transport.write(rest, 0.5)
            rest = rest[count:]
            total += count
        self.assertEqual(total, len(data))

    def test_read_timeout(self):
        started = time.monotonic()
        self.assertEqual(self.transport.read(32, 0.2), b"")
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_device_id_is_not_available_on_a_tty(self):
        self.assertIsNone(self.transport.device_id())

    def test_hangup(self):
        self.pty.unplug()
        with self.assertRaises(usbio.UsbError):
            for _ in range(3):
                self.transport.read(32, 0.2)
                self.transport.write(STATUS_REQUEST, 0.2)

    def test_closed(self):
        self.transport.close()
        self.transport.close()
        self.assertTrue(self.transport.closed)
        self.assertIsNone(self.transport.fileno())
        with self.assertRaises(usbio.UsbError):
            self.transport.write(b"x", 0.1)
        with self.assertRaises(usbio.UsbError):
            self.transport.read(1, 0.1)

    def test_open_errors(self):
        with self.assertRaises(usbio.DeviceGone):
            usbio.UsblpTransport("/nonexistent/usb/lp9")

    def test_open_transport_prefers_the_node(self):
        info = usbio.PrinterInfo(mode=usbio.MODE_PRINTER, lp_node=self.pty.path)
        with usbio.open_transport(info) as transport:
            self.assertIsInstance(transport, usbio.UsblpTransport)


class OpenTransportTest(unittest.TestCase):
    def test_refuses_other_modes(self):
        with self.assertRaises(usbio.UsbError) as caught:
            usbio.open_transport(usbio.PrinterInfo(mode=usbio.MODE_MASS_STORAGE))
        self.assertIn("Editor Lite", str(caught.exception))
        with self.assertRaises(usbio.UsbError):
            usbio.open_transport(usbio.PrinterInfo(mode=usbio.MODE_OTHER))

    def test_no_node_without_pyusb(self):
        info = usbio.PrinterInfo(vid="04f9", pid="209c", mode=usbio.MODE_PRINTER)
        with self.assertRaises(usbio.UsbError):
            usbio.open_transport(info, allow_pyusb=False)
        with mock.patch.dict(sys.modules, {"usb": None, "usb.core": None, "usb.util": None}):
            with self.assertRaises(usbio.UsbError) as caught:
                usbio.open_transport(info)
        self.assertIn("python3-pyusb", str(caught.exception))


# ---------------------------------------------------------------------------
# A fake pyusb, enough for PyUsbTransport
# ---------------------------------------------------------------------------

class USBError(IOError):
    def __init__(self, text: str, error_code: Optional[int] = None, code: Optional[int] = None) -> None:
        super().__init__(code, text)
        self.backend_error_code = error_code


class USBTimeoutError(USBError):
    pass


class NoBackendError(ValueError):
    pass


class FakeEndpoint:
    def __init__(self, device: "FakeUsbDevice", address: int) -> None:
        self.device = device
        self.bEndpointAddress = address
        self.bmAttributes = 0x02                   # bulk
        self.wMaxPacketSize = 64

    def write(self, data: bytes, timeout: int) -> int:
        return self.device.on_write(bytes(data), timeout)

    def read(self, size: int, timeout: int) -> bytes:
        return self.device.on_read(size, timeout)


class FakeInterface(list):
    def __init__(self, number: int, alt: int, cls: int, protocol: int, endpoints: Sequence[FakeEndpoint]) -> None:
        super().__init__(endpoints)
        self.bInterfaceNumber, self.bAlternateSetting = number, alt
        self.bInterfaceClass, self.bInterfaceProtocol = cls, protocol


class FakeUsbDevice:
    """The tested QL-810Wc's layout: if0 alt0 7/1/2 (0x02 OUT, 0x81 IN), if0
    alt1 IPP-USB, if1 vendor; answers through a PrinterModel."""

    def __init__(self, model: PrinterModel) -> None:
        self.model = model
        self.bus, self.address = 1, 4
        self.idVendor, self.idProduct = 0x04F9, 0x209C
        self.kernel_driver = {0: True}
        self.calls: List[Tuple[Any, ...]] = []
        self.write_error: Optional[BaseException] = None
        self.claim_error: Optional[BaseException] = None
        self._buffer = b""
        self.config = [
            FakeInterface(0, 0, 0x07, 0x02, [FakeEndpoint(self, 0x02), FakeEndpoint(self, 0x81)]),
            FakeInterface(0, 1, 0x07, 0x04, [FakeEndpoint(self, 0x01), FakeEndpoint(self, 0x82)]),
            FakeInterface(1, 0, 0xFF, 0x00, [FakeEndpoint(self, 0x83)]),
        ]

    def get_active_configuration(self) -> List[FakeInterface]:
        return self.config

    def is_kernel_driver_active(self, number: int) -> bool:
        return self.kernel_driver.get(number, False)

    def detach_kernel_driver(self, number: int) -> None:
        self.calls.append(("detach", number))
        self.kernel_driver[number] = False

    def attach_kernel_driver(self, number: int) -> None:
        self.calls.append(("attach", number))
        self.kernel_driver[number] = True

    def set_interface_altsetting(self, interface: int, alternate_setting: int) -> None:
        self.calls.append(("altsetting", interface, alternate_setting))

    def ctrl_transfer(self, request_type: int, request: int, value: int, index: int, length: int,
                      timeout: int) -> bytes:
        self.calls.append(("ctrl", request_type, request, value, index, length))
        text = TEST_DEVICE_ID.encode()
        return ((len(text) + 2).to_bytes(2, "big") + text)[:length]

    def on_write(self, data: bytes, timeout: int) -> int:
        if self.write_error is not None:
            raise self.write_error
        self.model.feed(data)
        return len(data)

    def on_read(self, size: int, timeout: int) -> bytes:
        deadline = time.monotonic() + timeout / 1000
        while True:
            self._buffer += self.model.pending()
            if self._buffer:
                out, self._buffer = self._buffer[:size], self._buffer[size:]
                return out
            if time.monotonic() >= deadline:
                raise USBTimeoutError("Operation timed out", -7, errno.ETIMEDOUT)
            time.sleep(0.002)


def fake_pyusb(device: Optional[FakeUsbDevice]) -> types.SimpleNamespace:
    """A stand-in for the ``usb`` package with one attached device."""
    calls: List[Tuple[Any, ...]] = []

    def find(custom_match: Any = None, **criteria: Any) -> Optional[FakeUsbDevice]:
        if device is None:
            return None
        if any(getattr(device, key) != value for key, value in criteria.items()):
            return None
        return device if custom_match is None or custom_match(device) else None

    def claim_interface(dev: FakeUsbDevice, number: int) -> None:
        calls.append(("claim", number))
        if dev.claim_error is not None:
            raise dev.claim_error

    def find_descriptor(desc: Sequence[Any], custom_match: Any) -> Any:
        return next((item for item in desc if custom_match(item)), None)

    core = types.SimpleNamespace(find=find, USBError=USBError, USBTimeoutError=USBTimeoutError,
                                 NoBackendError=NoBackendError)
    util = types.SimpleNamespace(
        claim_interface=claim_interface,
        release_interface=lambda dev, number: calls.append(("release", number)),
        dispose_resources=lambda dev: calls.append(("dispose",)),
        find_descriptor=find_descriptor,
        endpoint_direction=lambda address: address & 0x80,
        endpoint_type=lambda attributes: attributes & 0x03,
        ENDPOINT_IN=0x80, ENDPOINT_OUT=0x00, ENDPOINT_TYPE_BULK=0x02, calls=calls)
    return types.SimpleNamespace(core=core, util=util)


INFO = usbio.PrinterInfo(vid="04f9", pid="209c", mode=usbio.MODE_PRINTER, busnum=1, devnum=4)


class PyUsbTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.model = PrinterModel()
        self.device = FakeUsbDevice(self.model)
        self.usb = fake_pyusb(self.device)

    def open(self) -> usbio.PyUsbTransport:
        transport = usbio.PyUsbTransport(INFO, usb_module=self.usb)
        self.addCleanup(transport.close)
        return transport

    def test_claims_the_raster_interface(self):
        transport = self.open()
        self.assertIn(("detach", 0), self.device.calls)
        self.assertIn(("altsetting", 0, 0), self.device.calls)
        self.assertEqual(self.usb.util.calls, [("claim", 0)])
        self.assertEqual(transport._ep_out.bEndpointAddress, 0x02)
        self.assertEqual(transport._ep_in.bEndpointAddress, 0x81)
        self.assertEqual(transport.node, "/dev/bus/usb/001/004")
        self.assertEqual(transport.kind, "pyusb")

    def test_status_and_device_id(self):
        transport = self.open()
        frame = usbio.request_status(transport, resync=True)
        self.assertEqual(frame.raw, IDLE_DK22251)
        self.assertEqual(bytes(self.model.received), RESYNC_REQUEST)
        self.assertEqual(transport.device_id(), TEST_DEVICE_ID)
        self.assertIn(("ctrl", 0xA1, 0x00, 0, 0x0000, usbio.DEVICE_ID_BUFFER - 1), self.device.calls)

    def test_read_timeout_and_buffering(self):
        transport = self.open()
        self.assertEqual(transport.read(32, 0.05), b"")
        self.model.inject(IDLE_DK22251 + IDLE_DK22251)
        self.assertEqual(transport.read(32, 1.0), IDLE_DK22251)
        self.assertEqual(transport.read(32, 0.0), IDLE_DK22251, "the rest of the packet is kept")

    def test_write_timeout_and_errors(self):
        transport = self.open()
        self.device.write_error = USBTimeoutError("Operation timed out", -7, errno.ETIMEDOUT)
        self.assertEqual(transport.write(b"\x00" * 10, 0.1), 0, "one packet: nothing was sent")
        with self.assertRaises(usbio.TransportTimeout) as caught:
            transport.write(b"\x00" * 1000, 0.1)       # several packets: how many arrived is unknown
        self.assertIn("unknown", str(caught.exception))
        self.device.write_error = USBError("No such device", -4, errno.ENODEV)
        with self.assertRaises(usbio.DeviceGone):
            transport.write(b"\x00", 0.1)
        self.assertGreaterEqual(transport.min_write_timeout, 1.0, "callers allow pyusb long writes")

    def test_close_gives_the_interface_back(self):
        transport = self.open()
        transport.close()
        transport.close()
        self.assertEqual(self.usb.util.calls[-2:], [("release", 0), ("dispose",)])
        self.assertEqual(self.device.calls[-1], ("attach", 0))
        self.assertTrue(transport.closed)
        with self.assertRaises(usbio.UsbError):
            transport.read(1, 0.0)

    def test_busy_interface(self):
        self.device.claim_error = USBError("Resource busy", -6, errno.EBUSY)
        with self.assertRaises(usbio.DeviceBusy):
            usbio.PyUsbTransport(INFO, usb_module=self.usb)
        self.assertEqual(self.device.calls[-1], ("attach", 0), "the kernel driver is given back")

    def test_device_not_found(self):
        with self.assertRaises(usbio.DeviceGone):
            usbio.PyUsbTransport(INFO, usb_module=fake_pyusb(None))

    def test_no_printer_interface(self):
        self.device.config = [self.device.config[2]]
        with self.assertRaises(usbio.UsbError):
            usbio.PyUsbTransport(INFO, usb_module=self.usb)


if __name__ == "__main__":
    unittest.main()
