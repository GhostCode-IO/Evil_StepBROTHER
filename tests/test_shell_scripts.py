"""The install-time shell layer: scripts/lib/common.sh, scripts/NN-*.sh, uninstall.sh,
and the repository's hygiene hook, dev/hooks/check-hygiene.sh.

The shell tests need bash >= 4.2 (what common.sh requires) and are skipped
without it. macOS ships bash 3.2: point QLP_TEST_BASH at a newer bash to run
them there; CI runs them on Fedora. The hygiene hook supports bash 3.2, so its
tests run with every bash at hand, in a throwaway git repository. Nothing here
touches the system. Each test works in a temporary directory, and every
command that would change or probe the system (systemctl, firewall-cmd,
semanage, lpadmin, userdel, ss, dmesg, ...) is a stub on PATH that records its
arguments and answers from a state directory. Addresses are documentation
ranges; the serial is an obvious fake.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Dict, List, Optional, Sequence

from tests._util import REPO, bashes

AGENT_DIR = REPO / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from ql810wc import config as qconfig  # noqa: E402
from ql810wc.proxyd import ProxySettings  # noqa: E402

UNITS = AGENT_DIR / "systemd"
SERIAL = "Z9TESTSERIAL01"   # fake USB serial
MASKED = "Z9T***"           # what logs may show of it
UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")


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

# Stub commands (POSIX sh). Each first logs "<name> <args>" to $QLP_TEST_LOG.
_LOG_LINE = 'printf \'%s %s\\n\' "${0##*/}" "$*" >>"$QLP_TEST_LOG"\n'
STUBS: Dict[str, str] = {
    # A unit is active / enabled / installed while $S/<active|enabled|units>/<unit> exists.
    "systemctl": r'''
S="$QLP_TEST_STATE" cmd="" now=0 units=""
for a in "$@"; do
  case "$a" in
    --now) now=1 ;;
    -*) ;;
    *) if [ -z "$cmd" ]; then cmd="$a"; else units="$units $a"; fi ;;
  esac
done
rc=0
for u in $units; do
  case "$cmd" in
    is-active) [ -e "$S/active/$u" ] || rc=3 ;;
    is-enabled) [ -e "$S/enabled/$u" ] || rc=1 ;;
    cat) [ -e "$S/units/$u" ] || rc=1 ;;
    start|restart) : >"$S/active/$u" ;;
    stop) rm -f "$S/active/$u" ;;
    disable) rm -f "$S/enabled/$u"; if [ "$now" = 1 ]; then rm -f "$S/active/$u"; fi ;;
  esac
done
exit "$rc"
''',
    "firewall-cmd": "exit 0\n",   # every port and rule asked about is present
    "semanage": 'if [ "$1 $2" = "fcontext -l" ]; then cat "$QLP_TEST_STATE/fcontext" 2>/dev/null; fi\nexit 0\n',
    "semodule": 'if [ "$1" = "-l" ]; then cat "$QLP_TEST_STATE/semodules" 2>/dev/null; fi\nexit 0\n',
    "selinuxenabled": "exit 0\n",
    "restorecon": "exit 0\n",
    "lpstat": r'''
case "${1:-}" in
  -r) exit 0 ;;
  -v) if [ -n "${2:-}" ]; then [ -e "$QLP_TEST_STATE/cupsq/$2" ]; exit; fi
      cat "$QLP_TEST_STATE/lpstat-v" 2>/dev/null ;;
esac
exit 0
''',
    "lpadmin": 'if [ "$1" = "-x" ]; then rm -f "$QLP_TEST_STATE/cupsq/$2"; fi\nexit 0\n',
    "userdel": "exit 0\n",
    "id": 'case "${1:-}" in -*|"") exec /usr/bin/id "$@" ;; esac\n[ -e "$QLP_TEST_STATE/users/$1" ]\n',
    "udevadm": "exit 0\n",
    "dnf": "exit 0\n",
    "timeout": 'shift\nexec "$@"\n',
    "ss": 'cat "$QLP_TEST_STATE/ss.out" 2>/dev/null\nexit 0\n',
    "dmesg": 'cat "$QLP_TEST_STATE/dmesg.out" 2>/dev/null\n',
    "journalctl": 'cat "$QLP_TEST_STATE/journal.out" 2>/dev/null\nexit 0\n',
    "lsusb": 'cat "$QLP_TEST_STATE/lsusb.out" 2>/dev/null\n',
    "curl": "exit 7\n",
    "getenforce": "echo Enforcing\n",
    "rpm": "exit 1\n",
    "ip": "exit 0\n",
}
# Calls that change something (a dry run makes none of them).
MUTATING = re.compile(r"^(systemctl (stop|start|restart|try-restart|reload|disable|enable|daemon-reload)\b"
                      r"|lpadmin |userdel |udevadm |dnf |semodule -r|semanage (fcontext -d|port -d)"
                      r"|firewall-cmd .*--remove-)")


@unittest.skipIf(BASH is None, "bash >= 4.2 not available (common.sh needs it; set QLP_TEST_BASH)")
class ShellTestCase(unittest.TestCase):
    """A temporary root ($T), the stubs on PATH and a way to run bash snippets."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="qlpsh")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name).resolve()
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        self.calls_log = self.root / "calls.log"
        for directory in (self.bin, *(self.state / k for k in ("active", "enabled", "units", "cupsq", "users"))):
            directory.mkdir(parents=True)
        for name, body in STUBS.items():
            path = self.bin / name
            path.write_text("#!/bin/sh\n" + _LOG_LINE + body, encoding="utf-8")
            path.chmod(0o755)

    def run_bash(self, script: str, *args: str, env: Optional[Dict[str, str]] = None,
                 stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        full_env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.root), "NO_COLOR": "1", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8",
            "REPO": str(REPO), "T": str(self.root), "PY": sys.executable,
            "QLP_TEST_STATE": str(self.state), "QLP_TEST_LOG": str(self.calls_log),
        }
        full_env.update(env or {})
        return subprocess.run([BASH, "-c", script, "qlp-test", *args], env=full_env, input=stdin,
                              capture_output=True, text=True, timeout=120)

    def write(self, rel: str, text: str = "") -> pathlib.Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def set_state(self, kind: str, *names: str) -> None:
        for name in names:
            (self.state / kind / name).touch()

    def calls(self) -> List[str]:
        if not self.calls_log.exists():
            return []
        return self.calls_log.read_text(encoding="utf-8").splitlines()

    def assertCalledOnce(self, call: str) -> None:
        self.assertEqual(self.calls().count(call), 1, f"{call!r} in {self.calls()}")

    def assertNotCalled(self, call: str) -> None:
        self.assertNotIn(call, self.calls())


def directives(text: str) -> List[str]:
    """The non-comment, non-blank lines of a config file."""
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


class SyntaxTest(unittest.TestCase):
    """Every install-time script parses with every bash at hand, old ones too:
    macOS's bash 3.2 mis-scans quotes in a heredoc inside $(...) (SCAN_PY in
    30-snmp-agent.sh), which only shows up as a syntax error there."""

    SCRIPTS = ("install.sh", "uninstall.sh", "scripts/lib/common.sh", "scripts/00-preflight.sh",
               "scripts/10-deps.sh", "scripts/20-cups-queue.sh", "scripts/30-snmp-agent.sh",
               "scripts/40-discovery.sh", "dev/hooks/check-hygiene.sh", "dev/snmpwalk/walk.sh",
               "dev/snmpwalk/media-swap.sh")

    def test_bash_n(self):
        shells = {}
        for bash in (BASH, shutil.which("bash"), "/bin/bash"):
            if bash and os.access(bash, os.X_OK):
                shells.setdefault(os.path.realpath(bash), bash)
        if not shells:
            self.skipTest("no bash at all")
        for bash in shells.values():
            for rel in self.SCRIPTS:
                with self.subTest(bash=bash, script=rel):
                    done = subprocess.run([bash, "-n", str(REPO / rel)], capture_output=True, text=True, timeout=30)
                    self.assertEqual(done.returncode, 0, done.stderr)


class InstallUsageTest(ShellTestCase):
    """install.sh --help prints its header comment and nothing of the code."""

    def test_help_and_unknown_option(self):
        done = self.run_bash('exec "$BASH" "$REPO/install.sh" "$@"', "--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--data-plane-only", done.stdout)
        self.assertIn("Exit codes: 0 ok", done.stdout)
        self.assertNotIn("shellcheck", done.stdout)
        self.assertNotIn("source ", done.stdout)
        done = self.run_bash('exec "$BASH" "$REPO/install.sh" "$@"', "--no-color")
        self.assertEqual(done.returncode, 64)
        self.assertIn("unknown option: --no-color", done.stderr)


