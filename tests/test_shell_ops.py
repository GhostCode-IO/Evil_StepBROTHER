"""The install-time shell layer, operational edge cases.

* AgentX access next to another snmpd setup (scripts/30-snmp-agent.sh):
  agentXPerms lines elsewhere, a /var/agentx of another group, snmpd's own
  root-only directory (and uninstall.sh restoring it).
* uninstall.sh when something cannot be reversed right now (firewalld or CUPS
  down, a failing userdel or systemctl disable): the record stays and a re-run
  finishes the job.
* SNMP_AGENT_MODE=pass_persist: snmpd is restarted when the persona's code or
  settings change.
* STATUS_MAX_AGE_SECONDS follows STATUS_POLL_SECONDS; LOG_LEVEL and the
  persona overrides reach runtime.env.
* Step 20 starts a CUPS that step 10 installed, and stops with exit 3 for one
  its admin stopped. "lpstat -r" exits 0 whether or not the scheduler runs,
  as real CUPS does: only its message tells.
* One run at a time: the lock of setup.sh, install.sh and uninstall.sh.
* install.sh records which config.env an install applies, and refuses to
  apply the template defaults to a box installed with settings of its own.
* The printer identity read with the daemons' rules; a broken identity.env
  stops the step instead of presenting a new printer.
* uninstall.sh --purge and --remove-packages after a completed uninstall.
* The preflight as a regular user while TCP 9100 or UDP 161 is in use.
* Client ranges that admit nobody (empty, or the template's documentation-only
  example): warned by the install steps, a T2 failure in selftest.sh.

Same harness as tests/test_shell_scripts.py: bash >= 4.2 (QLP_TEST_BASH on
macOS), stub commands on PATH, everything inside a temporary directory.
Addresses are documentation (RFC 5737, RFC 3849) or benchmarking (RFC 2544)
ranges; user and group ids are made up.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import unittest
from typing import Dict, List, Optional, Sequence

from tests import test_shell_scripts as shell
from tests._util import REPO

from ql810wc import config as qconfig  # noqa: E402  (tests.test_shell_scripts put agent/ on sys.path)

INSTALLED_PATHS = ("/usr/local/libexec/ql810wc-proxy", "/etc/ql810wc-proxy")
RICH = 'rule family="ipv4" source address="198.18.0.0/24" port port="161" protocol="udp" accept'

# Stubs on top of those of tests/test_shell_scripts.py (same conventions:
# POSIX sh, first line logs the call, state in $QLP_TEST_STATE).
SYSTEMCTL = r'''
S="$QLP_TEST_STATE"
if [ "${1:-}" = show ]; then
  for a in "$@"; do last="$a"; done
  cat "$S/show/$last" 2>/dev/null
  exit 0
fi
cmd="" now=0 quiet=0 units=""
for a in "$@"; do
  case "$a" in
    --now) now=1 ;;
    --quiet|-q) quiet=1 ;;
    -*) ;;
    *) if [ -z "$cmd" ]; then cmd="$a"; else units="$units $a"; fi ;;
  esac
done
rc=0
for u in $units; do
  case "$cmd" in
    is-active) if [ -e "$S/active/$u" ]; then s=active; else s=inactive rc=3; fi
               [ "$quiet" = 1 ] || echo "$s" ;;
    is-enabled) if [ -e "$S/enabled/$u" ]; then s=enabled; else s=disabled rc=1; fi
                [ "$quiet" = 1 ] || echo "$s" ;;
    cat) [ -e "$S/units/$u" ] || rc=1 ;;
    start|restart) : >"$S/active/$u" ;;
    stop) rm -f "$S/active/$u" ;;
    enable) : >"$S/enabled/$u" ;;
    disable)
      if [ -e "$S/fail-disable/$u" ]; then rc=1
      else rm -f "$S/enabled/$u"; if [ "$now" = 1 ]; then rm -f "$S/active/$u"; fi; fi ;;
  esac
done
exit "$rc"
'''
# Like CUPS's lpstat, "-r" exits 0 whether or not the scheduler runs.
LPSTAT = r'''
S="$QLP_TEST_STATE"
case "${1:-}" in
  -r) if [ ! -e "$S/cups-broken" ] && { [ -e "$S/active/cups.socket" ] || [ -e "$S/active/cups.service" ]; }; then
        echo "scheduler is running"
      else
        echo "scheduler is not running"
      fi
      exit 0 ;;
  -v) if [ -n "${2:-}" ]; then [ -e "$S/cupsq/$2" ]; exit; fi
      cat "$S/lpstat-v" 2>/dev/null ;;
esac
exit 0
'''
LPADMIN = r'''
case "${1:-}" in
  -x) rm -f "$QLP_TEST_STATE/cupsq/$2" ;;
  -p) : >"$QLP_TEST_STATE/cupsq/$2" ;;
esac
exit 0
'''
USERDEL = 'if [ -e "$QLP_TEST_STATE/fail-userdel" ]; then exit 1; fi\nrm -f "$QLP_TEST_STATE/users/$1"\nexit 0\n'
FW_OFFLINE = r'''
for a in "$@"; do
  case "$a" in
    --query-*) if [ -e "$QLP_TEST_STATE/offline-absent" ]; then exit 1; fi; exit 0 ;;
  esac
done
exit 0
'''
IP = 'case " $* " in *" addr "*) cat "$QLP_TEST_STATE/ip-addr.out" 2>/dev/null ;; esac\nexit 0\n'
# "flock -n 9": the lock is free unless $QLP_TEST_STATE/lock-busy exists.
FLOCK = 'if [ -e "$QLP_TEST_STATE/lock-busy" ]; then exit 1; fi\nexit 0\n'
RPM = 'for a in "$@"; do last="$a"; done\n[ -e "$QLP_TEST_STATE/rpm/$last" ]\n'
LOG_ONLY = "exit 0\n"

# A made-up account database for the AgentX tests: ql810wc is 990,
# snmpagents 4242, frr 4300; root and unknown names are 0, as net-snmp has it.
ACCOUNTS = r'''
agentx_id() {
  case "$1:$2" in
    user:ql810wc) echo 990 ;;
    group:ql810wc) if [[ -n "${NO_QL_GROUP:-}" ]]; then echo 0; else echo 990; fi ;;
    group:snmpagents) echo 4242 ;;
    user:frr|group:frr) echo 4300 ;;
    *) if [[ "$2" =~ ^[0-9]+$ ]]; then echo "$((10#$2))"; else echo 0; fi ;;
  esac
}
'''


class OpsTestCase(shell.ShellTestCase):
    """shell.ShellTestCase with the extra stubs above."""

    EXTRA = {"systemctl": SYSTEMCTL, "lpstat": LPSTAT, "lpadmin": LPADMIN, "userdel": USERDEL,
             "ip": IP, "rpm": RPM, "flock": FLOCK}

    def setUp(self) -> None:
        super().setUp()
        for name, body in self.EXTRA.items():
            self.stub(name, body)
        for kind in ("show", "fail-disable", "rpm"):
            (self.state / kind).mkdir(exist_ok=True)

    def stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + shell._LOG_LINE + body, encoding="utf-8")
        path.chmod(0o755)

    def records(self, manifest: pathlib.Path) -> List[List[str]]:
        return [line.split("\t") for line in manifest.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# 30-snmp-agent: agentXPerms of another snmpd configuration
# ---------------------------------------------------------------------------
class AgentxPermsScanTest(OpsTestCase):
    SCAN = r'''
source "$REPO/scripts/30-snmp-agent.sh"
qlp_init test
SNMPD_OTHER_CONFIGS=("$T/etc/snmpd.conf" "$T/etc/snmpd.conf.d")
SNMPD_SYSCONFIG="$T/sysconfig-snmpd"
COMMUNITIES=(public internal) V3_ENABLED=0
check_snmpd_conflicts
for p in "${AGENTX_PERMS[@]}"; do printf 'PERMS %s\n' "$p"; done
'''

    def test_every_agentxperms_line_is_reported(self):
        self.write("etc/snmpd.conf", "rocommunity site 198.18.0.0/24\nagentXPerms 0660 0750 root snmpagents\n"
                                     "includeDir " + str(self.root / "etc/extra") + "\n")
        self.write("etc/extra/a.conf", "[snmpd] agentxperms 0777\n")
        self.write("etc/snmpd.conf.d/site.conf", "# agentXPerms 0600 (a comment)\nagentXPerms 0660 0700 frr frr x\n")
        done = self.run_bash(self.SCAN)
        self.assertEqual(done.returncode, 0, done.stderr)
        perms = [line[6:] for line in done.stdout.splitlines() if line.startswith("PERMS ")]
        self.assertEqual(perms, [f"{self.root}/etc/snmpd.conf:2\t0660 0750 root snmpagents",
                                 f"{self.root}/etc/extra/a.conf:1\t0777",
                                 f"{self.root}/etc/snmpd.conf.d/site.conf:2\t0660 0700 frr frr"])

    def test_our_own_drop_in_is_not_reported(self):
        self.write("etc/snmpd.conf", "view systemview included .1.3.6.1.2.1.1\n")
        done = self.run_bash(self.SCAN.replace('check_snmpd_conflicts',
                                               'SNMPD_OTHER_CONFIGS+=("$SNMPD_DROPIN"); check_snmpd_conflicts'))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("PERMS", done.stdout)


class AgentxPlanTest(OpsTestCase):
    PLAN = r'''
source "$REPO/scripts/30-snmp-agent.sh"
qlp_init test
''' + ACCOUNTS + r'''
AGENTX_PERMS=("$@")
plan_agentx_access
printf 'OWN=%s\nGROUPS=%s\nACCESS=%s\n' "$AGENTX_OWN_PERMS" "${AGENT_GROUPS[*]}" "$AGENTX_ACCESS"
'''

    def plan(self, *lines: str, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        return self.run_bash(self.PLAN, *lines, env=env)

    def result(self, done: subprocess.CompletedProcess) -> Dict[str, str]:
        self.assertEqual(done.returncode, 0, done.stderr)
        return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)

    def test_nothing_else_sets_it(self):
        got = self.result(self.plan())
        self.assertEqual((got["OWN"], got["GROUPS"]), ("1", ""))
        self.assertIn("agentXPerms 0660 0750 root ql810wc", got["ACCESS"])

    def test_a_group_of_the_admins_is_joined(self):
        for line, group in (("/etc/snmp/snmpd.conf:7\t0660 0750 root snmpagents", "snmpagents"),
                            ("/etc/snmp/snmpd.conf:7\t660 750 frr 4242", "4242")):
            with self.subTest(line=line):
                done = self.plan(line)
                got = self.result(done)
                self.assertEqual((got["OWN"], got["GROUPS"]), ("0", group))
                self.assertIn("the drop-in sets none", done.stderr)

    def test_access_without_joining(self):
        for line in ("x:1\t0666 0755", "x:1\t0660 0750 root ql810wc", "x:1\t0660 0750 root 990",
                     "x:1\t0662 0755 root snmpagents"):
            with self.subTest(line=line):
                got = self.result(self.plan(line))
                self.assertEqual((got["OWN"], got["GROUPS"]), ("0", ""), "others or ql810wc may connect")

    def test_identical_lines_are_one_setting(self):
        got = self.result(self.plan("a:1\t0660 0750 root snmpagents", "b:3\t0660 0750 root snmpagents"))
        self.assertEqual((got["OWN"], got["GROUPS"]), ("0", "snmpagents"))

    def test_no_way_in_stops_the_step(self):
        for line in ("x:4\t0600 0700 frr frr", "x:4\t0660 0750 root root", "x:4\t0660 0750 root 0",
                     "x:4\t0660 0750", "x:4\t0644 0755 root snmpagents"):
            with self.subTest(line=line):
                done = self.plan(line)
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("does not let ql810wc-snmp-agent.service", done.stderr)
                self.assertIn("pass_persist", done.stderr)
                self.assertNotIn("OWN=", done.stdout)

    def test_unknown_group_is_ignored_like_net_snmp_does(self):
        done = self.plan("x:4\t0660 0750 root nosuchgroup")
        self.assertEqual(done.returncode, 1)
        self.assertIn("names group 'nosuchgroup', which does not exist: net-snmp ignores it", done.stderr)

    def test_conflicting_lines_stop_the_step(self):
        done = self.plan("/etc/snmp/snmpd.conf:2\t0660 0750 root snmpagents",
                         "/etc/snmp/snmpd.local.conf:9\t0666 0750 root ql810wc")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("agentXPerms is set 2 times with different values (SOCKPERMS GROUP)", done.stderr)
        self.assertIn("/etc/snmp/snmpd.local.conf:9: agentXPerms 0666 0750 root ql810wc", done.stderr)

    def test_missing_service_group(self):
        done = self.plan(env={"NO_QL_GROUP": "1"})
        self.assertEqual(done.returncode, 1)
        self.assertIn("group ql810wc does not exist", done.stderr)

    def test_the_drop_in_then_sets_no_agentxperms(self):
        script = self.PLAN.replace("printf 'OWN=", 'qlp_write_file() { cat >"$T/dropin.conf"; QLP_CHANGED=1; }\n'
                                   'SNMP_AGENT_MODE=agentx PRINTER_CIDRS="" NEED_UDP6=0 V3_CIDRS="" V3_ENABLED=0\n'
                                   'COMMUNITIES=(public internal)\nrender_dropin\nprintf \'OWN=')
        for perms, own in ((("/etc/snmp/snmpd.conf:7\t0660 0750 root snmpagents",), False), ((), True)):
            with self.subTest(perms=perms):
                done = self.run_bash(script, *perms)
                self.assertEqual(done.returncode, 0, done.stderr)
                text = (self.root / "dropin.conf").read_text(encoding="utf-8")
                lines = shell.directives(text)
                self.assertIn("master agentx", lines)
                self.assertEqual([line for line in lines if line.startswith("agentXPerms")],
                                 ["agentXPerms 0660 0750 root ql810wc"] if own else [])
                if not own:
                    self.assertIn("# No agentXPerms here: another snmpd configuration sets it "
                                  "(/etc/snmp/snmpd.conf:7: agentXPerms 0660 0750 root snmpagents),", text)
                    self.assertIn("group snmpagents, which ql810wc-snmp-agent.service joins", text)


class AgentxDirTest(OpsTestCase):
    EXTRA = dict(OpsTestCase.EXTRA, chgrp=LOG_ONLY, chmod=LOG_ONLY)
    DIR = r'''
source "$REPO/scripts/30-snmp-agent.sh"
qlp_init test
QLP_STATE_DIR="$T/state-dir" QLP_MANIFEST="$T/state-dir/manifest"
AGENTX_DIR="$T/var/agentx"
''' + ACCOUNTS + r'''
qlp_path_attrs() { if [[ -n "${DIR_ATTRS:-}" ]]; then printf '%s' "$DIR_ATTRS"; else return 1; fi; }
qlp_ensure_dir() { printf 'ENSURE_DIR %s\n' "$*"; }
AGENTX_PERMS=("$@")
plan_agentx_access
ensure_agentx_dir
printf 'GROUPS=%s\n' "${AGENT_GROUPS[*]}"
'''

    def setUp(self) -> None:
        super().setUp()
        self.manifest = self.root / "state-dir/manifest"
        self.dir = f"{self.root}/var/agentx"

    def ensure(self, attrs: str = "", *perms: str) -> subprocess.CompletedProcess:
        env = {"DIR_ATTRS": attrs} if attrs else {}
        return self.run_bash(self.DIR, *perms, env=env)

    def groups(self, done: subprocess.CompletedProcess) -> str:
        self.assertEqual(done.returncode, 0, done.stderr)
        return [line for line in done.stdout.splitlines() if line.startswith("GROUPS=")][0][7:]

    def changes(self) -> List[str]:
        return [c for c in self.calls() if c.startswith(("chgrp ", "chmod "))]

    def test_a_missing_directory_is_created(self):
        for perms, mode, group in (((), "0750", "ql810wc"), (("x:1\t0666 0755",), "0755", "ql810wc"),
                                   (("x:1\t0662 0711",), "0751", "ql810wc"),
                                   (("x:1\t0662 4777",), "0777", "ql810wc"),
                                   (("x:1\t0660 0700 root snmpagents",), "0750", "snmpagents"),
                                   (("x:1\t0660 0750 root ql810wc",), "0750", "ql810wc")):
            with self.subTest(perms=perms):
                done = self.ensure("", *perms)
                self.groups(done)
                self.assertIn(f"ENSURE_DIR {self.dir} {mode} root:{group}", done.stdout)
        self.assertEqual(self.changes(), [])

    def test_snmpds_own_directory_gets_group_ql810wc_and_is_recorded(self):
        done = self.ensure("0 0 700 root")
        self.assertEqual(self.groups(done), "")
        self.assertEqual(self.changes(), [f"chgrp ql810wc {self.dir}", f"chmod g+rx {self.dir}"])
        self.assertEqual(self.records(self.manifest), [["attrs", self.dir, "0:0", "700"]])
        self.assertIn("uninstall.sh restores it", done.stderr)
        # Again, as the directory is then: nothing to do.
        self.calls_log.unlink()
        self.groups(self.ensure("0 990 750 ql810wc"))
        self.assertEqual(self.changes(), [])
        # Group ql810wc without x: fixed, and the first (pristine) record stays the only one.
        self.groups(self.ensure("0 990 700 ql810wc"))
        self.assertEqual(self.records(self.manifest), [["attrs", self.dir, "0:0", "700"]])
        self.assertIn(f"chmod g+rx {self.dir}", self.calls())

    def test_a_directory_others_may_enter_stays(self):
        for attrs in ("0 0 711 root", "0 4242 755 snmpagents", "0 0 755 root"):
            with self.subTest(attrs=attrs):
                self.assertEqual(self.groups(self.ensure(attrs)), "")
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.manifest.exists())

    def test_a_leftover_directory_of_group_ql810wc_is_recorded_again(self):
        # Kept by an uninstall because of snmpd's stale socket, then found by a reinstall
        # whose new group ql810wc got the same gid: ours, so the next uninstall removes it.
        done = self.ensure("0 990 750 ql810wc")
        self.assertEqual(self.groups(done), "")
        self.assertEqual(self.records(self.manifest), [["created", self.dir]])
        self.assertIn("was left by an earlier install: recorded again", done.stderr)
        self.assertEqual(self.changes(), [])
        # Once only.
        self.groups(self.ensure("0 990 750 ql810wc"))
        self.assertEqual(self.records(self.manifest), [["created", self.dir]])
        # Not for a directory of another owner, or one an attrs record already describes.
        self.manifest.unlink()
        self.groups(self.ensure("990 990 750 ql810wc"))
        self.assertFalse(self.manifest.exists())
        self.manifest.write_text(f"attrs\t{self.dir}\t0:0\t700\n", encoding="utf-8")
        self.groups(self.ensure("0 990 750 ql810wc"))
        self.assertEqual(self.records(self.manifest), [["attrs", self.dir, "0:0", "700"]])

    def test_another_groups_directory_is_joined_not_changed(self):
        done = self.ensure("0 4242 750 snmpagents")
        self.assertEqual(self.groups(done), "snmpagents")
        self.assertIn("joins that group instead of the installer changing the directory", done.stderr)
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.manifest.exists())
        # The socket's group and the directory's group are the same: joined once.
        done = self.ensure("0 4242 750 snmpagents", "x:1\t0660 0750 root snmpagents")
        self.assertEqual(self.groups(done), "snmpagents")
        # A name systemd would not take, or one that is no name of that gid, is written as the number.
        for attrs in ("0 4243 750 grp:odd", "0 4243 750 UNKNOWN", "0 4243 750 snmpagents"):
            with self.subTest(attrs=attrs):
                self.assertEqual(self.groups(self.ensure(attrs)), "4243")

    def test_a_directory_its_group_may_not_enter_stops_the_step(self):
        done = self.ensure("0 4242 700 snmpagents")
        self.assertEqual(done.returncode, 1)
        self.assertIn("neither its group nor others may enter it", done.stderr)
        self.assertEqual(self.changes(), [])

    def test_joining_must_not_lock_the_subagent_out_of_the_socket(self):
        # Others may write to the socket, its group (snmpagents) may not: joining
        # snmpagents for the directory would apply those group bits.
        done = self.ensure("0 4242 750 snmpagents", "x:1\t0606 0750 root snmpagents")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("would join group snmpagents to enter", done.stderr)


class AgentUnitGroupsTest(OpsTestCase):
    GROUPS = r'''
source "$REPO/scripts/30-snmp-agent.sh"
qlp_init test
QLP_SYSTEMD_DIR="$T/systemd" QLP_STATE_DIR="$T/state-dir" QLP_MANIFEST="$T/state-dir/manifest"
qlp_write_file() {
  mkdir -p "${1%/*}"
  if [[ -e "$1" ]] && cmp -s - "$1"; then QLP_CHANGED=0; return 0; fi
  cat >"$1"; qlp_manifest_add created "$1"; QLP_CHANGED=1
}
qlp_ensure_dir() { if [[ ! -d "$1" ]]; then mkdir -p "$1"; qlp_manifest_add created "$1"; fi; }
read -r -a AGENT_GROUPS <<<"$1"
configure_agent_groups
echo "changed=$AGENT_DROPIN_CHANGED"
'''

    def test_drop_in_follows_the_groups(self):
        dropin_dir = self.root / "systemd/ql810wc-snmp-agent.service.d"
        dropin = dropin_dir / "ql810wc-proxy-agentx-groups.conf"
        done = self.run_bash(self.GROUPS, "snmpagents 4243")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("changed=1", done.stdout)
        text = dropin.read_text(encoding="utf-8")
        self.assertEqual(shell.directives(text), ["[Service]", "SupplementaryGroups=snmpagents 4243"])
        self.assertNotIn("root", shell.directives(text)[1])
        done = self.run_bash(self.GROUPS, "snmpagents 4243")
        self.assertIn("changed=0", done.stdout, "unchanged: no daemon-reload, no restart")
        done = self.run_bash(self.GROUPS, "")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("changed=1", done.stdout)
        self.assertFalse(dropin_dir.exists())
        self.assertEqual((self.root / "state-dir/manifest").read_text(encoding="utf-8"), "")

    def test_nothing_without_groups(self):
        done = self.run_bash(self.GROUPS, "")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("changed=0", done.stdout)
        self.assertFalse((self.root / "systemd").exists())


# ---------------------------------------------------------------------------
# uninstall.sh: what cannot be reversed now stays for the next run
# ---------------------------------------------------------------------------
class UninstallPartialTest(OpsTestCase):
    EXTRA = dict(OpsTestCase.EXTRA, **{"firewall-offline-cmd": FW_OFFLINE})
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
UNINSTALL_WATCH=("$T/etc/snmp/|snmpd" "$QLP_SYSTEMD_DIR/|systemd")
FW_OFFLINE_CMD="${OFFLINE_CMD:-firewall-offline-cmd}"
main "$@"
'''
    NO_OFFLINE = {"OFFLINE_CMD": "ql810wc-test-no-such-command"}

    def setUp(self) -> None:
        super().setUp()
        self.manifest = self.root / "var/lib/ql810wc-proxy/manifest"
        self.state_dir = self.manifest.parent

    def build(self) -> List[List[str]]:
        r = self.root
        self.write("etc/thing.conf", "ours\n")
        self.write("var/log/ql810wc-proxy/install.log", "x\n")
        recs = [["pkg", "cups"], ["user", "ql810wc"], ["created", f"{r}/etc/thing.conf"],
                ["fw-port", "public", "9100/tcp"], ["unit", "ql810wc-proxyd.service"],
                ["cupsq", "QL810Wc-proxy"], ["fw-rich", "public", RICH]]
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text("".join("\t".join(rec) + "\n" for rec in recs), encoding="utf-8")
        self.set_state("users", "ql810wc")
        self.set_state("cupsq", "QL810Wc-proxy")
        self.set_state("units", "ql810wc-proxyd.service")
        self.set_state("enabled", "ql810wc-proxyd.service")
        self.set_state("rpm", "cups")   # CUPS is installed; its scheduler may be down
        return recs

    def uninstall(self, *args: str, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        return self.run_bash(self.UNINSTALL, *args, env=env)

    def archived(self) -> List[pathlib.Path]:
        return sorted(self.state_dir.glob("manifest.uninstalled-*"))

    def test_firewalld_and_cups_down_then_a_re_run_finishes(self):
        recs = self.build()
        (self.state / "fail-userdel").touch()
        done = self.uninstall(env=self.NO_OFFLINE)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("partially uninstalled: 4 change(s) could not be reversed now", done.stderr)
        self.assertIn("firewalld is not running", done.stderr)
        self.assertIn("the CUPS scheduler is not running", done.stderr)
        self.assertIn("userdel could not delete user ql810wc", done.stderr)
        self.assertNotIn("uninstall complete", done.stderr)
        # Exactly what is left, in the original order (with the packages).
        self.assertEqual(self.records(self.manifest), [recs[0], recs[1], recs[3], recs[5], recs[6]])
        self.assertEqual(self.archived(), [])
        self.assertFalse((self.root / "etc/thing.conf").exists(), "the rest was reversed")
        self.assertIn("systemctl disable --now ql810wc-proxyd.service", self.calls())
        # A second run while nothing changed keeps the same records.
        done = self.uninstall(env=self.NO_OFFLINE)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.records(self.manifest), [recs[0], recs[1], recs[3], recs[5], recs[6]])
        # firewalld and CUPS up, userdel works: the next run finishes.
        (self.state / "fail-userdel").unlink()
        self.set_state("active", "firewalld", "cups.service")
        self.calls_log.unlink()
        done = self.uninstall(env=self.NO_OFFLINE)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("uninstall complete", done.stderr)
        for call in ("firewall-cmd --permanent --zone=public --remove-port=9100/tcp",
                     f"firewall-cmd --permanent --zone=public --remove-rich-rule={RICH}",
                     "lpadmin -x QL810Wc-proxy", "userdel ql810wc"):
            self.assertCalledOnce(call)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(len(self.archived()), 1)
        self.assertIn("packages installed by us and left in place: cups", done.stderr)

    def test_firewalld_stopped_is_handled_offline(self):
        self.build()
        self.set_state("active", "cups.service")
        done = self.uninstall()
        self.assertEqual(done.returncode, 0, done.stderr)
        for call in ("firewall-offline-cmd --zone=public --remove-port=9100/tcp",
                     f"firewall-offline-cmd --zone=public --remove-rich-rule={RICH}"):
            self.assertCalledOnce(call)
        self.assertFalse([c for c in self.calls() if c.startswith("firewall-cmd ")])
        self.assertEqual(len(self.archived()), 1)
        # A rule the offline configuration no longer has is simply done.
        self.build()
        (self.state / "offline-absent").touch()
        self.calls_log.unlink()
        done = self.uninstall()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse([c for c in self.calls() if "--remove-" in c])
        self.assertIn("is no longer in zone public", done.stderr)

    def test_a_unit_that_cannot_be_disabled_stays(self):
        recs = self.build()
        self.set_state("active", "firewalld", "cups.service")
        (self.state / "fail-disable" / "ql810wc-proxyd.service").touch()
        done = self.uninstall()
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("systemctl could not disable ql810wc-proxyd.service", done.stderr)
        self.assertEqual(self.records(self.manifest), [recs[0], recs[4]])

    def test_purge_keeps_the_state_while_records_remain(self):
        self.build()
        done = self.uninstall("--purge", env=self.NO_OFFLINE)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertTrue(self.manifest.exists())
        self.assertFalse((self.root / "var/log/ql810wc-proxy").exists())
        self.assertIn("--purge: kept", done.stderr)
        self.set_state("active", "firewalld", "cups.service")
        done = self.uninstall("--purge", env=self.NO_OFFLINE)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.state_dir.exists())

    def test_remove_packages_drops_their_records(self):
        recs = self.build()
        done = self.uninstall("--remove-packages", env=self.NO_OFFLINE)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertCalledOnce("dnf -y remove cups")
        self.assertNotIn(recs[0], self.records(self.manifest))

    def test_dry_run_reports_and_changes_nothing(self):
        self.build()
        before = self.manifest.read_text(encoding="utf-8")
        done = self.uninstall("--dry-run", env=self.NO_OFFLINE)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("[dry-run] 3 change(s) could not be reversed now", done.stderr)
        self.assertEqual(self.manifest.read_text(encoding="utf-8"), before)
        self.assertEqual([c for c in self.calls() if shell.MUTATING.match(c)], [])
        self.assertTrue((self.root / "etc/thing.conf").exists())

    def test_a_scheduler_that_answers_nothing_keeps_the_queue(self):
        """lpstat -r exits 0 with cupsd down: the queue is not taken for gone."""
        recs = self.build()
        self.set_state("active", "firewalld")
        done = self.uninstall()
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("the CUPS scheduler is not running: queue QL810Wc-proxy not removed", done.stderr)
        self.assertNotIn("already gone", done.stderr)
        self.assertIn(recs[5], self.records(self.manifest))
        self.assertNotIn("lpadmin -x QL810Wc-proxy", self.calls())

    def test_cups_removed_takes_its_queue_along(self):
        """The cups package gone, its client programs still there: no scheduler
        will ever answer again, and the queue went with CUPS."""
        self.build()
        (self.state / "rpm" / "cups").unlink()
        self.set_state("active", "firewalld")
        done = self.uninstall()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("CUPS is not installed any more (only its client programs are): queue QL810Wc-proxy went with it",
                      done.stderr)
        self.assertEqual(len(self.archived()), 1, "nothing left to undo")
        self.assertNotIn("lpadmin -x QL810Wc-proxy", self.calls())
        # A cups.service unit alone (CUPS built from source) counts as installed.
        self.build()
        (self.state / "rpm" / "cups").unlink()
        self.set_state("units", "cups.service")
        done = self.uninstall()
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("the CUPS scheduler is not running", done.stderr)

    def finished_uninstall(self) -> pathlib.Path:
        """A complete plain uninstall: the manifest is archived."""
        self.build()
        self.set_state("active", "firewalld", "cups.service")
        done = self.uninstall()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("(use --remove-packages to remove)", done.stderr)
        self.assertIn("(remove with --purge)", done.stderr)
        self.assertFalse(self.manifest.exists())
        archived = self.archived()
        self.assertEqual(len(archived), 1)
        self.calls_log.unlink()
        return archived[0]

    def test_purge_after_a_plain_uninstall(self):
        self.finished_uninstall()
        self.write("var/lib/ql810wc-proxy/identity.env", 'PRINTER_UUID="3f2504e0-4f89-41d3-9a0c-0305e82c3301"\n')
        done = self.uninstall("--purge", "--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(self.state_dir.exists(), "a dry run deletes nothing")
        done = self.uninstall("--purge")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.state_dir.exists(), "backups, archives and the saved identity go")
        self.assertFalse((self.root / "var/log/ql810wc-proxy").exists())
        self.assertNotIn("nothing to do", done.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("dnf ")])

    def test_remove_packages_after_a_plain_uninstall(self):
        archive = self.finished_uninstall()
        # An older uninstalled install that recorded git-core, and one package listed twice.
        older = self.state_dir / "manifest.uninstalled-20260101T000000Z"
        older.write_text("pkg\tgit-core\npkg\tcups\ncreated\t/x\n", encoding="utf-8")
        self.set_state("rpm", "git-core")
        done = self.uninstall("--remove-packages", "--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("[dry-run] dnf -y remove git-core cups", done.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("dnf ")], [])
        done = self.uninstall("--remove-packages")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertCalledOnce("dnf -y remove git-core cups")
        self.assertEqual(older.read_text(encoding="utf-8"), "created\t/x\n", "their records are done")
        self.assertNotIn("pkg\tcups", archive.read_text(encoding="utf-8"))
        self.assertTrue(self.state_dir.exists(), "without --purge the state stays")
        # Nothing left to remove: no second dnf call.
        self.calls_log.unlink()
        done = self.uninstall("--remove-packages")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no earlier install recorded a package of its own", done.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("dnf ")], [])

    def test_packages_removed_by_hand_are_not_removed_again(self):
        self.finished_uninstall()
        (self.state / "rpm" / "cups").unlink()
        done = self.uninstall("--remove-packages")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("are no longer installed: cups", done.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("dnf ")], [])

    def test_attrs_restores_the_directory(self):
        agentx = self.root / "var/agentx"
        agentx.mkdir(parents=True)
        agentx.chmod(0o750)
        owner = f"{os.getuid()}:{os.getgid()}"
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(f"attrs\t{agentx}\t{owner}\t700\n", encoding="utf-8")
        done = self.uninstall()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(agentx.stat().st_mode & 0o7777, 0o700)
        self.assertIn(f"+ chown -- {owner} {agentx}", done.stderr)
        # Gone, or a malformed record: nothing happens.
        for record in (f"attrs\t{self.root}/var/gone\t0:0\t700\n", f"attrs\t{agentx}\troot\t700\n"):
            with self.subTest(record=record):
                self.manifest.write_text(record, encoding="utf-8")
                done = self.uninstall()
                self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("malformed attrs record", done.stderr)

    # -- a created directory that holds snmpd's AgentX socket ---------------------
    # ss lists the socket as listening only while snmpd runs (as snmpd does).
    SS_WHILE_SNMPD = ('if [ -e "$QLP_TEST_STATE/active/snmpd.service" ]; then '
                      'cat "$QLP_TEST_STATE/ss.out" 2>/dev/null; fi\nexit 0\n')

    def agentx_dir(self, *extra_files: str) -> pathlib.Path:
        """var/agentx with a socket file "master" that nothing listens on (as
        snmpd leaves it when it stops), plus EXTRA_FILES (plain files)."""
        import socket
        agentx = self.root / "var/agentx"
        agentx.mkdir(parents=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(agentx / "master"))
        sock.close()
        for name in extra_files:
            (agentx / name).write_text("x\n", encoding="utf-8")
        self.write("state/ss.out", f"u_str LISTEN 0 5 {agentx}/master 153510 * 0\n")
        return agentx

    def test_a_stale_agentx_socket_goes_with_its_directory(self):
        agentx = self.agentx_dir()
        done = self.uninstall_created(agentx, self.SS_WHILE_SNMPD)      # snmpd is not running
        self.assertFalse(agentx.exists(), done.stderr)
        self.assertIn(f"+ rm -f -- {agentx}/master", done.stderr)
        self.assertIn(f"removed {agentx}", done.stderr)

    def test_a_socket_in_use_is_tried_again_once_snmpd_is_stopped(self):
        # The directory's record is newer than snmpd's unit record (step 30 adopted
        # it on a later run), so it comes first, while snmpd still listens.
        agentx = self.agentx_dir()
        self.stub("ss", self.SS_WHILE_SNMPD)
        self.set_state("units", "snmpd.service")
        self.set_state("enabled", "snmpd.service")
        self.set_state("active", "snmpd.service")
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(f"unit\tsnmpd.service\ncreated\t{agentx}\n", encoding="utf-8")
        done = self.uninstall()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(agentx.exists(), done.stderr)
        trace = done.stderr
        self.assertLess(trace.index("+ systemctl disable --now snmpd.service"),
                        trace.index(f"+ rm -f -- {agentx}/master"), "removed only once snmpd had stopped")

    def uninstall_created(self, agentx: pathlib.Path, ss: str, *args: str) -> subprocess.CompletedProcess:
        self.stub("ss", ss)
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(f"created\t{agentx}\n", encoding="utf-8")
        done = self.uninstall(*args)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def test_a_socket_still_in_use_keeps_the_directory(self):
        agentx = self.agentx_dir()
        done = self.uninstall_created(agentx, 'cat "$QLP_TEST_STATE/ss.out"\nexit 0\n')
        self.assertTrue((agentx / "master").exists())
        self.assertIn(f"kept {agentx}: a socket in it is still in use", done.stderr)

    def test_a_file_of_someone_else_keeps_the_directory(self):
        agentx = self.agentx_dir("notes.txt")
        done = self.uninstall_created(agentx, "exit 0\n")
        self.assertEqual(sorted(p.name for p in agentx.iterdir()), ["notes.txt"], "the stale socket went")
        self.assertIn(f"kept {agentx}: it holds files this installer did not create", done.stderr)

    def test_without_ss_no_socket_is_judged_stale(self):
        agentx = self.agentx_dir()
        done = self.uninstall_created(agentx, "exit 127\n")
        self.assertTrue((agentx / "master").exists())
        self.assertIn(f"kept {agentx}: it holds files this installer did not create", done.stderr)

    def test_dry_run_keeps_the_socket(self):
        agentx = self.agentx_dir()
        done = self.uninstall_created(agentx, "exit 0\n", "--dry-run")
        self.assertTrue((agentx / "master").exists())
        self.assertIn("after removing sockets nothing listens on", done.stderr)


# ---------------------------------------------------------------------------
# SNMP_AGENT_MODE=pass_persist: snmpd restarts when the persona's inputs change
# ---------------------------------------------------------------------------
DROPIN_PASS_PERSIST = ("pass_persist .1.3.6.1.4.1.2435 /usr/bin/python3 -I -B "
                       "/usr/local/libexec/ql810wc-proxy/brother_snmp_agent.py --mode pass_persist\n")


class PassPersistRestartTest(OpsTestCase):
    REFRESH = r'''
source "$REPO/scripts/lib/common.sh"
qlp_init test
QLP_LIB_DIR="$T/lib" QLP_RUNTIME_ENV="$T/etc/runtime.env" QLP_IDENTITY_ENV="$T/etc/identity.env"
QLP_STATE_DIR="$T/state-dir" QLP_SNMPD_DROPIN="$T/etc/dropin.conf"
SNMP_AGENT_MODE="$1"
qlp_passpersist_refresh
'''
    STEP30 = r'''
source "$REPO/scripts/30-snmp-agent.sh"
qlp_init test
QLP_LIB_DIR="$T/lib" QLP_RUNTIME_ENV="$T/etc/runtime.env" QLP_IDENTITY_ENV="$T/etc/identity.env"
QLP_STATE_DIR="$T/state-dir" QLP_SNMPD_DROPIN="$T/etc/dropin.conf"
SNMP_AGENT_MODE=pass_persist SNMPD_CHANGED=0
check_passpersist_inputs
echo "first=$SNMPD_CHANGED"
qlp_passpersist_record
SNMPD_CHANGED=0
check_passpersist_inputs
echo "second=$SNMPD_CHANGED"
'''

    def setUp(self) -> None:
        super().setUp()
        self.write("lib/brother_snmp_agent.py", "# agent\n")
        self.write("lib/ql810wc/persona.py", "# persona\n")
        self.write("lib/oid-map.yaml", "schema_version: 1\n")
        self.write("etc/runtime.env", 'STATUS_POLL_SECONDS="3"\n')
        self.write("etc/identity.env", 'PRINTER_UUID="3f2504e0-4f89-41d3-9a0c-0305e82c3301"\n')
        self.write("etc/dropin.conf", DROPIN_PASS_PERSIST)
        self.digest = self.root / "state-dir/passpersist.digest"

    def refresh(self, mode: str = "pass_persist") -> int:
        done = self.run_bash(self.REFRESH, mode)
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.calls().count("systemctl restart snmpd.service")

    def test_restart_whenever_an_input_changes(self):
        self.set_state("active", "snmpd.service")
        self.assertEqual(self.refresh(), 1, "no note yet: the children may run anything")
        self.assertTrue(self.digest.exists())
        self.assertEqual(self.refresh(), 1, "unchanged: no restart")
        self.write("etc/runtime.env", 'STATUS_POLL_SECONDS="20"\n')
        self.assertEqual(self.refresh(), 2)
        self.write("lib/ql810wc/persona.py", "# persona v2\n")
        self.assertEqual(self.refresh(), 3)
        self.write("lib/ql810wc/new_module.py", "# new\n")
        self.assertEqual(self.refresh(), 4)
        self.write("etc/identity.env", 'PRINTER_UUID="3f2504e0-4f89-41d3-9a0c-0305e82c3302"\n')
        self.write("lib/oid-map.yaml", "schema_version: 1\n# v2\n")
        self.assertEqual(self.refresh(), 5, "several changes: one restart")
        self.assertEqual(self.refresh(), 5)

    def test_stopped_snmpd_is_not_started(self):
        self.assertEqual(self.refresh(), 0)
        self.assertNotIn("systemctl start snmpd.service", self.calls())
        self.assertTrue(self.digest.exists(), "snmpd loads the current inputs when it starts")
        self.set_state("active", "snmpd.service")
        self.assertEqual(self.refresh(), 0)

    def test_only_in_pass_persist_mode_with_its_drop_in(self):
        self.set_state("active", "snmpd.service")
        self.digest.parent.mkdir(parents=True)
        self.digest.write_text("old\n")
        self.assertEqual(self.refresh("agentx"), 0)
        self.assertTrue(self.digest.exists(), "refresh never touches the note outside pass_persist mode")
        self.write("etc/dropin.conf", "master agentx\n")
        self.assertEqual(self.refresh(), 0, "no pass_persist children (yet): nothing to restart")
        done = self.run_bash('source "$REPO/scripts/lib/common.sh"; QLP_STATE_DIR="$T/state-dir";'
                             ' SNMP_AGENT_MODE=agentx qlp_passpersist_record')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.digest.exists(), "agentx mode drops the note")

    def test_step_30_restarts_for_changed_inputs(self):
        done = self.run_bash(self.STEP30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["first=1", "second=0"])

    def test_every_step_that_changes_inputs_refreshes(self):
        def body(rel: str, func: str = "main") -> str:
            text = (REPO / rel).read_text(encoding="utf-8")
            match = re.search(r"(?ms)^%s\(\) \{\n(.*?)^\}" % func, text)
            self.assertIsNotNone(match, rel)
            return match.group(1)
        for rel in ("scripts/20-cups-queue.sh", "scripts/40-discovery.sh"):
            main = body(rel)
            with self.subTest(step=rel):
                self.assertIn("qlp_passpersist_refresh", main)
                self.assertLess(main.index("qlp_sync_runtime_env"), main.index("qlp_passpersist_refresh"))
        lines = [line.strip() for line in body("scripts/30-snmp-agent.sh").splitlines()]
        order = [lines.index(word) for word in ("qlp_sync_runtime_env", "render_dropin", "check_passpersist_inputs",
                                                "start_snmpd", "qlp_passpersist_record")]
        self.assertEqual(order, sorted(order))
        self.assertIn('rm -f -- "$QLP_STATE_DIR/passpersist.digest"', body("uninstall.sh"))


# ---------------------------------------------------------------------------
# runtime.env: derived STATUS_MAX_AGE_SECONDS, LOG_LEVEL, persona overrides
# ---------------------------------------------------------------------------
class RuntimeEnvDerivedTest(OpsTestCase):
    SYNC = shell.RuntimeSettingsTest.SYNC

    def setUp(self) -> None:
        super().setUp()
        self.write("identity.env", f'PRINTER_UUID="{shell.UUID}"\n')
        self.runtime = self.root / "runtime.env"

    def sync(self, **values: str) -> subprocess.CompletedProcess:
        cfg = self.write("config.env", "".join(f'{k}="{v}"\n' for k, v in values.items()))
        cfg.chmod(0o600)
        if self.runtime.exists():
            self.runtime.unlink()
        return self.run_bash(self.SYNC, env={"QL_PROXY_CONFIG": str(cfg)})

    def test_status_age_follows_the_poll_interval(self):
        for poll, age in (("3", "60"), ("30", "60"), ("31", "62"), ("45", "90"), ("060", "120")):
            with self.subTest(poll=poll):
                done = self.sync(STATUS_POLL_SECONDS=poll)
                self.assertEqual(done.returncode, 0, done.stderr)
                values = qconfig.parse_env_file(self.runtime).values
                self.assertEqual(values["STATUS_MAX_AGE_SECONDS"], age)
                cfg = qconfig.load_runtime_config(self.runtime)   # what the daemons load: accepted
                self.assertEqual(cfg.get_int("STATUS_MAX_AGE_SECONDS"), int(age))
        self.assertIn("# Derived from the settings above", self.runtime.read_text(encoding="utf-8"))

    def test_status_age_is_no_config_setting(self):
        done = self.sync(STATUS_POLL_SECONDS="45", STATUS_MAX_AGE_SECONDS="5")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("unknown setting 'STATUS_MAX_AGE_SECONDS'", done.stderr)
        self.assertEqual(qconfig.parse_env_file(self.runtime).values["STATUS_MAX_AGE_SECONDS"], "90")

    def test_log_level_and_persona_overrides(self):
        done = self.sync(LOG_LEVEL="debug", PERSONA_SERIAL="Z9PERSONA01", PERSONA_MAC="00:00:5E:00:53:01",
                         PERSONA_SYSNAME="BRW00005E005301")
        self.assertEqual(done.returncode, 0, done.stderr)
        values = qconfig.parse_env_file(self.runtime).values
        self.assertEqual((values["LOG_LEVEL"], values["PERSONA_SERIAL"], values["PERSONA_MAC"],
                          values["PERSONA_SYSNAME"]), ("DEBUG", "Z9PERSONA01", "00:00:5E:00:53:01", "BRW00005E005301"))
        self.assertEqual(qconfig.load_runtime_config(self.runtime)["LOG_LEVEL"], "DEBUG")
        done = self.sync(LOG_LEVEL="")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(qconfig.parse_env_file(self.runtime).values["LOG_LEVEL"], "INFO",
                         "never empty: the SNMP agent hands it to logging as it is")
        done = self.sync(LOG_LEVEL="verbose")
        self.assertEqual(done.returncode, 1)
        self.assertIn("LOG_LEVEL='verbose' must be one of", done.stderr)
        self.assertFalse(self.runtime.exists())
        done = self.sync(PERSONA_MAC="00:00:5E:00:53")
        self.assertEqual(done.returncode, 1, "the daemons' loader refuses it before anything is written")
        self.assertIn("PERSONA_MAC", done.stderr)

    def test_runtime_values_are_shared_with_selftest(self):
        script = r'''
source "$REPO/scripts/lib/common.sh"
STATUS_POLL_SECONDS="$1" LOG_LEVEL="$2" RAW_BIDIRECTIONAL=On USB_PRODUCT_ID=209C MAX_JOB_BYTES=0065536
for k in STATUS_MAX_AGE_SECONDS LOG_LEVEL RAW_BIDIRECTIONAL USB_PRODUCT_ID MAX_JOB_BYTES; do
  printf '%s=%s\n' "$k" "$(qlp_runtime_value "$k")"
done
for p in 0 1 30 31 60 61 x; do printf 'age(%s)=%s\n' "$p" "$(qlp_status_max_age "$p")"; done
'''
        done = self.run_bash(script, "45", "warning")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), [
            "STATUS_MAX_AGE_SECONDS=90", "LOG_LEVEL=WARNING", "RAW_BIDIRECTIONAL=yes", "USB_PRODUCT_ID=209c",
            "MAX_JOB_BYTES=65536", "age(0)=60", "age(1)=60", "age(30)=60", "age(31)=62", "age(60)=120",
            "age(61)=60", "age(x)=60"])
        text = (REPO / "selftest.sh").read_text(encoding="utf-8")
        match = re.search(r"(?ms)^runtime_env_checks\(\) \{\n(.*?)^\}", text)
        self.assertIsNotNone(match)
        self.assertIn('"${QLP_RUNTIME_DERIVED[@]}"', match.group(1))
        self.assertIn("qlp_runtime_value", match.group(1))


class ConfigTemplateTest(unittest.TestCase):
    """No bash needed: the template and the key lists."""

    def test_template(self):
        text = (REPO / "config.sample.env").read_text(encoding="utf-8")
        for line in ('LOG_LEVEL="INFO"', 'PERSONA_SERIAL=""', 'PERSONA_MAC=""', 'PERSONA_SYSNAME=""'):
            self.assertRegex(text, r"(?m)^%s$" % re.escape(line))
        self.assertIn('"29x90" (DK-11201', text)
        self.assertNotIn('"62x29"', text)
        self.assertNotRegex(text, r"(?m)^STATUS_MAX_AGE_SECONDS=", "derived, never configured")

    def test_key_lists(self):
        text = (REPO / "scripts/lib/common.sh").read_text(encoding="utf-8")
        keys = re.search(r"(?ms)^QLP_RUNTIME_KEYS=\((.*?)\)", text).group(1).split()
        derived = re.search(r"(?m)^QLP_RUNTIME_DERIVED=\((.*?)\)", text).group(1).split()
        for key in ("LOG_LEVEL", "PERSONA_SERIAL", "PERSONA_MAC", "PERSONA_SYSNAME"):
            self.assertIn(key, keys)
        self.assertEqual(derived, ["STATUS_MAX_AGE_SECONDS"])
        for key in keys + derived:
            if key != "CUPS_QUEUE":   # the installer's own setting (step 20), carried along
                self.assertIn(key, qconfig.SETTINGS, "the daemons know, and validate, every key they are given")


# ---------------------------------------------------------------------------
# 20-cups-queue: CUPS for the local queue
# ---------------------------------------------------------------------------
class CupsSchedulerTest(OpsTestCase):
    QUEUE = r'''
source "$REPO/scripts/20-cups-queue.sh"
qlp_init test
QLP_STATE_DIR="$T/state-dir" QLP_MANIFEST="$T/state-dir/manifest"
# CUPS not installed (macOS has a real lpadmin of its own on PATH).
if [[ -n "${NO_CUPS:-}" ]]; then have() { [[ "$1" != lpadmin ]] && command -v "$1" >/dev/null 2>&1; }; fi
CUPS_START_WAIT_S=2
CUPS_QUEUE="${QUEUE-QL810Wc-proxy}" CUPS_QUEUE_SHARED=no RAW_PORT=9100 RAW_BIND_KIND=""
configure_cups_queue
printf 'RESULT=%s\nHUMAN=%s\n' "$CUPS_RESULT" "$CUPS_HUMAN"
'''

    def setUp(self) -> None:
        super().setUp()
        self.manifest = self.root / "state-dir/manifest"

    def manifest_with(self, *records: Sequence[str]) -> None:
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text("".join("\t".join(r) + "\n" for r in records), encoding="utf-8")

    def configure(self, queue: Optional[str] = None, no_cups: bool = False) -> Dict[str, str]:
        env = {} if queue is None else {"QUEUE": queue}
        if no_cups:
            env["NO_CUPS"] = "1"
        done = self.run_bash(self.QUEUE, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.stderr = done.stderr
        return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)

    def test_cups_installed_by_step_10_is_started(self):
        self.manifest_with(("pkg", "cups"))
        got = self.configure()
        self.assertEqual(got["HUMAN"], "")
        self.assertEqual(got["RESULT"], "'QL810Wc-proxy' -> socket://127.0.0.1:9100")
        self.assertCalledOnce("systemctl enable cups.socket")
        self.assertCalledOnce("systemctl start cups.socket")
        self.assertTrue((self.state / "cupsq/QL810Wc-proxy").exists())
        self.assertIn(["unit", "cups.socket"], self.records(self.manifest))
        self.assertIn(["cupsq", "QL810Wc-proxy"], self.records(self.manifest))

    def test_an_enabled_socket_is_only_started(self):
        self.manifest_with(("pkg", "cups"))
        self.set_state("enabled", "cups.socket")
        self.configure()
        self.assertNotCalled("systemctl enable cups.socket")
        self.assertCalledOnce("systemctl start cups.socket")
        self.assertNotIn(["unit", "cups.socket"], self.records(self.manifest), "the preset enabled it, not us")

    def test_a_cups_that_does_not_come_up(self):
        self.manifest_with(("pkg", "cups"))
        (self.state / "cups-broken").touch()
        got = self.configure()
        self.assertIn("does not answer after starting cups.socket", got["HUMAN"])
        self.assertEqual(got["RESULT"], "not set up (CUPS not reachable)")

    def test_the_admins_stopped_cups_stays_stopped(self):
        got = self.configure()
        self.assertIn("systemctl enable --now cups.socket", got["HUMAN"])
        self.assertIn('set CUPS_QUEUE=""', got["HUMAN"])
        self.assertFalse([c for c in self.calls() if c.startswith(("systemctl start", "systemctl enable",
                                                                    "lpadmin"))])

    def test_cups_not_installed(self):
        self.manifest_with(("pkg", "cups"))   # removed again since: nothing to start
        got = self.configure(no_cups=True)
        self.assertIn("needs CUPS, which is not installed", got["HUMAN"])
        self.assertNotIn("systemctl start cups.socket", self.calls())

    def test_empty_queue_needs_no_cups(self):
        got = self.configure(queue="")
        self.assertEqual((got["RESULT"], got["HUMAN"]), ("none (CUPS_QUEUE empty)", ""))
        self.assertNotIn("lpstat -r", self.calls())
        # An earlier queue of ours while CUPS is down: a warning, the record stays.
        self.manifest_with(("cupsq", "OldQueue"))
        got = self.configure(queue="")
        self.assertEqual(got["HUMAN"], "")
        self.assertIn("stay until this step runs again with CUPS up", self.stderr)
        self.assertEqual(self.records(self.manifest), [["cupsq", "OldQueue"]])
        # ... and with CUPS gone entirely, the queue went with it.
        self.configure(queue="", no_cups=True)
        self.assertEqual(self.records(self.manifest), [])
        self.assertIn("went with it", self.stderr)

    def test_only_the_message_of_lpstat_tells(self):
        script = ('source "$REPO/scripts/lib/common.sh"\nqlp_init test\n'
                  'if qlp_cups_running 5; then echo UP; else echo DOWN; fi\n')
        self.assertEqual(self.run_bash(script).stdout.split(), ["DOWN"], "exit 0 and 'scheduler is not running'")
        self.set_state("active", "cups.socket")
        self.assertEqual(self.run_bash(script).stdout.split(), ["UP"])
        self.stub("lpstat", "exit 1\n")
        self.assertEqual(self.run_bash(script).stdout.split(), ["DOWN"], "lpstat failed or timed out")

    def test_the_step_ends_with_exit_3(self):
        script = r'''
source "$REPO/scripts/20-cups-queue.sh"
for f in require_root qlp_load_config require_cmd validate_config report_printer qlp_ensure_service_user \
         deploy_code qlp_ensure_identity qlp_sync_runtime_env install_udev_rule exclude_from_ipp_usb \
         exclude_from_cups_usb ensure_usblp_bound check_port_free install_units start_data_plane \
         configure_firewall note_ipp_usb_queues summary; do eval "$f() { :; }"; done
qlp_passpersist_refresh() { echo REFRESHED; }
configure_cups_queue() { CUPS_HUMAN="start CUPS please"; }
main
'''
        done = self.run_bash(script)
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("HUMAN", done.stderr)
        self.assertIn("start CUPS please", done.stderr)
        self.assertIn("REFRESHED", done.stdout, "the rest of the step is done first")


# ---------------------------------------------------------------------------
# 00-preflight: read-only, also as a regular user
# ---------------------------------------------------------------------------
class PreflightOpsTest(OpsTestCase):
    def preflight(self, config: str = 'SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24"\n',
                  manifest: str = "") -> subprocess.CompletedProcess:
        cfg = self.write("config.env", config)
        cfg.chmod(0o600)
        script = ('source "$REPO/scripts/lib/common.sh"\nQLP_MANIFEST="$T/manifest"\n'
                  'source "$REPO/scripts/00-preflight.sh"\n')
        if manifest:
            self.write("manifest", manifest)
        return self.run_bash(script, env={"QL_PROXY_CONFIG": str(cfg)})

    def assertReachedVerdict(self, done: subprocess.CompletedProcess) -> None:
        self.assertIn(done.returncode, (0, 1, 3), done.stderr)
        self.assertNotIn("failed (exit", done.stderr, "the ERR trap fired")
        for title in ("── SELinux ──", "── firewall ──", "── services ──", "── verdict ──"):
            self.assertIn(title, done.stderr)

    def test_ports_in_use_by_processes_ss_cannot_name(self):
        # ss without root: no users:(("...")) field. The stub prints this for 9100 and 161 alike.
        (self.state / "ss.out").write_text("LISTEN 0      4096               *:9100            *:*\n")
        done = self.preflight()
        self.assertReachedVerdict(done)
        if os.geteuid() == 0:
            self.assertIn("9100/tcp already in use: unknown", done.stderr)
            self.assertIn("UDP 161 in use by a process ss does not name", done.stderr)
        else:
            self.assertIn("9100/tcp is in use by a process only root can name (re-run with sudo)", done.stderr)
            self.assertIn("UDP 161 in use by a process only root can name", done.stderr)
            self.assertNotIn("already in use", done.stderr)

    def test_our_socket_from_a_previous_run(self):
        (self.state / "ss.out").write_text("LISTEN 0      4096               *:9100            *:*\n")
        self.set_state("active", "ql810wc-proxyd.socket")
        (self.state / "show/ql810wc-proxyd.socket").write_text("[::]:9100 (Stream)\n")
        done = self.preflight()
        self.assertReachedVerdict(done)
        self.assertIn("9100/tcp held by ql810wc-proxyd.socket (previous run)", done.stderr)

    def test_cups_messages(self):
        (self.state / "rpm/cups").touch()
        done = self.preflight()
        self.assertReachedVerdict(done)
        self.assertIn("cups installed but not running: start it (systemctl enable --now cups.socket) or set",
                      done.stderr)
        self.assertNotIn("will enable it", done.stderr)
        done = self.preflight(manifest="pkg\tcups\n")
        self.assertIn("cups (installed by 10-deps) not running yet: 20-cups-queue starts cups.socket", done.stderr)
        done = self.preflight(config='SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24"\nCUPS_QUEUE=""\n')
        self.assertIn("fine: CUPS_QUEUE is empty", done.stderr)
        self.set_state("active", "cups.socket")
        done = self.preflight()
        self.assertIn("cups.socket active (cupsd starts on the first request)", done.stderr)
        (self.state / "rpm/cups").unlink()
        (self.state / "active/cups.socket").unlink()
        done = self.preflight()
        self.assertIn("cups not installed (10-deps installs it", done.stderr)

    def test_a_cups_service_whose_scheduler_does_not_answer(self):
        self.set_state("active", "cups", "cups.service")
        done = self.preflight()
        self.assertIn("PASS  CUPS scheduler responding", done.stderr)
        (self.state / "cups-broken").touch()
        done = self.preflight()
        self.assertReachedVerdict(done)
        self.assertIn("WARN  the CUPS scheduler is not running", done.stderr)

    def test_snmpv3_lines_follow_snmp_v3_enable(self):
        base = 'SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24"\n'
        auth = 'SNMP_V3_AUTH_PASS="aaaaaaaaaa"\n'
        # What setup.sh writes without --snmpv3: off on purpose, so no warning.
        done = self.preflight(base + 'SNMP_V3_ENABLE="no"\n')
        self.assertReachedVerdict(done)
        self.assertIn('INFO  SNMPv3 host monitoring off (SNMP_V3_ENABLE="no")', done.stderr)
        self.assertNotIn("SNMP_V3_AUTH_PASS", done.stderr)
        self.assertNotIn("SNMP_V3_PRIV_PASS", done.stderr)
        done = self.preflight(base)   # auto, with the template's placeholders
        self.assertIn("INFO  SNMPv3 host monitoring off (SNMP_V3_ENABLE=auto, passphrases unset)", done.stderr)
        self.assertNotIn("WARN  SNMP_V3", done.stderr)
        done = self.preflight(base + auth)
        self.assertIn("WARN  SNMP_V3_PRIV_PASS is not set (empty, or still the template placeholder): with", done.stderr)
        done = self.preflight(base + auth + 'SNMP_V3_PRIV_PASS="bbbbbbbbbb"\nSNMP_V3_ENABLE="AUTO"\n')
        self.assertIn("INFO  SNMPv3 host monitoring on (SNMP_V3_ENABLE=auto, both passphrases set)", done.stderr)
        # "yes" without a passphrase: step 30 would stop, so the preflight does.
        done = self.preflight(base + auth + 'SNMP_V3_ENABLE="Yes"\n')
        self.assertReachedVerdict(done)
        self.assertIn("FAIL  SNMP_V3_PRIV_PASS is not set (empty, or still the template placeholder), and", done.stderr)
        self.assertNotIn("FAIL  SNMP_V3_AUTH_PASS", done.stderr)
        self.assertRegex(done.stderr, r"[1-9]\d* failure\(s\)")
        done = self.preflight(base + 'SNMP_V3_ENABLE="sometimes"\n')
        self.assertIn("FAIL  SNMP_V3_ENABLE='sometimes' must be auto, yes or no", done.stderr)

    def test_no_config_env_points_to_setup(self):
        script = ('source "$REPO/scripts/lib/common.sh"\nQLP_MANIFEST="$T/manifest" QLP_ETC_DIR="$T/etc"\n'
                  'QLP_REPO_ROOT="$T/empty-checkout"\nsource "$REPO/scripts/00-preflight.sh"\n')
        (self.root / "empty-checkout").mkdir()
        done = self.run_bash(script)
        self.assertReachedVerdict(done)
        self.assertIn("WARN  no config.env yet — create it with: sudo ./setup.sh --config-only (or by hand:",
                      done.stderr)

    def test_client_ranges_that_admit_nobody_are_warned(self):
        self.set_state("active", "firewalld")
        done = self.preflight(config='SNMP_PRINTER_ALLOWED_CIDRS="192.0.2.0/24"\n')
        self.assertReachedVerdict(done)
        self.assertIn("WARN  SNMP_PRINTER_ALLOWED_CIDRS holds only documentation-only example ranges "
                      "(192.0.2.0/24, RFC 5737/3849), which no real client uses: no network client could reach "
                      "the printer persona", done.stderr)
        self.assertIn("no network client could reach the Bonjour record", done.stderr)
        done = self.preflight(config='SNMP_PRINTER_ALLOWED_CIDRS=""\nMDNS_ALLOWED_CIDRS="2001:db8::/64"\n')
        self.assertIn("SNMP_PRINTER_ALLOWED_CIDRS is empty: no network client could reach", done.stderr)
        self.assertIn("MDNS_ALLOWED_CIDRS holds only documentation-only example ranges (2001:db8::/64", done.stderr)
        done = self.preflight(config='SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24 192.0.2.0/24"\n')
        self.assertNotIn("no network client could reach", done.stderr)


# ---------------------------------------------------------------------------
# Client ranges in the install steps and in selftest.sh
# ---------------------------------------------------------------------------
class SourcesClassifyTest(OpsTestCase):
    CLASSIFY = r'''
source "$REPO/scripts/lib/common.sh"
qlp_classify_sources "$@"
printf 'usable=%s\nplaceholder=%s\nlocal=%s\nsubnets=%s\n' "$QLP_SRC_USABLE" "$QLP_SRC_PLACEHOLDER" \
  "$QLP_SRC_LOCAL" "$QLP_SRC_SUBNETS"
printf 'problem=%s\n' "$(qlp_sources_problem SETTING "$@")"
'''
    ADDRS = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
             "2: eth0    inet 198.18.0.7/24 brd 198.18.0.255 scope global eth0\\       valid_lft forever\n"
             "2: eth0    inet6 fe80::1/64 scope link \\       valid_lft forever\n")

    def classify(self, *cidrs: str) -> Dict[str, str]:
        done = self.run_bash(self.CLASSIFY, *cidrs)
        self.assertEqual(done.returncode, 0, done.stderr)
        return dict(line.split("=", 1) for line in done.stdout.splitlines())

    def test_classification(self):
        (self.state / "ip-addr.out").write_text(self.ADDRS)
        got = self.classify("192.0.2.0/24", "198.51.100.7", "203.0.113.0/25", "2001:db8::/48", "127.0.0.1",
                            "::1", "198.18.0.0/16", "198.19.0.0/24", "fe80::/10", "not-an-address")
        self.assertEqual(got["usable"], "198.18.0.0/16 198.19.0.0/24 fe80::/10")
        self.assertEqual(got["placeholder"], "192.0.2.0/24 198.51.100.7/32 203.0.113.0/25 2001:db8::/48")
        self.assertEqual(got["local"], "198.18.0.0/16 fe80::/10")
        self.assertEqual(got["subnets"], "198.18.0.0/24 fe80::/64", "loopback is no subnet of clients")
        self.assertEqual(got["problem"], "")

    def test_problems(self):
        self.assertEqual(self.classify()["problem"], "SETTING is empty")
        self.assertEqual(self.classify("192.0.2.0/24", "127.0.0.1")["problem"],
                         "SETTING holds only documentation-only example ranges (192.0.2.0/24, RFC 5737/3849), "
                         "which no real client uses")
        self.assertEqual(self.classify("127.0.0.1")["problem"],
                         "SETTING holds no address a network client can come from (127.0.0.1)")
        self.assertEqual(self.classify("192.0.0.0/16")["problem"], "", "a supernet holds real hosts too")
        got = self.classify("198.18.0.0/24")
        self.assertEqual((got["subnets"], got["local"]), ("", ""), "no ip output: subnets unknown")


class StepWarningsTest(OpsTestCase):
    STEP30 = r'''
source "$REPO/scripts/30-snmp-agent.sh"
qlp_init test
SNMP_AGENT_MODE=agentx SNMP_PRINTER_COMMUNITIES="public internal" SNMP_PRINTER_COMMUNITY=""
SNMP_PRINTER_ALLOWED_CIDRS="$1" SNMP_V3_ENABLE=yes SNMP_V3_USER=nms SNMP_V3_AUTH_PROTO=SHA-256
SNMP_V3_PRIV_PROTO=AES SNMP_V3_AUTH_PASS=aaaaaaaaaa SNMP_V3_PRIV_PASS=bbbbbbbbbb SNMP_V3_ALLOWED_CIDRS="$2"
validate_config
'''
    STEP40 = r'''
source "$REPO/scripts/40-discovery.sh"
qlp_init test
SNMP_PRINTER_ALLOWED_CIDRS="$1" MDNS_ALLOWED_CIDRS="$2"
qlp_fw_zone() { printf public; }
qlp_fw_sync_rich() { :; }
configure_firewall publish
'''

    def test_step_30(self):
        done = self.run_bash(self.STEP30, "192.0.2.0/24", "192.0.2.50/32")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("SNMP_PRINTER_ALLOWED_CIDRS holds only documentation-only example ranges (192.0.2.0/24",
                      done.stderr)
        self.assertIn("only this host can read the printer persona", done.stderr)
        self.assertIn("SNMP_V3_ALLOWED_CIDRS holds only documentation-only example ranges (192.0.2.50/32)",
                      done.stderr)
        done = self.run_bash(self.STEP30, "", "198.18.0.50/32")
        self.assertIn("SNMP_PRINTER_ALLOWED_CIDRS is empty: only this host can read", done.stderr)
        self.assertNotIn("SNMP_V3_ALLOWED_CIDRS holds", done.stderr)
        done = self.run_bash(self.STEP30, "198.18.0.0/24 2001:db8::/64", "198.18.0.50/32")
        self.assertNotIn("WARN", done.stderr)

    def test_step_40(self):
        self.set_state("active", "firewalld")
        (self.state / "ip-addr.out").write_text(SourcesClassifyTest.ADDRS)
        done = self.run_bash(self.STEP40, "192.0.2.0/24", "")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("SNMP_PRINTER_ALLOWED_CIDRS holds only documentation-only example ranges", done.stderr)
        self.assertIn("firewalld drops every Bonjour query", done.stderr)
        done = self.run_bash(self.STEP40, "192.0.2.0/24", "198.19.0.0/24")
        self.assertIn("no range in MDNS_ALLOWED_CIDRS (198.19.0.0/24) is a subnet of this host", done.stderr)
        done = self.run_bash(self.STEP40, "198.19.0.0/24", "198.18.0.0/24 fe80::/10")
        self.assertNotIn("WARN", done.stderr)
        self.assertIn("UDP 5353 open in zone 'public' for 2 source range(s) from MDNS_ALLOWED_CIDRS", done.stderr)


@unittest.skipIf(any(os.path.exists(p) for p in INSTALLED_PATHS),
                 "ql810wc-print-proxy is installed here: run ./selftest.sh itself")
class SelftestReachTest(OpsTestCase):
    """selftest.sh fails T2 while no real client may reach the persona or the
    Bonjour record (its own queries come from this host, which never counts),
    and checks snmpd's UDP 161 listeners like step 30 does. The agent unit
    stays inactive, so no SNMP query is ever sent."""

    def selftest(self, config: Optional[str], *, addrs: str = SourcesClassifyTest.ADDRS,
                 firewalld: bool = True, ss: Optional[str] = None) -> Dict[str, dict]:
        if firewalld:
            self.set_state("active", "firewalld")
        if ss is not None:
            self.set_state("active", "snmpd.service")
            (self.state / "ss.out").write_text(ss)
        (self.state / "ip-addr.out").write_text(addrs)
        env = {"TMPDIR": str(self.root)}
        if config is not None:
            cfg = self.write("config.env", config)
            cfg.chmod(0o600)
            env["QL_PROXY_CONFIG"] = str(cfg)
        done = self.run_bash('"$BASH" "$REPO/selftest.sh" --json', env=env)
        self.assertEqual(done.returncode, 1, done.stderr)   # nothing is installed: no T0
        self.doc = json.loads(done.stdout)
        return {c["id"]: c for c in self.doc["checks"]}

    def test_template_range_fails_t2(self):
        checks = self.selftest('SNMP_PRINTER_ALLOWED_CIDRS="192.0.2.0/24"\n')
        self.assertEqual(checks["t2.reach.snmp"]["status"], "fail")
        self.assertIn("no network client can reach the printer persona", checks["t2.reach.snmp"]["message"])
        self.assertIn("192.0.2.0/24", checks["t2.reach.snmp"]["message"])
        self.assertEqual(checks["t2.reach.mdns"]["status"], "fail")
        self.assertIn(checks["t2.reach.snmp"]["message"], self.doc["tiers"]["T2"]["problems"])

    def test_no_config_env_means_the_template(self):
        if (REPO / "config.env").exists():
            self.skipTest("this checkout has a config.env")
        checks = self.selftest(None)
        self.assertEqual(checks["t2.reach.snmp"]["status"], "fail", "the installer used the template too")

    def test_empty_ranges_fail(self):
        checks = self.selftest('SNMP_PRINTER_ALLOWED_CIDRS=""\n')
        self.assertEqual(checks["t2.reach.snmp"]["status"], "fail")
        self.assertIn("SNMP_PRINTER_ALLOWED_CIDRS is empty", checks["t2.reach.snmp"]["message"])

    def test_real_ranges_pass(self):
        checks = self.selftest('SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24"\n')
        self.assertEqual((checks["t2.reach.snmp"]["status"], checks["t2.reach.mdns"]["status"]), ("pass", "pass"))
        self.assertNotIn("t2.reach.snmp.local", checks)
        self.assertNotIn("t2.reach.mdns.local", checks)

    def test_ranges_off_this_hosts_networks(self):
        checks = self.selftest('SNMP_PRINTER_ALLOWED_CIDRS="198.19.0.0/24"\n')
        self.assertEqual(checks["t2.reach.snmp"]["status"], "pass")
        self.assertEqual(checks["t2.reach.snmp.local"]["status"], "info", "SNMP clients may be routed")
        self.assertEqual(checks["t2.reach.mdns.local"]["status"], "warn", "mDNS never crosses a router")
        checks = self.selftest('SNMP_PRINTER_ALLOWED_CIDRS="198.19.0.0/24"\nMDNS_ALLOWED_CIDRS="fe80::/10"\n')
        self.assertNotIn("t2.reach.mdns.local", checks)

    def test_mdns_without_firewalld(self):
        checks = self.selftest('SNMP_PRINTER_ALLOWED_CIDRS="192.0.2.0/24"\n', firewalld=False)
        self.assertEqual(checks["t2.reach.snmp"]["status"], "fail", "com2sec still limits SNMP")
        self.assertEqual(checks["t2.reach.mdns"]["status"], "info")
        checks = self.selftest('SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24"\nENABLE_MDNS="no"\n')
        self.assertNotIn("t2.reach.mdns", checks)

    def test_udp_161_listeners(self):
        snmpd = 'users:(("snmpd",pid=812,fd=6))'
        v4 = f"UNCONN 0      0            0.0.0.0:161        0.0.0.0:*    {snmpd}\n"
        v6 = f"UNCONN 0      0               [::]:161           [::]:*    {snmpd}\n"
        cfg = 'SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24"\n'
        cfg6 = 'SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24 2001:db8::/64"\n'
        checks = self.selftest(cfg, ss=v4)
        self.assertEqual(checks["t2.listen"]["status"], "pass")
        self.assertNotIn("t2.listen.ipv6", checks)
        checks = self.selftest(cfg, ss="UNCONN 0      0            0.0.0.0:161        0.0.0.0:*\n")
        self.assertEqual(checks["t2.listen"]["status"], "pass", "ss could not name the owner (not root)")
        checks = self.selftest(cfg, ss='UNCONN 0 0 0.0.0.0:161 0.0.0.0:* users:(("python3",pid=9,fd=3))\n')
        self.assertEqual(checks["t2.listen"]["status"], "fail")
        self.assertIn("belongs to 'python3', not snmpd", checks["t2.listen"]["message"])
        checks = self.selftest(cfg, ss=f"UNCONN 0      0          127.0.0.1:161        0.0.0.0:*    {snmpd}\n")
        self.assertIn("bound to specific addresses only", checks["t2.listen"]["message"])
        checks = self.selftest(cfg, ss=f"UNCONN 0      0    0.0.0.0%eth0:161        0.0.0.0:*    {snmpd}\n")
        self.assertEqual(checks["t2.listen"]["status"], "fail", "a device-bound socket is no wildcard")
        checks = self.selftest(cfg, ss="")
        self.assertIn("nothing listens on UDP 161", checks["t2.listen"]["message"])
        checks = self.selftest(cfg6, ss=v4)
        self.assertEqual((checks["t2.listen"]["status"], checks["t2.listen.ipv6"]["status"]), ("pass", "warn"))
        for listeners in (v4 + v6, f"UNCONN 0      0                  *:161              *:*    {snmpd}\n"):
            with self.subTest(listeners=listeners):
                checks = self.selftest(cfg6, ss=listeners)
                self.assertEqual(checks["t2.listen"]["status"], "pass")
                self.assertNotIn("t2.listen.ipv6", checks)
        self.assertFalse([c for c in checks if c.startswith(("t2.ieee1284", "t2.leak"))],
                         "no SNMP query without the persona")

    def test_agent_reload_warnings(self):
        """An agent unit that cannot reload (outdated), or a reload the agent rejected."""
        agent = "ql810wc-snmp-agent.service"
        self.set_state("active", agent)
        self.set_state("enabled", agent)
        cfg = 'SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24"\n'
        show = self.state / "show" / agent     # this stub answers every property of a unit alike
        show.write_text("no\n")
        checks = self.selftest(cfg)
        self.assertEqual(checks["t2.agent"]["status"], "pass")
        self.assertEqual(checks["t2.agent.canreload"]["status"], "warn")
        self.assertIn("unit file is outdated", checks["t2.agent.canreload"]["message"])
        self.assertNotIn("t2.agent.reload", checks)
        show.write_text("session 3: all 5 registrations accepted; last reload failed: x; printer live\n")
        checks = self.selftest(cfg)
        self.assertNotIn("t2.agent.canreload", checks)
        self.assertEqual(checks["t2.agent.reload"]["status"], "warn")
        self.assertIn("still serves the previous settings", checks["t2.agent.reload"]["message"])
        show.write_text("yes\n")
        checks = self.selftest(cfg)
        self.assertFalse([c for c in checks if c.startswith(("t2.agent.canreload", "t2.agent.reload"))])

    def test_monitoring_system_at_the_template_address(self):
        v3 = ('SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24"\nSNMP_V3_ENABLE="yes"\n'
              'SNMP_V3_AUTH_PASS="aaaaaaaaaa"\nSNMP_V3_PRIV_PASS="bbbbbbbbbb"\n')
        checks = self.selftest(v3 + 'SNMP_V3_ALLOWED_CIDRS="192.0.2.50/32"\n')
        self.assertEqual(checks["host.reach"]["status"], "warn")
        self.assertIn("192.0.2.50/32", checks["host.reach"]["message"])
        checks = self.selftest(v3 + 'SNMP_V3_ALLOWED_CIDRS="198.18.0.50/32"\n')
        self.assertNotIn("host.reach", checks)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode 000 file anyway")
    def test_root_only_config_is_not_judged(self):
        cfg = self.write("config.env", 'SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24"\n')
        cfg.chmod(0)
        self.set_state("active", "firewalld")
        done = self.run_bash('"$BASH" "$REPO/selftest.sh" --json',
                             env={"TMPDIR": str(self.root), "QL_PROXY_CONFIG": str(cfg)})
        checks = {c["id"]: c for c in json.loads(done.stdout)["checks"]}
        self.assertEqual(checks["t2.reach"]["status"], "skip")
        self.assertNotIn("t2.reach.snmp", checks)



# ---------------------------------------------------------------------------
# One run at a time (qlp_lock)
# ---------------------------------------------------------------------------
class LockTest(OpsTestCase):
    LOCK = r"""
