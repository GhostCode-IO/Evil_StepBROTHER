"""setup.sh, the one-command install, in both of its modes.

Checkout mode (setup.sh next to install.sh and scripts/lib/common.sh):
config.env from config.sample.env with the client network detected from a
stubbed "ip" or given with --clients, and the host's other networks named;
--name and --snmpv3; an existing config.env kept except for the keys an
option names (each rewrite listed); --print-config and what it masks; the
host-name warning; the error without a default route; usage errors; a box
installed with settings from elsewhere (stop, or --new-config); one run at a
time; and install.sh's exit status relayed, with a "what now" that names only
the parts that are installed.

One-line mode (no checkout next to it, or read from stdin): a clone of a
local bare git repository made in the test, a fast-forward update on the next
run, git-core installed with dnf when git is missing, and the refusals (not a
git checkout, another origin, a checkout that cannot be fast-forwarded, not
root, not Fedora).

Same conventions as tests/test_shell_scripts.py: bash >= 4.2 (QLP_TEST_BASH
on macOS), stub commands on PATH, everything inside a temporary directory,
documentation-range addresses only. setup.sh's test-only hooks stand in for
root (QLP_TEST_EUID), Fedora's /etc/os-release (QLP_TEST_OS_RELEASE), the
root directory of the paths outside the checkout (QLP_TEST_ROOT: $T/sys) and
the installer itself (QLP_TEST_INSTALL_CMD). Nothing here touches the system.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import time
import unittest
from typing import Dict, List, Optional

from tests import test_shell_scripts as shell
from tests._util import REPO

from ql810wc import config as qconfig  # noqa: E402  (tests.test_shell_scripts put agent/ on sys.path)

SETUP = REPO / "setup.sh"
TEMPLATE = REPO / "config.sample.env"
RAW_URL = "https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh"
CLONE_URL = "https://github.com/GhostCode-IO/Evil_StepBROTHER.git"
FILLED = ("SNMP_PRINTER_ALLOWED_CIDRS", "PRINTER_NAME_SUFFIX", "SNMP_V3_ENABLE")
SECRETS = ("SNMP_V3_AUTH_PASS", "SNMP_V3_PRIV_PASS")
REAL_PASS = ("Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8", "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2")   # made up
GIT = shutil.which("git")

ROUTE = "default via 192.0.2.1 dev eth0 proto dhcp src 192.0.2.10 metric 100\n"
ADDR_ETH0 = ("2: eth0    inet 192.0.2.10/24 brd 192.0.2.255 scope global dynamic noprefixroute eth0\\"
             "       valid_lft 86251sec preferred_lft 86251sec\n")
ROUTE_GET = "192.0.2.1 dev eth0 src 192.0.2.10 uid 0 \n    cache \n"

# Stubs on top of those of tests/test_shell_scripts.py (same conventions).
IP = r'''
S="$QLP_TEST_STATE"
case "$*" in
  "-4 route show default") cat "$S/ip-route" 2>/dev/null ;;
  "-4 route get "*) cat "$S/ip-route-get" 2>/dev/null ;;
  *" addr show "*)
    dev="" prev=""
    for a in "$@"; do if [ "$prev" = dev ]; then dev="$a"; fi; prev="$a"; done
    cat "$S/ip-addr-$dev" 2>/dev/null ;;
esac
exit 0
'''
UNAME = r'''
if [ "${1:-}" = -n ] && [ -e "$QLP_TEST_STATE/hostname" ]; then cat "$QLP_TEST_STATE/hostname"; exit 0; fi
exec /usr/bin/uname "$@"
'''
# "Installs" git-core by putting a git wrapper next to the stubs.
DNF = r'''
case " $* " in
  *" git-core "*)
    if [ -n "${REAL_GIT:-}" ]; then
      printf '#!/bin/sh\nexec "%s" "$@"\n' "$REAL_GIT" >"$QLP_TEST_BIN/git"
      chmod 755 "$QLP_TEST_BIN/git"
    fi ;;
esac
exit "${DNF_RC:-0}"
'''
# "flock -n 9": free unless $QLP_TEST_STATE/lock-busy exists. Its calls go to
# flock.log, so that the calls log still shows only what changes something.
FLOCK = r'''#!/bin/sh
printf 'flock %s\n' "$*" >>"$QLP_TEST_STATE/flock.log"
if [ -e "$QLP_TEST_STATE/lock-busy" ]; then exit 1; fi
exit 0
'''
FAKE_INSTALL = r'''#!/bin/sh
printf 'install.sh %s\n' "$*" >>"$QLP_TEST_LOG"
if [ -n "${FAKE_INSTALL_READ:-}" ]; then cat >"$QLP_TEST_STATE/install-stdin"; fi
exit "${FAKE_INSTALL_RC:-0}"
'''
# What bash needs besides its builtins when git is missing (test_git_missing...).
TOOLS = ("cat", "chmod", "date", "dirname", "basename", "grep", "install", "mkdir", "rm", "ls", "sed",
         "awk", "tr", "head", "tail", "env", "sort", "cut", "mv", "cp")


def make_checkout(dest: pathlib.Path) -> pathlib.Path:
    """The files setup.sh needs in a checkout (from the working tree)."""
    for rel in ("setup.sh", "install.sh", "config.sample.env", "scripts/lib/common.sh", ".gitignore"):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
    shutil.copytree(REPO / "agent" / "ql810wc", dest / "agent" / "ql810wc",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def env_values(path: pathlib.Path) -> Dict[str, str]:
    return qconfig.parse_env_file(path).values


def mode_of(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@unittest.skipIf(shell.BASH is None, "bash >= 4.2 not available (common.sh needs it; set QLP_TEST_BASH)")
class SetupTestCase(shell.ShellTestCase):
    """A checkout in $T/checkout, the stubs, and a way to run setup.sh."""

    def setUp(self) -> None:
        super().setUp()
        for name, body in (("ip", IP), ("uname", UNAME), ("dnf", DNF)):
            self.stub(name, body)
        flock = self.bin / "flock"
        flock.write_text(FLOCK, encoding="utf-8")
        flock.chmod(0o755)
        self.checkout = make_checkout(self.root / "checkout")
        self.config = self.checkout / "config.env"
        self.sysroot = self.root / "sys"          # QLP_TEST_ROOT: stands in for /
        (self.sysroot / "run").mkdir(parents=True)
        self.state_dir = self.sysroot / "var/lib/ql810wc-proxy"
        self.manifest = self.state_dir / "manifest"
        self.os_release = self.write("os-release", 'NAME="Fedora Linux"\nID=fedora\nVERSION_ID=44\n')
        self.fake_install = self.write("fake-install.sh", FAKE_INSTALL)
        self.fake_install.chmod(0o755)
        self.network(ROUTE, {"eth0": ADDR_ETH0})
        (self.state / "hostname").write_text("qlbox\n", encoding="utf-8")

    def stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + shell._LOG_LINE + body, encoding="utf-8")
        path.chmod(0o755)

    def network(self, route: str, addrs: Dict[str, str], route_get: str = ROUTE_GET) -> None:
        (self.state / "ip-route").write_text(route, encoding="utf-8")
        (self.state / "ip-route-get").write_text(route_get, encoding="utf-8")
        for dev, text in addrs.items():
            (self.state / f"ip-addr-{dev}").write_text(text, encoding="utf-8")

    def base_env(self, path: Optional[str] = None) -> Dict[str, str]:
        return {
            "PATH": path or f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.root), "NO_COLOR": "1", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8",
            "QLP_TEST_STATE": str(self.state), "QLP_TEST_LOG": str(self.calls_log), "QLP_TEST_BIN": str(self.bin),
            "QLP_TEST_EUID": "0", "QLP_TEST_OS_RELEASE": str(self.os_release),
            "QLP_TEST_ROOT": str(self.sysroot), "QLP_TEST_INSTALL_CMD": str(self.fake_install),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        }

    def setup_sh(self, *args: str, script: Optional[pathlib.Path] = None, env: Optional[Dict[str, str]] = None,
                 path: Optional[str] = None) -> subprocess.CompletedProcess:
        full_env = self.base_env(path)
        full_env.update(env or {})
        return subprocess.run([shell.BASH, str(script or self.checkout / "setup.sh"), *args], env=full_env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def installs(self) -> List[str]:
        return [call for call in self.calls() if call.startswith("install.sh")]

    def detections(self) -> List[str]:
        return [call for call in self.calls() if call.startswith("ip -4 route show default")]

    def flock_calls(self) -> List[str]:
        log = self.state / "flock.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def assertOk(self, done: subprocess.CompletedProcess, code: int = 0) -> None:
        self.assertEqual(done.returncode, code, done.stdout + done.stderr)
        self.assertNotIn("failed (exit", done.stderr, "the ERR trap fired")


class CheckoutConfigTest(SetupTestCase):
    def test_new_config_from_the_default_route(self):
        done = self.setup_sh("--config-only")
        self.assertOk(done)
        self.assertEqual(mode_of(self.config), 0o600)
        values, defaults = env_values(self.config), env_values(TEMPLATE)
        self.assertEqual(values["SNMP_PRINTER_ALLOWED_CIDRS"], "192.0.2.0/24")
        self.assertEqual(values["PRINTER_NAME_SUFFIX"], "")
        self.assertEqual(values["SNMP_V3_ENABLE"], "no")
        self.assertEqual({k: v for k, v in values.items() if k not in FILLED},
                         {k: v for k, v in defaults.items() if k not in FILLED}, "everything else: the template")
        self.assertEqual(set(values), set(defaults))
        text = self.config.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# ql810wc-print-proxy — the site configuration (config.env), written by"))
        self.assertNotIn("TEMPLATE", text)
        self.assertIn("# Format: KEY=\"value\" lines only.", text, "the template's comments are kept")
        self.assertIn(f"config.env: {self.config}", done.stdout)
        self.assertEqual(self.installs(), [], "--config-only installs nothing")
        self.assertFalse(self.manifest.exists())

    def test_the_installer_parser_reads_it_alike(self):
        self.assertOk(self.setup_sh("--snmpv3", "--name", "Büro 2", "--config-only"))
        script = ('source "$REPO/scripts/lib/common.sh"; qlp_parse_env_file "$1"\n'
                  'for k in SNMP_PRINTER_ALLOWED_CIDRS PRINTER_NAME_SUFFIX SNMP_V3_ENABLE SNMP_V3_AUTH_PASS '
                  'SNMP_V3_PRIV_PASS SNMP_V3_ALLOWED_CIDRS; do printf "%s=%s\\n" "$k" "${!k}"; done')
        done = self.run_bash(script, str(self.config))
        self.assertOk(done)
        values = env_values(self.config)
        self.assertEqual(dict(line.split("=", 1) for line in done.stdout.splitlines()),
                         {k: values[k] for k in ("SNMP_PRINTER_ALLOWED_CIDRS", "PRINTER_NAME_SUFFIX", "SNMP_V3_ENABLE",
                                                 *SECRETS, "SNMP_V3_ALLOWED_CIDRS")})
        self.assertEqual(values["PRINTER_NAME_SUFFIX"], "Büro 2")

    def test_lowest_metric_default_route_and_all_its_networks(self):
        self.network("default via 198.51.100.1 dev wlan0 proto dhcp metric 600\n" + ROUTE,
                     {"eth0": ADDR_ETH0 + "2: eth0    inet 198.51.100.7/25 scope global secondary eth0\n",
                      "wlan0": "3: wlan0    inet 198.51.100.20/24 scope global wlan0\n"})
        self.assertOk(self.setup_sh("--config-only"))
        self.assertEqual(env_values(self.config)["SNMP_PRINTER_ALLOWED_CIDRS"], "192.0.2.0/24 198.51.100.0/25")

    def test_a_multipath_default_route(self):
        self.network("default proto static metric 50 \n\tnexthop via 203.0.113.1 dev eth1 weight 1 \n" + ROUTE,
                     {"eth0": ADDR_ETH0, "eth1": "4: eth1    inet 203.0.113.5/26 scope global eth1\n"})
        self.assertOk(self.setup_sh("--config-only"))
        self.assertEqual(env_values(self.config)["SNMP_PRINTER_ALLOWED_CIDRS"], "203.0.113.0/26")

    def test_clients_option(self):
        done = self.setup_sh("--clients", "192.0.2.77/24, 198.51.100.0/24 192.0.2.0/24", "--clients=2001:db8::/32",
                             "--config-only")
        self.assertOk(done)
        self.assertEqual(env_values(self.config)["SNMP_PRINTER_ALLOWED_CIDRS"],
                         "192.0.2.0/24 198.51.100.0/24 2001:db8::/32")
        self.assertIn("192.0.2.77/24 has host bits set; using 192.0.2.0/24", done.stderr)
        self.assertEqual(self.detections(), [], "--clients: nothing is detected")

    def test_bad_clients_are_usage_errors(self):
        for bad in ("192.0.2.0/33", "printer", "", " , ", "192.0.2.0/24,nonsense"):
            with self.subTest(clients=bad):
                done = self.setup_sh("--clients", bad, "--config-only")
                self.assertOk(done, 64)
                self.assertIn("--clients", done.stderr)
                self.assertFalse(self.config.exists())
        self.assertEqual(self.installs(), [])

    def test_name(self):
        self.assertOk(self.setup_sh("--name", "office", "--config-only"))
        self.assertEqual(env_values(self.config)["PRINTER_NAME_SUFFIX"], "office")
        self.config.unlink()
        self.assertOk(self.setup_sh("--name=" + "n" * 45, "--config-only"))
        self.assertEqual(env_values(self.config)["PRINTER_NAME_SUFFIX"], "n" * 45)
        self.config.unlink()
        for bad in ("n" * 46, "ü" * 23, 'a"b', "a$b", "a\\b", "a`b", "a\tb", "--snmpv3"):
            with self.subTest(name=bad):
                done = self.setup_sh("--name", bad, "--config-only")
                self.assertOk(done, 64)
                self.assertIn("--name", done.stderr)
                self.assertFalse(self.config.exists())

    def test_snmpv3_passphrases(self):
        done = self.setup_sh("--snmpv3", "--clients", "198.51.100.0/24", "--config-only")
        self.assertOk(done)
        self.assertEqual(mode_of(self.config), 0o600)
        values = env_values(self.config)
        self.assertEqual((values["SNMP_V3_ENABLE"], values["SNMP_V3_ALLOWED_CIDRS"]), ("yes", "198.51.100.0/24"))
        auth, priv = values["SNMP_V3_AUTH_PASS"], values["SNMP_V3_PRIV_PASS"]
        for secret in (auth, priv):
            self.assertGreaterEqual(len(secret), 24)
            self.assertRegex(secret, r"^[A-Za-z0-9]+$", "no quotes, $, backticks or backslashes")
            self.assertNotIn(secret, done.stdout + done.stderr, "a passphrase was printed")
        self.assertNotEqual(auth, priv)
        self.assertIn("the passphrases are in config.env (never printed)", done.stdout)
        before = self.config.read_bytes()
        again = self.setup_sh("--snmpv3", "--clients", "198.51.100.0/24", "--config-only")
        self.assertOk(again)
        self.assertEqual(self.config.read_bytes(), before, "the same command again changes nothing")
        self.assertNotIn(auth, again.stdout + again.stderr)

    def test_print_config_for_a_new_config(self):
        done = self.setup_sh("--print-config", "--snmpv3")
        self.assertOk(done)
        self.assertFalse(self.config.exists(), "--print-config changes nothing")
        printed = self.write("printed.env", done.stdout)
        values = env_values(printed)
        self.assertEqual(values["SNMP_PRINTER_ALLOWED_CIDRS"], "192.0.2.0/24")
        self.assertEqual((values["SNMP_V3_AUTH_PASS"], values["SNMP_V3_PRIV_PASS"]), ("********", "********"))
        self.assertEqual(self.installs(), [])
        self.assertFalse(self.manifest.exists())

    def test_print_config_masks_an_existing_config_and_changes_nothing(self):
        self.config.write_text(f'SNMP_PRINTER_ALLOWED_CIDRS="203.0.113.0/24"\nSNMP_V3_ENABLE="yes"\n'
                               f'SNMP_V3_AUTH_PASS="{REAL_PASS[0]}"\nSNMP_V3_PRIV_PASS=\'{REAL_PASS[1]}\'  # rotated\n',
                               encoding="utf-8")
        self.config.chmod(0o600)
        before = (self.config.read_bytes(), self.config.stat().st_mtime_ns)
        done = self.setup_sh("--print-config", "--name", "desk")
        self.assertOk(done)
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), before)
        for secret in REAL_PASS:
            self.assertNotIn(secret, done.stdout + done.stderr)
        self.assertIn('SNMP_V3_AUTH_PASS="********"\n', done.stdout)
        self.assertIn('SNMP_V3_PRIV_PASS="********"  # rotated\n', done.stdout)
        self.assertIn('PRINTER_NAME_SUFFIX="desk"', done.stdout, "what it would write")
        self.assertEqual(self.detections(), [])
        # Template placeholders are no secret: shown as they are.
        self.config.write_text('SNMP_V3_AUTH_PASS="CHANGE-ME-auth-passphrase"\n', encoding="utf-8")
        done = self.setup_sh("--print-config")
        self.assertOk(done)
        self.assertIn('SNMP_V3_AUTH_PASS="CHANGE-ME-auth-passphrase"', done.stdout)

    def test_print_config_masks_serials_and_site_communities(self):
        """Terminal output gets pasted into public issues: --print-config masks
        serial numbers and community names as the installer's logs do."""
        text = ('USB_SERIAL="Z9TESTSERIAL01"\nPERSONA_SERIAL="Z9PERSONA01"  # pinned\n'
                'SNMP_PRINTER_COMMUNITIES="public s1teSecret internal"\nSNMP_PRINTER_COMMUNITY="ops"\n'
                'PERSONA_MAC="00:00:5E:00:53:01"\n')
        self.config.write_text(text, encoding="utf-8")
        self.config.chmod(0o600)
        done = self.setup_sh("--print-config")
        self.assertOk(done)
        for secret in ("Z9TESTSERIAL01", "Z9PERSONA01", "s1teSecret"):
            self.assertNotIn(secret, done.stdout + done.stderr)
        for line in ('USB_SERIAL="Z9T***"', 'PERSONA_SERIAL="Z9P***"  # pinned',
                     'SNMP_PRINTER_COMMUNITIES="public s1t*** internal"',
                     'SNMP_PRINTER_COMMUNITY="ops"',          # 3 characters or fewer: as the logs show them
                     'PERSONA_MAC="00:00:5E:00:53:01"'):
            self.assertIn(line + "\n", done.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), text, "the file stays as it is")
        self.assertIn("serial numbers and site community names masked", done.stderr)

    def test_temporary_files_are_ignored_by_git_and_cleaned_up(self):
        old = time.time() - 3600
        stale = self.checkout / ".config.k3j4h5g6.tmp.env"
        stale.write_text('SNMP_V3_AUTH_PASS="left by an interrupted run"\n', encoding="utf-8")
        os.utime(stale, (old, old))
        young = self.checkout / ".config.a1b2c3d4.tmp.env"
        young.write_text("", encoding="utf-8")
        other = self.checkout / ".config.notes"
        other.write_text("", encoding="utf-8")
        os.utime(other, (old, old))
        self.assertOk(self.setup_sh("--config-only"))
        self.assertFalse(stale.exists(), "an hour old: removed")
        self.assertTrue(young.exists(), "younger than a minute: perhaps the file of a run still writing")
        self.assertTrue(other.exists(), "not one of setup.sh's names")
        self.assertEqual(sorted(p.name for p in self.checkout.glob(".config.*")), [young.name, other.name])
        if GIT is not None:
            # A repository of this test's own with the project's .gitignore:
            # git refuses a checkout that another user owns ("dubious
            # ownership"), as in CI's container, where root runs the tests.
            repo = self.root / "ignore-check"
            repo.mkdir()
            shutil.copy2(REPO / ".gitignore", repo / ".gitignore")
            git_env = {**self.base_env(), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
            subprocess.run([GIT, "init", "-q", str(repo)], check=True, capture_output=True, timeout=30, env=git_env)
            done = subprocess.run([GIT, "-C", str(repo), "check-ignore", "--no-index", "-q", stale.name],
                                  capture_output=True, text=True, timeout=30, env=git_env)
            self.assertEqual(done.returncode, 0, "git must ignore a temporary config file (*.env): " + done.stderr)

    def test_networks_the_default_route_leaves_out(self):
        (self.state / "ip-addr-").write_text(
            ADDR_ETH0 + "3: wlan0    inet 198.51.100.20/24 brd 198.51.100.255 scope global dynamic wlan0\n"
            "4: podman0    inet 203.0.113.1/24 brd 203.0.113.255 scope global podman0\n"
            "5: eth1    inet 192.0.2.77/24 scope global secondary eth1\n", encoding="utf-8")
        for dev in ("eth0", "wlan0", "eth1"):
            (self.sysroot / "sys/class/net" / dev / "device").mkdir(parents=True)
        done = self.setup_sh("--config-only")
        self.assertOk(done)
        self.assertEqual(env_values(self.config)["SNMP_PRINTER_ALLOWED_CIDRS"], "192.0.2.0/24", "the default stays")
        self.assertIn("this host is also on 198.51.100.0/24 (wlan0), which the client network leaves out", done.stderr)
        self.assertIn('To admit them too, pass --clients "192.0.2.0/24 198.51.100.0/24"', done.stderr)
        self.assertIn("not a client network: 203.0.113.0/24 on podman0 (no device", done.stderr)
        self.assertNotIn("eth1", done.stderr, "its network is the client network already")
        # Nothing is detected, so nothing is reported, when --clients names the networks.
        self.config.unlink()
        done = self.setup_sh("--clients", "192.0.2.0/24", "--config-only")
        self.assertOk(done)
        self.assertNotIn("also on", done.stderr)

    def test_print_config_needs_no_root(self):
        done = self.setup_sh("--print-config", env={"QLP_TEST_EUID": "1000"})
        self.assertOk(done)
        self.assertIn('SNMP_PRINTER_ALLOWED_CIDRS="192.0.2.0/24"', done.stdout)

    def test_everything_else_needs_root(self):
        done = self.setup_sh("--config-only", env={"QLP_TEST_EUID": "1000"})
        self.assertOk(done, 1)
        self.assertIn("must run as root", done.stderr)
        self.assertFalse(self.config.exists())

    def test_hostname_warning(self):
        for host, shown in (("fedora", "fedora"), ("localhost.localdomain", "localhost"), ("localhost", "localhost")):
            with self.subTest(host=host):
                (self.state / "hostname").write_text(host + "\n", encoding="utf-8")
                done = self.setup_sh("--print-config")
                self.assertOk(done)
                self.assertIn(f'the printer would appear as "Brother QL-810W [{shown}]"', done.stderr)
                self.assertIn("sudo hostnamectl set-hostname NAME", done.stderr)
                self.assertIn("--name NAME", done.stderr)
                done = self.setup_sh("--print-config", "--name", "office")
                self.assertNotIn("hostnamectl", done.stderr)
        (self.state / "hostname").write_text("qlbox\n", encoding="utf-8")
        self.assertNotIn("hostnamectl", self.setup_sh("--print-config").stderr)
        (self.state / "hostname").write_text("fedora\n", encoding="utf-8")
        self.config.write_text('PRINTER_NAME_SUFFIX="front desk"\n', encoding="utf-8")
        self.config.chmod(0o600)
        self.assertNotIn("hostnamectl", self.setup_sh("--config-only").stderr, "the config names the printer")

    def test_no_default_route(self):
        for route, addrs in (("", {}), (ROUTE, {"eth0": ""})):
            with self.subTest(route=route):
                self.network(route, addrs)
                done = self.setup_sh("--config-only")
                self.assertOk(done, 1)
                self.assertIn("--clients CIDR", done.stderr)
                self.assertFalse(self.config.exists())
        self.assertOk(self.setup_sh("--clients", "192.0.2.0/24", "--config-only"))

    def test_usage_errors(self):
        for args in (("--bogus",), ("--name",), ("--clients",), ("--config-only", "--print-config"),
                     ("--config-only", "--", "--only", "10"), ("--dir", "--ref")):
            with self.subTest(args=args):
                done = self.setup_sh(*args)
                self.assertOk(done, 64)
                self.assertIn("see setup.sh --help", done.stderr)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.installs(), [])

    def test_help(self):
        done = self.setup_sh("--help", env={"QLP_TEST_EUID": "1000"})
        self.assertOk(done)
        for text in (f"curl -fsSL {RAW_URL} | sudo bash\n",
                     f"curl -fsSL {RAW_URL} | sudo bash -s -- --clients 192.0.2.0/24 --name office\n",
                     "sudo ./setup.sh [options]", CLONE_URL, "/usr/local/src/ql810wc-print-proxy",
                     "--clients", "--name", "--snmpv3", "--new-config", "--config-only", "--print-config", "--dir",
                     "--ref",
                     "QLP_REPO_URL", "QLP_SRC_DIR", "QLP_REPO_REF", "Exit codes", "64  usage error"):
            self.assertIn(text, done.stdout)
        self.assertNotIn("QLP_TEST_", done.stdout, "the test hooks are no options")
        self.assertEqual(done.stderr, "")
        self.assertEqual(self.setup_sh("-h").stdout, done.stdout)