# ---------------------------------------------------------------------------
# qlp_render_unit: only the .socket gets RAW_BIND
# ---------------------------------------------------------------------------
class RenderUnitTest(ShellTestCase):
    RENDER = 'source "$REPO/scripts/lib/common.sh"; RAW_PORT="$1" RAW_BIND="$2" qlp_render_unit "$3"'
    BINDS = ("", "192.0.2.10", "2001:db8::10")

    def render(self, unit: pathlib.Path, port: str, bind: str) -> subprocess.CompletedProcess:
        return self.run_bash(self.RENDER, port, bind, str(unit))

    def test_services_get_plain_substitution(self):
        for name in ("ql810wc-proxyd.service", "ql810wc-snmp-agent.service"):
            template = (UNITS / name).read_text(encoding="utf-8")
            for bind in self.BINDS:
                with self.subTest(unit=name, bind=bind):
                    done = self.render(UNITS / name, "9101", bind)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertEqual(done.stdout, template.replace("@RAW_PORT@", "9101").replace("@RAW_BIND@", bind))
                    self.assertEqual(PLACEHOLDER.findall(done.stdout), [])

    def test_socket_listens_on_raw_bind(self):
        template = (UNITS / "ql810wc-proxyd.socket").read_text(encoding="utf-8")
        listen = {"": "ListenStream=9101", "192.0.2.10": "ListenStream=192.0.2.10:9101",
                  "2001:db8::10": "ListenStream=[2001:db8::10]:9101"}
        for bind in self.BINDS:
            with self.subTest(bind=bind):
                done = self.render(UNITS / "ql810wc-proxyd.socket", "9101", bind)
                self.assertEqual(done.returncode, 0, done.stderr)
                lines = done.stdout.splitlines()
                self.assertEqual([line for line in lines if line.startswith("ListenStream=")], [listen[bind]])
                self.assertEqual("FreeBind=yes" in lines, bool(bind))
                self.assertEqual(PLACEHOLDER.findall(done.stdout), [])
                plain = template.replace("@RAW_PORT@", "9101").replace("@RAW_BIND@", bind).splitlines()
                self.assertEqual([line for line in lines if not line.startswith(("ListenStream=", "FreeBind="))],
                                 [line for line in plain if not line.startswith("ListenStream=")],
                                 "nothing else changes")

    def test_template_errors_are_loud(self):
        unit = self.write("x.service", "[Service]\nExecStart=@LIBDIR@/x\n")
        done = self.render(unit, "9100", "")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("@LIBDIR@", done.stderr)
        sock = self.write("x.socket", "[Socket]\nListenDatagram=@RAW_PORT@\n")
        done = self.render(sock, "9100", "192.0.2.10")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ListenStream=@RAW_PORT@", done.stderr)
        sock = self.write("y.socket", "[Socket]\nListenStream=@RAW_BIND@:@RAW_PORT@\n")
        done = self.render(sock, "9100", "")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("RAW_BIND", done.stderr)

    def test_step_20_installs_both_units_for_every_raw_bind(self):
        script = r'''
source "$REPO/scripts/20-cups-queue.sh"
qlp_write_file() { cat >"$T/out/${1##*/}"; QLP_CHANGED=1; }
qlp_daemon_reload() { :; }
RAW_PORT=9100 RAW_BIND="$1"
install_units
printf '%s %s\n' "$SOCKET_CHANGED" "$SERVICE_CHANGED"
'''
        service = (UNITS / "ql810wc-proxyd.service").read_text(encoding="utf-8")
        listen = {"": "ListenStream=9100", "192.0.2.10": "ListenStream=192.0.2.10:9100",
                  "2001:db8::10": "ListenStream=[2001:db8::10]:9100"}
        for bind in self.BINDS:
            with self.subTest(bind=bind):
                out = self.root / "out"
                shutil.rmtree(out, ignore_errors=True)
                out.mkdir()
                done = self.run_bash(script, bind)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout.split(), ["1", "1"])
                self.assertIn(listen[bind], (out / "ql810wc-proxyd.socket").read_text().splitlines())
                # qlp_write_file is fed through a here-string, which ends in exactly one newline.
                self.assertEqual((out / "ql810wc-proxyd.service").read_text().rstrip("\n"),
                                 service.replace("@RAW_PORT@", "9100").replace("@RAW_BIND@", bind).rstrip("\n"))

    def test_step_30_installs_the_agent_unit_with_raw_bind_set(self):
        script = r'''
source "$REPO/scripts/30-snmp-agent.sh"
qlp_init test
qlp_write_file() { cat >"$T/agent.service"; QLP_CHANGED=1; }
qlp_daemon_reload() { :; }
SNMP_AGENT_MODE=agentx RAW_PORT=9100 RAW_BIND="$1"
configure_agent_unit
'''
        self.set_state("active", "ql810wc-snmp-agent.service")
        self.set_state("enabled", "ql810wc-snmp-agent.service")
        template = (UNITS / "ql810wc-snmp-agent.service").read_text(encoding="utf-8")
        for bind in ("192.0.2.10", "2001:db8::10"):
            with self.subTest(bind=bind):
                done = self.run_bash(script, bind)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual((self.root / "agent.service").read_text().rstrip("\n"),
                                 template.replace("@RAW_PORT@", "9100").replace("@RAW_BIND@", bind).rstrip("\n"))