source "$REPO/scripts/lib/common.sh"
qlp_init test
QLP_LOCK_FILE="$T/run/ql810wc-proxy.lock"
if [[ -n "${NO_FLOCK:-}" ]]; then have() { [[ "$1" != flock ]] && command -v "$1" >/dev/null 2>&1; }; fi
qlp_lock
echo "HELD=$QLP_LOCK_HELD"
if [[ -n "${CHILD:-}" ]]; then
  chmod 0400 "$QLP_LOCK_FILE"   # a child that opened the file again would fail now (unless root)
  "$BASH" -c 'source "$REPO/scripts/lib/common.sh"; qlp_init child; QLP_LOCK_FILE="$T/run/ql810wc-proxy.lock"
              qlp_lock; echo "CHILD-HELD=$QLP_LOCK_HELD"'
fi
"""

    def lock(self, **env: str) -> subprocess.CompletedProcess:
        return self.run_bash(self.LOCK, env=env)

    def test_the_lock_is_taken_and_handed_down(self):
        (self.root / "run").mkdir()
        done = self.lock(CHILD="1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["HELD=1", "CHILD-HELD=1"])
        self.assertTrue((self.root / "run/ql810wc-proxy.lock").exists())
        self.assertEqual(self.calls().count("flock -n 9"), 2, "the child takes over the same lock")

    def test_another_run_holds_it(self):
        (self.root / "run").mkdir()
        (self.state / "lock-busy").touch()
        done = self.lock()
        self.assertEqual(done.returncode, 1)
        self.assertIn("another setup.sh, install.sh or uninstall.sh is changing this box right now", done.stderr)
        self.assertNotIn("HELD=", done.stdout)

    def test_no_lock_file_or_no_flock(self):
        done = self.lock()   # $T/run does not exist
        self.assertEqual(done.returncode, 1)
        self.assertIn("cannot open the lock file", done.stderr)
        self.assertNotIn("another", done.stderr)
        (self.root / "run").mkdir()
        done = self.lock(NO_FLOCK="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("flock is missing (package util-linux-core)", done.stderr)

    def test_every_script_that_changes_the_box_locks(self):
        def lines(rel: str) -> List[str]:
            return [line.strip() for line in (REPO / rel).read_text(encoding="utf-8").splitlines()]
        install = lines("install.sh")
        self.assertEqual(install[install.index("require_root") + 1].split()[0], "qlp_lock")
        uninstall = lines("uninstall.sh")
        self.assertIn('if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then qlp_lock; fi',
                      uninstall[uninstall.index("require_root") + 1:uninstall.index("require_root") + 3])
        setup = (REPO / "setup.sh").read_text(encoding="utf-8")
        common = (REPO / "scripts/lib/common.sh").read_text(encoding="utf-8")
        path = re.search(r'(?m)^QLP_LOCK_FILE="([^"]+)"', common).group(1)
        self.assertIn('S_LOCK_FILE="${QLP_TEST_ROOT:-}%s"' % path, setup, "the one-liner locks the same file")


# ---------------------------------------------------------------------------
# install.sh: which config.env an install applies
# ---------------------------------------------------------------------------
class InstallConfigGuardTest(OpsTestCase):
    """A copy of install.sh and common.sh with two stand-in steps: 00 (the
    read-only preflight) and 10. Paths outside the checkout live in $T."""

    INSTALL = r"""
