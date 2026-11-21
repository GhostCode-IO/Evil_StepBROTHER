"""Tests for the printer persona (agent/ql810wc/persona.py) and the agentx entry point.

* EvaluateTest: the status-block policy over state.json fixtures (idle
  DK-22251, 29x90 die-cut, cover open, no media, jobs, stale and absent
  printers, Editor Lite, missing state).
* ConditionTest: Host Resources / Printer-MIB values against the real QL-810W
  state matrix (BrotherRaster fixtures, fw 1.22).
* PersonaValuesTest: every provider through Persona, with state.json on disk.
* IdentityTest, Ieee1284Test, NetInfoTest: 1284 ID, serial, MAC, node name.
* StateSourceTest: state.json is checked at most every 0.5 s, re-read only when changed.
* ReloadContinuityTest: a persona built on reload carries on from the old one
  (sysUpTime, remembered USB identity, job progress).
* PersonaTreeTest: GET/GETNEXT order over the whole persona, contexts, absent
  values, registration order (status block first).
* CommandLineTest / SystemdNotifierTest: the entry point, its systemd unit and
  the snmpd drop-in agree; sd_notify.
* AgentXReloadTest / SignalFlagsTest: the agent's reload (SIGHUP) in this
  process against a fake master: same session, failures keep the old persona,
  Register before Unregister, RELOADING=1/READY=1, clean stop.
* AgentXEndToEndTest: the real entry script against a fake AgentX master
  (registration in context "printer", READY=1, values, SET refused, SIGHUP
  reload in the same session, SIGTERM).
* RealSnmpdAgentXTest: net-snmp's snmpd as the master and P-touch's exact
  SNMPv1 reads, also across reloads (Linux net-snmp with VACM; skipped elsewhere).

The helpers at the top are shared with tests/test_passpersist.py.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
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

from ql810wc import agentx, config, media, persona, statecache  # noqa: E402
from ql810wc.rasterproto import Status  # noqa: E402
from tests._util import load_module  # noqa: E402

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:  # pragma: no cover - depends on the interpreter
    yaml = None
    HAVE_YAML = False

AGENT_SCRIPT = AGENT_DIR / "brother_snmp_agent.py"
SHIPPED_MAP = AGENT_DIR / "oid-map.yaml"
UNIT_FILE = AGENT_DIR / "systemd" / "ql810wc-snmp-agent.service"
SNMP_SCRIPT = REPO / "scripts" / "30-snmp-agent.sh"

VT = agentx.VarType
NOW = 1_763_650_000.0

# Status blocks. The first three are hardware-verified on the tested QL-810Wc
# (dev/notes/usb-ground-truth.md); the others follow the documented layout.
DK22251 = "80 20 42 34 39 30 04 00 00 00 3e 0a 00 00 23 00 00 00 00 00 00 00 00 00 00 81 00 00 00 00 00 00"
COVER_OPEN = "80 20 42 34 39 30 04 00 00 10 3e 0a 00 00 23 00 00 00 02 00 00 00 00 00 00 81 00 00 00 00 00 00"
DIE_CUT_29X90 = "80 20 42 34 39 30 04 00 00 00 1d 0b 00 00 01 00 00 5a 00 00 00 00 00 00 00 01 00 00 00 00 00 00"
NO_MEDIA = "80 20 42 34 39 30 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
NO_MEDIA_AFTER_JOB = "80 20 42 34 39 30 04 00 01 00 00 00 00 00 00 00 00 00 02 00 00 00 00 00 00 00 00 00 00 00 00 00"
MISMATCH_LATCH = "80 20 42 34 39 30 04 00 00 00 3e 0a 00 00 23 00 00 00 02 00 00 00 00 00 00 81 00 00 00 00 00 24"
PRINTING_COMPLETED = "80 20 42 34 39 30 04 00 00 00 3e 0a 00 00 23 00 00 00 01 00 00 00 00 00 00 81 00 00 00 00 00 00"
PHASE_PRINTING = "80 20 42 34 39 30 04 00 00 00 3e 0a 00 00 23 00 00 00 06 01 00 00 00 00 00 81 00 00 00 00 00 00"

IEEE1284_USB = ("MFG:Brother;CMD:PT-CBP,URF;MDL:QL-810W;CLS:PRINTER;CID:Brother LabelPrinter TypeE1;"
                "URF:V1.5,W8,CP1,IS1,MT1-5,OB10,PQ4-5,RS300,FN3-11-60-63;")
IEEE1284_DEFAULT = "MFG:Brother;CMD:PT-CBP;MDL:QL-810W;CLS:PRINTER;"
SERIAL = "Z0TEST000001"            # placeholder, not a real serial
MAC = bytes.fromhex("00005E005301")  # RFC 7042 documentation MAC 00:00:5E:00:53:01
MAC_TEXT = "00:00:5E:00:53:01"

STATUS_OID = (1, 3, 6, 1, 4, 1, 2435, 3, 3, 9, 1, 6, 1, 0)
IEEE1284_OID = (1, 3, 6, 1, 4, 1, 2435, 2, 3, 9, 1, 1, 7, 0)
HR_PRINTER_STATUS_OID = (1, 3, 6, 1, 2, 1, 25, 3, 5, 1, 1, 1)
#: Brother Windows driver installer: one SNMPv1 broadcast GET of these 5 varbinds.
WIN_INSTALLER_BUNDLE = ("1.3.6.1.4.1.2435.2.3.9.1.1.7.0", "1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.2.2.1.6.1",
                        "1.3.6.1.4.1.2435.2.4.3.1240.1.3.0", "1.3.6.1.2.1.1.1.0")
#: Brother mobile SDK / iPrint&Label discovery: one GET of these 6 varbinds.
MOBILE_BUNDLE = ("1.3.6.1.2.1.1.6.0", "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.0", "1.3.6.1.2.1.1.5.0",
                 "1.3.6.1.2.1.2.2.1.6.1", "1.3.6.1.2.1.25.3.2.1.3.1", "1.3.6.1.4.1.2435.2.3.9.1.1.7.0")


def raw(hex_text: str) -> bytes:
    """Status block bytes from spaced hex."""
    return bytes.fromhex(hex_text.replace(" ", ""))


def make_state(frame: Optional[str] = DK22251, *, now: float = NOW, device: Any = "ready",
               updated_age: float = 1.0, polled_age: Optional[float] = 1.0, started_age: float = 3600.0,
               job: Optional[Dict[str, Any]] = None, ieee1284: Optional[str] = IEEE1284_USB,
               serial: Optional[str] = SERIAL) -> Dict[str, Any]:
    """A schema-1 state.json document; ages are seconds before ``now``."""
    doc = statecache.new_state(pid=4321, raw_port=9100, started=now - started_age)
    doc["updated"] = now - updated_age
    doc["device"].update(state=device, vid="04f9", pid="209c", product="QL-810W", manufacturer="Brother",
                         serial=serial, node="/dev/usb/lp0", transport="usblp", ieee1284_id=ieee1284)
    if frame is not None:
        doc["status"] = {"raw_hex": frame.replace(" ", ""),
                         "polled": None if polled_age is None else now - polled_age, "parsed": None}
    if job:
        doc["job"].update(job)
    return doc


def write_json(path: os.PathLike, doc: Any) -> None:
    """Replace ``path`` atomically (a new inode, as ql810wc-proxyd does)."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def _entry(oid: str, name: str, type_: str, source: Dict[str, Any]) -> Dict[str, Any]:
    return {"oid": oid, "name": name, "type": type_, "source": source, "context": "printer",
            "verified": False, "notes": "test entry"}


#: A self-contained map that uses every provider (persona logic tests do not
#: depend on the shipped map's content). brInfoSerialNumber has no default, so
#: it is absent while no serial is known.
TEST_MAP_DOC: Dict[str, Any] = {"schema_version": 1, "model": "QL-810W", "validated_firmware": [], "entries": [
    _entry("1.3.6.1.2.1.1.1.0", "sysDescr", "string", {"const": "Brother NC-36002w, Firmware Ver.1.00"}),
    _entry("1.3.6.1.2.1.1.2.0", "sysObjectID", "oid", {"const": "1.3.6.1.4.1.2435.2.3.9.1"}),
    _entry("1.3.6.1.2.1.1.3.0", "sysUpTime", "timeticks", {"provider": "sysuptime"}),
    _entry("1.3.6.1.2.1.1.4.0", "sysContact", "string", {"config": "PERSONA_CONTACT"}),
    _entry("1.3.6.1.2.1.1.5.0", "sysName", "string", {"provider": "sysname"}),
    _entry("1.3.6.1.2.1.1.6.0", "sysLocation", "string", {"config": "PERSONA_LOCATION"}),
    _entry("1.3.6.1.2.1.2.2.1.6.1", "ifPhysAddress.1", "octets", {"provider": "mac", "default": ""}),
    _entry("1.3.6.1.2.1.25.3.2.1.3.1", "hrDeviceDescr.1", "string", {"template": "Brother ${MODEL}"}),
    _entry("1.3.6.1.2.1.25.3.2.1.5.1", "hrDeviceStatus.1", "integer", {"provider": "hr_device_status"}),
    _entry("1.3.6.1.2.1.25.3.5.1.1.1", "hrPrinterStatus.1", "integer", {"provider": "hr_printer_status"}),
    _entry("1.3.6.1.2.1.25.3.5.1.2.1", "hrPrinterDetectedErrorState.1", "octets",
           {"provider": "hr_detected_error_state"}),
    _entry("1.3.6.1.2.1.43.5.1.1.17.1", "prtGeneralSerialNumber.1", "string", {"provider": "serial", "default": ""}),
    _entry("1.3.6.1.2.1.43.6.1.1.3.1.1", "prtCoverStatus.1.1", "integer", {"provider": "cover_status"}),
    _entry("1.3.6.1.2.1.43.8.2.1.4.1.1", "prtInputMediaDimFeedDirDeclared.1.1", "integer",
           {"provider": "media_length"}),
    _entry("1.3.6.1.2.1.43.8.2.1.5.1.1", "prtInputMediaDimXFeedDirDeclared.1.1", "integer",
           {"provider": "media_width"}),
    _entry("1.3.6.1.2.1.43.8.2.1.11.1.1", "prtInputStatus.1.1", "integer", {"provider": "input_status"}),
    _entry("1.3.6.1.2.1.43.8.2.1.12.1.1", "prtInputMediaName.1.1", "string", {"provider": "media_name"}),
    _entry("1.3.6.1.2.1.43.16.5.1.2.1.1", "prtConsoleDisplayBufferText.1.1", "string",
           {"provider": "console_text"}),
    _entry("1.3.6.1.4.1.2435.2.3.9.1.1.7.0", "brieee1284id", "string",
           {"provider": "ieee1284_id", "default": IEEE1284_DEFAULT}),
    _entry("1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.0", "brInfoSerialNumber", "string", {"provider": "serial"}),
    _entry("1.3.6.1.4.1.2435.2.4.3.1240.1.1.0", "brpsNodeName", "string", {"provider": "sysname"}),
    _entry("1.3.6.1.4.1.2435.3.3.9.1.6.1.0", "brStatusBlock", "octets", {"provider": "status_block"}),
]}
TEST_MAP = persona.OidMap.from_dict(TEST_MAP_DOC)
BY_NAME = {e.name: e for e in TEST_MAP.entries}


def shipped_map() -> persona.OidMap:
    """The shipped agent/oid-map.yaml (needs PyYAML in this interpreter)."""
    if not HAVE_YAML:
        raise unittest.SkipTest("PyYAML is not installed in this interpreter")
    return persona.load_oid_map(SHIPPED_MAP)


class FakeClock:
    """Wall and monotonic time under test control."""

    def __init__(self, wall: float = NOW, mono: float = 1000.0) -> None:
        self.wall = wall
        self.mono = mono

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


class StaticNet:
    """NetInfo stand-in with a fixed MAC (or none)."""

    def __init__(self, mac: Optional[bytes] = MAC) -> None:
        self._mac = mac

    def mac(self) -> Optional[bytes]:
        return self._mac


def settings_with(**settings: str) -> config.RuntimeConfig:
    """Built-in defaults overlaid with ``settings``."""
    cfg = config.load_runtime_config(None)
    cfg.update(settings)
    return cfg


def short_tempdir(prefix: str) -> str:
    """A temporary directory with a short path (unix socket paths are limited to ~104 bytes)."""
    base = None
    if len(tempfile.gettempdir()) > 40 and os.path.isdir("/tmp"):
        base = "/tmp"
    return tempfile.mkdtemp(prefix=prefix, dir=base)


_CHILD_YAML: Optional[bool] = None