class ExistingConfigTest(SetupTestCase):
    CUSTOM = ('# my box\nRAW_PORT="9101"\nPRINTER_NAME_SUFFIX="old"   # front desk\n'
              "SNMP_PRINTER_ALLOWED_CIDRS='203.0.113.0/24'\n  SNMP_V3_ENABLE=auto\n")

    def setUp(self) -> None:
        super().setUp()
        self.config.write_text(self.CUSTOM, encoding="utf-8")
        self.config.chmod(0o600)

    def changed_lines(self) -> List[tuple]:
        old, new = self.CUSTOM.split("\n"), self.config.read_text(encoding="utf-8").split("\n")
        self.assertEqual(len(old), len(new), new)
        return [(a, b) for a, b in zip(old, new) if a != b]

    def test_kept_as_it_is(self):
        before = os.stat(self.config)
        done = self.setup_sh("--config-only")
        self.assertOk(done)
        after = os.stat(self.config)
        self.assertEqual(self.config.read_text(encoding="utf-8"), self.CUSTOM)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns), "not even rewritten")
        self.assertEqual(self.detections(), [], "an existing config.env: nothing is detected")
        self.assertIn("kept as it is", done.stderr)

    def test_only_the_named_keys_are_rewritten_in_place(self):
        done = self.setup_sh("--name", "office", "--config-only")
        self.assertOk(done)
        self.assertEqual(self.changed_lines(), [('PRINTER_NAME_SUFFIX="old"   # front desk',
                                                 'PRINTER_NAME_SUFFIX="office"   # front desk')])
        self.assertIn("(rewrote PRINTER_NAME_SUFFIX)", done.stderr)
        self.assertIn('PRINTER_NAME_SUFFIX: "old" -> "office"', done.stderr, "each rewrite is listed")
        self.assertEqual(mode_of(self.config), 0o600)
        self.CUSTOM = self.config.read_text(encoding="utf-8")
        done = self.setup_sh("--clients", "192.0.2.0/24", "--config-only")
        self.assertOk(done)
        self.assertEqual(self.changed_lines(), [("SNMP_PRINTER_ALLOWED_CIDRS='203.0.113.0/24'",
                                                 'SNMP_PRINTER_ALLOWED_CIDRS="192.0.2.0/24"')])
        self.assertIn('SNMP_PRINTER_ALLOWED_CIDRS: "203.0.113.0/24" -> "192.0.2.0/24"', done.stderr)
        self.assertEqual(self.detections(), [])

    def test_a_missing_key_is_appended(self):
        self.config.write_text('RAW_PORT="9101"\n', encoding="utf-8")
        self.assertOk(self.setup_sh("--name", "office", "--config-only"))
        self.assertEqual(self.config.read_text(encoding="utf-8"),
                         'RAW_PORT="9101"\n\n# Added by setup.sh:\nPRINTER_NAME_SUFFIX="office"\n')

    def test_a_rewrite_leaves_mode_0600(self):
        self.config.chmod(0o644)
        self.assertOk(self.setup_sh("--name", "office", "--config-only"))
        self.assertEqual(mode_of(self.config), 0o600)

    def test_snmpv3_keeps_real_passphrases_and_a_custom_range(self):
        self.config.write_text(f'SNMP_V3_AUTH_PASS="{REAL_PASS[0]}"\nSNMP_V3_PRIV_PASS="{REAL_PASS[1]}"\n'
                               'SNMP_V3_ALLOWED_CIDRS="198.51.100.9/32"\n', encoding="utf-8")
        done = self.setup_sh("--snmpv3", "--config-only")
        self.assertOk(done)
        values = env_values(self.config)
        self.assertEqual((values["SNMP_V3_ENABLE"], values["SNMP_V3_AUTH_PASS"], values["SNMP_V3_PRIV_PASS"],
                          values["SNMP_V3_ALLOWED_CIDRS"]), ("yes", *REAL_PASS, "198.51.100.9/32"))

    def test_snmpv3_replaces_placeholders(self):
        done = self.setup_sh("--snmpv3", "--config-only")
        self.assertOk(done)
        values = env_values(self.config)
        self.assertEqual(values["SNMP_V3_ENABLE"], "yes")
        self.assertEqual(values["SNMP_V3_ALLOWED_CIDRS"], "203.0.113.0/24", "the template's example is replaced")
        for key in SECRETS:
            self.assertRegex(values[key], r"^[A-Za-z0-9]{24,}$")
            self.assertNotIn(values[key], done.stdout + done.stderr)
            self.assertIn(f"{key}: a new random passphrase (never printed)", done.stderr)
        self.assertEqual(values["RAW_PORT"], "9101")

    def test_the_same_options_again_keep_a_monitoring_range_of_its_own(self):
        """--clients names SNMP_PRINTER_ALLOWED_CIDRS, so it is rewritten;
        SNMP_V3_ALLOWED_CIDRS, the monitoring system's range, is kept."""
        self.config.write_text(f'SNMP_PRINTER_ALLOWED_CIDRS="192.0.2.0/26"\nSNMP_V3_ENABLE="yes"\n'
                               f'SNMP_V3_AUTH_PASS="{REAL_PASS[0]}"\nSNMP_V3_PRIV_PASS="{REAL_PASS[1]}"\n'
                               'SNMP_V3_ALLOWED_CIDRS="198.51.100.9/32"\nPRINTER_NAME_SUFFIX="office"\n',
                               encoding="utf-8")
        done = self.setup_sh("--clients", "192.0.2.0/24", "--snmpv3", "--name", "office", "--config-only")
        self.assertOk(done)
        values = env_values(self.config)
        self.assertEqual(values["SNMP_PRINTER_ALLOWED_CIDRS"], "192.0.2.0/24")
        self.assertEqual(values["SNMP_V3_ALLOWED_CIDRS"], "198.51.100.9/32", "a range of its own is kept")
        self.assertEqual((values["SNMP_V3_AUTH_PASS"], values["SNMP_V3_PRIV_PASS"]), REAL_PASS)
        self.assertIn('SNMP_PRINTER_ALLOWED_CIDRS: "192.0.2.0/26" -> "192.0.2.0/24"', done.stderr)
        self.assertNotIn("SNMP_V3_ALLOWED_CIDRS:", done.stderr)

    def test_the_monitoring_range_follows_new_clients_while_it_is_no_range_of_its_own(self):
        for v3 in ("192.0.2.50/32", "", "203.0.113.0/24"):   # the template's example, empty, the clients' range
            with self.subTest(v3=v3):
                self.config.write_text(f"SNMP_PRINTER_ALLOWED_CIDRS='203.0.113.0/24'\nSNMP_V3_ALLOWED_CIDRS=\"{v3}\"\n",
                                       encoding="utf-8")
                self.assertOk(self.setup_sh("--clients", "198.51.100.0/24", "--snmpv3", "--config-only"))
                self.assertEqual(env_values(self.config)["SNMP_V3_ALLOWED_CIDRS"], "198.51.100.0/24",
                                 "the new client range, not the old one")