source "$CO/scripts/lib/common.sh"
require_root() { :; }
QLP_STATE_DIR="$T/var/lib/ql810wc-proxy" QLP_LOG_DIR="$T/var/log/ql810wc-proxy" QLP_ETC_DIR="$T/etc/ql810wc-proxy"
QLP_MANIFEST="$T/var/lib/ql810wc-proxy/manifest" QLP_RUNTIME_ENV="$T/etc/ql810wc-proxy/runtime.env"
QLP_LOCK_FILE="$T/install.lock"
source "$CO/install.sh" "$@"
"""

    def setUp(self) -> None:
        super().setUp()
        self.co = self.root / "checkout"
        for rel in ("install.sh", "scripts/lib/common.sh", "config.sample.env"):
            self.write(f"checkout/{rel}", (REPO / rel).read_text(encoding="utf-8"))
        for num in ("00", "10"):
            self.write(f"checkout/scripts/{num}-step.sh", f'echo "step {num}" >>"$T/steps.log"\n')
        self.record = self.root / "var/lib/ql810wc-proxy/config-path"
        self.runtime = self.root / "etc/ql810wc-proxy/runtime.env"

    def install(self, *args: str) -> subprocess.CompletedProcess:
        return self.run_bash(self.INSTALL, "--no-selftest", *args, env={"CO": str(self.co)})

    def steps(self) -> List[str]:
        log = self.root / "steps.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def installed_from(self, recorded: Optional[str]) -> None:
        self.write("etc/ql810wc-proxy/runtime.env", 'RAW_PORT="9100"\n')
        if recorded is not None:
            self.write("var/lib/ql810wc-proxy/config-path", recorded + "\n")

    def test_the_config_in_use_is_recorded(self):
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.record.read_text(encoding="utf-8"), "none\n", "the template defaults")
        self.assertEqual(self.steps(), ["step 00", "step 10"])
        self.assertCalledOnce("flock -n 9")
        cfg = self.write("checkout/config.env", 'SNMP_PRINTER_ALLOWED_CIDRS="198.18.0.0/24"\n')
        cfg.chmod(0o600)
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.record.read_text(encoding="utf-8"), f"{cfg.resolve()}\n")

    def test_the_preflight_alone_records_nothing(self):
        done = self.install("--only", "00")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.record.exists())

    def test_an_installed_box_keeps_its_settings(self):
        other = self.write("other/config.env", 'PRINTER_NAME_SUFFIX="desk"\n')
        self.write("other/install.sh", "")
        self.installed_from(str(other))
        done = self.install()
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(self.steps(), [], "no step ran")
        self.assertEqual(self.record.read_text(encoding="utf-8"), f"{other}\n", "the record stays")
        for text in (f"this box is already installed with the settings in {other}, but {self.co}/config.env does",
                     f"sudo install -m 600 -o root -g root {other} {self.co}/config.env",
                     f"or run install.sh in the checkout you installed from: sudo {other.parent}/install.sh",
                     "sudo ./setup.sh --new-config"):
            self.assertIn(text, done.stderr)
        # The file named somewhere that is no checkout.
        (self.root / "other/install.sh").unlink()
        done = self.install()
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn(f"or name the file: sudo env QL_PROXY_CONFIG={other} {self.co}/install.sh", done.stderr)

    def test_an_install_on_the_template_defaults_goes_on(self):
        self.installed_from("none")
        done = self.install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.steps(), ["step 00", "step 10"])

    def test_older_installs_are_found_in_their_log(self):
        self.installed_from(None)
        log_dir = self.root / "var/log/ql810wc-proxy"
        self.write("var/log/ql810wc-proxy/install-20261101T000000Z.log",
                   "2026-11-01T00:00:00+0000 INFO  [install] config: /srv/old/config.env\n")
        self.write("var/log/ql810wc-proxy/install-20261102T000000Z.log",
                   "2026-11-02T00:00:00+0000 WARN  [install] no config.env found — using template defaults\n")
        done = self.install()
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("(/srv/old/config.env, which is gone)", done.stderr)
        for path in log_dir.glob("install-*.log"):
            path.unlink()
        done = self.install()
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("(which config.env is not recorded)", done.stderr)

    def test_uninstall_drops_the_record(self):
        text = (REPO / "uninstall.sh").read_text(encoding="utf-8")
        self.assertRegex(text, r'rm -f -- "\$QLP_STATE_DIR/passpersist\.digest" "\$QLP_STATE_DIR/config-path"')


# ---------------------------------------------------------------------------
# The printer identity (PRINTER_UUID)
# ---------------------------------------------------------------------------
class IdentityTest(OpsTestCase):
    IDENTITY = r"""