def child_has_yaml() -> bool:
    """Whether ``python3 -I`` (as the services run it) can import PyYAML."""
    global _CHILD_YAML
    if _CHILD_YAML is None:
        result = subprocess.run([sys.executable, "-I", "-c", "import yaml"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
        _CHILD_YAML = result.returncode == 0
    return _CHILD_YAML


def write_child_map(directory: str) -> Tuple[str, persona.OidMap]:
    """An oid-map.yaml path a ``python3 -I`` child can load, and the map it will see.

    The shipped YAML is copied; when the child lacks PyYAML, an oid-map.json
    sibling is written too (the loader's documented fallback). Without PyYAML
    anywhere, the sibling holds the test map instead.
    """
    path = os.path.join(directory, "oid-map.yaml")
    shutil.copyfile(SHIPPED_MAP, path)
    if child_has_yaml():
        return path, persona.load_oid_map(path) if HAVE_YAML else _child_view(path)
    doc = yaml.safe_load(SHIPPED_MAP.read_text(encoding="utf-8")) if HAVE_YAML else TEST_MAP_DOC
    with open(os.path.join(directory, "oid-map.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path, persona.OidMap.from_dict(doc)


def _child_view(path: str) -> persona.OidMap:
    """The map as a PyYAML-capable child loads it, when this interpreter has no PyYAML."""
    script = ("import json, sys, yaml; json.dump(yaml.safe_load(open(sys.argv[1], encoding='utf-8')), "
              "sys.stdout)")
    out = subprocess.run([sys.executable, "-I", "-c", script, path], stdout=subprocess.PIPE, check=True,
                         timeout=60)
    return persona.OidMap.from_dict(json.loads(out.stdout))


def write_runtime(directory: str, **settings: str) -> Tuple[str, str]:
    """runtime.env + identity.env for a child agent. Generous status limits keep the fixture fresh."""
    values = {"STATUS_POLL_SECONDS": "60", "STATUS_MAX_AGE_SECONDS": "600", "PERSONA_MAC": MAC_TEXT,
              "PERSONA_LOCATION": "Label bench", "LOG_LEVEL": "INFO"}
    values.update(settings)
    runtime = os.path.join(directory, "runtime.env")
    with open(runtime, "w", encoding="utf-8") as fh:
        fh.write("".join(f'{key}="{value}"\n' for key, value in values.items()))
    identity = os.path.join(directory, "identity.env")
    with open(identity, "w", encoding="utf-8") as fh:
        fh.write('PRINTER_UUID="e3248000-80ce-11db-8000-00005e005301"\n')
    return runtime, identity


def write_live_state(directory: str, frame: Optional[str] = DK22251, **kwargs: Any) -> str:
    """state.json stamped with the real clock (for child processes)."""
    path = os.path.join(directory, "state.json")
    write_json(path, make_state(frame, now=time.time(), **kwargs))
    return path


def _which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for folder in ("/usr/sbin", "/usr/bin", "/usr/local/sbin", "/sbin"):
        candidate = os.path.join(folder, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def free_udp_port() -> int:
    """An unused UDP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def selinux_enforcing() -> bool:
    try:
        with open("/sys/fs/selinux/enforce", encoding="ascii") as fh:
            return fh.read().strip() == "1"
    except OSError:
        return False


class SnmpdUnavailable(Exception):
    """The private snmpd could not be started or did not answer."""


class PrivateSnmpd:
    """net-snmp's snmpd on 127.0.0.1:<free port> with a private configuration.

    Only loopback is used. Everything started is stopped by :meth:`stop`,
    including pass_persist children left behind.
    """

    TOOLS = ("snmpget", "snmpgetnext", "snmpwalk", "snmpset")

    @staticmethod
    def find_tools() -> Optional[Dict[str, str]]:
        """Paths of snmpd and the client tools, or None if any is missing.

        On macOS /usr/sbin/snmpd is a wrapper that copies /etc config and waits
        for the network; the real daemon is /usr/libexec/snmpd.
        """
        if sys.platform == "darwin":
            snmpd = "/usr/libexec/snmpd" if os.access("/usr/libexec/snmpd", os.X_OK) else None
        else:
            snmpd = _which("snmpd")
        tools = {"snmpd": snmpd}
        for name in PrivateSnmpd.TOOLS:
            tools[name] = _which(name)
        return None if None in tools.values() else tools

    @staticmethod
    def has_vacm(snmpd: str) -> bool:
        """True if this snmpd understands VACM directives (Apple's build does not)."""
        out = subprocess.run([snmpd, "-C", "-H"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=30,
                             check=False)
        return bool(re.search(r"(?m)^\s+rocommunity\s", out.stdout))

    def __init__(self, tools: Dict[str, str], directory: str, config_text: str) -> None:
        self.tools = tools
        self.dir = directory
        self.port = free_udp_port()
        self.conf = os.path.join(directory, "snmpd.conf")
        self.log_path = os.path.join(directory, "snmpd.log")
        for sub in ("clientconf", "persist"):
            os.makedirs(os.path.join(directory, sub), exist_ok=True)
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(config_text)
        self.env = dict(os.environ, HOME=directory, LC_ALL="C",
                        SNMPCONFPATH=os.path.join(directory, "clientconf"),
                        SNMP_PERSISTENT_DIR=os.path.join(directory, "persist"))
        self.proc: Optional[subprocess.Popen] = None
        self._out = None

    def start(self, probe_community: str, timeout: float = 20.0) -> None:
        """Start snmpd and wait until it answers sysUpTime.0. Raises :class:`SnmpdUnavailable`."""
        self._out = open(os.path.join(self.dir, "snmpd.out"), "ab")
        self.proc = subprocess.Popen(
            [self.tools["snmpd"], "-f", "-C", "-c", self.conf, "-Lf", self.log_path, f"udp:127.0.0.1:{self.port}"],
            env=self.env, stdin=subprocess.DEVNULL, stdout=self._out, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SnmpdUnavailable(f"snmpd exited with status {self.proc.returncode}: {self.log_tail()}")
            probe = self.run("snmpget", "2c", probe_community, ".1.3.6.1.2.1.1.3.0", timeout=1, retries=0)
            if probe.returncode == 0 and "Timeticks" in probe.stdout:
                return
            time.sleep(0.2)
        raise SnmpdUnavailable(f"snmpd did not answer on 127.0.0.1:{self.port}: {self.log_tail()}")

    def stop(self) -> None:
        """Stop snmpd and any child it left running."""
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        if self._out is not None:
            self._out.close()
            self._out = None
        kill_strays(self.dir)

    def run(self, tool: str, version: str, community: str, *args: str, options: Tuple[str, ...] = (),
            timeout: float = 5, retries: int = 1) -> subprocess.CompletedProcess:
        """Run a net-snmp client tool against this snmpd (numeric OIDs, numeric
        enums: Fedora ships MIB files, so without -Oe an INTEGER prints as idle(3))."""
        return subprocess.run([self.tools[tool], "-v", version, "-c", community, "-On", "-Oe", "-t", str(timeout),
                               "-r", str(retries), *options, f"127.0.0.1:{self.port}", *args],
                              env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                              timeout=60, check=False)

    def log_tail(self) -> str:
        tail = []
        for name in ("snmpd.log", "snmpd.out"):
            try:
                with open(os.path.join(self.dir, name), encoding="utf-8", errors="replace") as fh:
                    tail.append(f"--- {name} ---\n{fh.read()[-3000:]}")
            except OSError:
                continue
        return "\n".join(tail)


def kill_strays(marker: str) -> None:
    """Kill processes whose command line contains ``marker`` (a unique temp dir), if any survive."""
    pgrep = _which("pgrep")
    if pgrep is None:
        return
    out = subprocess.run([pgrep, "-f", marker], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         encoding="ascii", errors="replace", timeout=30, check=False)
    for pid in out.stdout.split():
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass


_SNMP_LINE = re.compile(r"(?m)^(\.1\.3\.6\.1(?:\.\d+)+) = (.*)$")


def snmp_values(output: str) -> Dict[str, str]:
    """``{oid: value text}`` from numeric net-snmp output, joining wrapped hex lines.

    A walk's closing "No more variables" line repeats the last OID; it does not
    replace that OID's value.
    """
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


def snmp_oids(output: str) -> List[str]:
    """The OIDs that carry values in net-snmp output, in order (exceptions excluded)."""
    return [oid for oid, text in snmp_values(output).items()
            if not text.startswith(("No Such", "No more variables"))]


def hex_of(text: str) -> bytes:
    """Bytes of a net-snmp ``Hex-STRING: ..`` value."""
    return bytes.fromhex(text.split(":", 1)[1].replace(" ", ""))


# ---------------------------------------------------------------------------
# Status policy
# ---------------------------------------------------------------------------
class EvaluateTest(unittest.TestCase):
    POLICY = persona.Policy()   # heartbeat 9 s (3 x the 3 s poll), frame age 60 s, busy window 60 s, job stall 300 s

    def view(self, state, now=NOW, **kwargs):
        return persona.evaluate(state, now, self.POLICY, **kwargs)

    def assertLiveVerbatim(self, view, frame, busy=False):
        self.assertEqual(view.availability, persona.LIVE, view.reason)
        self.assertEqual(view.block.raw, raw(frame))
        self.assertFalse(view.synthesized)
        self.assertEqual(view.busy, busy)

    def assertSynthesizedPrinting(self, view, frame):
        """docs/architecture.md section 6: only bytes 18-21 change, to 00 01 00 00."""
        self.assertEqual(view.availability, persona.LIVE, view.reason)
        self.assertTrue(view.synthesized and view.busy)
        expected = bytearray(raw(frame))
        expected[18:22] = b"\x00\x01\x00\x00"   # reply, phase printing, phase number 00 00
        self.assertEqual(view.block.raw, bytes(expected))
        for offset in (10, 11, 14, 17, 24, 25):   # the media bytes are never touched
            self.assertEqual(view.block.raw[offset], raw(frame)[offset], offset)
        self.assertEqual(persona.SYNTH_BYTES, (18, 19, 20, 21))

    def assertDown(self, view, availability, reason_part):
        self.assertEqual(view.availability, availability)
        self.assertIn(reason_part, view.reason)
        self.assertEqual(view.block, persona.offline_block())

    def test_live_frames_are_served_verbatim(self):
        for frame in (DK22251, DIE_CUT_29X90, COVER_OPEN, NO_MEDIA, NO_MEDIA_AFTER_JOB, MISMATCH_LATCH,
                      PRINTING_COMPLETED):
            with self.subTest(frame=frame):
                self.assertLiveVerbatim(self.view(make_state(frame)), frame)
        self.assertLiveVerbatim(self.view(make_state(PHASE_PRINTING)), PHASE_PRINTING, busy=True)

    def test_job_in_flight_synthesises_the_printing_phase(self):
        job = {"active": True, "started": NOW - 5, "peer": "192.0.2.10"}
        self.assertSynthesizedPrinting(self.view(make_state(DK22251, polled_age=8, job=job)), DK22251)
        # Media bytes (and the two-colour byte 25) are untouched.
        block = self.view(make_state(DK22251, polled_age=8, job=job)).block
        self.assertEqual(media.describe(block).label_id, "62red")
        # The proxy may skip writing state.json while it streams the job.
        self.assertSynthesizedPrinting(self.view(make_state(DK22251, updated_age=40, polled_age=55,
                                                            job=dict(job, started=NOW - 50))), DK22251)

    def test_job_in_flight_with_a_fresh_frame_serves_it(self):
        job = {"active": True, "started": NOW - 5}
        self.assertLiveVerbatim(self.view(make_state(PHASE_PRINTING, polled_age=1, job=job)), PHASE_PRINTING,
                                busy=True)
        self.assertLiveVerbatim(self.view(make_state(PRINTING_COMPLETED, polled_age=1, job=job)),
                                PRINTING_COMPLETED)
        # No start time recorded: nothing to compare with, so the frame is served as it is.
        self.assertLiveVerbatim(self.view(make_state(DK22251, polled_age=8, job={"active": True})), DK22251)

    def test_job_on_a_faulty_printer_shows_the_fault(self):
        job = {"active": True, "started": NOW - 5}
        for frame in (COVER_OPEN, NO_MEDIA, NO_MEDIA_AFTER_JOB, MISMATCH_LATCH):
            with self.subTest(frame=frame):
                self.assertLiveVerbatim(self.view(make_state(frame, polled_age=8, job=job)), frame)

    def test_printing_phase_is_capped_per_job(self):
        # Past the cap the pre-job idle reply is never served as live: it would say the
        # printer is idle while the job has not ended (a stalled write, a half-close drain).
        job = {"active": True, "started": NOW - 61}
        self.assertDown(self.view(make_state(DK22251, polled_age=65, job=job)), persona.OFFLINE,
                        "the job has not ended")
        # A fault or no roll before the job is still the printer's own word.
        self.assertLiveVerbatim(self.view(make_state(COVER_OPEN, polled_age=65, job=job)), COVER_OPEN)

    def test_cap_counts_from_the_last_observed_byte(self):
        job = {"active": True, "started": NOW - 200}
        state = make_state(DK22251, updated_age=2, polled_age=205, job=job)
        self.assertSynthesizedPrinting(self.view(state, job_activity=NOW - 30), DK22251)   # still streaming
        self.assertSynthesizedPrinting(self.view(state, job_activity=NOW - 60), DK22251)   # at the cap
        self.assertDown(self.view(state, job_activity=NOW - 61), persona.OFFLINE, "61 s since the job's last data")
        self.assertDown(self.view(state, job_activity=NOW - 300), persona.OFFLINE,        # never before start
                        "200 s since the job's last data")

    def test_unsolicited_frames_are_never_synthesised_over(self):
        # Only the last idle-poll reply can predate the job. Frames the printer volunteers
        # (printing completed, phase change) are its own news, even with an old poll time.
        job = {"active": True, "started": NOW - 5}
        self.assertLiveVerbatim(self.view(make_state(PRINTING_COMPLETED, polled_age=8, job=job)),
                                PRINTING_COMPLETED)
        self.assertLiveVerbatim(self.view(make_state(PHASE_PRINTING, polled_age=8, job=job)), PHASE_PRINTING,
                                busy=True)

    def test_status_already_stale_when_the_job_started(self):
        job = {"active": True, "started": NOW - 5}
        self.assertDown(self.view(make_state(DK22251, polled_age=70, job=job)), persona.OFFLINE, "before the job")

    def test_no_synthesis_once_the_job_is_over(self):
        # job.active stays true until the printer reports completion (ql810wc-proxyd), so a
        # finished job means the printer is done: its last frame is served as it is.
        done = {"active": False, "last_finished": NOW - 5, "last_result": "ok"}
        self.assertLiveVerbatim(self.view(make_state(DK22251, polled_age=20, job=done)), DK22251)
        self.assertLiveVerbatim(self.view(make_state(PRINTING_COMPLETED, polled_age=1, job=done)),
                                PRINTING_COMPLETED)
        self.assertDown(self.view(make_state(DK22251, polled_age=70, job=done)), persona.OFFLINE, "fresh status")

    def test_proxy_stopped_and_device_detail(self):
        stopped = make_state(DK22251)
        stopped["proxy"]["pid"] = None   # ql810wc-proxyd's clean stop
        self.assertDown(self.view(stopped), persona.UNKNOWN, "ql810wc-proxyd has stopped")
        broken = make_state(DK22251, device="error")
        broken["device"]["detail"] = "write timed out\n  after 5 s"
        self.assertDown(self.view(broken), persona.OFFLINE, "USB error): write timed out after 5 s")

    def test_stale_frame_while_idle_is_offline(self):
        self.assertLiveVerbatim(self.view(make_state(DK22251, polled_age=59)), DK22251)
        self.assertDown(self.view(make_state(DK22251, polled_age=61)), persona.OFFLINE, "no fresh status")

    def test_stale_or_missing_state_is_unknown(self):
        self.assertDown(self.view(None), persona.UNKNOWN, "ql810wc-proxyd")
        self.assertDown(self.view(make_state(DK22251, updated_age=11, polled_age=11)), persona.UNKNOWN,
                        "not rewritten")
        bad = make_state(DK22251)
        bad["updated"] = "yesterday"
        self.assertDown(self.view(bad), persona.UNKNOWN, "updated")
        job = {"active": True, "started": NOW - 400}
        self.assertDown(self.view(make_state(DK22251, updated_age=311, polled_age=400, job=job)),
                        persona.UNKNOWN, "not rewritten")

    def test_device_states(self):
        self.assertDown(self.view(make_state(DK22251, device="absent")), persona.OFFLINE, "not connected")
        self.assertDown(self.view(make_state(DK22251, device="mass-storage")), persona.OFFLINE, "Editor Lite")
        self.assertDown(self.view(make_state(DK22251, device="error")), persona.OFFLINE, "USB error")
        self.assertDown(self.view(make_state(DK22251, device="rebooting")), persona.OFFLINE, "rebooting")
        for odd in (None, ["ready"], {"x": 1}, 7):
            with self.subTest(device=odd):
                self.assertDown(self.view(make_state(DK22251, device=odd)), persona.OFFLINE, "unknown")

    def test_warmup_until_the_first_status(self):
        self.assertDown(self.view(make_state(None, started_age=5)), persona.WARMUP, "first status")
        self.assertDown(self.view(make_state(None, started_age=3600)), persona.OFFLINE, "no status")
        corrupt = make_state(DK22251, started_age=3600)
        corrupt["status"]["raw_hex"] = "80zz"
        self.assertDown(self.view(corrupt), persona.OFFLINE, "no status")

    def test_clock_skew_and_missing_poll_time(self):
        future = make_state(DK22251, updated_age=-30, polled_age=-30)   # wall clock stepped back
        self.assertLiveVerbatim(self.view(future), DK22251)
        self.assertLiveVerbatim(self.view(make_state(DK22251, polled_age=None)), DK22251)

    def test_offline_block(self):
        block = persona.offline_block()
        self.assertEqual(block.raw[:7], bytes.fromhex("80204234393004"))
        self.assertEqual((block.err1, block.err2, block.status_type), (0x20, 0x00, 0x02))
        self.assertIn("printer turned off", block.errors())
        self.assertFalse(block.has_media)
        self.assertIsNone(media.describe(block))
        self.assertEqual(block.model_name, "QL-810W")
        self.assertEqual(persona.offline_block("QL-820NWB").model_code, 0x41)

    def test_policy_from_config(self):
        # Heartbeat 3 x STATUS_POLL_SECONDS; frame age STATUS_MAX_AGE_SECONDS, as configured.
        self.assertEqual(persona.Policy.from_config(settings_with()), persona.Policy(9.0, 60.0))
        slow = persona.Policy.from_config(settings_with(STATUS_POLL_SECONDS="20", STATUS_MAX_AGE_SECONDS="45"))
        self.assertEqual((slow.heartbeat_limit, slow.frame_max_age), (60.0, 45.0))
        with self.assertRaises(config.ConfigError):
            persona.Policy.from_config(settings_with(STATUS_POLL_SECONDS="soon"))


class ConditionTest(unittest.TestCase):
    """The real QL-810W state matrix (BrotherRaster fixtures, fw 1.22)."""

    def cond(self, frame=None, availability=persona.LIVE, **view_args):
        block = Status(raw(frame)) if frame else persona.offline_block()
        return persona.condition(persona.PrinterView(availability, "", block, **view_args))

    def test_matrix(self):
        rows = [  # frame, hrDeviceStatus, hrPrinterStatus, errorState, console, cover, input
            (DK22251, 2, 3, 0x00, "READY", 4, 0),
            (COVER_OPEN, 5, 1, 0x08, "COVER OPEN", 3, 0),
            (NO_MEDIA, 2, 3, 0x00, "READY", 4, 19),
            (NO_MEDIA_AFTER_JOB, 5, 1, 0x40, "ERROR", 4, 19),
            (MISMATCH_LATCH, 5, 1, 0x00, "ERROR", 4, 0),
            (DIE_CUT_29X90, 2, 3, 0x00, "READY", 4, 0),
            (PHASE_PRINTING, 2, 4, 0x00, "BUSY", 4, 0),
        ]
        for frame, dev, prt, bits, text, cover, inp in rows:
            with self.subTest(frame=frame):
                busy = Status(raw(frame)).is_printing
                self.assertEqual(self.cond(frame, busy=busy), (dev, prt, bits, text.ljust(16), cover, inp))

    def test_not_live(self):
        self.assertEqual(self.cond(availability=persona.WARMUP), (2, 5, 0, "BUSY".ljust(16), 2, 69))
        self.assertEqual(self.cond(availability=persona.OFFLINE), (5, 1, 0x02, "OFFLINE".ljust(16), 2, 37))
        self.assertEqual(self.cond(availability=persona.UNKNOWN), (1, 1, 0x02, "OFFLINE".ljust(16), 2, 37))

    def test_error_bits(self):
        base = Status(raw(DK22251))
        cases = {(0x04, 0x00): 0x04, (0x20, 0x00): 0x02, (0x02, 0x00): 0x40, (0x00, 0x80): 0x01,
                 (0x00, 0x40): 0x04, (0x00, 0x01): 0x00, (0x01, 0x10): 0x48}
        for (err1, err2), bits in cases.items():
            with self.subTest(err1=err1, err2=err2):
                block = base.with_overrides(err1=err1, err2=err2, status_type=0x02)
                cond = persona.condition(persona.PrinterView(persona.LIVE, "", block))
                self.assertEqual((cond.device_status, cond.printer_status, cond.error_state), (5, 1, bits))


# ---------------------------------------------------------------------------
# Persona values
# ---------------------------------------------------------------------------
class PersonaFixture(unittest.TestCase):
    """A Persona over the test map, a state.json file and a fake clock."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qlpersona-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_path = os.path.join(self.tmp, "state.json")
        self.clock = FakeClock()

    def make(self, state=None, *, oid_map=TEST_MAP, net=None, **settings) -> persona.Persona:
        if state is not None:
            write_json(self.state_path, state)
        return persona.Persona(oid_map, settings_with(**settings), self.state_path, clock=self.clock.time,
                               monotonic=self.clock.monotonic, netinfo=net or StaticNet())

    def write(self, state) -> None:
        write_json(self.state_path, state)
        self.clock.advance(0.6)   # past the 0.5 s re-check interval

    @staticmethod
    def values(p: persona.Persona) -> Dict[str, Any]:
        return {e.name: p.value(e) for e in p.oid_map.entries}


class PersonaValuesTest(PersonaFixture):
    def test_idle_dk22251(self):
        v = self.values(self.make(make_state(DK22251)))
        self.assertEqual(v["brStatusBlock"], raw(DK22251))
        self.assertEqual((v["hrDeviceStatus.1"], v["hrPrinterStatus.1"], v["hrPrinterDetectedErrorState.1"]),
                         (2, 3, b"\x00"))
        self.assertEqual(v["prtConsoleDisplayBufferText.1.1"], "READY           ")
        self.assertEqual((v["prtCoverStatus.1.1"], v["prtInputStatus.1.1"]), (4, 0))
        self.assertEqual(v["prtInputMediaName.1.1"], '62mm / 2.4"')
        self.assertEqual((v["prtInputMediaDimXFeedDirDeclared.1.1"], v["prtInputMediaDimFeedDirDeclared.1.1"]),
                         (6200, -1))

    def test_die_cut_29x90(self):
        v = self.values(self.make(make_state(DIE_CUT_29X90)))
        self.assertEqual(v["brStatusBlock"], raw(DIE_CUT_29X90))
        self.assertEqual(v["prtInputMediaName.1.1"], '29mm x 90mm / 1.1" x 3.5"')
        self.assertEqual((v["prtInputMediaDimXFeedDirDeclared.1.1"], v["prtInputMediaDimFeedDirDeclared.1.1"]),
                         (2900, 9000))
        self.assertEqual(v["hrPrinterStatus.1"], 3)

    def test_cover_open(self):
        v = self.values(self.make(make_state(COVER_OPEN)))
        self.assertEqual(v["brStatusBlock"], raw(COVER_OPEN))
        self.assertEqual((v["hrDeviceStatus.1"], v["hrPrinterStatus.1"], v["hrPrinterDetectedErrorState.1"]),
                         (5, 1, b"\x08"))
        self.assertEqual((v["prtConsoleDisplayBufferText.1.1"], v["prtCoverStatus.1.1"]),
                         ("COVER OPEN      ", 3))

    def test_no_media(self):
        v = self.values(self.make(make_state(NO_MEDIA)))
        self.assertEqual((v["prtInputMediaName.1.1"], v["prtInputStatus.1.1"]), ("", 19))
        self.assertEqual((v["prtInputMediaDimXFeedDirDeclared.1.1"], v["prtInputMediaDimFeedDirDeclared.1.1"]),
                         (-2, -2))
        self.assertEqual((v["hrPrinterStatus.1"], v["hrDeviceStatus.1"]), (3, 2))

    def test_job_active(self):
        v = self.values(self.make(make_state(DK22251, polled_age=8, job={"active": True, "started": NOW - 4})))
        block = v["brStatusBlock"]
        self.assertEqual(block[18:22], b"\x00\x01\x00\x00")
        self.assertEqual(block[:18] + block[22:], raw(DK22251)[:18] + raw(DK22251)[22:])
        self.assertEqual((v["hrPrinterStatus.1"], v["prtConsoleDisplayBufferText.1.1"]), (4, "BUSY".ljust(16)))
        self.assertEqual(v["prtInputMediaName.1.1"], '62mm / 2.4"')

    def test_job_progress_extends_the_printing_phase(self):
        status = BY_NAME["brStatusBlock"]
        job = {"active": True, "started": NOW - 50, "peer": "192.0.2.10", "bytes": 1000}
        p = self.make(make_state(DK22251, updated_age=1, polled_age=52, job=job))
        self.assertEqual(p.value(status)[18:22], b"\x00\x01\x00\x00")
        self.clock.advance(20)   # 70 s after the start, but the byte count moved 1 s ago
        write_json(self.state_path, make_state(DK22251, now=self.clock.wall, updated_age=1, polled_age=72,
                                               job=dict(job, bytes=2000)))
        self.assertEqual(p.value(status)[18:22], b"\x00\x01\x00\x00")
        self.clock.advance(70)   # no byte for 71 s and the job still in flight: stale, never "idle"
        write_json(self.state_path, make_state(DK22251, now=self.clock.wall, updated_age=1, polled_age=142,
                                               job=dict(job, bytes=2000)))
        self.assertEqual(p.value(status), persona.offline_block().raw)
        write_json(self.state_path, make_state(DK22251, now=self.clock.wall, updated_age=1, polled_age=1,
                                               job=dict(job, active=False, last_result="ok")))
        self.clock.advance(1)    # the job over and a fresh frame: the real block again
        self.assertEqual(p.value(status), raw(DK22251))

    def test_stale_absent_and_missing(self):
        offline = persona.offline_block().raw
        for state, device_status in ((make_state(DK22251, updated_age=30), 1),
                                     (make_state(DK22251, device="absent"), 5),
                                     (None, 1)):
            with self.subTest(state=None if state is None else state["device"]["state"]):
                if state is None and os.path.exists(self.state_path):
                    os.unlink(self.state_path)
                v = self.values(self.make(state))
                self.assertEqual(v["brStatusBlock"], offline)
                self.assertEqual((v["hrDeviceStatus.1"], v["hrPrinterStatus.1"]), (device_status, 1))
                self.assertEqual(v["hrPrinterDetectedErrorState.1"], b"\x02")
                self.assertEqual(v["prtConsoleDisplayBufferText.1.1"], "OFFLINE".ljust(16))
                self.assertEqual(v["prtInputMediaName.1.1"], "")

    def test_status_block_is_never_absent(self):
        states = [make_state(f) for f in (DK22251, COVER_OPEN, NO_MEDIA)] + [
            make_state(None), make_state(DK22251, device="absent"), make_state(DK22251, updated_age=1e6)]
        for state in states:
            with self.subTest(state=state["device"]["state"]):
                self.assertEqual(len(self.make(state).value(BY_NAME["brStatusBlock"])), 32)

    def test_provider_failure_is_contained(self):
        p = self.make(make_state(DK22251))
        p._providers["status_block"] = lambda v: 1 / 0
        p._providers["hr_printer_status"] = lambda v: 1 / 0
        with self.assertLogs("ql810wc.persona", logging.ERROR):
            self.assertEqual(p.value(BY_NAME["brStatusBlock"]), persona.offline_block().raw)
            self.assertIsNone(p.value(BY_NAME["hrPrinterStatus.1"]))

    def test_every_provider_is_implemented(self):
        p = self.make(make_state(DK22251))
        self.assertEqual(set(p._providers), set(persona.PROVIDERS))
        used = {e.source.arg for e in TEST_MAP.entries if e.source.kind == "provider"}
        self.assertEqual(used, set(persona.PROVIDERS), "the test map should use every provider")

    def test_values_follow_state_changes(self):
        p = self.make(make_state(DK22251))
        self.assertEqual(p.value(BY_NAME["brStatusBlock"]), raw(DK22251))
        self.write(make_state(DIE_CUT_29X90))
        self.assertEqual(p.value(BY_NAME["brStatusBlock"]), raw(DIE_CUT_29X90))

    def test_transitions_are_logged(self):
        with self.assertLogs("ql810wc.persona", logging.INFO) as logs:
            p = self.make(make_state(DK22251))
            p.view()
            self.write(make_state(DK22251, device="absent"))
            p.view()
        text = "\n".join(logs.output)
        self.assertIn("62mm continuous black/red (DK-22251), idle", text)
        self.assertIn("offline: printer not connected", text)


class IdentityTest(PersonaFixture):
    def test_ieee1284_from_usb_normalised(self):
        p = self.make(make_state(DK22251, ieee1284=IEEE1284_USB.replace("MDL:QL-810W", "MDL:QL-810Wc")))
        self.assertEqual(p.value(BY_NAME["brieee1284id"]), IEEE1284_USB)

    def test_ieee1284_default_and_remembered(self):
        p = self.make(make_state(None, device="absent", ieee1284=None))
        self.assertEqual(p.value(BY_NAME["brieee1284id"]), IEEE1284_DEFAULT)
        self.write(make_state(DK22251))
        self.assertEqual(p.value(BY_NAME["brieee1284id"]), IEEE1284_USB)
        self.write(make_state(None, device="absent", ieee1284=None, serial=None))   # unplugged
        self.assertEqual(p.value(BY_NAME["brieee1284id"]), IEEE1284_USB)
        self.assertEqual(p.value(BY_NAME["brInfoSerialNumber"]), SERIAL)

    def test_identity_kept_by_the_proxy_while_the_printer_is_away(self):
        # ql810wc-proxyd keeps the last printer-mode identity while the printer is absent:
        # an agent started then still reports it.
        p = self.make(make_state(None, device="absent"))
        self.assertEqual(p.value(BY_NAME["brInfoSerialNumber"]), SERIAL)
        self.assertEqual(p.value(BY_NAME["brieee1284id"]), IEEE1284_USB)
        # A value seen in another mode never replaces one a ready printer reported.
        self.write(make_state(None, device="mass-storage", serial="Z0EDITORLITE", ieee1284=None))
        self.assertEqual(p.value(BY_NAME["brInfoSerialNumber"]), SERIAL)

    def test_serial(self):
        p = self.make(make_state(DK22251, serial=None))
        self.assertEqual(p.value(BY_NAME["prtGeneralSerialNumber.1"]), "")      # default
        self.assertIsNone(p.value(BY_NAME["brInfoSerialNumber"]))                  # no default: absent
        self.write(make_state(DK22251, serial=f"  {SERIAL} "))
        self.assertEqual(p.value(BY_NAME["brInfoSerialNumber"]), SERIAL)
        q = self.make(make_state(DK22251), PERSONA_SERIAL="Z0OVERRIDE01")
        self.assertEqual(q.value(BY_NAME["prtGeneralSerialNumber.1"]), "Z0OVERRIDE01")

    def test_mac_and_node_name(self):
        p = self.make(make_state(DK22251))
        self.assertEqual(p.value(BY_NAME["ifPhysAddress.1"]), MAC)
        self.assertEqual(p.value(BY_NAME["sysName"]), "BRW00005E005301")
        self.assertEqual(p.value(BY_NAME["brpsNodeName"]), "BRW00005E005301")
        q = self.make(make_state(DK22251), net=StaticNet(None), PERSONA_MAC="00:00:5E:00:53:02",
                      PERSONA_SYSNAME="BRWLABELBENCH")
        self.assertEqual(q.value(BY_NAME["ifPhysAddress.1"]), bytes.fromhex("00005E005302"))
        self.assertEqual(q.value(BY_NAME["sysName"]), "BRWLABELBENCH")

    def test_no_mac(self):
        p = self.make(make_state(DK22251), net=StaticNet(None))
        with self.assertLogs("ql810wc.persona", logging.WARNING) as logs:
            self.assertEqual(p.value(BY_NAME["ifPhysAddress.1"]), b"")
            self.assertEqual(p.value(BY_NAME["sysName"]), "BRW000000000000")
        self.assertEqual(len([m for m in logs.output if "PERSONA_MAC" in m]), 1)

    def test_bad_persona_mac_is_a_config_error(self):
        for value in ("00-00-5E-00-53-01", "00:00:00:00:00:00", "junk"):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                self.make(make_state(DK22251), PERSONA_MAC=value)

    def test_config_template_and_uptime(self):
        p = self.make(make_state(DK22251), PERSONA_LOCATION="Shipping desk", PERSONA_CONTACT="it-helpdesk")
        self.assertEqual(p.value(BY_NAME["sysLocation"]), "Shipping desk")
        self.assertEqual(p.value(BY_NAME["sysContact"]), "it-helpdesk")
        self.assertEqual(p.value(BY_NAME["hrDeviceDescr.1"]), "Brother QL-810W")
        self.assertEqual(p.value(BY_NAME["sysObjectID"]), (1, 3, 6, 1, 4, 1, 2435, 2, 3, 9, 1))
        start = p.value(BY_NAME["sysUpTime"])
        self.clock.advance(12.34)
        self.assertEqual(p.value(BY_NAME["sysUpTime"]) - start, 1234)

    def test_display_strings_are_cut_to_255_bytes(self):
        p = self.make(make_state(DK22251), PERSONA_LOCATION="é" * 200)   # 400 bytes of UTF-8
        value = p.value(BY_NAME["sysLocation"])
        self.assertLessEqual(len(value.encode("utf-8")), 255)
        self.assertEqual(value, "é" * 127)


class ReloadContinuityTest(PersonaFixture):
    """Persona(previous=...): the persona built on a reload carries on from the one it replaces."""

    def reloaded(self, old: persona.Persona, state_path: Optional[str] = None, **settings) -> persona.Persona:
        return persona.Persona(old.oid_map, settings_with(**settings), state_path or self.state_path,
                               clock=self.clock.time, monotonic=self.clock.monotonic, netinfo=StaticNet(),
                               previous=old)

    def test_sysuptime_keeps_counting(self):
        old = self.make(make_state(DK22251))
        self.clock.advance(100)
        before = old.value(BY_NAME["sysUpTime"])
        self.assertEqual(before, 10000)
        new = self.reloaded(old)
        self.assertEqual(new.value(BY_NAME["sysUpTime"]), before)
        self.clock.advance(1.5)
        self.assertEqual(new.value(BY_NAME["sysUpTime"]), before + 150)
        self.assertEqual(self.make().value(BY_NAME["sysUpTime"]), 0, "a new start counts from zero")

    def test_new_settings_take_effect(self):
        old = self.make(make_state(DK22251), PERSONA_LOCATION="Label bench")
        new = self.reloaded(old, PERSONA_LOCATION="Shipping desk")
        self.assertEqual(new.value(BY_NAME["sysLocation"]), "Shipping desk")
        self.assertEqual(old.value(BY_NAME["sysLocation"]), "Label bench")
        self.assertEqual(new.value(BY_NAME["brStatusBlock"]), raw(DK22251))

    def test_remembered_identity_survives_a_reload_while_the_printer_is_away(self):
        old = self.make(make_state(DK22251))
        self.assertEqual(old.value(BY_NAME["brInfoSerialNumber"]), SERIAL)
        self.write(make_state(None, device="absent", serial=None, ieee1284=None))   # unplugged
        old.view()
        new = self.reloaded(old)
        self.assertEqual(new.value(BY_NAME["brInfoSerialNumber"]), SERIAL)
        self.assertEqual(new.value(BY_NAME["brieee1284id"]), IEEE1284_USB)
        self.assertIsNone(self.make().value(BY_NAME["brInfoSerialNumber"]), "a new start knows no serial")
        # A persona over another state.json keeps only sysUpTime.
        other = self.reloaded(old, state_path=os.path.join(self.tmp, "other.json"))
        self.assertIsNone(other.value(BY_NAME["brInfoSerialNumber"]))

    def test_job_progress_survives_a_reload(self):
        # The busy-phase cap counts from the job's last byte. A persona that
        # forgot it would count from the job start and end the printing phase
        # too early: P-touch would see the printer switched off mid-job.
        status = BY_NAME["brStatusBlock"]
        job = {"active": True, "started": NOW - 50, "peer": "192.0.2.10", "bytes": 1000}
        old = self.make(make_state(DK22251, updated_age=1, polled_age=52, job=job))
        self.assertEqual(old.value(status)[18:22], b"\x00\x01\x00\x00")
        self.clock.advance(20)   # 70 s after the start; the byte count moved 1 s ago
        write_json(self.state_path, make_state(DK22251, now=self.clock.wall, updated_age=1, polled_age=72,
                                               job=dict(job, bytes=2000)))
        self.assertEqual(old.value(status)[18:22], b"\x00\x01\x00\x00")
        new = self.reloaded(old)
        self.assertEqual(new.value(status)[18:22], b"\x00\x01\x00\x00")
        self.assertEqual(self.make().value(status), persona.offline_block().raw,
                         "without the history: 70 s > the 60 s cap")


def ptouch_model(device_id: str) -> Optional[str]:
    """P-touch for Mac's getModelName: split on ';', then ':'; exactly two parts, key MDL, value untrimmed."""
    for part in device_id.split(";"):
        pieces = part.split(":")
        if len(pieces) == 2 and pieces[0] == "MDL":
            return pieces[1]
    return None


class Ieee1284Test(unittest.TestCase):
    def test_normalisation(self):
        cases = {
            IEEE1284_USB: IEEE1284_USB,
            "MFG:Brother;CMD:PT-CBP;MDL:QL-810Wc;CLS:PRINTER;": "MFG:Brother;CMD:PT-CBP;MDL:QL-810W;CLS:PRINTER;",
            "MFG:Brother;MODEL:QL-810Wc;CMD:PT-CBP": "MFG:Brother;MDL:QL-810W;CMD:PT-CBP;",
            "MFG: Brother; MDL: QL-810W": "MFG: Brother;MDL:QL-810W;",
            "MFG:Brother;CMD:PT-CBP;": "MFG:Brother;MDL:QL-810W;CMD:PT-CBP;",
            "CMD:PT-CBP;CLS:PRINTER": "MDL:QL-810W;CMD:PT-CBP;CLS:PRINTER;",
            "MFG:Brother;MDL:QL-810W;MDL:QL-810Wc;": "MFG:Brother;MDL:QL-810W;",
            "MFG:Brother;\nMDL:QL-810W;\r\n": "MFG:Brother;MDL:QL-810W;",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                result = persona.normalize_ieee1284(given, "QL-810W")
                self.assertEqual(result, expected)
                self.assertEqual(ptouch_model(result), "QL-810W")

    def test_empty(self):
        for given in (None, "", ";;", "  ", 42):
            with self.subTest(given=given):
                self.assertIsNone(persona.normalize_ieee1284(given, "QL-810W"))


class NetInfoTest(unittest.TestCase):
    """MAC lookup from /proc/net/route and /sys/class/net fixtures."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qlnet-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.route = os.path.join(self.tmp, "route")
        self.sysfs = os.path.join(self.tmp, "net")
        self.clock = FakeClock()
        self.set_routes([("wlan0", "0.0.0.0", "192.0.2.1", 0x0003, 600, "0.0.0.0"),
                         ("wlan0", "192.0.2.0", "0.0.0.0", 0x0001, 600, "255.255.255.0"),
                         ("eth1", "198.51.100.0", "0.0.0.0", 0x0001, 100, "255.255.255.128"),
                         ("eth2", "198.51.100.0", "0.0.0.0", 0x0001, 100, "255.255.255.0"),
                         ("eth3", "198.51.100.0", "192.0.2.1", 0x0003, 50, "255.255.255.0")])
        for name, mac in (("wlan0", "00:00:5e:00:53:01"), ("eth1", "00:00:5e:00:53:02"),
                          ("eth2", "00:00:5e:00:53:03"), ("eth3", "00:00:5e:00:53:04"),
                          ("lo", "00:00:00:00:00:00")):
            self.set_mac(name, mac)

    @staticmethod
    def _hex(address: str) -> str:
        """An address as the kernel prints it in /proc/net/route (host-order hex of network bytes)."""
        import ipaddress
        return "%08X" % int.from_bytes(ipaddress.IPv4Address(address).packed, sys.byteorder)

    def set_routes(self, routes) -> None:
        lines = ["Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"]
        for iface, dest, gateway, flags, metric, mask in routes:
            lines.append(f"{iface}\t{self._hex(dest)}\t{self._hex(gateway)}\t{flags:04X}\t0\t0\t{metric}\t"
                         f"{self._hex(mask)}\t0\t0\t0")
        with open(self.route, "w", encoding="ascii") as fh:
            fh.write("\n".join(lines) + "\n")

    def set_mac(self, iface: str, mac: str) -> None:
        os.makedirs(os.path.join(self.sysfs, iface), exist_ok=True)
        with open(os.path.join(self.sysfs, iface, "address"), "w", encoding="ascii") as fh:
            fh.write(mac + "\n")

    def net(self, proxy_ip: str = "") -> persona.NetInfo:
        return persona.NetInfo(proxy_ip, route_path=self.route, sysfs_net=self.sysfs, monotonic=self.clock.monotonic)

    def test_default_route_interface(self):
        net = self.net()
        self.assertEqual((net.interface(), net.mac()), ("wlan0", bytes.fromhex("00005e005301")))

    def test_proxy_ip_picks_the_connected_route(self):
        self.assertEqual(self.net("198.51.100.7").interface(), "eth1")    # /25 beats /24, no gateway
        self.assertEqual(self.net("198.51.100.200").interface(), "eth2")
        self.assertEqual(self.net("192.0.2.55").interface(), "wlan0")
        self.assertEqual(self.net("203.0.113.9").interface(), "wlan0")    # no route: default route

    def test_invalid_proxy_ip(self):
        with self.assertLogs("ql810wc.persona", logging.WARNING):
            self.assertEqual(self.net("not-an-ip").interface(), "wlan0")

    def test_interfaces_without_a_mac_are_skipped(self):
        os.unlink(os.path.join(self.sysfs, "eth1", "address"))
        self.assertEqual(self.net("198.51.100.7").interface(), "eth2")   # next most specific route
        self.set_routes([("lo", "0.0.0.0", "0.0.0.0", 0x0001, 0, "0.0.0.0")])
        self.assertIsNone(self.net().mac())

    def test_missing_files(self):
        os.unlink(self.route)
        self.assertIsNone(self.net().mac())

    def test_lookups_are_cached(self):
        net = self.net()
        self.assertEqual(net.mac(), bytes.fromhex("00005e005301"))
        self.set_mac("wlan0", "00:00:5e:00:53:09")
        self.assertEqual(net.mac(), bytes.fromhex("00005e005301"))
        self.clock.advance(persona.NETINFO_TTL_SECONDS + 1)
        self.assertEqual(net.mac(), bytes.fromhex("00005e005309"))

    def test_a_missing_mac_is_retried_soon(self):
        os.unlink(self.route)   # e.g. network not up yet at boot
        net = self.net()
        self.assertIsNone(net.mac())
        self.set_routes([("wlan0", "0.0.0.0", "192.0.2.1", 0x0003, 600, "0.0.0.0")])
        self.assertIsNone(net.mac())   # the failed lookup is reused for a moment
        self.clock.advance(persona.NETINFO_RETRY_SECONDS + 0.1)
        self.assertEqual(net.mac(), bytes.fromhex("00005e005301"))

    @unittest.skipUnless(os.path.exists("/proc/net/route"), "Linux only")
    def test_real_system_does_not_fail(self):
        persona.NetInfo().mac()   # whatever this host has; must not raise


class StateSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qlstate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "state.json")
        self.clock = FakeClock()
        self.source = persona.StateSource(self.path, monotonic=self.clock.monotonic)

    def test_checked_at_most_every_half_second_and_reread_only_on_change(self):
        write_json(self.path, make_state(DK22251))
        self.assertEqual(self.source.get()["status"]["raw_hex"], DK22251.replace(" ", ""))
        self.assertEqual(self.source.reads, 1)
        write_json(self.path, make_state(COVER_OPEN))
        self.clock.advance(0.4)
        self.assertEqual(self.source.get()["status"]["raw_hex"], DK22251.replace(" ", ""))  # not checked yet
        self.clock.advance(0.2)
        self.assertEqual(self.source.get()["status"]["raw_hex"], COVER_OPEN.replace(" ", ""))
        self.assertEqual(self.source.reads, 2)
        for _ in range(3):
            self.clock.advance(0.6)
            self.source.get()
        self.assertEqual(self.source.reads, 2)   # unchanged file: stat only

    def test_missing_corrupt_and_reappearing(self):
        self.assertIsNone(self.source.get())
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.clock.advance(0.6)
        self.assertIsNone(self.source.get())
        write_json(self.path, make_state(DK22251))
        self.clock.advance(0.6)
        self.assertIsNotNone(self.source.get())
        generation = self.source.generation
        os.unlink(self.path)
        self.clock.advance(0.6)
        self.assertIsNone(self.source.get())
        self.assertGreater(self.source.generation, generation)


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------
class PersonaTreeTest(PersonaFixture):
    def test_walk_is_in_strict_oid_order(self):
        tree = persona.PersonaTree(self.make(make_state(DK22251)))
        oids = [entry.oid for entry, _ in tree.walk()]
        self.assertEqual(oids, sorted(e.oid for e in TEST_MAP.entries))
        self.assertTrue(all(a < b for a, b in zip(oids, oids[1:])))
        for entry, vb in tree.walk():
            agentx.encode_varbind(vb)   # every value is sendable
            self.assertEqual(vb.type, entry.vtype)

    def test_getnext_semantics(self):
        tree = persona.PersonaTree(self.make(make_state(DK22251)))
        keys = [e.oid for e in TEST_MAP.entries]
        first = tree.getnext((1, 3, 6, 1), None, False, "printer")
        self.assertEqual(first.oid, keys[0])
        self.assertEqual(tree.getnext(keys[3], None, True, "printer").oid, keys[3])
        self.assertEqual(tree.getnext(keys[3], None, False, "printer").oid, keys[4])
        self.assertIsNone(tree.getnext(keys[3], keys[4], False, "printer"))   # end is exclusive
        self.assertEqual(tree.getnext(keys[3], keys[5], False, "printer").oid, keys[4])
        self.assertIsNone(tree.getnext(keys[-1], None, False, "printer"))
        self.assertEqual(tree.getnext((1, 3, 6, 1, 2, 1, 1, 3), None, False, "printer").oid,
                         (1, 3, 6, 1, 2, 1, 1, 3, 0))   # inside an object: its instance

    def test_absent_values_are_skipped_and_reported(self):
        tree = persona.PersonaTree(self.make(make_state(DK22251, serial=None)))
        serial = BY_NAME["brInfoSerialNumber"].oid
        before = BY_NAME["brieee1284id"].oid
        self.assertEqual(tree.getnext(before, None, False, "printer").oid, BY_NAME["brpsNodeName"].oid)
        self.assertEqual(tree.get(serial, "printer"), agentx.VarBind(serial, VT.NO_SUCH_INSTANCE, None))
        self.assertNotIn(serial, [e.oid for e, _ in tree.walk()])

    def test_get_and_contexts(self):
        tree = persona.PersonaTree(self.make(make_state(DK22251)))
        vb = tree.get(STATUS_OID, "printer")
        self.assertEqual((vb.type, vb.value), (VT.OCTET_STRING, raw(DK22251)))
        self.assertIsNone(tree.get(STATUS_OID + (0,), "printer"))
        self.assertIsNone(tree.get((1, 3, 6, 1, 4, 1, 2435, 99, 0), "printer"))
        self.assertIsNone(tree.get(STATUS_OID, ""))
        self.assertIsNone(tree.getnext((1, 3, 6, 1), None, False, ""))
        default_ctx = persona.PersonaTree(self.make(make_state(DK22251)), context="")
        self.assertIsNotNone(default_ctx.get(STATUS_OID, ""))

    def test_subtrees_and_registrations(self):
        p = self.make(make_state(DK22251))
        brother = (1, 3, 6, 1, 4, 1, 2435)
        # The subtree with the status block registers first, then OID order.
        self.assertEqual(persona.PersonaTree(p).registrations(),
                         (brother,) + tuple(s for s in persona.AGENTX_SUBTREES if s != brother))
        pp = persona.PersonaTree(p, context="", subtrees=persona.PASS_PERSIST_SUBTREES)
        self.assertEqual(pp.registrations(), (brother, (1, 3, 6, 1, 2, 1, 43)))
        self.assertEqual(sorted(pp.registrations()), sorted(persona.PASS_PERSIST_SUBTREES))
        self.assertTrue(all(any(persona.within(e.oid, s) for s in persona.PASS_PERSIST_SUBTREES)
                            for e in pp.entries))
        self.assertIsNone(pp.lookup(BY_NAME["sysName"].oid))

    def test_registrations_skip_empty_subtrees_and_follow_the_status_block(self):
        doc = dict(TEST_MAP_DOC, entries=[e for e in TEST_MAP_DOC["entries"]
                                          if not e["oid"].startswith("1.3.6.1.2.1.2.")])
        tree = persona.PersonaTree(self.make(make_state(DK22251), oid_map=persona.OidMap.from_dict(doc)))
        self.assertEqual(tree.registrations(), ((1, 3, 6, 1, 4, 1, 2435), (1, 3, 6, 1, 2, 1, 1),
                                                (1, 3, 6, 1, 2, 1, 25), (1, 3, 6, 1, 2, 1, 43)))
        # Without a status block entry the order is plain OID order.
        doc = dict(TEST_MAP_DOC, entries=[e for e in TEST_MAP_DOC["entries"] if e["name"] != "brStatusBlock"])
        tree = persona.PersonaTree(self.make(make_state(DK22251), oid_map=persona.OidMap.from_dict(doc)))
        self.assertEqual(tree.registrations(), persona.AGENTX_SUBTREES)

    def test_shipped_map(self):
        oid_map = shipped_map()
        tree = persona.PersonaTree(self.make(make_state(DK22251), oid_map=oid_map))
        walked = list(tree.walk())
        self.assertEqual([e.oid for e, _ in walked], [e.oid for e in oid_map.entries])
        for entry, vb in walked:
            agentx.encode_varbind(vb)
            if "ptouch-mac" in entry.clients:
                self.assertIn(vb.type, (VT.INTEGER, VT.OCTET_STRING), entry.name)
        for bundle in (WIN_INSTALLER_BUNDLE, MOBILE_BUNDLE):
            for oid in bundle:
                with self.subTest(oid=oid):
                    vb = tree.get(agentx.parse_oid(oid), "printer")
                    self.assertIsNotNone(vb)
                    self.assertNotIn(vb.type, (VT.NO_SUCH_INSTANCE, VT.NO_SUCH_OBJECT))
        os.unlink(self.state_path)
        offline = persona.PersonaTree(self.make(None, oid_map=oid_map))   # no state.json at all
        for bundle in (WIN_INSTALLER_BUNDLE, MOBILE_BUNDLE, ("1.3.6.1.4.1.2435.3.3.9.1.6.1.0",)):
            for oid in bundle:
                with self.subTest(offline=oid):
                    self.assertNotIn(offline.get(agentx.parse_oid(oid), "printer").type,
                                     (VT.NO_SUCH_INSTANCE, VT.NO_SUCH_OBJECT))

    def test_describe_varbind(self):
        tree = persona.PersonaTree(self.make(make_state(DK22251), PERSONA_LOCATION='Desk "A"'))
        lines = {e.name: persona.describe_varbind(e, vb) for e, vb in tree.walk()}
        self.assertEqual(lines["hrPrinterStatus.1"], ".1.3.6.1.2.1.25.3.5.1.1.1 = INTEGER: 3")
        self.assertEqual(lines["sysLocation"], '.1.3.6.1.2.1.1.6.0 = STRING: "Desk \\"A\\""')
        self.assertTrue(lines["brStatusBlock"].endswith("Hex-STRING: " + DK22251.upper()))
        self.assertEqual(lines["sysObjectID"], ".1.3.6.1.2.1.1.2.0 = OID: .1.3.6.1.4.1.2435.2.3.9.1")
        self.assertEqual(lines["sysContact"], '.1.3.6.1.2.1.1.4.0 = STRING: ""')


# ---------------------------------------------------------------------------
# Entry point, systemd unit, snmpd drop-in
# ---------------------------------------------------------------------------
entry_point = load_module("agent/brother_snmp_agent.py", "brother_snmp_agent_under_test")


def unit_settings(text: str) -> Dict[str, List[str]]:
    settings: Dict[str, List[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ";", "[")) and "=" in line:
            key, value = line.split("=", 1)
            settings.setdefault(key.strip(), []).append(value.strip())
    return settings


class CommandLineTest(unittest.TestCase):
    def test_unit_execstart_matches_the_contract(self):
        unit = unit_settings(UNIT_FILE.read_text(encoding="utf-8"))
        argv = unit["ExecStart"][0].split()
        self.assertEqual(argv[:4], ["/usr/bin/python3", "-I", "-B",
                                    "/usr/local/libexec/ql810wc-proxy/brother_snmp_agent.py"])
        args, _ = entry_point.parse_args(argv[4:])
        self.assertEqual((args.mode, args.socket, args.context), ("agentx", "/var/agentx/master", "printer"))
        self.assertEqual((args.config, args.identity, args.state, args.oid_map),
                         ("/etc/ql810wc-proxy/runtime.env", "/etc/ql810wc-proxy/identity.env",
                          "/run/ql810wc-proxy/state.json", "/usr/local/libexec/ql810wc-proxy/oid-map.yaml"))

    def test_unit_file(self):
        text = UNIT_FILE.read_text(encoding="utf-8")
        unit = unit_settings(text)
        self.assertEqual(re.findall(r"@[A-Z][A-Z0-9_]*@", text), [], "qlp_render_unit accepts no other placeholders")
        for key, value in (("Type", "notify-reload"), ("User", "ql810wc"), ("Group", "ql810wc"),
                           ("Restart", "always"), ("RestartPreventExitStatus", "64 78"), ("PrivateDevices", "yes"),
                           ("NoNewPrivileges", "yes"), ("ProtectSystem", "strict")):
            self.assertEqual(unit.get(key), [value], key)
        # Settings and OID-map changes are applied in the running AgentX session: systemd
        # sends SIGHUP itself (ReloadSignal= default) and waits for RELOADING=1, then READY=1.
        # An ExecReload= as well would deliver a second SIGHUP and a second reload.
        self.assertNotIn("ExecReload", unit)
        self.assertNotIn("ReloadSignal", unit)
        # A crash leaves snmpd answering noSuchName until the agent is back: restart
        # fast, and never give up (RestartSec=1 alone would hit the default start limit).
        self.assertEqual(unit.get("RestartSec"), ["1"])
        self.assertEqual(unit.get("StartLimitIntervalSec"), ["0"])
        self.assertLess(text.index("StartLimitIntervalSec"), text.index("[Service]"), "a [Unit] setting")
        self.assertTrue({"network-online.target", "snmpd.service", "ql810wc-proxyd.service"}
                        <= set(unit["After"][0].split()))
        self.assertIn("snmpd.service", unit["Wants"][0].split())
        self.assertEqual(unit.get("WantedBy"), ["multi-user.target"])

    def test_snmpd_drop_in_pass_persist_lines(self):
        script = SNMP_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('AGENT_SCRIPT="$QLP_LIB_DIR/brother_snmp_agent.py"', script)
        lines = re.findall(r'^pass_persist (\S+) \$QLP_PYTHON -I -B \$AGENT_SCRIPT (.*?)"?$', script, re.M)
        self.assertEqual(sorted(oid for oid, _ in lines), [".1.3.6.1.2.1.43", ".1.3.6.1.4.1.2435"])
        for _, rest in lines:
            args, subtrees = entry_point.parse_args(rest.split())
            self.assertEqual((args.mode, subtrees), ("pass_persist", persona.PASS_PERSIST_SUBTREES))

    def test_usage_errors(self):
        for argv in (["--mode", "agentx", ".1.3.6.1.4.1.2435"], ["--mode", "pass_persist", ".1.3.6.1.2.1.1"],
                     ["--mode", "pass_persist", "1.3.x"], ["--context", "bad context"], ["--mode", "snmp"],
                     ["--mo", "agentx"], ["--check", "--dump"]):
            with self.subTest(argv=argv), self.assertRaises(entry_point.UsageError):
                entry_point.parse_args(argv)
        args, subtrees = entry_point.parse_args(["--mode=pass_persist", ".1.3.6.1.4.1.2435.3"])
        self.assertEqual(subtrees, ((1, 3, 6, 1, 4, 1, 2435, 3),))

    def test_pass_persist_detection(self):
        detect = entry_point._wants_pass_persist
        self.assertTrue(detect(["--mode", "pass_persist"]))
        self.assertTrue(detect(["--config", "x", "--mode=pass_persist"]))
        self.assertFalse(detect(["--mode", "agentx"]))
        self.assertFalse(detect(["--mode"]))
        self.assertFalse(detect([]))


class ChildProcessTest(unittest.TestCase):
    """The entry script as a child process (python3 -I -B, as systemd and snmpd run it)."""

    def setUp(self):
        self.tmp = short_tempdir("qlcli")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.map_path, self.oid_map = write_child_map(self.tmp)
        self.runtime, self.identity = write_runtime(self.tmp)
        self.state = write_live_state(self.tmp)

    def run_agent(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", "-B", str(AGENT_SCRIPT), *args], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False)

    def common(self, **overrides: str) -> List[str]:
        options = {"--config": self.runtime, "--identity": self.identity, "--state": self.state,
                   "--oid-map": self.map_path}
        options.update(overrides)
        return [item for pair in options.items() for item in pair]

    def test_check(self):
        out = self.run_agent("--check", *self.common())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"{len(self.oid_map.entries)} entries, model QL-810W", out.stdout)

    def test_dump(self):
        out = self.run_agent("--dump", *self.common())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("# printer live: 62mm continuous black/red (DK-22251), idle", out.stdout)
        values = snmp_values(out.stdout)
        self.assertEqual(hex_of(values[".1.3.6.1.4.1.2435.3.3.9.1.6.1.0"]), raw(DK22251))
        self.assertIn("MDL:QL-810W;", values[".1.3.6.1.4.1.2435.2.3.9.1.1.7.0"])

    def test_configuration_errors_exit_78(self):
        bad_runtime = os.path.join(self.tmp, "bad.env")
        with open(bad_runtime, "w", encoding="utf-8") as fh:
            fh.write('STATUS_POLL_SECONDS="soon"\n')
        out = self.run_agent("--check", *self.common(**{"--config": bad_runtime}))
        self.assertEqual(out.returncode, 78)
        self.assertIn("STATUS_POLL_SECONDS", out.stderr)
        bad_map = os.path.join(self.tmp, "broken.json")
        with open(bad_map, "w", encoding="utf-8") as fh:
            fh.write('{"schema_version": 2}')
        out = self.run_agent("--mode", "agentx", *self.common(**{"--oid-map": bad_map}))
        self.assertEqual(out.returncode, 78)
        self.assertIn("schema_version must be 1", out.stderr)
        mac_runtime = os.path.join(self.tmp, "mac.env")
        with open(mac_runtime, "w", encoding="utf-8") as fh:
            fh.write('PERSONA_MAC="00:00:00:00:00:00"\n')
        out = self.run_agent("--dump", *self.common(**{"--config": mac_runtime}))
        self.assertEqual(out.returncode, 78)
        self.assertIn("PERSONA_MAC", out.stderr)

    def test_usage_error_exits_64(self):
        out = self.run_agent("--mode", "agentx", ".1.3.6.1.4.1.2435")
        self.assertEqual(out.returncode, 64)
        self.assertIn("pass_persist", out.stderr)


class SystemdNotifierTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs unix sockets")
    def test_notify_over_a_datagram_socket(self):
        tmp = short_tempdir("qlnotify")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "notify")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(path)
            server.settimeout(5)
            notifier = entry_point.SystemdNotifier(path)
            self.assertTrue(notifier.enabled)
            self.assertTrue(notifier.notify("READY=1", "STATUS=serving"))
            self.assertEqual(server.recv(4096), b"READY=1\nSTATUS=serving")
            notifier.close()

    def test_disabled_without_a_usable_socket(self):
        for address in ("", "vsock:2:1234"):
            with self.subTest(address=address):
                notifier = entry_point.SystemdNotifier(address)
                self.assertFalse(notifier.enabled)
                self.assertFalse(notifier.notify("READY=1"))
        missing = entry_point.SystemdNotifier("/nonexistent/notify-socket")
        self.addCleanup(missing.close)
        self.assertFalse(missing.notify("READY=1"))   # an unreachable socket is not an error

    @unittest.skipUnless(sys.platform.startswith("linux"), "abstract unix sockets are Linux only")
    def test_abstract_namespace(self):
        name = f"ql810wc-test-{os.getpid()}"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind("\0" + name)
            server.settimeout(5)
            entry_point.SystemdNotifier("@" + name).notify("READY=1")
            self.assertEqual(server.recv(4096), b"READY=1")


def next_notification(sock: socket.socket, field: str, timeout: float, describe=lambda: "") -> List[str]:
    """The fields of the next sd_notify datagram on ``sock`` that carries ``field`` (others are skipped)."""
    deadline = time.monotonic() + timeout
    while True:
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            fields = sock.recv(65536).decode("utf-8").split("\n")
        except socket.timeout:
            raise AssertionError(f"no sd_notify {field} within {timeout} s\n{describe()}") from None
        if field in fields:
            return fields


def status_field(fields: List[str]) -> str:
    found = [f[len("STATUS="):] for f in fields if f.startswith("STATUS=")]
    return found[0] if found else ""


# ---------------------------------------------------------------------------
# Reload (SIGHUP) in this process
# ---------------------------------------------------------------------------
@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs unix sockets")
class AgentXReloadTest(unittest.TestCase):
    """AgentXService against a fake master (tests/test_agentx.py), in this process.

    A reload must serve new settings and a new OID map in the same AgentX
    session: closing it would make snmpd answer the status OID with noSuchName.
    """

    BROTHER = (1, 3, 6, 1, 4, 1, 2435)
    INTERFACES = (1, 3, 6, 1, 2, 1, 2)
    HOST_RESOURCES = (1, 3, 6, 1, 2, 1, 25)
    SYS_UPTIME = (1, 3, 6, 1, 2, 1, 1, 3, 0)
    SYS_LOCATION = (1, 3, 6, 1, 2, 1, 1, 6, 0)
    IF_PHYS_ADDRESS = (1, 3, 6, 1, 2, 1, 2, 2, 1, 6, 1)

    def setUp(self):
        from tests import test_agentx
        self.tax = test_agentx
        self.tmp = short_tempdir("qlrl")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.map_path = os.path.join(self.tmp, "oid-map.json")
        self.write_map()
        self.runtime, self.identity = write_runtime(self.tmp)
        self.state = write_live_state(self.tmp)
        self.sockets: "queue.Queue[socket.socket]" = queue.Queue()
        self.addCleanup(self._close_unused_sockets)
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(self.notify.close)
        self.notify_path = os.path.join(self.tmp, "notify")
        self.notify.bind(self.notify_path)
        quiet = logging.NullHandler()   # expected warnings stay out of the output; assertLogs still sees them
        logging.getLogger("ql810wc").addHandler(quiet)
        self.addCleanup(logging.getLogger("ql810wc").removeHandler, quiet)

    def write_map(self, without=()) -> None:
        """The test map as JSON, minus the entries under the subtrees ``without``."""
        entries = [e for e in TEST_MAP_DOC["entries"]
                   if not any(persona.within(agentx.parse_oid(e["oid"]), s) for s in without)]
        with open(self.map_path, "w", encoding="utf-8") as fh:
            json.dump(dict(TEST_MAP_DOC, entries=entries), fh)

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

    def master(self):
        ours, theirs = socket.socketpair()
        self.sockets.put(theirs)
        self.addCleanup(ours.close)
        return self.tax.FakeMaster(ours)

    def make_service(self, *extra: str):
        args, _ = entry_point.parse_args(["--mode", "agentx", "--socket", os.path.join(self.tmp, "master"),
                                          "--config", self.runtime, "--identity", self.identity,
                                          "--state", self.state, "--oid-map", self.map_path, *extra])
        cfg = config.load_runtime_config(self.runtime, identity_path=self.identity)
        the_persona = persona.Persona(persona.load_oid_map(self.map_path), cfg, self.state)
        return entry_point.AgentXService(args, the_persona, cfg,
                                         notifier=entry_point.SystemdNotifier(self.notify_path),
                                         connector=self._connector, ping_interval=60.0, response_timeout=2.0,
                                         backoff_min=0.05, backoff_max=0.2)

    def serve(self, service) -> None:
        """Run only the session thread; the test calls reload() itself."""
        stop = threading.Event()
        thread = threading.Thread(target=service.subagent.run_forever, args=(stop,), daemon=True)
        thread.start()

        def join():
            stop.set()
            thread.join(10)

        self.addCleanup(join)

    def get(self, m, session_id: int, oid):
        ax, P = self.tax.ax, self.tax.P
        reply = m.call(ax.Pdu(type=P.GET, session_id=session_id, context="printer", ranges=[ax.SearchRange(oid)]))
        self.assertEqual(reply.error, 0)
        return reply.varbinds[0]

    def test_reload_serves_new_settings_in_the_same_session(self):
        service = self.make_service("--log-level", "WARNING")
        self.serve(service)
        m = self.master()
        regs = m.accept(session_id=9, registrations=5)
        self.assertEqual(regs[0].subtree, self.BROTHER, "the status block's subtree registers first")
        self.assertTrue(service.subagent.ready.wait(5), service.subagent.status)
        self.assertEqual(self.get(m, 9, self.SYS_LOCATION).value, b"Label bench")
        time.sleep(0.3)
        uptime = self.get(m, 9, self.SYS_UPTIME).value
        self.assertGreaterEqual(uptime, 30)
        write_runtime(self.tmp, PERSONA_LOCATION="Shipping desk")
        self.assertTrue(service.reload())
        self.assertTrue(service.subagent.wait_applied(service.generation, 5))
        self.assertEqual(self.get(m, 9, self.SYS_LOCATION).value, b"Shipping desk")
        self.assertGreaterEqual(self.get(m, 9, self.SYS_UPTIME).value, uptime, "sysUpTime did not restart")
        self.assertEqual(self.get(m, 9, STATUS_OID).value, raw(DK22251))
        m.assert_silent(0.7)   # no Register, Unregister or Close, and no new session
        self.assertEqual(service.subagent.session_id, 9)
        self.assertIsNone(service.reload_error)
        self.assertNotIn("reload failed", service.status_line())

    def test_a_failed_reload_keeps_the_previous_persona(self):
        service = self.make_service("--log-level", "WARNING")
        self.serve(service)
        m = self.master()
        m.accept(session_id=9, registrations=5)
        self.assertTrue(service.subagent.ready.wait(5), service.subagent.status)
        with open(self.runtime, "w", encoding="utf-8") as fh:
            fh.write('STATUS_POLL_SECONDS="soon"\nPERSONA_LOCATION="Shipping desk"\n')
        with self.assertLogs("ql810wc.agent", logging.ERROR) as logs:
            self.assertFalse(service.reload())
        self.assertIn("still serving the previous settings and OID map", logs.output[0])
        self.assertIn("STATUS_POLL_SECONDS", logs.output[0])
        self.assertIn("last reload failed: invalid configuration", service.status_line())
        self.assertEqual(self.get(m, 9, self.SYS_LOCATION).value, b"Label bench")
        write_runtime(self.tmp, PERSONA_LOCATION="Shipping desk")
        with open(self.map_path, "w", encoding="utf-8") as fh:
            fh.write('{"schema_version": 2}')
        with self.assertLogs("ql810wc.agent", logging.ERROR) as logs:
            self.assertFalse(service.reload())
        self.assertIn("schema_version must be 1", logs.output[0])
        self.assertEqual(self.get(m, 9, self.SYS_LOCATION).value, b"Label bench")
        self.assertEqual(self.get(m, 9, STATUS_OID).value, raw(DK22251))
        self.write_map()
        self.assertTrue(service.reload())   # fixed: the next reload applies and clears the error
        self.assertIsNone(service.reload_error)
        self.assertEqual(self.get(m, 9, self.SYS_LOCATION).value, b"Shipping desk")
        m.assert_silent(0.5)
        self.assertEqual(service.subagent.session_id, 9)

    def test_a_changed_map_registers_new_subtrees_before_unregistering_dropped_ones(self):
        P = self.tax.P
        self.write_map(without=[self.INTERFACES])
        service = self.make_service("--log-level", "WARNING")
        self.serve(service)
        m = self.master()
        regs = m.accept(session_id=4, registrations=4)
        self.assertNotIn(self.INTERFACES, [r.subtree for r in regs])
        self.assertTrue(service.subagent.ready.wait(5), service.subagent.status)
        self.write_map(without=[self.HOST_RESOURCES])
        self.assertTrue(service.reload())
        reg = m.expect(P.REGISTER)
        self.assertEqual((reg.session_id, reg.context, reg.subtree), (4, "printer", self.INTERFACES))
        m.respond(reg)
        unreg = m.expect(P.UNREGISTER)
        self.assertEqual((unreg.session_id, unreg.context, unreg.subtree, unreg.priority),
                         (4, "printer", self.HOST_RESOURCES, 127))
        m.respond(unreg)
        self.assertTrue(service.subagent.wait_applied(service.generation, 5))
        self.assertEqual(self.get(m, 4, self.IF_PHYS_ADDRESS).value, MAC)
        self.assertEqual(self.get(m, 4, HR_PRINTER_STATUS_OID).type, VT.NO_SUCH_OBJECT)
        self.assertEqual(service.subagent.status, "session 4: all 4 registrations accepted")
        self.assertEqual([r.subtree for r in service.registrations],
                         [self.BROTHER, (1, 3, 6, 1, 2, 1, 1), self.INTERFACES, (1, 3, 6, 1, 2, 1, 43)])
        m.assert_silent(0.3)

    def test_a_reload_while_snmpd_is_away_applies_to_the_next_session(self):
        self.write_map(without=[self.INTERFACES])
        service = self.make_service("--log-level", "WARNING")
        self.serve(service)
        time.sleep(0.2)   # the subagent is retrying: no master yet
        self.write_map()
        self.assertTrue(service.reload())
        self.assertTrue(service.subagent.wait_applied(service.generation, 0))
        m = self.master()
        regs = m.accept(session_id=6, registrations=5)
        self.assertEqual(sorted(r.subtree for r in regs), sorted(persona.AGENTX_SUBTREES))
        self.assertTrue(service.subagent.ready.wait(5), service.subagent.status)

    def test_a_reload_applies_log_level_unless_given_on_the_command_line(self):
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)
        service = self.make_service()
        write_runtime(self.tmp, LOG_LEVEL="DEBUG")
        self.assertTrue(service.reload())
        self.assertEqual(root.level, logging.DEBUG)
        pinned = self.make_service("--log-level", "ERROR")
        write_runtime(self.tmp, LOG_LEVEL="WARNING")
        self.assertTrue(pinned.reload())
        self.assertEqual(root.level, logging.DEBUG, "--log-level wins over LOG_LEVEL")

    def test_run_brackets_a_reload_with_reloading_and_ready_and_stops_cleanly(self):
        P = self.tax.P
        service = self.make_service("--log-level", "WARNING")
        signals = entry_point.SignalFlags()
        result: List[int] = []
        thread = threading.Thread(target=lambda: result.append(service.run(signals)), daemon=True)
        thread.start()

        def stop():
            signals.stop = signal.SIGTERM
            thread.join(15)

        self.addCleanup(stop)
        m = self.master()
        m.accept(session_id=3, registrations=5)
        fields = next_notification(self.notify, "READY=1", 10)
        self.assertIn("session 3: all 5 registrations accepted", status_field(fields))
        write_runtime(self.tmp, PERSONA_LOCATION="Shipping desk")
        signals.reloads += 1
        fields = next_notification(self.notify, "RELOADING=1", 5)
        usec = [f.split("=", 1)[1] for f in fields if f.startswith("MONOTONIC_USEC=")]
        self.assertEqual(len(usec), 1)
        self.assertTrue(usec[0].isdigit(), usec)
        fields = next_notification(self.notify, "READY=1", 5)
        self.assertNotIn("reload failed", status_field(fields))
        self.assertEqual(self.get(m, 3, self.SYS_LOCATION).value, b"Shipping desk")
        with open(self.runtime, "w", encoding="utf-8") as fh:
            fh.write('STATUS_POLL_SECONDS="soon"\n')
        with self.assertLogs("ql810wc.agent", logging.ERROR):
            signals.reloads += 1
            next_notification(self.notify, "RELOADING=1", 5)
            fields = next_notification(self.notify, "READY=1", 5)   # systemd must not wait forever
        self.assertIn("last reload failed", status_field(fields))
        signals.stop = signal.SIGTERM
        m.respond(m.expect(P.CLOSE, timeout=5))
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [entry_point.EXIT_OK])
        next_notification(self.notify, "STOPPING=1", 2)

    def test_run_exits_when_the_session_thread_dies(self):
        service = self.make_service("--log-level", "WARNING")
        service.subagent.run_forever = lambda stop: None   # as if it had crashed
        with self.assertLogs("ql810wc.agent", logging.ERROR) as logs:
            self.assertEqual(service.run(entry_point.SignalFlags()), entry_point.EXIT_FAILURE)
        self.assertIn("stopped unexpectedly", logs.output[0])


@unittest.skipUnless(hasattr(signal, "SIGHUP"), "POSIX signals")
class SignalFlagsTest(unittest.TestCase):
    def test_handlers_only_record_the_request(self):
        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        for sig, handler in saved.items():
            self.addCleanup(signal.signal, sig, handler)
        flags = entry_point.SignalFlags()
        flags.install()

        def wait_for(predicate) -> bool:
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                time.sleep(0.01)   # signal handlers run on this (the main) thread
            return predicate()

        os.kill(os.getpid(), signal.SIGHUP)
        self.assertTrue(wait_for(lambda: flags.reloads == 1))
        os.kill(os.getpid(), signal.SIGHUP)
        self.assertTrue(wait_for(lambda: flags.reloads == 2))
        self.assertIsNone(flags.stop)
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(wait_for(lambda: flags.stop == signal.SIGTERM))
        os.kill(os.getpid(), signal.SIGINT)   # no KeyboardInterrupt; the first stop signal is kept
        time.sleep(0.05)
        self.assertEqual(flags.stop, signal.SIGTERM)


# ---------------------------------------------------------------------------
# AgentX, end to end
# ---------------------------------------------------------------------------
@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs unix sockets")
class AgentXEndToEndTest(unittest.TestCase):
    """brother_snmp_agent.py --mode agentx against a fake master (tests/test_agentx.py)."""

    def setUp(self):
        from tests import test_agentx
        self.tax = test_agentx
        self.tmp = short_tempdir("qlax")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.map_path, self.oid_map = write_child_map(self.tmp)
        self.runtime, self.identity = write_runtime(self.tmp)
        self.state = write_live_state(self.tmp)
        self.master_path = os.path.join(self.tmp, "master")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        self.listener.bind(self.master_path)
        self.listener.listen(1)
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(self.notify.close)
        self.notify.bind(os.path.join(self.tmp, "notify"))
        self.stderr = open(os.path.join(self.tmp, "agent.err"), "w+b")
        self.addCleanup(self.stderr.close)
        env = dict(os.environ, NOTIFY_SOCKET=os.path.join(self.tmp, "notify"))
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-B", str(AGENT_SCRIPT), "--mode", "agentx", "--socket", self.master_path,
             "--context", "printer", "--config", self.runtime, "--identity", self.identity,
             "--state", self.state, "--oid-map", self.map_path, "--log-level", "DEBUG"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.stderr, env=env)
        self.addCleanup(self._stop)

    def _stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)

    def agent_log(self) -> str:
        self.stderr.flush()
        self.stderr.seek(0)
        return self.stderr.read().decode("utf-8", "replace")

    def notification_with(self, field: str, timeout: float) -> List[str]:
        """The fields of the next sd_notify message that carries ``field``."""
        return next_notification(self.notify, field, timeout, self.agent_log)

    def connect(self, session_id: int = 42):
        """Accept the agent's connection, open its session and accept every registration."""
        P = self.tax.P
        self.listener.settimeout(30)
        try:
            conn, _ = self.listener.accept()
        except socket.timeout:
            self.fail(f"the agent did not connect\n{self.agent_log()}")
        master = self.tax.FakeMaster(conn)
        self.addCleanup(master.close)
        opened = master.expect(P.OPEN, timeout=30)
        self.assertIn("printer persona", opened.descr)
        master.respond(opened, session_id=session_id)
        subtrees = []
        for _ in persona.AGENTX_SUBTREES:
            reg = master.expect(P.REGISTER)
            self.assertEqual((reg.context, reg.session_id), ("printer", session_id))
            subtrees.append(reg.subtree)
            master.respond(reg)
        self.assertEqual(sorted(subtrees), sorted(persona.AGENTX_SUBTREES))
        self.assertEqual(subtrees[0], (1, 3, 6, 1, 4, 1, 2435), "the status block's subtree registers first")
        return master

    def getter(self, master, session_id: int = 42):
        ax, P = self.tax.ax, self.tax.P

        def get(oid, context="printer"):
            reply = master.call(ax.Pdu(type=P.GET, session_id=session_id, context=context,
                                       ranges=[ax.SearchRange(oid)]))
            self.assertEqual(reply.error, 0)
            return reply.varbinds[0]

        return get

    def stop_agent(self, master) -> str:
        """SIGTERM: Close, exit 0, STOPPING=1. Returns the agent's log."""
        ax, P = self.tax.ax, self.tax.P
        self.proc.send_signal(signal.SIGTERM)
        close = master.expect(P.CLOSE, timeout=10)
        self.assertEqual(close.reason, ax.CloseReason.SHUTDOWN)
        master.respond(close)
        self.assertEqual(self.proc.wait(15), 0, self.agent_log())
        self.notification_with("STOPPING=1", 5)
        log_text = self.agent_log()
        self.assertNotIn("Traceback", log_text)
        self.assertIn("SIGTERM received", log_text)
        return log_text

    def test_session(self):
        ax, P = self.tax.ax, self.tax.P
        master = self.connect()
        fields = self.notification_with("READY=1", 10)
        self.assertTrue(any(f.startswith("STATUS=") and "registrations accepted" in f for f in fields), fields)
        get = self.getter(master)

        status = get(STATUS_OID)
        self.assertEqual((status.type, status.value), (VT.OCTET_STRING, raw(DK22251)))
        self.assertIn(b"MDL:QL-810W;", get(IEEE1284_OID).value)
        self.assertEqual(get(HR_PRINTER_STATUS_OID).value, 3)
        self.assertEqual(get(STATUS_OID, context=None).type, VT.NO_SUCH_OBJECT)   # default context: not ours

        walked = []
        start = (1, 3, 6, 1)
        while True:
            reply = master.call(ax.Pdu(type=P.GETNEXT, session_id=42, context="printer",
                                       ranges=[ax.SearchRange(start)]))
            vb = reply.varbinds[0]
            if vb.type == VT.END_OF_MIB_VIEW:
                break
            walked.append(vb.oid)
            start = vb.oid
        self.assertEqual(walked, [e.oid for e in self.oid_map.entries])

        refused = master.call(ax.Pdu(type=P.TESTSET, session_id=42, context="printer",
                                     varbinds=[ax.VarBind(STATUS_OID, VT.OCTET_STRING, b"x")]))
        self.assertEqual(refused.error, ax.NOT_WRITABLE)
        self.stop_agent(master)

    def test_sighup_reloads_in_the_same_session(self):
        """systemctl reload: new settings in the open session; the status OID answers throughout."""
        sys_location = (1, 3, 6, 1, 2, 1, 1, 6, 0)
        sys_uptime = (1, 3, 6, 1, 2, 1, 1, 3, 0)
        master = self.connect()
        self.notification_with("READY=1", 10)
        get = self.getter(master)
        self.assertEqual(get(sys_location).value, b"Label bench")
        time.sleep(0.3)
        uptime = get(sys_uptime).value

        write_runtime(self.tmp, PERSONA_LOCATION="Shipping desk")
        self.proc.send_signal(signal.SIGHUP)
        fields = self.notification_with("RELOADING=1", 10)
        self.assertTrue(any(f.startswith("MONOTONIC_USEC=") for f in fields), fields)
        for _ in range(3):   # P-touch's burst while the agent reloads
            status = get(STATUS_OID)
            self.assertEqual((status.type, status.value), (VT.OCTET_STRING, raw(DK22251)))
        fields = self.notification_with("READY=1", 10)
        self.assertIn("registrations accepted", status_field(fields))
        self.assertEqual(get(sys_location).value, b"Shipping desk")
        self.assertGreaterEqual(get(sys_uptime).value, uptime, "sysUpTime did not restart")

        with open(self.runtime, "w", encoding="utf-8") as fh:   # an invalid file: the old persona stays
            fh.write('STATUS_POLL_SECONDS="soon"\n')
        self.proc.send_signal(signal.SIGHUP)
        self.notification_with("RELOADING=1", 10)
        fields = self.notification_with("READY=1", 10)
        self.assertIn("last reload failed", status_field(fields))
        self.assertEqual(get(sys_location).value, b"Shipping desk")
        self.assertEqual(get(STATUS_OID).value, raw(DK22251))
        master.assert_silent(0.5)   # no Unregister, no Close: one session throughout
        self.assertIsNone(self.proc.poll(), self.agent_log())
        log_text = self.stop_agent(master)
        self.assertIn("reloaded: printer persona", log_text)
        self.assertIn("reload failed; still serving the previous settings and OID map", log_text)
        self.assertEqual(log_text.count("AgentX session 42 open"), 1, log_text)

    def test_waits_for_the_master_and_exits_on_sigterm(self):
        time.sleep(1.0)   # the agent is retrying: nothing accepts on the socket yet
        self.assertIsNone(self.proc.poll(), self.agent_log())
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(15), 0, self.agent_log())

    def test_sighup_before_ready_is_harmless(self):
        """A reload requested while the agent still waits for snmpd neither kills it nor fakes READY=1."""
        time.sleep(1.0)
        self.proc.send_signal(signal.SIGHUP)
        time.sleep(0.5)
        self.assertIsNone(self.proc.poll(), self.agent_log())
        master = self.connect()
        fields = self.notification_with("READY=1", 10)
        self.assertNotIn("RELOADING=1", fields)
        self.assertIn("registrations accepted", status_field(fields))
        log_text = self.stop_agent(master)
        self.assertIn("reloaded: printer persona", log_text)


# ---------------------------------------------------------------------------
# Real net-snmp master
# ---------------------------------------------------------------------------
class RealSnmpdAgentXTest(unittest.TestCase):
    """snmpd (VACM + AgentX master) with the persona in context "printer", as installed."""

    HOST = "ql810wc-host"

    @classmethod
    def setUpClass(cls):
        if sys.platform == "darwin":
            raise unittest.SkipTest("Apple's snmpd has no VACM; community->context mapping needs Linux net-snmp")
        if not hasattr(socket, "AF_UNIX"):
            raise unittest.SkipTest("needs unix sockets")
        tools = PrivateSnmpd.find_tools()
        if tools is None:
            raise unittest.SkipTest("net-snmp (snmpd and client tools) is not installed")
        if not PrivateSnmpd.has_vacm(tools["snmpd"]):
            raise unittest.SkipTest("this snmpd has no VACM")
        cls.tmp = short_tempdir("qlrs")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.agentx_path = os.path.join(cls.tmp, "agentx.sock")
        views = "\n".join(f"view printerView included .{agentx.format_oid(s)}" for s in persona.AGENTX_SUBTREES)
        conf = "\n".join([
            "# Private test instance written by tests/test_persona.py (mirrors scripts/30-snmp-agent.sh)",
            "master agentx",
            f"agentXSocket {cls.agentx_path}",
            "agentXTimeout 3",
            views,
            "com2sec -Cn printer ql810wcPersona 127.0.0.1 public",
            "com2sec -Cn printer ql810wcPersona 127.0.0.1 internal",
            f"com2sec hostSec 127.0.0.1 {cls.HOST}",
            "group ql810wcPersona v1 ql810wcPersona",
            "group ql810wcPersona v2c ql810wcPersona",
            "group hostGrp v2c hostSec",
            "view allView included .1",
            "access ql810wcPersona printer any noauth exact printerView none none",
            'access hostGrp "" any noauth exact allView none none',
            "",
        ])
        cls.snmpd = PrivateSnmpd(tools, cls.tmp, conf)
        cls.addClassCleanup(cls.snmpd.stop)
        try:
            cls.snmpd.start(cls.HOST)
        except SnmpdUnavailable as exc:
            if not is_root() or selinux_enforcing():
                raise unittest.SkipTest(f"cannot run a private snmpd here: {exc}") from None
            raise AssertionError(str(exc)) from None
        cls.map_path, cls.oid_map = write_child_map(cls.tmp)
        cls.runtime, cls.identity = write_runtime(cls.tmp)
        cls.state = write_live_state(cls.tmp)
        cls.err = open(os.path.join(cls.tmp, "agent.err"), "wb")
        cls.addClassCleanup(cls.err.close)
        cls.agent = subprocess.Popen(
            [sys.executable, "-I", "-B", str(AGENT_SCRIPT), "--mode", "agentx", "--socket", cls.agentx_path,
             "--config", cls.runtime, "--identity", cls.identity, "--state", cls.state,
             "--oid-map", cls.map_path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=cls.err)
        cls.addClassCleanup(cls._stop_agent)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            out = cls.snmpd.run("snmpget", "1", "internal", "." + agentx.format_oid(STATUS_OID), timeout=1, retries=0)
            if "Hex-STRING" in out.stdout:
                return
            time.sleep(0.3)
        raise AssertionError(f"the persona did not register with snmpd\n{cls.snmpd.log_tail()}\n{cls._agent_log()}")

    @classmethod
    def _stop_agent(cls):
        if cls.agent.poll() is None:
            cls.agent.terminate()
            try:
                cls.agent.wait(15)
            except subprocess.TimeoutExpired:
                cls.agent.kill()
                cls.agent.wait(5)

    @classmethod
    def _agent_log(cls) -> str:
        try:
            with open(os.path.join(cls.tmp, "agent.err"), encoding="utf-8", errors="replace") as fh:
                return fh.read()[-3000:]
        except OSError:
            return ""

    def test_ptouch_mac_reads(self):
        """P-touch for Mac: SNMPv1, community "internal", one varbind per GET."""
        out = self.snmpd.run("snmpget", "1", "internal", "." + agentx.format_oid(STATUS_OID))
        self.assertEqual(hex_of(snmp_values(out.stdout)["." + agentx.format_oid(STATUS_OID)]), raw(DK22251))
        out = self.snmpd.run("snmpget", "1", "internal", "." + agentx.format_oid(IEEE1284_OID))
        self.assertIn("MDL:QL-810W;", out.stdout)
        out = self.snmpd.run("snmpget", "1", "internal", "." + agentx.format_oid(HR_PRINTER_STATUS_OID))
        self.assertIn("INTEGER: 3", out.stdout)

    def test_discovery_bundles_in_one_v1_pdu(self):
        for bundle in (WIN_INSTALLER_BUNDLE, MOBILE_BUNDLE):
            with self.subTest(bundle=bundle):
                out = self.snmpd.run("snmpget", "1", "public", *("." + oid for oid in bundle))
                self.assertEqual(out.returncode, 0, out.stdout)
                self.assertNotIn("Error in packet", out.stdout)
                self.assertEqual(sorted(snmp_values(out.stdout)), sorted("." + oid for oid in bundle))

    def test_walk_of_the_printer_context_is_the_persona(self):
        out = self.snmpd.run("snmpwalk", "2c", "public", ".1")
        self.assertEqual(snmp_oids(out.stdout), ["." + e.dotted for e in self.oid_map.entries], out.stdout)

    def test_host_context_does_not_see_the_persona(self):
        out = self.snmpd.run("snmpget", "2c", self.HOST, "." + agentx.format_oid(STATUS_OID))
        self.assertIn("No Such Object", out.stdout)

    def test_zz_reload_never_leaves_the_status_oid_unanswered(self):
        """P-touch's SNMPv1 GET keeps getting the status block while the agent reloads (SIGHUP).

        A restart would close the AgentX session, and net-snmp would answer
        noSuchName until the new process registered, failing the print.
        """
        oid = "." + agentx.format_oid(STATUS_OID)
        failures = []
        gets = 0
        for _ in range(3):
            self.agent.send_signal(signal.SIGHUP)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                out = self.snmpd.run("snmpget", "1", "internal", oid, timeout=1, retries=0)
                gets += 1
                if "Hex-STRING" not in out.stdout:
                    failures.append(out.stdout.strip())
        self.assertEqual(failures, [], f"{len(failures)} of {gets} GETs failed\n{self._agent_log()}")
        self.assertIsNone(self.agent.poll(), self._agent_log())
        self.assertIn("reloaded: printer persona", self._agent_log())


if __name__ == "__main__":
    unittest.main()
