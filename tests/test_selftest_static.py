"""selftest.sh without the installed system: syntax, options, the embedded
helpers, and one complete run on a host where nothing is installed.

* The embedded Python helpers (PY_STATE, PY_T3, PY_OIDMAP, PY_AVAHI,
  PY_VERDICT) are taken out of selftest.sh and run directly, so the parsing,
  status-block comparison and tier logic are tested everywhere.
* The shell tests need bash >= 4.2 (what scripts/lib/common.sh requires) and
  are skipped without it. macOS ships bash 3.2: point QLP_TEST_BASH at a newer
  bash to run them there; CI runs them on Fedora.

Nothing here touches the system: the full run is read-only and is skipped
where ql810wc-print-proxy is installed (run ./selftest.sh itself there).
Addresses are documentation ranges; the UUID is made up.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Dict, List, Optional, Sequence

from tests._util import REPO

AGENT_DIR = REPO / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from ql810wc import media, statecache  # noqa: E402
from ql810wc.rasterproto import Status  # noqa: E402

SELFTEST = REPO / "selftest.sh"
QLCTL = AGENT_DIR / "qlctl"
UUID = "00000000-0000-4000-8000-000000000001"
OTHER_UUID = "11111111-1111-4111-8111-111111111111"
STATUS_OID = ".1.3.6.1.4.1.2435.3.3.9.1.6.1.0"
FIRMWARE_NOTE = ("NOTE: identity strings in oid-map.yaml (sysDescr, sysObjectID, ...) are not yet verified "
                 "on a real QL-810Wc; P-touch Editor for Mac and iPrint&Label for iOS work with them as they are")
OPTIONS = ("--json", "--quiet", "--print-test", "--mono", "--no-color")
INSTALLED_PATHS = ("/usr/local/libexec/ql810wc-proxy", "/etc/ql810wc-proxy")

IDLE_DK22251 = media.synthesize_status(media.lookup_label("62red")).raw
IDLE_DK22205 = media.synthesize_status(media.lookup_label("62")).raw
IDLE_DK11201 = media.synthesize_status(media.lookup_label("29x90")).raw


def _bash_version(bash: str) -> tuple:
    out = subprocess.run([bash, "-c", 'printf "%s %s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
                         capture_output=True, text=True, timeout=10, check=True).stdout
    return tuple(int(part) for part in out.split())


def find_bash() -> Optional[str]:
    """A bash >= 4.2: $QLP_TEST_BASH when set, else the one on PATH."""
    for bash in (os.environ.get("QLP_TEST_BASH"), shutil.which("bash")):
        if not bash:
            continue
        try:
            if _bash_version(bash) >= (4, 2):
                return bash
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return None


BASH = find_bash()


def helper(name: str) -> str:
    """The Python source of an embedded helper (NAME="$(cat <<'PY' ... PY )")."""
    text = SELFTEST.read_text(encoding="utf-8")
    match = re.search(r"^%s=\"\$\(cat <<'PY'\n(.*?)\nPY\n\)\"$" % re.escape(name), text, re.M | re.S)
    if match is None:
        raise AssertionError(f"selftest.sh has no embedded helper {name}")
    return match.group(1)


def run_helper(name: str, args: Sequence[str], stdin: str = "") -> Dict[str, str]:
    """Run an embedded helper as selftest.sh does and parse its key<TAB>value lines."""
    done = subprocess.run([sys.executable, "-B", "-c", helper(name), *args], input=stdin,
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise AssertionError(f"{name} failed ({done.returncode}): {done.stderr}")
    values: Dict[str, str] = {}
    for line in done.stdout.splitlines():
        key, _, value = line.partition("\t")
        values[key] = value
    return values


def snmp_hex(frame: bytes) -> str:
    """snmpget -On -Ox output for the status OID, wrapped as net-snmp prints it."""
    text = " ".join("%02X" % b for b in frame)
    return f"{STATUS_OID} = Hex-STRING: {text[:47]} \n{text[48:]} \n"


def state_doc(frame: Optional[bytes] = IDLE_DK22251, *, device_state: str = "ready", polled_ago: float = 1.0,
              job: Optional[dict] = None) -> dict:
    now = time.time()
    doc = statecache.new_state(pid=os.getpid(), raw_port=9100, started=now - 3700)
    doc["device"].update(state=device_state, vid="04f9", pid="209c", product="QL-810W", manufacturer="Brother",
                         serial="FAKE0SERIAL1", node="/dev/usb/lp0", transport="usblp",
                         ieee1284_id="MFG:Brother;CMD:PT-CBP,URF;MDL:QL-810W;CLS:PRINTER;", detail="ok")
    if frame is not None:
        doc["status"] = {"raw_hex": frame.hex(), "polled": now - polled_ago, "parsed": Status(frame).to_dict(),
                         "source": "reply"}
    doc["job"].update(last_result="ok", peer="192.0.2.10:50000", bytes=19000, two_color=True,
                      last_finished=now - 300)
    if job:
        doc["job"].update(job)
    return doc


class HelperExtractionTest(unittest.TestCase):
    def test_executable(self):
        # install.sh runs "$QLP_REPO_ROOT/selftest.sh" directly.
        self.assertTrue(os.access(SELFTEST, os.X_OK), "selftest.sh must be executable")
        self.assertTrue(SELFTEST.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n"))

    def test_every_helper_compiles(self):
        for name in ("PY_STATE", "PY_T3", "PY_OIDMAP", "PY_AVAHI", "PY_VERDICT"):
            with self.subTest(name=name):
                compile(helper(name), name, "exec")


class StateHelperTest(unittest.TestCase):
    """PY_STATE reads what "qlctl status --json" prints."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.config = self.dir / "runtime.env"
        self.config.write_text('STATUS_POLL_SECONDS="3"\nSTATUS_MAX_AGE_SECONDS="60"\n', encoding="utf-8")
        self.state = self.dir / "state.json"

    def summarize(self, doc: Optional[dict], *, updated_ago: float = 0.0) -> Dict[str, str]:
        if doc is not None:
            statecache.StateCache(self.state).write(doc)
            if updated_ago:
                written = json.loads(self.state.read_text(encoding="utf-8"))
                written["updated"] = time.time() - updated_ago
                self.state.write_text(json.dumps(written), encoding="utf-8")
        done = subprocess.run([sys.executable, "-I", "-B", str(QLCTL), "--config", str(self.config),
                               "--state", str(self.state), "status", "--json"],
                              capture_output=True, text=True, timeout=60)
        return run_helper("PY_STATE", [str(AGENT_DIR), "3"], done.stdout)

    def test_ready_two_color_roll(self):
        kv = self.summarize(state_doc())
        self.assertEqual(kv["available"], "1")
        self.assertEqual(kv["fresh"], "1")
        self.assertEqual(kv["fresh_limit"], "9.0")
        self.assertEqual(kv["device_state"], "ready")
        self.assertEqual(kv["device_mdl"], "QL-810W")
        self.assertEqual(kv["status_valid"], "1")
        self.assertEqual(kv["status_hex"], IDLE_DK22251.hex())
        self.assertEqual((kv["series_code"], kv["model_code"], kv["model_name"]), ("34", "39", "QL-810W"))
        self.assertEqual(kv["errors"], "")
        self.assertEqual((kv["has_media"], kv["media_known"], kv["media_two_color"]), ("1", "1", "1"))
        self.assertEqual(kv["media_desc"], "62mm continuous black/red (DK-22251)")
        self.assertEqual(kv["print_roll"], "two-color")
        self.assertEqual((kv["job_active"], kv["job_last_result"], kv["job_two_color"]), ("0", "ok", "1"))
        self.assertEqual(kv["proxy_pid"], str(os.getpid()))

    def test_no_serial_in_the_summary(self):
        kv = self.summarize(state_doc())
        self.assertNotIn("FAKE0SERIAL1", "\n".join(kv.values()))

    def test_stale_state(self):
        kv = self.summarize(state_doc(), updated_ago=60)
        self.assertEqual(kv["fresh"], "0")

    def test_old_status_while_idle_is_stale(self):
        kv = self.summarize(state_doc(polled_ago=120))
        self.assertEqual(kv["stale"], "1")
        self.assertEqual(kv["status_fresh"], "0")

    def test_old_status_during_a_job_is_not_stale(self):
        kv = self.summarize(state_doc(polled_ago=120, job={"active": True, "started": time.time() - 100}))
        self.assertEqual(kv["stale"], "0")
        self.assertEqual(kv["status_fresh"], "1")
        self.assertEqual(kv["job_active"], "1")

    def test_rolls_for_the_test_label(self):
        self.assertEqual(self.summarize(state_doc(IDLE_DK22205))["print_roll"], "mono")
        kv = self.summarize(state_doc(IDLE_DK11201))
        self.assertEqual((kv["print_roll"], kv["media_two_color"]), ("die-cut", "0"))
        self.assertEqual(kv["media_desc"], "29mm x 90mm die-cut (DK-11201)")

    def test_cover_open(self):
        frame = bytearray(IDLE_DK22251)
        frame[9], frame[18] = 0x10, 0x02
        kv = self.summarize(state_doc(bytes(frame)))
        self.assertEqual(kv["errors"], "cover open")
        self.assertEqual(kv["media_known"], "1")   # the media bytes stay valid with the cover open

    def test_no_roll(self):
        frame = bytearray(IDLE_DK22251)
        frame[10] = frame[11] = frame[14] = 0
        kv = self.summarize(state_doc(bytes(frame)))
        self.assertEqual((kv["has_media"], kv["media_two_color"], kv["print_roll"]), ("0", "0", "none"))

    def test_editor_lite(self):
        kv = self.summarize(state_doc(None, device_state="mass-storage"))
        self.assertEqual(kv["device_state"], "mass-storage")
        self.assertEqual(kv["status_valid"], "0")
        self.assertIn("no status block", kv["status_problem"])

    def test_missing_state(self):
        kv = self.summarize(None)
        self.assertEqual(kv["available"], "0")
        self.assertIn("is ql810wc-proxyd running?", kv["problem"])

    def test_not_json(self):
        kv = run_helper("PY_STATE", [str(AGENT_DIR), "3"], "Traceback (most recent call last):\n")
        self.assertEqual(kv["available"], "0")
        self.assertEqual(kv["problem"], "qlctl printed no status document")