source "$REPO/scripts/lib/common.sh"
qlp_init test
QLP_ETC_DIR="$T/etc" QLP_IDENTITY_ENV="$T/etc/identity.env" QLP_STATE_DIR="$T/var-lib"
qlp_ensure_dir() { mkdir -p "$1"; }
qlp_write_file() {
  local tmp; tmp="$(mktemp)"; cat >"$tmp"; QLP_CHANGED=0
  if ! cmp -s "$tmp" "$1"; then mkdir -p "${1%/*}"; cp "$tmp" "$1"; QLP_CHANGED=1; fi
  rm -f "$tmp"
}
qlp_ensure_identity
echo "UUID=$PRINTER_UUID"
"""
    UPPER = "3F2504E0-4F89-41D3-9A0C-0305E82C3301"

    def identity(self) -> subprocess.CompletedProcess:
        return self.run_bash(self.IDENTITY)

    def uuid_in(self, rel: str) -> str:
        return qconfig.parse_env_file(self.root / rel).values["PRINTER_UUID"]

    def test_the_daemons_spellings_are_kept_in_lower_case(self):
        lower = self.UPPER.lower()
        for line in (f'PRINTER_UUID="{self.UPPER}"', f"PRINTER_UUID={lower}", f"PRINTER_UUID='{lower}'",
                     f'  PRINTER_UUID="{lower}"   # restored from the old card'):
            with self.subTest(line=line):
                self.write("etc/identity.env", line + "\n")
                self.write("var-lib/identity.env", 'PRINTER_UUID="00000000-0000-4000-8000-000000000001"\n')
                done = self.identity()
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(f"UUID={lower}", done.stdout)
                self.assertEqual(self.uuid_in("etc/identity.env"), lower)
                self.assertEqual(self.uuid_in("var-lib/identity.env"), lower, "the copy follows the hand edit")
                self.assertNotIn("generated", done.stderr)

    def test_the_copy_restores_a_deleted_file(self):
        self.write("var-lib/identity.env", f'PRINTER_UUID="{shell.UUID}"\n')
        done = self.identity()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.uuid_in("etc/identity.env"), shell.UUID)

    def test_a_broken_identity_stops_the_step(self):
        for text, where in (('PRINTER_UUID="not-a-uuid"\n', "identity.env:2: PRINTER_UUID='not-a-uuid'"),
                            ("PRINTER_UUID=$(uuidgen)\n", "identity.env:2: command substitution")):
            with self.subTest(text=text):
                before = "# hand-written\n" + text
                self.write("etc/identity.env", before)
                self.write("var-lib/identity.env", f'PRINTER_UUID="{shell.UUID}"\n')
                done = self.identity()
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(where, done.stderr)
                self.assertIn("Correct the file, or delete it", done.stderr)
                self.assertEqual((self.root / "etc/identity.env").read_text(encoding="utf-8"), before)
                self.assertNotIn("UUID=", done.stdout)

    def test_a_new_identity_is_announced(self):
        done = self.identity()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("INFO  [test] generated a new printer identity (PRINTER_UUID)", done.stderr)
        self.write("etc/identity.env", "# PRINTER_UUID left out\n")
        (self.root / "var-lib/identity.env").unlink()
        done = self.identity()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("WARN  [test] generated a new printer identity (PRINTER_UUID):", done.stderr)
        self.assertIn("Clients see a new printer.", done.stderr)

    READER = r"""