# ---------------------------------------------------------------------------
# Serial numbers never reach logs in full
# ---------------------------------------------------------------------------
class MaskSerialsTest(ShellTestCase):
    MASK = 'source "$REPO/scripts/lib/common.sh"; QL_USB_SERIAL="$1"; qlp_mask_serials'

    def mask(self, text: str, known: str = "") -> str:
        done = self.run_bash(self.MASK, known, stdin=text)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_patterns(self):
        cases = [
            (f"[    2.2] usb 1-1.2: SerialNumber: {SERIAL}", f"[    2.2] usb 1-1.2: SerialNumber: {MASKED}"),
            ("usb 1-1.2: New USB device strings: Mfr=1, Product=2, SerialNumber=3",
             "usb 1-1.2: New USB device strings: Mfr=1, Product=2, SerialNumber=3"),
            (f"device for QL: usb://Brother/QL-810W?serial={SERIAL}&interface=1",
             f"device for QL: usb://Brother/QL-810W?serial={MASKED}&interface=1"),
            (f"  iSerial                 3 {SERIAL}", f"  iSerial                 3 {MASKED}"),
            (f"MFG:Brother;CMD:PT-CBP;MDL:QL-810W;SN:{SERIAL};", f"MFG:Brother;CMD:PT-CBP;MDL:QL-810W;SN:{MASKED};"),
            (f"Serial Number: {SERIAL}", f"Serial Number: {MASKED}"),
            ("serial=ab and SerialNumber: abc", "serial=ab and SerialNumber: abc"),
            (f"SerialNumber: {MASKED} serial ABC***", f"SerialNumber: {MASKED} serial ABC***"),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(self.mask(text + "\n"), want + "\n")

    def test_the_printers_own_serial_anywhere(self):
        self.assertEqual(self.mask(f"printer {SERIAL} attached ({SERIAL})\n", SERIAL),
                         f"printer {MASKED} attached ({MASKED})\n")
        odd = "Q.1*[x]Z"   # glob and regex characters are taken literally
        self.assertEqual(self.mask(f"a {odd} b Q.1zzzZ\n", odd), "a Q.1*** b Q.1zzzZ\n")

    def test_edge_cases(self):
        self.assertEqual(self.mask(""), "")
        self.assertEqual(self.mask(f"SerialNumber: {SERIAL}"), f"SerialNumber: {MASKED}\n", "unterminated last line")
        once = self.mask(f"serial={SERIAL} SN:{SERIAL}\n", SERIAL)
        self.assertEqual(self.mask(once, SERIAL), once, "masking is idempotent")

    def test_kernel_messages(self):
        dmesg = self.state / "dmesg.out"
        dmesg.write_text("[    1.0] usb 1-1.2: new full-speed USB device number 4 using xhci_hcd\n"
                         f"[    1.1] usb 1-1.2: SerialNumber: {SERIAL}\n"
                         "[    1.2] EXT4-fs (sda1): mounted filesystem\n")
        script = 'source "$REPO/scripts/lib/common.sh"; qlp_init test; qlp_kernel_usb_messages 6; echo "rc=$?"'
        done = self.run_bash(script)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(SERIAL, done.stdout)
        self.assertIn(f"SerialNumber: {MASKED}", done.stdout)
        self.assertNotIn("EXT4", done.stdout)
        dmesg.unlink()   # kernel log not readable: nothing, and no failure
        done = self.run_bash(script)
        self.assertEqual((done.returncode, done.stdout), (0, "rc=0\n"), done.stderr)
        self.assertNotIn("ERROR", done.stderr)

    def test_journal_shown_on_a_failed_start_is_masked(self):
        (self.state / "journal.out").write_text(f"ql810wc-proxyd[1]: printer {SERIAL} gone\n"
                                                "kernel: SerialNumber: OTHERSERIAL99\n")
        done = self.run_bash('source "$REPO/scripts/lib/common.sh"; QL_USB_SERIAL="$1";'
                             ' qlp_require_active ql810wc-proxyd.service', SERIAL)
        self.assertEqual(done.returncode, 1)
        self.assertNotIn(SERIAL, done.stdout + done.stderr)
        self.assertNotIn("OTHERSERIAL99", done.stdout + done.stderr)
        self.assertIn(f"printer {MASKED} gone", done.stderr)

    def test_preflight_never_prints_a_full_serial(self):
        cfg = self.write("config.env", f'USB_SERIAL="{SERIAL}"\n')
        cfg.chmod(0o600)
        (self.state / "dmesg.out").write_text(f"[    2.2] usb 1-1.2: SerialNumber: {SERIAL}\n"
                                              f"[    2.3] usblp 1-1.2:1.0: brother device {SERIAL} bound\n")
        (self.state / "lsusb.out").write_text("Bus 001 Device 004: ID 04f9:209c Brother Industries, Ltd QL-810W\n"
                                              f"  iSerial                 3 {SERIAL}\n")
        (self.state / "lpstat-v").write_text(f"device for QL810W: usb://Brother/QL-810W?serial={SERIAL}\n")
        self.set_state("active", "cups")
        done = self.run_bash('"$BASH" "$REPO/scripts/00-preflight.sh"', env={"QL_PROXY_CONFIG": str(cfg)})
        output = done.stdout + done.stderr
        self.assertNotIn(SERIAL, output)
        self.assertIn(f"iSerial                 3 {MASKED}", output)            # lsusb dump
        self.assertIn(f"usb://Brother/QL-810W?serial={MASKED}", output)         # existing CUPS queue
        if os.geteuid() == 0:                                                   # the kernel log needs root
            self.assertIn(f"SerialNumber: {MASKED}", output)
            self.assertIn(f"brother device {MASKED} bound", output)


# ---------------------------------------------------------------------------
# RAW_IDLE_TIMEOUT / MAX_JOB_BYTES
# ---------------------------------------------------------------------------
class RuntimeKeysStaticTest(unittest.TestCase):
    """No bash needed: the template and the key list carry both settings."""

    def test_template_documents_the_limits(self):
        text = (REPO / "config.sample.env").read_text(encoding="utf-8")
        self.assertRegex(text, r'(?m)^RAW_IDLE_TIMEOUT="300"$')
        self.assertRegex(text, r'(?m)^MAX_JOB_BYTES="16777216"$')
        self.assertIn("(10-86400)", text)
        self.assertIn("(65536-1073741824)", text)

    def test_runtime_keys_include_the_limits(self):
        text = (REPO / "scripts/lib/common.sh").read_text(encoding="utf-8")
        match = re.search(r"(?ms)^QLP_RUNTIME_KEYS=\((.*?)\)", text)
        self.assertIsNotNone(match)
        keys = match.group(1).split()
        self.assertIn("RAW_IDLE_TIMEOUT", keys)
        self.assertIn("MAX_JOB_BYTES", keys)
        self.assertIn("RAW_COLOR_CHECK", keys)
        self.assertIn("RAW_MEDIA_CHECK", keys)

    def test_template_turns_the_color_check_on(self):
        text = (REPO / "config.sample.env").read_text(encoding="utf-8")
        self.assertRegex(text, r'(?m)^RAW_COLOR_CHECK="yes"$')

    def test_template_turns_the_media_check_on(self):
        text = (REPO / "config.sample.env").read_text(encoding="utf-8")
        self.assertRegex(text, r'(?m)^RAW_MEDIA_CHECK="yes"$')
        keys = re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=", text)
        self.assertEqual(keys[keys.index("RAW_COLOR_CHECK") + 1], "RAW_MEDIA_CHECK",
                         "the media check follows the colour check (the README's table keeps this order)")


class RuntimeSettingsTest(ShellTestCase):
    SYNC = r'''
source "$REPO/scripts/lib/common.sh"
qlp_init test
QLP_LIB_DIR="$REPO/agent" QLP_PYTHON="$PY" QLP_IDENTITY_ENV="$T/identity.env"
qlp_ensure_dir() { :; }
qlp_write_file() { cat >"$T/runtime.env"; QLP_CHANGED=1; }
qlp_load_config
qlp_sync_runtime_env
'''

    def setUp(self) -> None:
        super().setUp()
        self.write("identity.env", f'PRINTER_UUID="{UUID}"\n')
        self.runtime = self.root / "runtime.env"

    def sync(self, **values: str) -> subprocess.CompletedProcess:
        cfg = self.write("config.env", "".join(f'{k}="{v}"\n' for k, v in values.items()))
        cfg.chmod(0o600)
        if self.runtime.exists():
            self.runtime.unlink()
        return self.run_bash(self.SYNC, env={"QL_PROXY_CONFIG": str(cfg)})

    def written(self) -> Dict[str, str]:
        return qconfig.parse_env_file(self.runtime).values

    def daemon_settings(self) -> ProxySettings:
        return ProxySettings.from_config(qconfig.load_runtime_config(self.runtime))

    def test_defaults_are_written_and_accepted(self):
        done = self.sync()
        self.assertEqual(done.returncode, 0, done.stderr)
        values = self.written()
        self.assertEqual((values["RAW_IDLE_TIMEOUT"], values["MAX_JOB_BYTES"]), ("300", "16777216"))
        self.assertEqual(values["RAW_COLOR_CHECK"], "yes", "the colour check is on unless switched off")
        self.assertEqual(values["RAW_MEDIA_CHECK"], "yes", "the media check is on unless switched off")
        settings = self.daemon_settings()
        self.assertEqual((settings.raw_idle_timeout, settings.max_job_bytes), (300.0, 16 * 1024 * 1024))
        self.assertTrue(settings.color_check)
        self.assertTrue(settings.media_check)

    def test_the_color_check_can_be_switched_off(self):
        for value, written, on in (("off", "no", False), ("No", "no", False), ("TRUE", "yes", True)):
            with self.subTest(value=value):
                done = self.sync(RAW_COLOR_CHECK=value)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.written()["RAW_COLOR_CHECK"], written)
                self.assertIs(self.daemon_settings().color_check, on)
                self.assertTrue(self.daemon_settings().media_check, "each check has a setting of its own")
        done = self.sync(RAW_COLOR_CHECK="sometimes")
        self.assertEqual(done.returncode, 1)
        self.assertIn("RAW_COLOR_CHECK='sometimes' is not a yes/no value", done.stderr)
        self.assertFalse(self.runtime.exists())

    def test_the_media_check_can_be_switched_off(self):
        for value, written, on in (("off", "no", False), ("0", "no", False), ("", "no", False), ("Yes", "yes", True)):
            with self.subTest(value=value):
                done = self.sync(RAW_MEDIA_CHECK=value)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.written()["RAW_MEDIA_CHECK"], written)
                self.assertIs(self.daemon_settings().media_check, on)
                self.assertTrue(self.daemon_settings().color_check, "each check has a setting of its own")
        done = self.sync(RAW_MEDIA_CHECK="sometimes")
        self.assertEqual(done.returncode, 1)
        self.assertIn("RAW_MEDIA_CHECK='sometimes' is not a yes/no value", done.stderr)
        self.assertFalse(self.runtime.exists())

    def test_values_are_written_canonically(self):
        done = self.sync(RAW_IDLE_TIMEOUT="0120", MAX_JOB_BYTES="1073741824", STATUS_POLL_SECONDS="05")
        self.assertEqual(done.returncode, 0, done.stderr)
        values = self.written()
        self.assertEqual((values["RAW_IDLE_TIMEOUT"], values["MAX_JOB_BYTES"], values["STATUS_POLL_SECONDS"]),
                         ("120", "1073741824", "5"))
        settings = self.daemon_settings()
        self.assertEqual((settings.raw_idle_timeout, settings.max_job_bytes), (120.0, 1 << 30))

    def test_bounds_and_empty_are_accepted(self):
        for values in ({"RAW_IDLE_TIMEOUT": "10", "MAX_JOB_BYTES": "65536"},
                       {"RAW_IDLE_TIMEOUT": "86400", "MAX_JOB_BYTES": "1073741824"}):
            with self.subTest(values=values):
                done = self.sync(**values)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual({k: self.written()[k] for k in values}, values)
        done = self.sync(RAW_IDLE_TIMEOUT="", MAX_JOB_BYTES="")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.written()["RAW_IDLE_TIMEOUT"], self.written()["MAX_JOB_BYTES"]), ("", ""))
        settings = self.daemon_settings()
        self.assertEqual((settings.raw_idle_timeout, settings.max_job_bytes), (300.0, 16 * 1024 * 1024),
                         "empty = the daemon default")

    def test_out_of_range_values_fail_before_anything_is_written(self):
        for key, value in (("RAW_IDLE_TIMEOUT", "9"), ("RAW_IDLE_TIMEOUT", "86401"), ("RAW_IDLE_TIMEOUT", "5m"),
                           ("RAW_IDLE_TIMEOUT", "-1"), ("MAX_JOB_BYTES", "65535"),
                           ("MAX_JOB_BYTES", "1073741825"), ("MAX_JOB_BYTES", "99999999999999999999"),
                           ("STATUS_POLL_SECONDS", "0")):
            with self.subTest(key=key, value=value):
                done = self.sync(**{key: value})
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn(f"{key}='{value}' must be a whole number", done.stderr)
                self.assertFalse(self.runtime.exists())

    def test_the_daemons_loader_rejects_them_too(self):
        bad = self.write("bad.env", 'RAW_IDLE_TIMEOUT="5"\n')
        script = 'source "$REPO/scripts/lib/common.sh"; "$PY" -I -B -c "$_QLP_PY_VALIDATE_RUNTIME" "$REPO/agent" "$1" "$2"'
        done = self.run_bash(script, str(bad), str(self.root / "identity.env"))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("RAW_IDLE_TIMEOUT", done.stdout)