class StatusBlockHelperTest(unittest.TestCase):
    """PY_T3 compares the block P-touch reads over SNMP with the proxy's."""

    def t3(self, out: str, *snapshots: str, err: str = "", model: str = "QL-810W") -> Dict[str, str]:
        return run_helper("PY_T3", [str(AGENT_DIR), model, err, *snapshots], out)

    def test_exact(self):
        kv = self.t3(snmp_hex(IDLE_DK22251), IDLE_DK22251.hex() + ":0", IDLE_DK22251.hex() + ":0")
        self.assertEqual((kv["snmp_ok"], kv["match"], kv["model_ok"]), ("1", "exact", "1"))
        self.assertEqual(kv["media_desc"], "62mm continuous black/red (DK-22251)")
        self.assertEqual(kv["snmp_hex"], IDLE_DK22251.hex(" "))

    def test_either_snapshot_matches(self):
        kv = self.t3(snmp_hex(IDLE_DK22251), IDLE_DK11201.hex() + ":0", IDLE_DK22251.hex() + ":0")
        self.assertEqual(kv["match"], "exact")

    def test_synthesised_phase_only_during_a_job(self):
        busy = bytearray(IDLE_DK22251)
        busy[19] = 0x01
        self.assertEqual(self.t3(snmp_hex(bytes(busy)), IDLE_DK22251.hex() + ":1")["match"], "job")
        kv = self.t3(snmp_hex(bytes(busy)), IDLE_DK22251.hex() + ":0")
        self.assertEqual((kv["match"], kv["diff"]), ("mismatch", "19"))

    def test_media_bytes_never_excused(self):
        other = bytearray(IDLE_DK22251)
        other[19], other[25] = 0x01, 0x01
        kv = self.t3(snmp_hex(bytes(other)), IDLE_DK22251.hex() + ":1")
        self.assertEqual((kv["match"], kv["diff"]), ("mismatch", "19,25"))

    def test_offline_block(self):
        off = bytearray(32)
        off[0:5] = b"\x80\x20\x42\x34\x39"
        off[5], off[6], off[8], off[18] = 0x30, 0x04, 0x20, 0x02
        kv = self.t3(snmp_hex(bytes(off)), IDLE_DK22251.hex() + ":0")
        self.assertEqual((kv["match"], kv["has_media"]), ("offline", "0"))
        self.assertEqual(self.t3(snmp_hex(bytes(off)), ":0")["match"], "offline")

    def test_no_answer(self):
        kv = self.t3("", IDLE_DK22251.hex() + ":0", err="Timeout: No Response from 127.0.0.1.\n")
        self.assertEqual((kv["snmp_ok"], kv["problem"]), ("0", "no answer (timeout)"))

    def test_no_such_name(self):
        err = ("Error in packet\nReason: (noSuchName) There is no such variable name in this MIB.\n"
               f"Failed object: {STATUS_OID}\n")
        kv = self.t3("", IDLE_DK22251.hex() + ":0", err=err)
        self.assertEqual(kv["snmp_ok"], "0")
        self.assertIn("noSuchName", kv["problem"])

    def test_wrong_length_and_header(self):
        kv = self.t3(f"{STATUS_OID} = Hex-STRING: 80 20 42 34 \n", IDLE_DK22251.hex() + ":0")
        self.assertEqual(kv["snmp_ok"], "0")
        self.assertIn("4 bytes", kv["problem"])
        kv = self.t3(snmp_hex(bytes(32)), IDLE_DK22251.hex() + ":0")
        self.assertEqual(kv["snmp_ok"], "0")
        self.assertIn("not a Brother status block", kv["problem"])

    def test_stderr_never_parsed_as_bytes(self):
        kv = self.t3(snmp_hex(IDLE_DK22251), IDLE_DK22251.hex() + ":0", err="Cannot find module (FOO)\n")
        self.assertEqual(kv["match"], "exact")

    def test_other_model(self):
        frame = bytearray(IDLE_DK22251)
        frame[4] = 0x41   # QL-820NWB
        kv = self.t3(snmp_hex(bytes(frame)), bytes(frame).hex() + ":0")
        self.assertEqual((kv["match"], kv["model_ok"], kv["model_name"]), ("exact", "0", "QL-820NWB"))

    def test_manual_media_type_byte(self):
        frame = bytearray(IDLE_DK22251)
        frame[11] = 0x4A   # the manual's encoding; real printers report 0x0A
        kv = self.t3(snmp_hex(bytes(frame)), bytes(frame).hex() + ":0")
        self.assertEqual((kv["media_encoding_ok"], kv["media_known"]), ("0", "0"))

    def test_die_cut(self):
        kv = self.t3(snmp_hex(IDLE_DK11201), IDLE_DK11201.hex() + ":0")
        self.assertEqual((kv["media_known"], kv["media_label"]), ("1", "29x90"))