source "$REPO/scripts/lib/common.sh"
qlp_init test
uuid="$(qlp_identity_uuid "$T/etc/identity.env")"
echo "UUID=[$uuid]"
"""

    def test_the_selftests_reader_prints_the_uuid_and_never_stops(self):
        """selftest.sh reports a missing or broken identity instead of stopping:
        qlp_identity_uuid prints the UUID (lower case) or nothing."""
        lower = self.UPPER.lower()
        for text, expected in ((f'# kept\nPRINTER_UUID="{self.UPPER}"  # comment\n', lower),
                               ('PRINTER_UUID="not-a-uuid"\n', ""), ("PRINTER_UUID=$(uuidgen)\n", ""),
                               ("# PRINTER_UUID left out\n", ""), (None, "")):
            with self.subTest(text=text):
                path = self.root / "etc/identity.env"
                if text is None:
                    path.unlink(missing_ok=True)
                else:
                    self.write("etc/identity.env", text)
                done = self.run_bash(self.READER)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(f"UUID=[{expected}]", done.stdout)
                self.assertNotIn("ERROR", done.stderr)

    def test_no_caller_reads_the_printout_of_the_installers_reader(self):
        """_qlp_read_uuid sets QLP_READ_UUID and prints nothing: a "$(...)"
        around it always yields an empty UUID (selftest.sh once did that, and
        failed T2 on a working box)."""
        for path in sorted({*REPO.glob("*.sh"), *REPO.glob("scripts/*.sh"), *REPO.glob("scripts/lib/*.sh")}):
            with self.subTest(path=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"\$\(\s*_qlp_read_uuid\b")


if __name__ == "__main__":
    unittest.main()
