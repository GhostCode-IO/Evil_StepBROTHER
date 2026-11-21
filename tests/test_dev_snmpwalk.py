"""Tests for the development tools in dev/snmpwalk/.

* diffwalks.py and scrub.py, imported as modules;
* walk.sh and media-swap.sh, run with every bash at hand (macOS's 3.2 too)
  against stub SNMP tools, so nothing goes on the network. A stub git points
  their output at a temporary dev/walks/ instead of this checkout's.

Sensitive-looking values are assembled at runtime so this file never contains
a literal private IP or MAC (the hygiene hook would rightly block it).
"""
from __future__ import annotations

import contextlib
import io
import ipaddress
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Dict, Tuple

from tests._util import REPO, bashes, load_module

diffwalks = load_module("dev/snmpwalk/diffwalks.py", "diffwalks")
scrub = load_module("dev/snmpwalk/scrub.py", "scrub")

WALK_SH = REPO / "dev" / "snmpwalk" / "walk.sh"
MEDIA_SWAP_SH = REPO / "dev" / "snmpwalk" / "media-swap.sh"

PRIV_IP = ".".join(["10", "20", "30", "40"])          # private, built at runtime
MAC = ":".join(["3C", "2A", "F4", "11", "22", "33"])  # real-looking OUI
SERIAL = "K9" + "Z123456"
STATUS_OID = "1.3.6.1.4.1.2435.3.3.9.1.6.1.0"


def walk_text(media_byte: str, uptime: int) -> str:
    return "\n".join([
        f'.1.3.6.1.2.1.1.1.0 = STRING: "Brother NC-XXXXw, Firmware Ver.1.00"',
        f".1.3.6.1.2.1.1.3.0 = Timeticks: {uptime}",
        f".1.3.6.1.2.1.2.2.1.6.1 = Hex-STRING: {MAC.replace(':', ' ')}",
        f".1.3.6.1.2.1.4.20.1.1.{PRIV_IP} = IpAddress: {PRIV_IP}",
        f'.1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.0 = STRING: "{SERIAL}"',
        f".1.3.6.1.4.1.2435.9.9.1.0 = Hex-STRING: 80 20 42 34 39 30 00 00 00 00 3E {media_byte} 00 00",
        "   00 15 00 00 00 00",  # wrapped continuation of the hex value above
        f'.1.3.6.1.4.1.2435.9.9.2.0 = STRING: "BRW{MAC.replace(":", "")}"',
        '.1.3.6.1.4.1.2435.9.9.3.0 = ""',
    ]) + "\n"


class DiffWalksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = os.path.join(self.tmp.name, "a.walk")
        self.b = os.path.join(self.tmp.name, "b.walk")
        with open(self.a, "w") as fh:
            fh.write(walk_text("0A", 100))
        with open(self.b, "w") as fh:
            fh.write(walk_text("0B", 999))

    def test_parse_continuation_and_types(self):
        w = diffwalks.parse_walk(self.a)
        v = w["1.3.6.1.4.1.2435.9.9.1.0"]
        self.assertEqual(v.type, "Hex-STRING")
        self.assertEqual(len(v.as_bytes()), 20)  # 14 + 6 continuation bytes
        self.assertEqual(w["1.3.6.1.2.1.1.1.0"].text, "Brother NC-XXXXw, Firmware Ver.1.00")
        self.assertEqual(w["1.3.6.1.4.1.2435.9.9.3.0"].text, '""')

    def test_nway_diff_reports_byte_offset_and_ignores_uptime(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = diffwalks.main([self.a, self.b])
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("* 1.3.6.1.4.1.2435.9.9.1.0", text)
        self.assertIn("[11] 0a->0b", text)
        self.assertNotIn("1.3.6.1.2.1.1.3.0", text)  # sysUpTime is volatile
        self.assertIn("1 OID(s) differ", text)

    def test_prefix_filter(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            diffwalks.main([self.a, self.b, "--prefix", "1.3.6.1.2.1", "--all"])
        self.assertIn("1.3.6.1.2.1.1.3.0", out.getvalue())
        self.assertNotIn("2435.9.9.1.0", out.getvalue())


class ScrubTest(unittest.TestCase):
    def test_scrub_removes_serial_mac_ip_in_all_forms(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "x.walk")
            dst = os.path.join(d, "y.walk")
            with open(src, "w") as fh:
                fh.write(walk_text("0A", 1))
                fh.write(f".1.3.6.1.4.1.2435.9.9.4.0 = Hex-STRING: {SERIAL.encode().hex(' ').upper()}\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = scrub.main([src, "-o", dst])
            with open(dst) as fh:
                out = fh.read()
        self.assertEqual(rc, 0, err.getvalue())
        self.assertNotIn(PRIV_IP, out)
        self.assertNotIn(SERIAL, out)
        self.assertNotIn(SERIAL.encode().hex(" ").upper(), out)
        self.assertNotIn(MAC.replace(":", " "), out)
        self.assertNotIn(MAC.replace(":", ""), out)
        self.assertIn("XXXXXXXXX", out)                      # same-length serial
        self.assertIn("00 00 5E 00 53 00", out)              # documentation MAC
        self.assertIn("BRW00005E005300", out)                # node name rewritten
        self.assertIn("1.3.6.1.2.1.4.20.1.1.192.0.2.1 = IpAddress: 192.0.2.1", out)

    def test_given_values_scrub_a_hex_walk_of_the_brother_mib(self):
        """Such a walk has no ifPhysAddress, ipAdEntAddr or readable serial to collect
        values from, which is why the runbook passes --serial, --mac and --ip."""
        packed_ip = ipaddress.ip_address(PRIV_IP).packed.hex(" ").upper()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "x.2435.hex.walk")
            dst = os.path.join(d, "y.walk")
            with open(src, "w") as fh:
                fh.write(f".1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.0 = Hex-STRING: {SERIAL.encode().hex(' ').upper()} \n"
                         f".1.3.6.1.4.1.2435.9.9.5.0 = Hex-STRING: {MAC.replace(':', ' ')} \n"
                         f".1.3.6.1.4.1.2435.9.9.6.0 = Hex-STRING: {packed_ip} \n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = scrub.main([src, "--serial", SERIAL, "--mac", MAC, "--ip", PRIV_IP, "-o", dst])
            with open(dst) as fh:
                out = fh.read()
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("1 serial(s), 1 MAC(s), 1 IP(s) replaced", err.getvalue())
        for value in (SERIAL.encode().hex(" ").upper(), MAC.replace(":", " "), packed_ip):
            self.assertNotIn(value, out)
        self.assertIn("Hex-STRING: 58 58 58 58 58 58 58 58 58", out)   # "XXXXXXXXX"
        self.assertIn("Hex-STRING: 00 00 5E 00 53 00", out)
        self.assertIn("Hex-STRING: C0 00 02 01", out)                  # 192.0.2.1


# ---------------------------------------------------------------------------
# walk.sh and media-swap.sh
# ---------------------------------------------------------------------------
#: Stand-in for net-snmp's snmpbulkwalk. It logs its arguments and answers
#: with numeric OIDs; the status block switches between DK-22251 and DK-11201
#: every four calls (one walk.sh run: two trees, each as hex and as text).
STUB_SNMPBULKWALK = r"""#!/bin/sh
n=$(cat "$STUB_STATE/calls" 2>/dev/null || echo 0)
echo $((n + 1)) >"$STUB_STATE/calls"
printf '%s\n' "$*" >>"$STUB_STATE/args"
hex=no
for arg in "$@"; do
  if [ "$arg" = -Ox ]; then hex=yes; fi
  oid=$arg
done
if [ $(( (n / 4) % 2 )) -eq 0 ]; then
  roll="3E 0A 00 00 23 00 00 00" colour=81
else
  roll="1D 0B 00 00 01 00 00 5A" colour=01
fi
case "$oid" in
  1.3.6.1.4.1.2435)
    if [ "$hex" = yes ]; then
      echo ".1.3.6.1.4.1.2435.3.3.9.1.6.1.0 = Hex-STRING: 80 20 42 34 39 30 04 00 00 00 $roll "
      echo "00 00 00 00 00 00 00 $colour 00 00 00 00 00 00 "
    else
      echo '.1.3.6.1.4.1.2435.3.3.9.1.6.1.0 = STRING: "status"'
    fi ;;
  1.3.6.1.2.1)
    echo '.1.3.6.1.2.1.1.1.0 = STRING: "Brother NC-XXXXw, Firmware Ver.1.00"'
    echo ".1.3.6.1.2.1.1.3.0 = Timeticks: $n" ;;
  *)
    echo "stub snmpbulkwalk: unexpected OID $oid" >&2
    exit 1 ;;
esac
"""

#: Stand-in for git: answers only "git -C DIR rev-parse --show-toplevel".
STUB_GIT = r"""#!/bin/sh
if [ "$1" = -C ] && [ "$3" = rev-parse ] && [ "$4" = --show-toplevel ]; then
  printf '%s\n' "$STUB_TOPLEVEL"
  exit 0
fi
echo "stub git: unexpected arguments: $*" >&2
exit 2
"""


class UsageTest(unittest.TestCase):
    def test_usage_is_the_header_comment(self):
        for script in (WALK_SH, MEDIA_SWAP_SH):
            for bash in bashes():
                with self.subTest(script=script.name, bash=bash):
                    done = subprocess.run([bash, str(script)], capture_output=True, text=True, timeout=30)
                    self.assertEqual(done.returncode, 64, done.stderr)
                    self.assertIn(f"dev/snmpwalk/{script.name} <printer-ip>", done.stdout)
                    self.assertNotIn("set -euo pipefail", done.stdout)
                    self.assertFalse([line for line in done.stdout.splitlines() if line.startswith("#")])

    def test_walk_refuses_a_bad_label(self):
        for bash in bashes():
            with self.subTest(bash=bash):
                done = subprocess.run([bash, str(WALK_SH), "192.0.2.10", "no such label"],
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, 64)
                self.assertIn("bad label", done.stderr)


@unittest.skipUnless(shutil.which("perl"), "walk.sh needs perl for its timeout wrapper")
class ScriptRunTest(unittest.TestCase):
    def sandbox(self, bash: str) -> Tuple[Dict[str, str], pathlib.Path, pathlib.Path]:
        """(environment, stub state directory, dev/walks/ the scripts write to)."""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="qlwalk-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        stub_bin = tmp / "bin"
        stub_bin.mkdir()
        for name, text in (("snmpbulkwalk", STUB_SNMPBULKWALK), ("git", STUB_GIT)):
            (stub_bin / name).write_text(text, encoding="utf-8")
            (stub_bin / name).chmod(0o755)
        # media-swap.sh starts walk.sh and diffwalks.py through their #! lines.
        (stub_bin / "bash").symlink_to(bash)
        (stub_bin / "python3").symlink_to(sys.executable)
        env = dict(os.environ, PATH=f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                   STUB_STATE=str(tmp), STUB_TOPLEVEL=str(tmp / "checkout"), LC_ALL="C")
        return env, tmp, tmp / "checkout" / "dev" / "walks"

    def test_walk_writes_hex_and_text_walks_of_both_trees(self):
        for bash in bashes():
            with self.subTest(bash=bash):
                env, state, walks = self.sandbox(bash)
                done = subprocess.run([bash, str(WALK_SH), "192.0.2.10", "dk22251"], env=env,
                                      capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(sorted(path.name.split("_", 1)[1] for path in walks.iterdir()),
                                 ["dk22251.2435.hex.walk", "dk22251.2435.txt.walk",
                                  "dk22251.mib2.hex.walk", "dk22251.mib2.txt.walk"])
                calls = (state / "args").read_text(encoding="utf-8").splitlines()
                self.assertEqual(["-Ox" in call.split() for call in calls], [True, False, True, False])
                for call, oid in zip(calls, ("1.3.6.1.4.1.2435", "1.3.6.1.4.1.2435", "1.3.6.1.2.1", "1.3.6.1.2.1")):
                    self.assertTrue(call.startswith("-v2c -c public "), call)
                    self.assertTrue(call.endswith(f" 192.0.2.10 {oid}"), call)
                status = diffwalks.parse_walk(str(next(walks.glob("*.2435.hex.walk"))))[STATUS_OID]
                self.assertEqual(len(status.as_bytes()), 32)   # the wrapped hex line was joined
                self.assertIn("dev/snmpwalk/diffwalks.py", done.stdout)

    def test_media_swap_diffs_the_states_it_walked(self):
        for bash in bashes():
            with self.subTest(bash=bash):
                env, _, walks = self.sandbox(bash)
                # Walk the first two states (DK-22251, then die-cut) and skip the other three.
                done = subprocess.run([bash, str(MEDIA_SWAP_SH), "192.0.2.10"], env=env,
                                      input="\n\nskip\nskip\nskip\n", capture_output=True, text=True,
                                      timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("1 OID(s) changed across states", done.stdout)
                self.assertEqual(len(list(walks.glob("*.walk"))), 8)
                reports = list(walks.glob("*_media-diff.txt"))
                self.assertEqual(len(reports), 1)
                report = reports[0].read_text(encoding="utf-8")
                self.assertIn(f"* {STATUS_OID}", report)
                self.assertIn("bytes: [10] 3e->1d, [11] 0a->0b, [14] 23->01, [17] 00->5a, [25] 81->01", report)

    def test_media_swap_needs_two_states(self):
        for bash in bashes():
            with self.subTest(bash=bash):
                env, _, walks = self.sandbox(bash)
                done = subprocess.run([bash, str(MEDIA_SWAP_SH), "192.0.2.10"], env=env,
                                      input="skip\n" * 5, capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 1)
                self.assertIn("need at least two states to diff", done.stderr)
                self.assertEqual(list(walks.glob("*_media-diff.txt")), [])


if __name__ == "__main__":
    unittest.main()