class InstallRelayTest(SetupTestCase):
    # The files of the parts install.sh sets up (steps 20, 30 and 40), below QLP_TEST_ROOT.
    PARTS = {"data": "etc/systemd/system/ql810wc-proxyd.socket",
             "persona": "etc/snmp/snmpd.conf.d/ql810wc-proxy.conf",
             "bonjour": "etc/avahi/services/ql810wc-proxy.service"}

    def installed(self, *parts: str) -> None:
        for part in parts or tuple(self.PARTS):
            path = self.sysroot / self.PARTS[part]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def run_install(self, code: int, *args: str) -> subprocess.CompletedProcess:
        return self.setup_sh(*args, env={"FAKE_INSTALL_RC": str(code)})

    def test_success_says_what_now(self):
        self.installed()
        done = self.run_install(0)
        self.assertOk(done)
        self.assertEqual(self.installs(), ["install.sh "])
        for text in ("\nql810wc-print-proxy is installed.\n",
                     'P-touch Editor (Mac) lists the printer as "Brother QL-810W [qlbox]";\n'
                     "iPrint&Label (iOS) finds it too.",
                     '"Connect to printer with an IP address" there and enter 192.0.2.10\n(not tested yet).',
                     f"In {self.checkout}:", "Settings:  config.env; after a change, run: sudo ./setup.sh",
                     "pull --ff-only && sudo ./setup.sh", "Check:     sudo ./selftest.sh",
                     "Remove:    sudo ./uninstall.sh"):
            self.assertIn(text, done.stdout)
        self.assertNotIn("Not installed yet", done.stdout)
        self.assertNotIn("one-line install", done.stdout, "not the one-liner's checkout")

    def test_the_configured_address_and_name(self):
        self.installed()
        self.config.write_text('PROXY_IP="198.51.100.20"\nPRINTER_NAME_SUFFIX="front desk"\n', encoding="utf-8")
        self.config.chmod(0o600)
        done = self.run_install(0)
        self.assertOk(done)
        self.assertIn('"Brother QL-810W [front desk]"', done.stdout)
        self.assertIn("enter 198.51.100.20\n(not tested yet).", done.stdout)

    def test_bonjour_off(self):
        self.installed("data", "persona")
        self.config.write_text('ENABLE_MDNS="no"\n', encoding="utf-8")
        self.config.chmod(0o600)
        done = self.run_install(0)
        self.assertOk(done)
        self.assertIn('Bonjour is off (ENABLE_MDNS="no"): in P-touch Editor (Mac), use "Connect to\n'
                      'printer with an IP address" and enter 192.0.2.10 (not tested yet).', done.stdout)
        self.assertNotIn("lists the printer", done.stdout)

    def test_only_what_is_installed_is_promised(self):
        """Options after "--" may leave steps out: the summary names what is
        missing instead of promising discovery, and no selftest result."""
        for args, parts, headline, missing in (
                (("--data-plane-only",), ("data",), "ql810wc-print-proxy is installed.",
                 "the SNMP persona (step 30); the Bonjour record (step 40)"),
                (("--skip", "40"), ("data", "persona"), "ql810wc-print-proxy is installed.",
                 "the Bonjour record (step 40)"),
                (("--only", "20"), ("data",), "install.sh finished (install.sh --only 20); no selftest ran.",
                 "the SNMP persona (step 30); the Bonjour record (step 40)"),
                (("--only", "30"), ("persona",), "install.sh finished (install.sh --only 30); no selftest ran.",
                 "the data plane, TCP 9100 to the printer (step 20); the Bonjour record (step 40)")):
            with self.subTest(args=args):
                for path in self.PARTS.values():
                    (self.sysroot / path).unlink(missing_ok=True)
                self.installed(*parts)
                done = self.run_install(0, "--", *args)
                self.assertOk(done)
                self.assertIn(f"\n{headline}\n", done.stdout)
                self.assertIn(f"Not installed yet: {missing}.\n", done.stdout)
                self.assertIn("To install every step: sudo ./setup.sh", done.stdout)
                self.assertNotIn("lists the printer", done.stdout)
        self.installed()
        done = self.run_install(0, "--", "--no-selftest")
        self.assertOk(done)
        self.assertIn("install.sh finished (install.sh --no-selftest); no selftest ran.", done.stdout)
        self.assertIn("lists the printer", done.stdout)

    def test_installer_options_after_the_separator(self):
        self.assertOk(self.run_install(0, "--name", "office", "--", "--data-plane-only"))
        self.assertEqual(self.installs(), ["install.sh --data-plane-only"])

    def test_a_person_must_act_first(self):
        done = self.run_install(3, "--name", "front desk", "--", "--skip", "40")
        self.assertOk(done, 3)
        self.assertIn("HUMAN", done.stderr)
        self.assertIn("a person must act first", done.stderr)
        self.assertIn(f"Then run again: sudo {self.checkout}/setup.sh --name 'front desk' -- --skip 40",
                      done.stderr)
        self.assertNotIn("is installed", done.stdout)

    def test_errors(self):
        for code, want in ((1, 1), (2, 1), (130, 1), (64, 64)):
            with self.subTest(code=code):
                done = self.run_install(code)
                self.assertOk(done, want)
                if want == 1:
                    self.assertIn(f"install.sh stopped with exit {code}", done.stderr)
                    self.assertIn("/var/log/ql810wc-proxy/", done.stderr)
                else:
                    self.assertIn('install.sh does not accept the options after "--"', done.stderr)
                self.assertNotIn("is installed", done.stdout)