class OidMapHelperTest(unittest.TestCase):
    def setUp(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is not installed in this interpreter")

    def test_shipped_map(self):
        kv = run_helper("PY_OIDMAP", [str(AGENT_DIR), str(AGENT_DIR / "oid-map.yaml"), ""])
        self.assertEqual((kv["ok"], kv["model"], kv["status_entry"], kv["firmware_match"]),
                         ("1", "QL-810W", "1", "0"))
        self.assertGreater(int(kv["entries"]), 0)

    def test_invalid_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "oid-map.yaml"
            path.write_text("schema_version: 2\n", encoding="utf-8")
            kv = run_helper("PY_OIDMAP", [str(AGENT_DIR), str(path), ""])
        self.assertEqual(kv["ok"], "0")
        self.assertIn("schema_version", kv["problem"])


AVAHI_FILE = f"""<?xml version="1.0" standalone="no"?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="no">Brother QL-810W [printhost]</name>
  <service protocol="any">
    <type>_pdl-datastream._tcp</type>
    <port>9100</port>
    <txt-record>usb_MFG=Brother</txt-record>
    <txt-record>usb_MDL=QL-810W</txt-record>
    <txt-record>UUID={UUID}</txt-record>
    <txt-record>note=Front &amp; back</txt-record>
  </service>
</service-group>
"""
NAME = r"Brother\032QL-810W\032\091printhost\093"
TXT = f'"note=Front & back" "UUID={UUID}" "usb_MDL=QL-810W" "usb_MFG=Brother"'
BROWSE = (f"+;eth0;IPv4;{NAME};_pdl-datastream._tcp;local\n"
          f"=;eth0;IPv4;{NAME};_pdl-datastream._tcp;local;printhost.local;192.0.2.10;9100;{TXT}\n"
          f"=;eth0;IPv6;{NAME};_pdl-datastream._tcp;local;printhost.local;2001:db8::10;9100;{TXT}\n"
          "=;eth0;IPv4;Brother\\032QL-820NWB;_pdl-datastream._tcp;local;other.local;192.0.2.20;9100;"
          f'"usb_MDL=QL-820NWB" "UUID={OTHER_UUID}"\n')


class AvahiHelperTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = pathlib.Path(tmp.name) / "ql810wc-proxy.service"
        self.file.write_text(AVAHI_FILE, encoding="utf-8")

    def avahi(self, browse: str = "", *, uuid: str = UUID, port: str = "9100", ran: str = "1") -> Dict[str, str]:
        return run_helper("PY_AVAHI", [str(self.file), uuid, port, ran], browse)

    def test_published_and_resolved(self):
        kv = self.avahi(BROWSE)
        self.assertEqual((kv["file_ok"], kv["file_name"]), ("1", "Brother QL-810W [printhost]"))
        self.assertEqual((kv["browse_found"], kv["browse_name"]), ("1", "Brother QL-810W [printhost]"))
        self.assertEqual((kv["browse_protocols"], kv["browse_mdl_ok"], kv["browse_port_ok"]),
                         ("IPv4, IPv6", "1", "1"))
        self.assertEqual(kv["browse_others"], "1")

    def test_renamed_by_avahi(self):
        kv = self.avahi(BROWSE.replace(r"\093;", r"\093\032#2;"))
        self.assertEqual(kv["browse_name"], "Brother QL-810W [printhost] #2")

    def test_other_uuid_is_not_ours(self):
        kv = self.avahi(BROWSE, uuid=OTHER_UUID)
        self.assertEqual(kv["file_ok"], "0")
        self.assertIn("UUID", kv["file_problem"])
        self.assertEqual((kv["browse_found"], kv["browse_mdl_ok"]), ("1", "0"))

    def test_wrong_port_and_model(self):
        self.file.write_text(AVAHI_FILE.replace("usb_MDL=QL-810W", "usb_MDL=QL-810Wc"), encoding="utf-8")
        kv = self.avahi(port="9101", ran="0")
        self.assertEqual(kv["file_ok"], "0")
        self.assertIn("port 9100, expected 9101", kv["file_problem"])
        self.assertIn("usb_MDL=QL-810Wc", kv["file_problem"])
        self.assertEqual(kv["browse_found"], "0")

    def test_missing_file(self):
        self.file.unlink()
        kv = self.avahi(ran="0")
        self.assertEqual(kv["file_ok"], "0")
        self.assertIn("cannot read", kv["file_problem"])


class VerdictHelperTest(unittest.TestCase):
    """PY_VERDICT: the tier ladder from the check records."""

    def verdict(self, records: Sequence[str]) -> tuple:
        with tempfile.TemporaryDirectory() as tmp:
            path, out = pathlib.Path(tmp) / "records", pathlib.Path(tmp) / "summary.json"
            path.write_text("".join(r.replace("|", "\t") + "\n" for r in records), encoding="utf-8")
            done = subprocess.run([sys.executable, "-B", "-c", helper("PY_VERDICT"), str(path), str(out),
                                   "root=1", "print_test=0", "mono=0", "version=0.1.0"],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            doc = json.loads(out.read_text(encoding="utf-8"))
        lines: Dict[str, List[str]] = {}
        for line in done.stdout.splitlines():
            kind, _, rest = line.partition("\t")
            lines.setdefault(kind, []).append(rest)
        return lines, doc

    HEALTHY = ("setup|pass|setup.code|installed", "t0|pass|t0.socket|active", "t0|info|t0.job|last job ok",
               "t1|pass|t1.roll|DK-22251", "t1|skip|t1.print|not printed", "t2|pass|t2.snmpd|active",
               "t2|pass|t2.leak.public|persona only", "t3|pass|t3.status|exact", "t3|info|t3.firmware|none yet",
               "host|pass|host.v3|host sysDescr")

    def test_healthy_box(self):
        lines, doc = self.verdict(self.HEALTHY)
        self.assertEqual(lines["TIER"], ["TIER REACHED: T3 (+host SNMP)"])
        self.assertEqual(lines["EXIT"], ["0"])
        # Unverified identity strings are a remark, not a degradation.
        self.assertEqual(lines["REMARK"], [FIRMWARE_NOTE])
        self.assertNotIn("DEGRADED", lines)
        self.assertIn(FIRMWARE_NOTE, doc["notes"])
        self.assertFalse(doc["firmware_validated"])
        self.assertIn("T1 two-colour\tcapable (not printed)", lines["SUMMARY"])
        self.assertEqual((doc["tier_reached"], doc["host_snmp"], doc["degraded"]), ("T3", True, False))
        self.assertEqual(doc["counts"]["pass"], 7)
        self.assertEqual(len(doc["checks"]), len(self.HEALTHY))

    def test_validated_firmware_drops_the_note(self):
        records = [r.replace("t3|info|t3.firmware", "t3|pass|t3.firmware") for r in self.HEALTHY]
        lines, doc = self.verdict(records)
        self.assertNotIn("DEGRADED", lines)
        self.assertNotIn("REMARK", lines)
        self.assertNotIn(FIRMWARE_NOTE, doc["notes"])
        self.assertTrue(doc["firmware_validated"])

    def test_the_firmware_note_is_printed_as_it_is(self):
        """The report prints a REMARK line verbatim and uncoloured; DEGRADED
        stays for a real mismatch only."""
        text = SELFTEST.read_text(encoding="utf-8")
        arm = re.search(r"^[ \t]*REMARK\)[ \t]*(.*?);;", text, re.M | re.S)
        self.assertIsNotNone(arm, "verdict() prints no REMARK lines")
        self.assertIn("printf '%s\\n' \"$a\" >&3", arm.group(1))
        self.assertNotIn("C_YEL", arm.group(1))
        self.assertNotIn("DEGRADED NOTE", text)

    def test_single_colour_roll_holds_the_ladder_at_t0(self):
        records = [r.replace("t1|pass|t1.roll", "t1|skip|t1.roll") for r in self.HEALTHY]
        records.append("fact|info|t1_summary|not verified: no black/red roll loaded")
        lines, _doc = self.verdict(records)
        self.assertEqual(lines["TIER"], ["TIER REACHED: T0 (+host SNMP)"])
        self.assertIn("T1 two-colour\tnot verified: no black/red roll loaded", lines["SUMMARY"])
        self.assertEqual(lines["NOTE"], ["T2 and T3 passed as well, but the tiers build on each other and "
                                         "T1 comes first"])

    def test_confirmed_and_failed_test_labels(self):
        lines, _doc = self.verdict([*self.HEALTHY, "t1|pass|t1.print|you confirmed"])
        self.assertIn("T1 two-colour\tconfirmed (test label printed and checked)", lines["SUMMARY"])
        lines, _doc = self.verdict([*self.HEALTHY, "t1|info|t1.print|printed, not confirmed"])
        self.assertIn("T1 two-colour\tcapable (test label printed, not confirmed)", lines["SUMMARY"])
        lines, _doc = self.verdict([*self.HEALTHY, "t1|fail|t1.print|you reported it wrong"])
        self.assertEqual(lines["TIER"], ["TIER REACHED: T0 (+host SNMP)"])

    def test_degraded_media(self):
        lines, doc = self.verdict([*self.HEALTHY, "t3|fail|t3.degraded|DEGRADED: model 34 41"])
        self.assertEqual(lines["TIER"], ["TIER REACHED: T2 (+host SNMP)"])
        self.assertEqual(lines["DEGRADED"], ["DEGRADED: T0-T2 with manual media"])
        self.assertNotIn("REMARK", lines)
        self.assertTrue(doc["degraded"])
        self.assertEqual(doc["tiers"]["T3"]["status"], "degraded: T0-T2 with manual media")

    def test_host_leak_fails_discovery_and_host_snmp(self):
        lines, doc = self.verdict([r.replace("t2|pass|t2.leak.public", "t2|fail|t2.leak.public")
                                   for r in self.HEALTHY])
        self.assertEqual(lines["TIER"], ["TIER REACHED: T1"])
        self.assertFalse(doc["host_snmp"])

    def test_host_snmp_off(self):
        records = [r.replace("host|pass|host.v3", "host|skip|host.v3") for r in self.HEALTHY]
        lines, _doc = self.verdict([*records, "fact|info|host_summary|off"])
        self.assertEqual(lines["TIER"], ["TIER REACHED: T3"])
        self.assertIn("host SNMP (bonus)\toff", lines["SUMMARY"])

    def test_nothing_installed(self):
        lines, doc = self.verdict(["setup|fail|setup.code|not installed", "t0|fail|t0.socket|inactive",
                                   "t1|skip|t1.roll|unknown", "t2|fail|t2.snmpd|inactive",
                                   "t3|fail|t3.status|not checked", "host|skip|host.v3|off"])
        self.assertEqual(lines["TIER"], ["TIER REACHED: NONE"])
        self.assertEqual(lines["EXIT"], ["1"])
        self.assertNotIn("DEGRADED", lines)
        self.assertEqual(doc["exit_code"], 1)


@unittest.skipIf(BASH is None, "bash >= 4.2 not available (common.sh needs it; set QLP_TEST_BASH)")
class SelftestShellTest(unittest.TestCase):
    def run_selftest(self, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": tmp.name, "TMPDIR": tmp.name,
               "NO_COLOR": "1", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
        return subprocess.run([BASH, str(SELFTEST), *args], capture_output=True, text=True, env=env,
                              timeout=timeout)

    def test_syntax(self):
        done = subprocess.run([BASH, "-n", str(SELFTEST)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_help(self):
        done = self.run_selftest("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        lines = done.stdout.splitlines()
        self.assertTrue(lines and lines[0].startswith("selftest.sh"), done.stdout)
        for option in OPTIONS:
            self.assertIn(option, done.stdout)
        self.assertIn("TIER REACHED", done.stdout)
        self.assertIn("Exit codes", done.stdout)
        self.assertFalse(any(line.startswith("#") or "shellcheck" in line for line in lines), done.stdout)
        self.assertEqual(self.run_selftest("-h").stdout, done.stdout)

    def test_usage_errors(self):
        done = self.run_selftest("--no-such-option")
        self.assertEqual(done.returncode, 64)
        self.assertIn("unknown option: --no-such-option", done.stderr)
        self.assertEqual(done.stdout, "")
        done = self.run_selftest("--mono")
        self.assertEqual(done.returncode, 64)
        self.assertIn("--mono only goes with --print-test", done.stderr)

    @unittest.skipIf(any(os.path.exists(p) for p in INSTALLED_PATHS),
                     "ql810wc-print-proxy is installed here: run ./selftest.sh itself")
    def test_complete_run_without_an_installation(self):
        done = self.run_selftest("--json")
        self.assertEqual(done.returncode, 1, done.stderr)
        doc = json.loads(done.stdout)   # stdout is the JSON document and nothing else
        self.assertEqual((doc["tier_reached"], doc["verdict"], doc["exit_code"]), ("NONE", "TIER REACHED: NONE", 1))
        self.assertEqual(doc["tiers"]["T0"]["status"], "not reached")
        self.assertTrue(doc["tiers"]["T0"]["problems"])
        self.assertTrue(any(c["id"] == "setup.code" and c["status"] == "fail" for c in doc["checks"]))
        report = done.stderr.rstrip("\n").splitlines()
        self.assertEqual(report[-1], "TIER REACHED: NONE")
        self.assertNotIn("stopped before its verdict", done.stderr)
        self.assertIsNone(re.search(r"\x1b\[", done.stderr), "NO_COLOR is honoured")
        for line in report:
            if line and not line.startswith(("==", "ql810wc-print-proxy selftest", "TIER REACHED", "NOTE ",
                                             "NOTE: ", "DEGRADED")):
                if re.match(r"(PASS|WARN|FAIL|SKIP|INFO)  ", line) or re.match(r"\S.* {2,}\S", line):
                    continue
                self.fail(f"unexpected report line: {line!r}")

    @unittest.skipIf(any(os.path.exists(p) for p in INSTALLED_PATHS),
                     "ql810wc-print-proxy is installed here: run ./selftest.sh itself")
    def test_quiet_prints_problems_and_the_verdict(self):
        done = self.run_selftest("--quiet")
        self.assertEqual(done.returncode, 1, done.stderr)
        lines = done.stdout.rstrip("\n").splitlines()
        self.assertEqual(lines[-1], "TIER REACHED: NONE")
        self.assertTrue(all(re.match(r"(WARN|FAIL)  |NOTE  |NOTE: |TIER REACHED: |DEGRADED", line)
                            for line in lines), done.stdout)


@unittest.skipIf(shutil.which("shellcheck") is None, "shellcheck is not installed")
class ShellcheckTest(unittest.TestCase):
    def test_selftest_passes_shellcheck(self):
        done = subprocess.run(["shellcheck", "-x", str(SELFTEST.name)], cwd=str(REPO), capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