# ---------------------------------------------------------------------------
# 30-snmp-agent: listening addresses
# ---------------------------------------------------------------------------
class SnmpListeningAddressTest(ShellTestCase):
    CONFLICTS = r'''
source "$REPO/scripts/30-snmp-agent.sh"
qlp_init test
SNMPD_OTHER_CONFIGS=("$T/etc/snmpd.conf" "$T/etc/snmpd.conf.d")
SNMPD_SYSCONFIG="$T/sysconfig-snmpd"
COMMUNITIES=(public internal) V3_ENABLED=0
check_snmpd_conflicts
for a in "${ADMIN_LISTEN[@]}"; do printf 'ADMIN %s\n' "$a"; done
'''
    RENDER = r'''
source "$REPO/scripts/30-snmp-agent.sh"
qlp_write_file() { cat >"$T/dropin.conf"; QLP_CHANGED=1; }
SNMP_AGENT_MODE="$1" PRINTER_CIDRS="$2" NEED_UDP6="$3" V3_CIDRS="" V3_ENABLED=0
COMMUNITIES=(public internal)
if [[ -n "$4" ]]; then ADMIN_LISTEN=("$4"); fi
render_dropin
'''
    VERIFY = r'''
source "$REPO/scripts/30-snmp-agent.sh"
qlp_init test
SNMP_LISTEN_TRIES=1 NEED_UDP6="$1"
if [[ -n "$2" ]]; then ADMIN_LISTEN=("$2"); fi
if [[ -n "${NO_SS:-}" ]]; then have() { [[ "$1" != ss ]] && command -v "$1" >/dev/null 2>&1; }; fi
verify_snmp_listeners
'''
    V4 = "ipv4\t192.0.2.0/24"
    V6 = "ipv4\t192.0.2.0/24\nipv6\t2001:db8::/64"

    def conflicts(self, snmpd_conf: str = "", sysconfig: Optional[str] = None,
                  dropin: str = "") -> subprocess.CompletedProcess:
        self.write("etc/snmpd.conf", snmpd_conf)
        self.write("etc/snmpd.conf.d/admin.conf", dropin)
        sysconfig_path = self.root / "sysconfig-snmpd"
        if sysconfig is None:
            sysconfig_path.unlink(missing_ok=True)
        else:
            self.write("sysconfig-snmpd", sysconfig)
        return self.run_bash(self.CONFLICTS)

    def admin(self, done: subprocess.CompletedProcess) -> List[str]:
        self.assertEqual(done.returncode, 0, done.stderr)
        return [line[6:] for line in done.stdout.splitlines() if line.startswith("ADMIN ")]

    def test_agentaddress_elsewhere_is_reported(self):
        done = self.conflicts("agentaddress udp:127.0.0.1:161\n[snmpd] agentAddress udp6:[::1]:161\n"
                              "agentXSocket unix:/var/agentx/master\n",
                              dropin="agentXSocket tcp:localhost:705\n")
        conf = self.root / "etc/snmpd.conf"
        self.assertEqual(self.admin(done), [f"{conf}:1: udp:127.0.0.1:161", f"{conf}:2: udp6:[::1]:161"])
        self.assertIn("listening addresses are set elsewhere", done.stderr)
        self.assertIn("admin.conf:1: agentXSocket tcp:localhost:705", done.stderr)
        self.assertNotIn("unix:/var/agentx/master", done.stderr, "the same socket, spelled with unix:")

    def test_stock_config_has_no_listening_addresses(self):
        done = self.conflicts("view systemview included .1.3.6.1.2.1.1\n", sysconfig='# OPTIONS="-LS0-6d"\n')
        self.assertEqual(self.admin(done), [])
        self.assertNotIn("WARN", done.stderr)

    def test_command_line_addresses(self):
        where = f"{self.root}/sysconfig-snmpd (OPTIONS): "
        cases = [
            ('OPTIONS="-LS0-6d"\n', []),
            ('# OPTIONS="-LS0-6d udp:161"\n', []),
            ('OPTIONS="-LS0-6d -p /run/snmpd.pid -c /etc/snmp/x.conf"\n', []),
            ('OPTIONS="-x tcp:localhost:705 -u snmp -g snmp -I -smux"\n', []),
            ("OPTIONS='-Lsd -Dagentx,snmp udp:161,udp6:161'\n", ["udp:161,udp6:161"]),
            ("OPTIONS=-a -LS0-6d udp:192.0.2.10:161\n", ["udp:192.0.2.10:161"]),
            ('OPTIONS="-LS0-6d"\nOPTIONS="-LS0-6d -p/run/x.pid udp:161"\n', ["udp:161"]),
            ('OPTIONS="-f -- udp:161"\n', ["udp:161"]),
            # -L reads its value from the next word(s) unless attached (snmp_log_options)
            ('OPTIONS="-LS0-6d -Lf /var/log/snmpd.log"\n', []),
            ('OPTIONS="-Lf/var/log/snmpd.log udp:161"\n', ["udp:161"]),
            ('OPTIONS="-Ls d udp:161"\n', ["udp:161"]),
            ('OPTIONS="-LF 0-6 /var/log/snmpd.log udp6:161"\n', ["udp6:161"]),
            ('OPTIONS="-LF0-6 /var/log/snmpd.log"\n', []),
            ('OPTIONS="-L f /var/log/snmpd.log -Le -LS0-6 d"\n', []),
            ('OPTIONS="-aLE 3 udp:161"\n', ["udp:161"]),
        ]
        for sysconfig, want in cases:
            with self.subTest(sysconfig=sysconfig):
                self.assertEqual(self.admin(self.conflicts(sysconfig=sysconfig)), [where + w for w in want])

    def render(self, mode: str, cidrs: str, need_udp6: str, admin: str = "") -> str:
        done = self.run_bash(self.RENDER, mode, cidrs, need_udp6, admin)
        self.assertEqual(done.returncode, 0, done.stderr)
        text = (self.root / "dropin.conf").read_text(encoding="utf-8")
        self.assertEqual(PLACEHOLDER.findall(text), [])
        for line in directives(text):
            self.assertNotIn("#", line, "net-snmp has no trailing comments")
        return text

    def test_no_agentaddress_by_default(self):
        lines = directives(self.render("agentx", self.V4, "0"))
        self.assertEqual([line for line in lines if line.lower().startswith("agentaddress")], [])
        self.assertEqual(lines.count("agentXSocket /var/agentx/master"), 1)
        self.assertIn("master agentx", lines)

    def test_ipv6_sources_add_udp6_together_with_the_ipv4_wildcard(self):
        lines = directives(self.render("agentx", self.V6, "1"))
        self.assertEqual([line for line in lines if line.lower().startswith("agentaddress")],
                         ["agentaddress udp:161,udp6:161"])
        self.assertIn("com2sec6 -Cn printer ql810wcPersona 2001:db8::/64 internal", lines)
        self.assertIn("com2sec6 -Cn printer ql810wcPersona ::1/128 public", lines)

    def test_listening_addresses_set_elsewhere_get_no_second_bind(self):
        text = self.render("agentx", self.V6, "1", "/etc/snmp/snmpd.conf:9: udp:161")
        self.assertEqual([line for line in directives(text) if line.lower().startswith("agentaddress")], [])
        self.assertIn("another snmpd configuration already sets the listening addresses", text)

    def test_agentx_socket_is_kept_and_explained(self):
        text = self.render("agentx", self.V4, "0")
        self.assertIn("agentXSocket repeats net-snmp's default path", text)
        self.assertIn("--socket /var/agentx/master", text)
        unit = (UNITS / "ql810wc-snmp-agent.service").read_text(encoding="utf-8")
        self.assertIn("--socket /var/agentx/master", unit, "the drop-in and the subagent agree on the socket")
        lines = directives(self.render("pass_persist", self.V4, "0"))
        self.assertFalse([line for line in lines if line.split()[0] in ("master", "agentXSocket", "agentaddress")])

    def verify(self, ss_lines: Sequence[str], need_udp6: str = "0", admin: str = "",
               env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        (self.state / "ss.out").write_text("".join(line + "\n" for line in ss_lines))
        return self.run_bash(self.VERIFY, need_udp6, admin, env=env)

    def test_listener_check(self):
        snmpd = 'users:(("snmpd",pid=812,fd=6))'
        v4 = f"UNCONN 0      0            0.0.0.0:161        0.0.0.0:*    {snmpd}"
        v6 = f"UNCONN 0      0               [::]:161           [::]:*    {snmpd}"
        done = self.verify([v4])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("UDP 161 wildcard (IPv4)", done.stderr)
        done = self.verify([v4, v6], "1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("(IPv4 and IPv6)", done.stderr)
        done = self.verify([f"UNCONN 0      0                  *:161              *:*    {snmpd}"], "1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("WARN", done.stderr)
        done = self.verify(["UNCONN 0      0            0.0.0.0:161        0.0.0.0:*"])
        self.assertEqual(done.returncode, 0, "ss could not name the owner: " + done.stderr)

    def test_listener_check_failures(self):
        snmpd = 'users:(("snmpd",pid=812,fd=6))'
        done = self.verify([f"UNCONN 0      0          127.0.0.1:161        0.0.0.0:*    {snmpd}"],
                           admin="/etc/snmp/snmpd.conf:12: udp:127.0.0.1:161")
        self.assertEqual(done.returncode, 1)
        self.assertIn("does not listen on the UDP 161 wildcard", done.stderr)
        self.assertIn("/etc/snmp/snmpd.conf:12: udp:127.0.0.1:161", done.stderr)
        done = self.verify([f"UNCONN 0      0    0.0.0.0%eth0:161        0.0.0.0:*    {snmpd}"])
        self.assertEqual(done.returncode, 1, "a device-bound socket is no wildcard")
        done = self.verify(['UNCONN 0      0            0.0.0.0:161        0.0.0.0:*    users:(("python3",pid=9,fd=3))'])
        self.assertEqual(done.returncode, 1)
        self.assertIn("belongs to 'python3'", done.stderr)
        done = self.verify([f"UNCONN 0      0            0.0.0.0:161        0.0.0.0:*    {snmpd}"], "1",
                           admin="/etc/snmp/snmpd.conf:3: udp:161")
        self.assertEqual(done.returncode, 0, "IPv4 works: IPv6 is only warned about")
        self.assertIn("does not listen on UDP 161 over IPv6", done.stderr)
        self.assertIn("add udp6:161", done.stderr)
        done = self.verify([], env={"NO_SS": "1"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ss (iproute) not found", done.stderr)


# ---------------------------------------------------------------------------
# Directory trees (manifest "tree"): never anything outside our own dirs
# ---------------------------------------------------------------------------
class TreeRemovalTest(ShellTestCase):
    ROOTS = 'QLP_OPT_DIR="$T/opt/ql810wc-proxy" QLP_STATE_DIR="$T/var/lib/ql810wc-proxy"\n'

    def setUp(self) -> None:
        super().setUp()
        for rel in ("opt/ql810wc-proxy/venv/bin", "var/lib/ql810wc-proxy/backups", "elsewhere/venv", "opt/other",
                    "realopt/ql810wc-proxy/venv"):
            (self.root / rel).mkdir(parents=True)
        self.write("opt/ql810wc-proxy/file")
        self.write("elsewhere/venv/keep.txt", "not ours\n")
        (self.root / "opt/ql810wc-proxy/link").symlink_to(self.root / "elsewhere/venv")
        (self.root / "opt/ql810wc-proxy/sub").symlink_to(self.root / "elsewhere")
        (self.root / "optlink").symlink_to(self.root / "realopt")

    def refusals(self, *paths: str, roots: str = ROOTS) -> Dict[str, str]:
        script = ('source "$REPO/scripts/lib/common.sh"\n' + roots +
                  'for p in "$@"; do printf "%s=%s\\n" "$p" "$(qlp_tree_refusal "$p")"; done')
        done = self.run_bash(script, *paths)
        self.assertEqual(done.returncode, 0, done.stderr)
        return dict(line.split("=", 1) for line in done.stdout.splitlines())

    def test_refusals(self):
        r = str(self.root)
        got = self.refusals(f"{r}/opt/ql810wc-proxy/venv", f"{r}/var/lib/ql810wc-proxy/backups",
                            f"{r}/opt/ql810wc-proxy", f"{r}/opt/other", f"{r}/opt/ql810wc-proxy/file",
                            f"{r}/opt/ql810wc-proxy/link", f"{r}/opt/ql810wc-proxy/sub/venv", "relative/venv",
                            f"{r}/opt/ql810wc-proxy/venv/../venv", f"{r}/opt/ql810wc-proxy/./venv")
        self.assertEqual(got[f"{r}/opt/ql810wc-proxy/venv"], "")
        self.assertEqual(got[f"{r}/var/lib/ql810wc-proxy/backups"], "")
        self.assertTrue(got[f"{r}/opt/ql810wc-proxy"].startswith("outside "), "never the root itself")
        self.assertTrue(got[f"{r}/opt/other"].startswith("outside "))
        self.assertEqual(got[f"{r}/opt/ql810wc-proxy/file"], "not a directory")
        self.assertEqual(got[f"{r}/opt/ql810wc-proxy/link"], "a symbolic link")
        self.assertTrue(got[f"{r}/opt/ql810wc-proxy/sub/venv"].startswith("outside "), "symlinked component")
        self.assertEqual(got["relative/venv"], "not an absolute path")
        self.assertEqual(got[f"{r}/opt/ql810wc-proxy/venv/../venv"], 'has "." or ".." components')
        self.assertEqual(got[f"{r}/opt/ql810wc-proxy/./venv"], 'has "." or ".." components')
        moved = self.refusals(f"{r}/optlink/ql810wc-proxy/venv", roots='QLP_OPT_DIR="$T/optlink/ql810wc-proxy"\n'
                                                                    'QLP_STATE_DIR="$T/var/lib/ql810wc-proxy"\n')
        self.assertEqual(moved[f"{r}/optlink/ql810wc-proxy/venv"], "", "a symlinked /opt itself is fine")

    def test_remove_tree(self):
        script = ('source "$REPO/scripts/lib/common.sh"\n' + self.ROOTS +
                  'qlp_remove_tree "$1"; echo "rc=$?"')
        r = self.root
        done = self.run_bash(script, f"{r}/opt/ql810wc-proxy/venv")
        self.assertIn("rc=0", done.stdout, done.stderr)
        self.assertFalse((r / "opt/ql810wc-proxy/venv").exists())
        done = self.run_bash(script, f"{r}/opt/ql810wc-proxy/link")
        self.assertIn("rc=1", done.stdout, done.stderr)
        self.assertIn("refusing to remove", done.stderr)
        self.assertTrue((r / "elsewhere/venv/keep.txt").exists())
        self.assertTrue((r / "opt/ql810wc-proxy/link").is_symlink())
        done = self.run_bash(script, f"{r}/opt/ql810wc-proxy/never-existed")
        self.assertIn("rc=0", done.stdout, done.stderr)

    def test_step_10_drops_the_venv_only_when_it_is_ours(self):
        script = r'''
source "$REPO/scripts/10-deps.sh"
qlp_init test
QLP_OPT_DIR="$T/opt/ql810wc-proxy" QLP_STATE_DIR="$T/var/lib/ql810wc-proxy"
QLP_MANIFEST="$QLP_STATE_DIR/manifest" QLP_VENV_DIR="$1" INSTALL_BROTHER_QL=no
install_brother_ql
'''
        r = self.root
        manifest = r / "var/lib/ql810wc-proxy/manifest"
        venv = f"{r}/opt/ql810wc-proxy/venv"
        manifest.write_text(f"tree\t{venv}\n")
        done = self.run_bash(script, venv)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(pathlib.Path(venv).exists())
        self.assertEqual(manifest.read_text(), "")
        link = f"{r}/opt/ql810wc-proxy/link"
        manifest.write_text(f"tree\t{link}\n")
        done = self.run_bash(script, link)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("refusing to remove", done.stderr)
        self.assertTrue((r / "elsewhere/venv/keep.txt").exists())
        self.assertEqual(manifest.read_text(), f"tree\t{link}\n", "the record stays for uninstall.sh")


# ---------------------------------------------------------------------------
# uninstall.sh: every record kind the scripts write
# ---------------------------------------------------------------------------
PERSIST = """#
# net-snmp persistent data file (test copy).
#
usmUser 1 3 0x80001f8880000000000000000000 "nms" "nms" NULL .1.3.6.1.6.3.10.1.1.3 0x01 .1.3.6.1.6.3.10.1.2.4 0x02 ""
usmUser 1 3 0x80001f8880000000000000000000 "other" "other" NULL .1.3.6.1.6.3.10.1.1.3 0x01 .1.3.6.1.6.3.10.1.2.4 0x02 ""
engineBoots 3
"""
SNMPD_ORIGINAL = "# stock snmpd.conf\nview systemview included .1.3.6.1.2.1.1\n"
RICH = 'rule family="ipv4" source address="192.0.2.0/24" port port="161" protocol="udp" accept'


class UninstallTest(ShellTestCase):
    UNINSTALL = r'''
source "$REPO/uninstall.sh"
require_root() { :; }
QLP_STATE_DIR="$T/var/lib/ql810wc-proxy"
QLP_MANIFEST="$QLP_STATE_DIR/manifest"
QLP_BACKUP_DIR="$QLP_STATE_DIR/backups"
QLP_OPT_DIR="$T/opt/ql810wc-proxy"
QLP_LOG_DIR="$T/var/log/ql810wc-proxy"
QLP_RUN_DIR="$T/run/ql810wc-proxy"
QLP_SYSTEMD_DIR="$T/etc/systemd/system"
QLP_SNMP_PERSIST="$T/var/lib/net-snmp/snmpd.conf"
UNINSTALL_WATCH=("$T/etc/snmp/|snmpd" "$T/etc/avahi/|avahi" "$T/etc/cups/|cups" "$T/etc/ipp-usb/|ipp-usb"
                 "$T/etc/udev/rules.d/|udev" "$QLP_SYSTEMD_DIR/|systemd")
main "$@"
'''

    def setUp(self) -> None:
        super().setUp()
        self.manifest = self.root / "var/lib/ql810wc-proxy/manifest"
        self.backups = self.root / "var/lib/ql810wc-proxy/backups"

    def build(self, foreign_include: bool = True) -> None:
        """Files, state and manifest as a complete install leaves them."""
        r, b = self.root, self.backups
        self.write("opt/ql810wc-proxy/venv/bin/python", "#!/bin/sh\n")
        self.write("opt/ql810wc-proxy/venv/lib/site.py")
        self.write("elsewhere/tree/keep.txt", "not ours\n")
        (r / "opt/ql810wc-proxy/link").symlink_to(r / "elsewhere/tree")
        self.write("opt/escape/keep.txt", "not ours either\n")
        self.write("var/lib/ql810wc-proxy/snmpv3-user", "nms\tsalt\tdigest\n")
        self.write("var/lib/net-snmp/snmpd.conf", PERSIST)
        self.write("etc/snmp/snmpd.conf", SNMPD_ORIGINAL +
                   "# BEGIN ql810wc-print-proxy (managed — do not edit inside this block)\n"
                   f"includeDir {r}/etc/snmp/snmpd.conf.d\n# END ql810wc-print-proxy\n")
        self.write("var/lib/ql810wc-proxy/backups/etc__snmp__snmpd.conf.orig", SNMPD_ORIGINAL)
        self.write("etc/snmp/snmpd.conf.d/ql810wc-proxy.conf", "master agentx\n")
        if foreign_include:
            self.write("etc/snmp/snmpd.conf.d/site.conf", "rocommunity site 192.0.2.0/24\n")
        self.write("etc/avahi/services/ql810wc-proxy.service", "<service-group/>\n")
        self.write("etc/ipp-usb/quirks/ql810wc-proxy.conf", "[Brother QL-810W]\n  blacklist = true\n")
        self.write("etc/udev/rules.d/60-ql810wc-proxy.rules", 'SUBSYSTEM=="usb"\n')
        self.write("etc/systemd/system/ql810wc-proxyd.service", "[Service]\n")
        self.write("etc/cups/x.conf", "changed by the installer\n")
        self.write("var/lib/ql810wc-proxy/backups/etc__cups__x.conf.orig", "pristine\n")
        self.records([
            ("pkg", "cups"),
            ("created", f"{r}/opt/ql810wc-proxy"),
            ("tree", f"{r}/opt/ql810wc-proxy/venv"),
            ("tree", f"{r}/elsewhere/tree"),
            ("tree", f"{r}/opt/ql810wc-proxy/link"),
            ("tree", f"{r}/opt/ql810wc-proxy/../escape"),
            ("user", "ql810wc"),
            ("created", f"{r}/etc/udev/rules.d/60-ql810wc-proxy.rules"),
            ("created", f"{r}/etc/ipp-usb"),
            ("created", f"{r}/etc/ipp-usb/quirks"),
            ("created", f"{r}/etc/ipp-usb/quirks/ql810wc-proxy.conf"),
            ("modified", f"{r}/etc/cups/x.conf", f"{b}/etc__cups__x.conf.orig"),
            ("created", f"{r}/etc/systemd/system/ql810wc-proxyd.service"),
            ("unit", "ql810wc-proxyd.service"),
            ("fw-port", "public", "9100/tcp"),
            ("cupsq", "QL810Wc-proxy"),
            ("created", f"{r}/etc/snmp/snmpd.conf.d"),
            ("block", f"{r}/etc/snmp/snmpd.conf", f"{b}/etc__snmp__snmpd.conf.orig"),
            ("fcontext", "snmpd_var_run_t", f"{r}/run/ql810wc-proxy(/.*)?"),
            ("created", f"{r}/etc/snmp/snmpd.conf.d/ql810wc-proxy.conf"),
            ("usmuser", "nms"),
            ("fw-rich", "public", RICH),
            ("seport", "snmp_port_t", "udp", "10161"),
            ("semodule", "ql810wcproxy"),
            ("created", f"{r}/etc/avahi/services/ql810wc-proxy.service"),
            ("bogus", "x"),
        ])
        self.set_state("active", "snmpd.service", "avahi-daemon.service", "ipp-usb.service", "cups.service",
                       "ql810wc-proxyd.service", "firewalld")
        self.set_state("enabled", "ql810wc-proxyd.service")
        self.set_state("units", "ql810wc-proxyd.service", "snmpd.service")
        self.set_state("users", "ql810wc")
        self.set_state("cupsq", "QL810Wc-proxy")
        (self.state / "fcontext").write_text(f"{r}/run/ql810wc-proxy(/.*)?  all files  "
                                             "system_u:object_r:snmpd_var_run_t:s0\n")
        (self.state / "semodules").write_text("ql810wcproxy\n")

    def records(self, records: Sequence[Sequence[str]]) -> None:
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text("".join("\t".join(rec) + "\n" for rec in records), encoding="utf-8")

    def uninstall(self, *args: str) -> subprocess.CompletedProcess:
        return self.run_bash(self.UNINSTALL, *args)

    def snapshot(self) -> Dict[str, Optional[str]]:
        found: Dict[str, Optional[str]] = {}
        for path in sorted(self.root.rglob("*")):
            rel = str(path.relative_to(self.root))
            if rel.startswith(("state", "bin")) or rel == "calls.log":
                continue
            found[rel] = None if path.is_dir() or path.is_symlink() else path.read_text(encoding="utf-8")
        return found

    def test_every_record_kind_is_reversed(self):
        self.build()
        done = self.uninstall()
        self.assertEqual(done.returncode, 0, done.stderr)
        r = self.root
        for rel in ("opt/ql810wc-proxy/venv", "etc/avahi/services/ql810wc-proxy.service", "etc/ipp-usb",
                    "etc/udev/rules.d/60-ql810wc-proxy.rules", "etc/systemd/system/ql810wc-proxyd.service",
                    "etc/snmp/snmpd.conf.d/ql810wc-proxy.conf", "var/lib/ql810wc-proxy/snmpv3-user",
                    "var/lib/ql810wc-proxy/manifest"):
            self.assertFalse((r / rel).exists(), rel)
        self.assertEqual(len(list((r / "var/lib/ql810wc-proxy").glob("manifest.uninstalled-*"))), 1)
        self.assertEqual((r / "etc/cups/x.conf").read_text(), "pristine\n")
        persist = (r / "var/lib/net-snmp/snmpd.conf").read_text()
        self.assertNotIn('"nms"', persist)
        self.assertIn('"other"', persist)
        self.assertIn("engineBoots 3", persist)
        for call in ("systemctl disable --now ql810wc-proxyd.service", "lpadmin -x QL810Wc-proxy",
                     "firewall-cmd --permanent --zone=public --remove-port=9100/tcp",
                     "firewall-cmd --zone=public --remove-port=9100/tcp",
                     f"firewall-cmd --permanent --zone=public --remove-rich-rule={RICH}",
                     f"semanage fcontext -d {r}/run/ql810wc-proxy(/.*)?",
                     "semanage port -d -t snmp_port_t -p udp 10161", "semodule -r ql810wcproxy",
                     "userdel ql810wc", "udevadm control --reload", "systemctl daemon-reload",
                     "systemctl stop snmpd.service", "systemctl start snmpd.service",
                     "systemctl reload avahi-daemon.service", "systemctl try-restart ipp-usb.service",
                     "systemctl try-restart cups.service"):
            with self.subTest(call=call):
                self.assertCalledOnce(call)
        self.assertNotCalled("systemctl try-restart snmpd.service")   # stopped for the edit, started once
        self.assertFalse([c for c in self.calls() if c.startswith("dnf ")])
        self.assertIn("packages installed by us and left in place: cups", done.stderr)
        # refused trees stay, with a warning each
        self.assertTrue((r / "elsewhere/tree/keep.txt").exists())
        self.assertTrue((r / "opt/escape/keep.txt").exists())
        self.assertTrue((r / "opt/ql810wc-proxy/link").is_symlink())
        self.assertIn(f"refusing to remove {r}/elsewhere/tree (outside ", done.stderr)
        self.assertIn(f"refusing to remove {r}/opt/ql810wc-proxy/link (a symbolic link)", done.stderr)
        self.assertIn('components)', done.stderr)
        self.assertIn(f"kept {r}/opt/ql810wc-proxy: it holds files", done.stderr)
        self.assertIn("unknown manifest record (left alone): bogus", done.stderr)
        # the include block stays while another file in its directory relies on it
        self.assertIn(f"includeDir {r}/etc/snmp/snmpd.conf.d", (r / "etc/snmp/snmpd.conf").read_text())
        self.assertIn("kept the managed include block", done.stderr)
        self.assertTrue((r / "etc/snmp/snmpd.conf.d/site.conf").exists())

    def test_an_empty_include_block_goes_and_idle_services_stay_alone(self):
        self.build(foreign_include=False)
        for unit in ("ipp-usb.service", "avahi-daemon.service", "cups.service"):
            (self.state / "active" / unit).unlink()
        done = self.uninstall()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.root / "etc/snmp/snmpd.conf").read_text(), SNMPD_ORIGINAL)
        self.assertFalse((self.root / "etc/snmp/snmpd.conf.d").exists())
        for call in ("systemctl try-restart ipp-usb.service", "systemctl reload avahi-daemon.service",
                     "systemctl try-restart cups.service"):
            self.assertNotCalled(call)
        self.assertCalledOnce("systemctl start snmpd.service")

    def test_dry_run_changes_nothing(self):
        self.build()
        before = self.snapshot()
        done = self.uninstall("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([c for c in self.calls() if MUTATING.match(c)], [])
        self.assertIn("[dry-run] rm -rf", done.stderr)
        self.assertIn("would refuse to remove", done.stderr)
        self.assertIn("[dry-run] remove SNMPv3 user 'nms'", done.stderr)

    def test_snmpd_enabled_by_the_installer_stays_stopped(self):
        r = self.root
        self.write("var/lib/net-snmp/snmpd.conf", PERSIST)
        self.set_state("active", "snmpd.service")
        self.set_state("enabled", "snmpd.service")
        self.set_state("units", "snmpd.service")
        for order in (("unit", "usmuser"), ("usmuser", "unit")):
            with self.subTest(order=order):
                self.write("var/lib/net-snmp/snmpd.conf", PERSIST)
                self.set_state("active", "snmpd.service")
                self.calls_log.unlink(missing_ok=True)
                rec = {"unit": ("unit", "snmpd.service"), "usmuser": ("usmuser", "nms")}
                self.records([rec[kind] for kind in order])
                done = self.uninstall()
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertCalledOnce("systemctl disable --now snmpd.service")
                self.assertNotCalled("systemctl start snmpd.service")
                self.assertNotIn('"nms"', (r / "var/lib/net-snmp/snmpd.conf").read_text())

    def test_an_interrupted_run_starts_snmpd_again(self):
        r = self.root
        self.write("var/lib/net-snmp/snmpd.conf", PERSIST)
        self.write("var/lib/ql810wc-proxy/backups/x.orig", "pristine\n")
        self.set_state("active", "snmpd.service")
        # The older record fails (its target directory is gone) after snmpd was stopped.
        self.records([("modified", f"{r}/gone/x.conf", f"{self.backups}/x.orig"), ("usmuser", "nms")])
        done = self.uninstall()
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.calls().count("systemctl stop snmpd.service"), 1)
        self.assertEqual(self.calls().count("systemctl start snmpd.service"), 1)
        self.assertIn("starting snmpd again after an interrupted run", done.stderr)
        self.assertTrue(self.manifest.exists(), "the manifest stays for a re-run")

    def test_purge_and_nothing_to_do(self):
        done = self.uninstall()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("nothing to do", done.stderr)
        self.write("var/log/ql810wc-proxy/install.log", "x\n")
        self.records([("pkg", "cups")])
        done = self.uninstall("--purge")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse((self.root / "var/lib/ql810wc-proxy").exists())
        self.assertFalse((self.root / "var/log/ql810wc-proxy").exists())

    def test_unknown_option(self):
        done = self.uninstall("--bogus")
        self.assertEqual(done.returncode, 64)
        self.assertIn("unknown option: --bogus", done.stderr)
        done = self.uninstall("--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("--dry-run", done.stdout)
        self.assertNotIn("shellcheck", done.stdout)


# ---------------------------------------------------------------------------
# ql810wc-snmp-agent: reloaded for settings, identity and the OID map,
# restarted only for new code or a new unit (a restart leaves snmpd answering
# noSuchName until the agent registers again, which fails a P-touch print)
# ---------------------------------------------------------------------------
AGENT_UNIT = "ql810wc-snmp-agent.service"
PROXYD_UNIT = "ql810wc-proxyd.service"
# systemctl with "show -p PROP --value UNIT" and "reload". CanReload is "yes"
# unless $S/no-reload/<unit> exists; "reload" fails while $S/fail-reload
# exists, and with $S/reject it leaves the status a rejected reload leaves.
RELOAD_SYSTEMCTL = r"""
S="$QLP_TEST_STATE" cmd="" prop="" next=0 units=""
for a in "$@"; do
  if [ "$next" = 1 ]; then prop="$a"; next=0; continue; fi
  case "$a" in
    -p) next=1 ;;
    -*) ;;
    *) if [ -z "$cmd" ]; then cmd="$a"; else units="$units $a"; fi ;;
  esac
done
rc=0
for u in $units; do
  case "$cmd" in
    is-active) [ -e "$S/active/$u" ] || rc=3 ;;
    is-enabled) [ -e "$S/enabled/$u" ] || rc=1 ;;
    start|restart) : >"$S/active/$u" ;;
    stop) rm -f "$S/active/$u" ;;
    enable) : >"$S/enabled/$u" ;;
    reload)
      if [ -e "$S/fail-reload" ] || [ ! -e "$S/active/$u" ]; then rc=1
      elif [ -e "$S/reject" ]; then
        echo "session 7: all 5 registrations accepted; last reload failed: STATUS_POLL_SECONDS: not an integer; printer live" >"$S/status-$u"
      else
        echo "session 7: all 5 registrations accepted; printer live: 62mm continuous black/red (DK-22251), idle" >"$S/status-$u"
      fi ;;
    show)
      case "$prop" in
        CanReload) if [ -e "$S/no-reload/$u" ]; then echo no; else echo yes; fi ;;
        StatusText) cat "$S/status-$u" 2>/dev/null || echo ;;
      esac ;;
  esac
done
exit "$rc"
"""


class AgentReloadTest(ShellTestCase):
    """Settings-only or map-only changes reload ql810wc-snmp-agent and never
    restart it; new code or a new unit restarts it."""

    TRY_RELOAD = r"""
source "$REPO/scripts/lib/common.sh"
qlp_init test
qlp_try_reload "$@"
"""
    STEP20 = r"""
source "$REPO/scripts/20-cups-queue.sh"
qlp_init test
QLP_STATE_DIR="$T/state-dir" QLP_MANIFEST="$T/state-dir/manifest"
RAW_PORT=9100 RAW_BIND=""
for kv in $FLAGS; do printf -v "${kv%%=*}" '%s' "${kv#*=}"; done
start_data_plane
"""
    STEP30 = r"""
source "$REPO/scripts/30-snmp-agent.sh"
qlp_init test
QLP_STATE_DIR="$T/state-dir" QLP_MANIFEST="$T/state-dir/manifest" QLP_SYSTEMD_DIR="$T/systemd"
qlp_write_file() { cat >/dev/null; QLP_CHANGED="${UNIT_CHANGED:-0}"; }
qlp_daemon_reload() { :; }
SNMP_AGENT_MODE=agentx RAW_PORT=9100 RAW_BIND="" AGENT_GROUPS=()
for kv in $FLAGS; do printf -v "${kv%%=*}" '%s' "${kv#*=}"; done
configure_agent_unit
"""
    DEPLOY = r"""
source "$REPO/scripts/20-cups-queue.sh"
qlp_init test
QLP_REPO_ROOT="$T/checkout" QLP_LIB_DIR="$T/lib"
qlp_ensure_dir() { mkdir -p "$1"; }
qlp_manifest_del() { :; }
qlp_install_file() {
  if cmp -s "$1" "$2"; then QLP_CHANGED=0; else mkdir -p "${2%/*}"; cp "$1" "$2"; QLP_CHANGED=1; fi
}
deploy_code >/dev/null 2>&1
echo "code=$CODE_CHANGED proxyd=$PROXYD_CODE_CHANGED agent=$AGENT_CODE_CHANGED map=$OIDMAP_CHANGED"
"""

    def setUp(self) -> None:
        super().setUp()
        stub = self.bin / "systemctl"
        stub.write_text("#!/bin/sh\n" + _LOG_LINE + RELOAD_SYSTEMCTL, encoding="utf-8")
        stub.chmod(0o755)
        (self.state / "no-reload").mkdir()

    def actions(self) -> List[str]:
        """The calls that started, restarted, stopped or reloaded a unit."""
        return [c for c in self.calls() if re.match(r"systemctl (start|restart|stop|reload) ", c)]

    # -- qlp_try_reload ---------------------------------------------------------------
    def test_try_reload_reloads_active_units_only(self):
        self.set_state("active", AGENT_UNIT)
        done = self.run_bash(self.TRY_RELOAD, AGENT_UNIT, "other.service")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.actions(), [f"systemctl reload {AGENT_UNIT}"], "a stopped unit stays stopped")

    def test_try_reload_restarts_a_unit_that_cannot_reload(self):
        self.set_state("active", AGENT_UNIT)
        (self.state / "no-reload" / AGENT_UNIT).touch()
        done = self.run_bash(self.TRY_RELOAD, AGENT_UNIT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.actions(), [f"systemctl restart {AGENT_UNIT}"])
        self.assertIn("cannot reload", done.stderr)

    def test_try_reload_falls_back_to_a_restart_when_the_reload_fails(self):
        self.set_state("active", AGENT_UNIT)
        (self.state / "fail-reload").touch()
        done = self.run_bash(self.TRY_RELOAD, AGENT_UNIT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.actions(), [f"systemctl reload {AGENT_UNIT}", f"systemctl restart {AGENT_UNIT}"])

    def test_a_rejected_reload_stops_the_step(self):
        self.set_state("active", AGENT_UNIT)
        (self.state / "reject").touch()
        done = self.run_bash(self.TRY_RELOAD, AGENT_UNIT)
        self.assertEqual(done.returncode, 1)
        self.assertIn("rejected the new settings or OID map", done.stderr)
        self.assertIn("last reload failed: STATUS_POLL_SECONDS", done.stderr)
        self.assertEqual(self.actions(), [f"systemctl reload {AGENT_UNIT}"],
                         "never restarted: a new process would refuse the same files")

    # -- step 20 ----------------------------------------------------------------------
    def step20(self, flags: str) -> List[str]:
        if self.calls_log.exists():
            self.calls_log.unlink()
        for unit in ("ql810wc-proxyd.socket", PROXYD_UNIT, AGENT_UNIT):
            self.set_state("active", unit)
            self.set_state("enabled", unit)
        done = self.run_bash(self.STEP20, env={"FLAGS": flags})
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.actions()

    def test_step_20_reloads_the_agent_for_settings_identity_and_the_map(self):
        for flags in ("QLP_RUNTIME_CHANGED=1", "QLP_IDENTITY_CHANGED=1", "OIDMAP_CHANGED=1",
                      "QLP_RUNTIME_CHANGED=1 OIDMAP_CHANGED=1"):
            with self.subTest(flags=flags):
                actions = self.step20(flags)
                self.assertIn(f"systemctl reload {AGENT_UNIT}", actions)
                self.assertNotIn(f"systemctl restart {AGENT_UNIT}", actions)
                self.assertEqual(f"systemctl restart {PROXYD_UNIT}" in actions, flags != "OIDMAP_CHANGED=1",
                                 "ql810wc-proxyd never reads the OID map")

    def test_step_20_restarts_the_agent_for_new_code(self):
        actions = self.step20("AGENT_CODE_CHANGED=1 OIDMAP_CHANGED=1 QLP_RUNTIME_CHANGED=1")
        self.assertIn(f"systemctl restart {AGENT_UNIT}", actions)
        self.assertNotIn(f"systemctl reload {AGENT_UNIT}", actions, "restart, never reload, for new code")
        actions = self.step20("PROXYD_CODE_CHANGED=1")
        self.assertEqual(actions, [f"systemctl restart {PROXYD_UNIT}"], "the agent's code did not change")
        self.assertEqual(self.step20(""), [], "nothing changed: nothing restarted")

    def test_step_20_tells_the_daemons_code_apart(self):
        checkout = self.root / "checkout" / "agent"
        for name in ("ql810wc-proxyd", "qlctl", "brother_snmp_agent.py", "oid-map.yaml",
                     "ql810wc/__init__.py", "ql810wc/persona.py"):
            self.write(f"checkout/agent/{name}", f"# {name}\n")

        def deploy() -> str:
            done = self.run_bash(self.DEPLOY)
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout.strip()

        self.assertEqual(deploy(), "code=1 proxyd=1 agent=1 map=1")
        self.assertEqual(deploy(), "code=0 proxyd=0 agent=0 map=0")
        for name, expected in (("oid-map.yaml", "code=1 proxyd=0 agent=0 map=1"),
                               ("brother_snmp_agent.py", "code=1 proxyd=0 agent=1 map=0"),
                               ("ql810wc-proxyd", "code=1 proxyd=1 agent=0 map=0"),
                               ("qlctl", "code=1 proxyd=0 agent=0 map=0"),
                               ("ql810wc/persona.py", "code=1 proxyd=1 agent=1 map=0")):
            with self.subTest(changed=name):
                (checkout / name).write_text(f"# {name}, new version\n", encoding="utf-8")
                self.assertEqual(deploy(), expected)
        self.write("lib/ql810wc/old_module.py", "# shipped by an older version\n")
        self.assertEqual(deploy(), "code=1 proxyd=1 agent=1 map=0", "a stale module: both daemons may have used it")
        self.assertFalse((self.root / "lib/ql810wc/old_module.py").exists())

    # -- step 30 ----------------------------------------------------------------------
    def step30(self, flags: str = "", unit_changed: bool = False, active: bool = True) -> List[str]:
        if self.calls_log.exists():
            self.calls_log.unlink()
        self.set_state("enabled", AGENT_UNIT)
        if active:
            self.set_state("active", AGENT_UNIT)
        done = self.run_bash(self.STEP30, env={"FLAGS": flags, "UNIT_CHANGED": "1" if unit_changed else "0"})
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.actions()

    def test_step_30_restarts_the_agent_only_for_its_unit(self):
        self.assertEqual(self.step30(unit_changed=True), [f"systemctl restart {AGENT_UNIT}"])
        self.assertEqual(self.step30("QLP_RUNTIME_CHANGED=1", unit_changed=True), [f"systemctl restart {AGENT_UNIT}"])
        for flags in ("QLP_RUNTIME_CHANGED=1", "QLP_IDENTITY_CHANGED=1"):
            with self.subTest(flags=flags):
                self.assertEqual(self.step30(flags), [f"systemctl reload {AGENT_UNIT}"])
        self.assertEqual(self.step30(), [], "nothing changed")
        (self.state / "active" / AGENT_UNIT).unlink()
        self.assertEqual(self.step30("QLP_RUNTIME_CHANGED=1", active=False), [f"systemctl start {AGENT_UNIT}"],
                         "a stopped agent is started, and reads the new settings then")

    def test_step_30_restarts_the_data_plane_before_snmp_pauses(self):
        body = re.search(r"(?ms)^main\(\) \{\n(.*?)^\}", (REPO / "scripts/30-snmp-agent.sh").read_text()).group(1)
        lines = [line.strip() for line in body.splitlines()]
        proxyd = next(i for i, line in enumerate(lines) if 'qlp_try_restart "$QLP_UNIT_PROXYD"' in line)
        for later in ("manage_v3_user", "start_snmpd", "configure_agent_unit"):
            self.assertLess(proxyd, lines.index(later), later)

    # -- every step -------------------------------------------------------------------
    def test_no_step_restarts_the_agent_for_settings_alone(self):
        """Static: the agent is restarted only by step 20 for new code and by step
        30 for its unit; settings changes everywhere go through qlp_try_reload."""
        texts = {rel: (REPO / rel).read_text(encoding="utf-8")
                 for rel in ("scripts/20-cups-queue.sh", "scripts/30-snmp-agent.sh", "scripts/40-discovery.sh")}
        restarts = {rel: [line.strip() for line in re.findall(r"(?m)^.*restart\b.*\$QLP_UNIT_AGENT.*$", text)
                          if not line.lstrip().startswith("#")]
                    for rel, text in texts.items()}
        self.assertEqual(restarts["scripts/20-cups-queue.sh"], ['qlp_try_restart "$QLP_UNIT_AGENT"'])
        self.assertRegex(texts["scripts/20-cups-queue.sh"],
                         r'if \(\(AGENT_CODE_CHANGED\)\); then\n\s+qlp_try_restart "\$QLP_UNIT_AGENT"')
        self.assertEqual(restarts["scripts/30-snmp-agent.sh"], ['run systemctl restart "$QLP_UNIT_AGENT"'])
        self.assertEqual(restarts["scripts/40-discovery.sh"], [])
        for rel, text in texts.items():
            with self.subTest(step=rel):
                self.assertIn('qlp_try_reload "$QLP_UNIT_AGENT"', text)


# ---------------------------------------------------------------------------
# dev/hooks/check-hygiene.sh: the pre-commit hook (--staged) and CI (--all)
# ---------------------------------------------------------------------------
HYGIENE_HOOK = REPO / "dev" / "hooks" / "check-hygiene.sh"
#: Built at runtime: the hook scans this file too.
PRIVATE_IP = ".".join(["10", "20", "30", "40"])
PRIVATE_IP_BYTES = bytes(int(part) for part in PRIVATE_IP.split("."))
BINARY_REFUSED = "binary file not allowed in git (its content cannot be checked): "
IP_FOUND = "private/site IPv4 address in "


@unittest.skipUnless(shutil.which("git"), "git not available")
class HygieneHookTest(unittest.TestCase):
    """The hook in a throwaway repository, run with every bash at hand."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="qlphyg")
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name).resolve()
        self.repo = root / "repo"
        self.repo.mkdir()
        # No user or system git configuration (signing, hooks, rename and diff
        # settings) and no GIT_* variables from a calling hook. The locale is
        # UTF-8, where grep takes text that is not valid UTF-8 for binary data.
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(HOME=str(root), XDG_CONFIG_HOME=str(root / "config"), GIT_CONFIG_NOSYSTEM="1",
                        LC_ALL="C.UTF-8", LANG="C.UTF-8")
        self.git("init", "-q")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "user.name=test",
                        "-c", "user.email=test@example.invalid", *args],
                       cwd=self.repo, env=self.env, capture_output=True, check=True, timeout=60)

    def add(self, name: str, data: bytes) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.git("add", "--", name)

    def commit(self) -> None:
        self.git("commit", "-q", "--no-verify", "-m", "test")

    def assertHook(self, mode: str, returncode: int, *needles: str, absent: Sequence[str] = ()) -> None:
        """Run the hook with every bash at hand; check its exit code and stderr."""
        for bash in bashes():
            with self.subTest(bash=bash, mode=mode):
                done = subprocess.run([bash, str(HYGIENE_HOOK), mode], cwd=self.repo, env=self.env,
                                      capture_output=True, encoding="utf-8", errors="replace", timeout=120)
                self.assertEqual(done.returncode, returncode, done.stderr)
                for needle in needles:
                    self.assertIn(needle, done.stderr)
                for needle in absent:
                    self.assertNotIn(needle, done.stderr)
                if returncode == 0:
                    self.assertEqual(done.stderr, "")

    def test_clean_files_pass(self):
        self.add("README.md", b"Point the client at 192.0.2.10 (MAC 00:00:5E:00:53:01).\n")
        self.add("scripts/run.sh", b"#!/bin/sh\necho ok\n")
        self.assertHook("--staged", 0)
        self.commit()
        self.assertHook("--all", 0)

    def test_a_binary_file_is_refused_in_both_modes(self):
        """grep -I passes over binary data, where an address may sit as text or as raw bytes."""
        self.add("notes/data.bin", b"\x00\x01\x02\x03" + PRIVATE_IP.encode() + b"\x00" + PRIVATE_IP_BYTES)
        self.add("notes/ok.md", b"nothing to see\n")
        self.assertHook("--staged", 1, BINARY_REFUSED + "notes/data.bin", absent=["notes/ok.md"])
        self.commit()
        self.assertHook("--all", 1, BINARY_REFUSED + "notes/data.bin", absent=["notes/ok.md"])

    def test_a_nul_byte_after_the_first_8000_bytes_still_makes_a_file_binary(self):
        """git diff looks for NUL bytes in the first 8000 bytes only, CI's check
        reads the whole blob: the hook must refuse what CI refuses."""
        self.add("late.txt", f"host {PRIVATE_IP}\n".encode() + b"a" * 9000 + b"\n\x00\n")
        self.assertHook("--staged", 1, BINARY_REFUSED + "late.txt")
        self.commit()
        self.assertHook("--all", 1, BINARY_REFUSED + "late.txt")

    def test_text_that_is_not_utf8_is_still_read(self):
        self.add("latin1.txt", b"caf\xe9 " + PRIVATE_IP.encode() + b"\n")
        self.assertHook("--staged", 1, IP_FOUND + "latin1.txt")
        self.commit()
        self.assertHook("--all", 1, IP_FOUND + "latin1.txt")

    def test_a_staged_rename_is_read_under_its_new_name(self):
        """-z output with renames would put the old and the new path in records
        of their own; a name like "1:x" must not be read as stage 1 of "x"."""
        self.add("old.txt", f"host {PRIVATE_IP}\n".encode())
        self.commit()
        new = "1:new\tname.txt"
        self.git("mv", "old.txt", new)
        self.assertHook("--staged", 1, IP_FOUND + new, absent=["old.txt", BINARY_REFUSED])

    def test_the_local_deny_list_reads_large_and_binary_files(self):
        """A match early in a large file is found (grep -q at the end of a pipe
        used to lose it to the writer's SIGPIPE), and the value is not printed."""
        (self.repo / ".git" / "hygiene-deny").write_text(f"# site values\n\n{SERIAL}\n", encoding="utf-8")
        self.add("big.txt", f"serial {SERIAL.lower()}\n".encode() + (b"x" * 79 + b"\n") * 4000)
        self.add("data.bin", b"\x00\x00" + SERIAL.encode() + b"\x00")
        self.add("ok.txt", b"nothing to see\n")
        self.assertHook("--staged", 1, "hygiene-deny found in big.txt", "hygiene-deny found in data.bin",
                        BINARY_REFUSED + "data.bin", absent=[SERIAL, SERIAL.lower(), "ok.txt"])


if __name__ == "__main__":
    unittest.main()