class InstalledBoxTest(SetupTestCase):
    """A box installed from another checkout (runtime.env exists, and
    install.sh recorded the config.env it applied), and this checkout has no
    config.env: setup.sh stops before it writes anything."""

    def setUp(self) -> None:
        super().setUp()
        self.old = self.write("home/old-checkout/config.env", 'SNMP_V3_ENABLE="yes"\nPRINTER_NAME_SUFFIX="desk"\n')
        self.write("home/old-checkout/setup.sh", "")
        self.runtime = self.sysroot / "etc/ql810wc-proxy/runtime.env"
        self.runtime.parent.mkdir(parents=True)
        self.runtime.write_text('RAW_PORT="9100"\n', encoding="utf-8")
        self.record(str(self.old))

    def record(self, text: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "config-path").write_text(text + "\n", encoding="utf-8")

    def test_stops_before_writing_anything(self):
        for args in ((), ("--config-only",), ("--clients", "198.51.100.0/24", "--name", "x"), ("--snmpv3",)):
            with self.subTest(args=args):
                self.calls_log.unlink(missing_ok=True)
                done = self.setup_sh(*args)
                self.assertOk(done, 3)
                self.assertFalse(self.config.exists())
                self.assertEqual(self.installs(), [])
                self.assertEqual(self.detections(), [], "nothing detected either")
                for text in (f"this box is already installed with the settings in {self.old}, but {self.config} does not",
                             f"sudo install -m 600 -o root -g root {self.old} {self.config}",
                             f"or run setup.sh in the checkout you installed from: sudo {self.old.parent}/setup.sh",
                             "To start from new settings instead"):
                    self.assertIn(text, done.stderr)

    def test_the_command_to_start_anew_keeps_the_options(self):
        done = self.setup_sh("--name", "front desk", "--", "--skip", "40")
        self.assertOk(done, 3)
        self.assertIn(f"sudo {self.checkout}/setup.sh --name 'front desk' --new-config -- --skip 40", done.stderr)

    def test_new_config_starts_from_new_settings(self):
        done = self.setup_sh("--new-config", "--name", "office")
        self.assertOk(done)
        values = env_values(self.config)
        self.assertEqual((values["SNMP_V3_ENABLE"], values["PRINTER_NAME_SUFFIX"]), ("no", "office"))
        self.assertEqual(self.installs(), ["install.sh "])

    def test_a_copy_of_the_settings_goes_on(self):
        shutil.copy2(self.old, self.config)
        self.config.chmod(0o600)
        done = self.setup_sh("--config-only")
        self.assertOk(done)
        self.assertEqual(env_values(self.config)["PRINTER_NAME_SUFFIX"], "desk")
        done = self.setup_sh("--new-config", "--config-only")
        self.assertOk(done)
        self.assertIn(f"--new-config: {self.config} exists and is kept", done.stderr)
        self.assertEqual(env_values(self.config)["PRINTER_NAME_SUFFIX"], "desk")

    def test_print_config_only_warns(self):
        done = self.setup_sh("--print-config")
        self.assertOk(done)
        self.assertIn('SNMP_V3_ENABLE="no"', done.stdout, "what a new config.env would hold")
        self.assertIn(f"this box is installed with the settings in {self.old}, not with the file above", done.stderr)
        self.assertFalse(self.config.exists())

    def test_settings_that_are_gone_or_unknown(self):
        self.old.unlink()
        done = self.setup_sh()
        self.assertOk(done, 3)
        self.assertIn(f"({self.old}, which is gone)", done.stderr)
        (self.state_dir / "config-path").unlink()
        done = self.setup_sh()
        self.assertOk(done, 3)
        self.assertIn("(which config.env is not recorded)", done.stderr)

    def test_no_guard_where_nothing_would_be_lost(self):
        self.record("none")   # that install ran on the template defaults
        self.assertOk(self.setup_sh("--config-only"))
        self.config.unlink()
        self.record(str(self.old))
        self.runtime.unlink()   # not installed (any more)
        self.assertOk(self.setup_sh("--config-only"))


class SetupLockTest(SetupTestCase):
    def test_one_run_at_a_time(self):
        self.assertOk(self.setup_sh("--config-only"))
        self.assertEqual(self.flock_calls(), ["flock -n 9"])
        self.assertTrue((self.sysroot / "run/ql810wc-proxy.lock").exists())
        self.config.unlink()
        (self.state / "lock-busy").touch()
        done = self.setup_sh()
        self.assertOk(done, 1)
        self.assertIn("another setup.sh, install.sh or uninstall.sh is changing this box right now", done.stderr)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.installs(), [])

    def test_print_config_takes_no_lock(self):
        (self.state / "lock-busy").touch()
        self.assertOk(self.setup_sh("--print-config"))
        self.assertEqual(self.flock_calls(), [])


@unittest.skipIf(GIT is None, "git is not installed")
class BootstrapTest(SetupTestCase):
    """The one-liner: setup.sh alone, a bare repository as its origin."""

    def setUp(self) -> None:
        super().setUp()
        self.work = self.root / "work"
        shutil.copytree(self.checkout, self.work)
        self.git("init", "-q", "-b", "main", cwd=self.work)
        self.commit("first")
        self.origin = self.root / "origin.git"
        self.git("clone", "-q", "--bare", str(self.work), str(self.origin))
        self.download = self.root / "download" / "setup.sh"
        self.download.parent.mkdir()
        shutil.copy2(SETUP, self.download)
        self.src = self.root / "src" / "ql810wc-print-proxy"
        self.repo_env = {"QLP_REPO_URL": str(self.origin), "QLP_SRC_DIR": str(self.src)}

    def git(self, *args: str, cwd: Optional[pathlib.Path] = None) -> str:
        done = subprocess.run([GIT, *args], cwd=str(cwd or self.root), capture_output=True, text=True, timeout=60,
                              env={**self.base_env(), "GIT_AUTHOR_NAME": "test", "GIT_COMMITTER_NAME": "test",
                                   "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                   "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def commit(self, message: str, repo: Optional[pathlib.Path] = None, push: bool = False) -> str:
        repo = repo or self.work
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "--no-gpg-sign", "-m", message, cwd=repo)
        if push:
            self.git("push", "-q", str(self.origin), "HEAD:main", cwd=repo)
        return self.git("rev-parse", "HEAD", cwd=repo)

    def bootstrap(self, *args: str, env: Optional[Dict[str, str]] = None,
                  path: Optional[str] = None) -> subprocess.CompletedProcess:
        return self.setup_sh(*args, script=self.download, env={**self.repo_env, **(env or {})}, path=path)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD", cwd=self.src)

    def test_clone_then_the_checkouts_setup(self):
        done = self.bootstrap("--config-only")
        self.assertOk(done)
        self.assertEqual(self.head(), self.git("rev-parse", "main", cwd=self.origin))
        config = self.src / "config.env"
        self.assertEqual(mode_of(config), 0o600)
        self.assertEqual(env_values(config)["SNMP_PRINTER_ALLOWED_CIDRS"], "192.0.2.0/24")
        self.assertIn(f"continuing with {self.src}/setup.sh", done.stderr)
        self.assertIn(f"config.env: {config}", done.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("dnf")], "git was there: nothing installed")
        self.assertFalse(self.manifest.exists())
        self.assertFalse((self.checkout / "config.env").exists())
        self.assertEqual(self.flock_calls(), ["flock -n 9", "flock -n 9"], "taken, then taken over by the checkout")
        self.assertTrue((self.sysroot / "run/ql810wc-proxy.lock").exists())

    def test_a_second_run_changes_nothing(self):
        (self.state / "lock-busy").touch()
        done = self.bootstrap("--config-only")
        self.assertOk(done, 1)
        self.assertIn("another setup.sh, install.sh or uninstall.sh is changing this box right now", done.stderr)
        self.assertFalse(self.src.exists(), "no clone")
        self.assertEqual(self.calls(), [])

    def test_the_next_run_fast_forwards(self):
        self.assertOk(self.bootstrap("--config-only"))
        config = (self.src / "config.env").read_bytes()
        (self.work / "NEWS").write_text("second\n", encoding="utf-8")
        new = self.commit("second", push=True)
        done = self.bootstrap("--config-only")
        self.assertOk(done)
        self.assertEqual(self.head(), new)
        self.assertTrue((self.src / "NEWS").exists())
        self.assertEqual((self.src / "config.env").read_bytes(), config, "config.env is kept")
        self.assertIn("fast-forward only", done.stderr)
        done = self.bootstrap("--config-only")
        self.assertOk(done)
        self.assertIn("is up to date with main", done.stderr)

    def piped(self, script: str, *args: str, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        """As "curl ... | sudo bash -s -- ARGS": the script on stdin. (bash's
        syntax errors about a cut-off script can show the end of input as a
        0xff byte: stderr is decoded leniently.)"""
        full_env = {**self.base_env(), **self.repo_env, **(env or {})}
        return subprocess.run([shell.BASH, "-s", "--", *args], env=full_env, input=script, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=120)

    def test_read_from_stdin(self):
        done = self.piped(SETUP.read_text(encoding="utf-8"), "--config-only", "--name", "office")
        self.assertOk(done)
        self.assertEqual(env_values(self.src / "config.env")["PRINTER_NAME_SUFFIX"], "office")
        done = self.piped(SETUP.read_text(encoding="utf-8"), "--help")
        self.assertOk(done)
        self.assertIn(RAW_URL, done.stdout)

    def test_messages_use_the_c_locale(self):
        """The one-liner prints messages before common.sh is loaded: it sets
        the C locale itself (under some locales date prints a year of another
        calendar)."""
        self.stub("date", 'printf "date-LC_ALL=%s" "${LC_ALL:-}"\n')
        done = self.bootstrap("--config-only", env={"LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8"})
        self.assertOk(done)
        self.assertIn("cloning ", done.stderr)
        stamps = re.findall(r"date-LC_ALL=(\S*)", done.stderr)
        self.assertTrue(stamps, done.stderr)
        self.assertEqual(set(stamps), {"C.UTF-8"}, done.stderr)

    def test_children_never_read_the_script(self):
        """Piped into bash, setup.sh hands its children /dev/null as stdin:
        nothing can swallow what bash has not read yet."""
        script = SETUP.read_text(encoding="utf-8") + "echo LEFTOVER-RAN\n"
        done = self.piped(script, "--name", "office", env={"FAKE_INSTALL_READ": "1"})
        self.assertOk(done)
        self.assertEqual(self.installs(), ["install.sh "])
        self.assertEqual((self.state / "install-stdin").read_text(encoding="utf-8"), "")
        self.assertNotIn("LEFTOVER-RAN", done.stdout)

    def test_a_truncated_download_runs_nothing(self):
        text = SETUP.read_text(encoding="utf-8")
        cuts = [len(text) * share // 8 for share in range(1, 8)] + [len(text) - len('main "$@"\n')]
        for cut in cuts:
            with self.subTest(cut=cut):
                done = self.piped(text[:cut], "--config-only")
                self.assertEqual(done.stdout, "", f"cut at {cut}")
                self.assertNotIn("[setup]", done.stderr, f"cut at {cut}: setup.sh ran")
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.src.exists())

    def path_without_git(self) -> str:
        """The stubs plus wrappers for the few tools setup.sh runs: no git."""
        tools = self.root / "tools"
        tools.mkdir(exist_ok=True)
        wanted = {name: shutil.which(name) for name in TOOLS}
        wanted["python3"] = sys.executable
        for name, real in wanted.items():
            self.assertIsNotNone(real, name)
            wrapper = tools / name
            wrapper.write_text(f'#!/bin/sh\nexec "{real}" "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
        path = f"{self.bin}{os.pathsep}{tools}"
        self.assertIsNone(shutil.which("git", path=path))
        return path

    def test_git_missing_is_installed_with_dnf(self):
        done = self.bootstrap("--config-only", "--clients", "198.51.100.0/24", env={"REAL_GIT": GIT},
                              path=self.path_without_git())
        self.assertOk(done)
        self.assertIn("dnf -y --setopt=install_weak_deps=False install git-core", self.calls())
        self.assertIn("installing git-core", done.stderr)
        self.assertIn("pkg\tgit-core", self.manifest.read_text(encoding="utf-8").splitlines(),
                      "uninstall.sh lists it")
        self.assertEqual(env_values(self.src / "config.env")["SNMP_PRINTER_ALLOWED_CIDRS"], "198.51.100.0/24")

    def test_dnf_failing(self):
        done = self.bootstrap("--config-only", env={"DNF_RC": "1"}, path=self.path_without_git())
        self.assertOk(done, 1)
        self.assertIn("could not install git-core with dnf", done.stderr)
        self.assertFalse(self.src.exists())

    def test_an_empty_directory_is_cloned_into(self):
        self.src.mkdir(parents=True)
        self.assertOk(self.bootstrap("--config-only"))
        self.assertTrue((self.src / "install.sh").exists())

    def test_refuses_a_directory_that_is_no_checkout(self):
        self.src.mkdir(parents=True)
        (self.src / "notes.txt").write_text("mine\n", encoding="utf-8")
        done = self.bootstrap()
        self.assertOk(done, 1)
        self.assertIn("is not a git checkout: not changing it", done.stderr)
        self.assertEqual(sorted(p.name for p in self.src.iterdir()), ["notes.txt"])
        self.assertEqual(self.calls(), [], "nothing ran: no dnf, no installer")

    def test_refuses_a_checkout_of_another_repository(self):
        self.assertOk(self.bootstrap("--config-only"))
        before = self.head()
        done = self.bootstrap(env={"QLP_REPO_URL": str(self.root / "fork.git")})
        self.assertOk(done, 1)
        self.assertIn(f"is a checkout of {self.origin}, not of {self.root / 'fork.git'}", done.stderr)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.installs(), [])

    def test_refuses_a_checkout_that_cannot_fast_forward(self):
        self.assertOk(self.bootstrap("--config-only"))
        (self.src / "LOCAL").write_text("local change\n", encoding="utf-8")
        local = self.commit("local", repo=self.src)
        (self.work / "NEWS").write_text("upstream\n", encoding="utf-8")
        self.commit("upstream", push=True)
        done = self.bootstrap()
        self.assertOk(done, 1)
        self.assertIn("cannot be fast-forwarded to main", done.stderr)
        self.assertIn("Nothing was changed", done.stderr)
        self.assertEqual(self.head(), local)
        self.assertFalse((self.src / "NEWS").exists())
        self.assertEqual(self.installs(), [])

    def test_dir_and_ref(self):
        self.git("tag", "v0.1", cwd=self.work)
        self.git("push", "-q", str(self.origin), "v0.1", cwd=self.work)
        (self.work / "NEWS").write_text("later\n", encoding="utf-8")
        self.commit("later", push=True)
        elsewhere = self.root / "elsewhere"
        done = self.bootstrap("--dir", str(elsewhere), "--ref", "v0.1", "--config-only")
        self.assertOk(done)
        self.assertTrue((elsewhere / "config.env").exists())
        self.assertFalse((elsewhere / "NEWS").exists(), "checked out at the tag")
        self.assertFalse(self.src.exists())
        self.assertNotIn("ignored", done.stderr, "the checkout's setup.sh takes --dir/--ref silently")
        for ref in ("-x", "a..b", "bad ref"):
            with self.subTest(ref=ref):
                self.assertOk(self.bootstrap(f"--ref={ref}", "--config-only"), 64)

    def test_not_a_ql810wc_print_proxy_repository(self):
        other = self.root / "other"
        other.mkdir()
        shutil.copy2(SETUP, other / "setup.sh")
        self.git("init", "-q", "-b", "main", cwd=other)
        self.commit("only setup.sh", repo=other)
        bare = self.root / "other.git"
        self.git("clone", "-q", "--bare", str(other), str(bare))
        done = self.bootstrap("--config-only", env={"QLP_REPO_URL": str(bare)})
        self.assertOk(done, 1)
        self.assertIn("not a ql810wc-print-proxy checkout", done.stderr)

    def test_root_and_fedora(self):
        done = self.bootstrap(env={"QLP_TEST_EUID": "1000"})
        self.assertOk(done, 1)
        self.assertIn(f'curl -fsSL {RAW_URL} | sudo bash', done.stderr)
        self.write("os-release", "ID=debian\nVERSION_ID=12\n")
        done = self.bootstrap()
        self.assertOk(done, 1)
        self.assertIn("not Fedora", done.stderr)
        self.assertFalse(self.src.exists())
        self.write("os-release", 'ID="fedora"\nVERSION_ID=43\n')
        done = self.bootstrap("--config-only")
        self.assertOk(done)
        self.assertIn("tested on Fedora 44", done.stderr)

    def test_print_config_uses_the_checkout_as_it_is(self):
        done = self.bootstrap("--print-config")
        self.assertOk(done, 1)
        self.assertIn("--print-config changes nothing", done.stderr)
        self.assertFalse(self.src.exists())
        self.assertOk(self.bootstrap("--config-only"))
        before = self.head()
        (self.work / "NEWS").write_text("upstream\n", encoding="utf-8")
        self.commit("upstream", push=True)
        done = self.bootstrap("--print-config")
        self.assertOk(done)
        self.assertIn('SNMP_PRINTER_ALLOWED_CIDRS="192.0.2.0/24"', done.stdout)
        self.assertEqual(self.head(), before, "not updated")

    def test_usage_errors_come_before_any_change(self):
        done = self.bootstrap("--clients", "nonsense")
        self.assertOk(done, 64)
        self.assertFalse(self.src.exists())
        self.assertEqual(self.calls(), [])


class ShapeTest(unittest.TestCase):
    """No bash needed: the properties that keep "curl | sudo bash" safe."""

    TEXT = SETUP.read_text(encoding="utf-8")

    def test_last_line_calls_main(self):
        self.assertTrue(self.TEXT.endswith('\nmain "$@"\n'))
        self.assertTrue(self.TEXT.startswith("#!/usr/bin/env bash\n"))
        self.assertTrue(os.access(SETUP, os.X_OK), "setup.sh is executable")

    def test_only_functions_at_the_top_level(self):
        """A download cut short must never run part of the script: outside of
        functions there are only comments and the final call of main."""
        inside, heredoc, functions = False, None, 0
        header = re.compile(r"[a-z_][a-z0-9_]*\(\)[ \t]*\{(.*)")
        for number, line in enumerate(self.TEXT.split("\n"), 1):
            if heredoc is not None:
                if line == heredoc:
                    heredoc = None
                continue
            match = re.search(r"<<'([A-Z]+)'", line)
            if match:
                heredoc = match.group(1)
            if inside:
                inside = line != "}"
                continue
            if not line.strip() or line.startswith("#"):
                continue
            match = header.fullmatch(line)
            if match:
                functions += 1
                inside = not match.group(1).rstrip().endswith("}")   # a one-line function closes itself
                continue
            self.assertEqual(line, 'main "$@"', f"line {number} runs at the top level")
        self.assertGreater(functions, 30)
        self.assertFalse(inside)

    def test_no_test_hook_is_an_option(self):
        for hook in ("QLP_TEST_EUID", "QLP_TEST_OS_RELEASE", "QLP_TEST_ROOT", "QLP_TEST_INSTALL_CMD"):
            self.assertIn(f"#   {hook}", self.TEXT, f"{hook} is documented as test-only in the header")

    def test_syntax_with_every_bash(self):
        shells = {}
        for bash in (shell.BASH, shutil.which("bash"), "/bin/bash"):
            if bash and os.access(bash, os.X_OK):
                shells.setdefault(os.path.realpath(bash), bash)
        if not shells:
            self.skipTest("no bash at all")
        for bash in shells.values():
            with self.subTest(bash=bash):
                done = subprocess.run([bash, "-n", str(SETUP)], capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr)

    @unittest.skipIf(shutil.which("shellcheck") is None, "shellcheck is not installed")
    def test_shellcheck(self):
        done = subprocess.run(["shellcheck", "-x", SETUP.name], cwd=str(REPO), capture_output=True, text=True,
                              timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
