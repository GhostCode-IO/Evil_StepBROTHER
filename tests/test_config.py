"""Tests for agent/ql810wc/config.py (env-file parser, runtime defaults, getters).

ShellParityTest runs scripts/lib/common.sh's qlp_parse_env_file on the same
files and requires identical results. It needs bash >= 4.2 (as common.sh
does) and is skipped where only an older bash exists (macOS ships 3.2).
"""
from __future__ import annotations

import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import List, Optional, Sequence

REPO = pathlib.Path(__file__).resolve().parent.parent
AGENT_DIR = REPO / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from ql810wc import config  # noqa: E402
from ql810wc.config import ConfigError  # noqa: E402

UUID = "e3248000-80ce-11db-8000-00005e005301"   # Brother-style, documentation MAC suffix

TRICKY = (
    'A="x # y"  # comment\n'
    "B=plain # comment\n"
    "  C=  lead\n"
    "D='sq # x'\n"
    'E="a"b\n'
    "F=a#b\r\n"
    'G="crlf"\r\n'
    "# a comment line\n"
    "\n"
    "   \t\n"
    "H=\n"
    'I=""\n'
    "J=x=y z  \n"
    "\tK='a\"b'\t# tab\n"
    'L="it\'s"\n'
    "M=#all comment\n"
    'N="unterminated\n'
    "O=   \n"
    "P=a\\nb\n"
    'Q="café ☕"\n'
    "R=$HOME/x\n"
    "S=ok # $(not run)\n"
    "DUP=1\n"
    "DUP=2"
)
TRICKY_VALUES = {
    "A": "x # y", "B": "plain", "C": "  lead", "D": "sq # x", "E": '"a"b', "F": "a", "G": "crlf",
    "H": "", "I": "", "J": "x=y z", "K": 'a"b', "L": "it's", "M": "", "N": '"unterminated', "O": "",
    "P": "a\\nb", "Q": "café ☕", "R": "$HOME/x", "S": "ok", "DUP": "2",
}
BAD_FILES = {
    "lower=1\n": 'expected KEY="value"',
    "export A=1\n": 'expected KEY="value"',
    "A = 1\n": 'expected KEY="value"',
    "_A=1\n": 'expected KEY="value"',
    "1A=1\n": 'expected KEY="value"',
    "A\n": 'expected KEY="value"',
    'A="$(id)"\n': "command substitution is not allowed in config",
    "A=`id`\n": "command substitution is not allowed in config",
    "A='$(id)'\n": "command substitution is not allowed in config",
    "A=x$(id)\n": "command substitution is not allowed in config",
    "# ok\n\nA=1\nnot a setting\n": 'expected KEY="value"',
}


class ParseEnvFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, content, name="test.env") -> str:
        path = os.path.join(self.tmp.name, name)
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_value_rules(self):
        result = config.parse_env_file(self.write(TRICKY))
        self.assertEqual(result.values, TRICKY_VALUES)
        self.assertEqual(result.warnings, [])
        values, warnings = result                      # also a plain 2-tuple
        self.assertEqual(values, TRICKY_VALUES)

    def test_errors_name_file_and_line(self):
        for content, message in BAD_FILES.items():
            with self.subTest(content=content):
                path = self.write(content)
                line = content.count("\n") if content.startswith("#") else 1
                with self.assertRaises(ConfigError) as ctx:
                    config.parse_env_file(path)
                self.assertEqual(str(ctx.exception), f"{path}:{line}: {message}")

    def test_unknown_keys_are_warned_and_skipped(self):
        path = self.write("A=1\nTYPO=2\nB=3\n")
        result = config.parse_env_file(path, known_keys={"A", "B"})
        self.assertEqual(result.values, {"A": "1", "B": "3"})
        self.assertEqual(result.warnings, [f"{path}:2: unknown setting 'TYPO' (typo?) — ignored"])
        self.assertEqual(config.parse_env_file(path, known_keys=set()).values,
                         {"A": "1", "TYPO": "2", "B": "3"}, "an empty key set disables the check")

    def test_command_substitution_beats_unknown_key(self):
        path = self.write("TYPO=$(x)\n")
        with self.assertRaises(ConfigError):
            config.parse_env_file(path, known_keys={"A"})

    def test_unreadable(self):
        missing = os.path.join(self.tmp.name, "missing.env")
        with self.assertRaises(ConfigError) as ctx:
            config.parse_env_file(missing)
        self.assertEqual(str(ctx.exception), f"config not readable: {missing}")
        with self.assertRaises(ConfigError):
            config.parse_env_file(self.tmp.name)
        with self.assertRaises(ConfigError):
            config.parse_env_file(self.write(b"A=\xff\n"))

    def test_nul_bytes_are_dropped(self):
        path = self.write(b"A=1\x00\nB=\x002\n")
        result = config.parse_env_file(path)
        self.assertEqual(result.values, {"A": "1", "B": "2"})
        self.assertEqual(result.warnings, [f"{path}: ignored NUL byte(s)"])

    def test_accepts_path_objects(self):
        path = pathlib.Path(self.write("A=1\n"))
        self.assertEqual(config.parse_env_file(path).values, {"A": "1"})

    def test_repository_template_parses(self):
        template = REPO / "config.sample.env"
        result = config.parse_env_file(template)
        self.assertTrue(result.values, "config.sample.env defines settings")
        shared = {k: v for k, v in result.values.items() if k in config.SETTINGS}
        self.assertEqual(config.validate_config(shared), [],
                         "settings shared with the runtime config hold valid values")


def _modern_bash() -> Optional[str]:
    """Path of a bash >= 4.2 (what common.sh requires), or None."""
    bash = shutil.which("bash")
    if bash is None:
        return None
    try:
        out = subprocess.run([bash, "-c", 'printf "%s %s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
                             capture_output=True, text=True, timeout=10, check=True).stdout
        version = tuple(int(part) for part in out.split())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return bash if version >= (4, 2) else None


_SHELL_DUMP = r'''
source "$1/scripts/lib/common.sh"
file="$2"; known="$3"; shift 3
qlp_parse_env_file "$file" "$known"
for k in "$@"; do printf '%s\t%s\0' "$k" "${!k-__UNSET__}"; done
'''
_LOG_LINE = re.compile(r"^\S+ (WARN|ERROR)\s+\[[^\]]*\] (.*)$")
_KEY_LINE = re.compile(r"(?m)^[ \t\r\x0b\x0c]*([A-Z][A-Z0-9_]*)=")


class ShellParityTest(unittest.TestCase):
    """Python and shell must read every file identically."""

    @classmethod
    def setUpClass(cls):
        cls.bash = _modern_bash()
        if cls.bash is None:
            raise unittest.SkipTest("bash >= 4.2 not available (common.sh requires it)")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def shell(self, path: str, known: Sequence[str], keys: Sequence[str]) -> tuple:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NO_COLOR": "1",
               "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
        proc = subprocess.run([self.bash, "-c", _SHELL_DUMP, "qlp", str(REPO), path, "|".join(known), *keys],
                              capture_output=True, env=env, timeout=60)
        log_lines = [m for m in map(_LOG_LINE.match, proc.stderr.decode("utf-8", "replace").splitlines()) if m]
        if proc.returncode != 0:
            errors = [m.group(2) for m in log_lines if m.group(1) == "ERROR"]
            return ("error", errors[-1] if errors else proc.stderr.decode("utf-8", "replace"))
        values = {}
        for item in proc.stdout.split(b"\0"):
            if item:
                key, value = item.decode("utf-8").split("\t", 1)
                if value != "__UNSET__":
                    values[key] = value
        return ("ok", values, [m.group(2) for m in log_lines if m.group(1) == "WARN"])

    @staticmethod
    def python(path: str, known: Sequence[str]) -> tuple:
        try:
            result = config.parse_env_file(path, known)
        except ConfigError as exc:
            return ("error", str(exc))
        return ("ok", result.values, result.warnings)

    def check(self, content: str, known: List[str]) -> None:
        path = os.path.join(self.tmp.name, "parity.env")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        keys = sorted(set(_KEY_LINE.findall(content)))
        self.assertEqual(self.shell(path, known, keys), self.python(path, known), content)

    def test_value_rules(self):
        self.check(TRICKY, [])

    def test_errors(self):
        for content in BAD_FILES:
            with self.subTest(content=content):
                self.check(content, [])

    def test_known_keys(self):
        self.check("A=1\nTYPO=2\nB='3'\nC=x # y\n", ["A", "B"])

    def test_repository_template(self):
        text = (REPO / "config.sample.env").read_text(encoding="utf-8")
        self.check(text, sorted(set(_KEY_LINE.findall(text))))


class LoadRuntimeConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def write(self, name: str, text: str) -> pathlib.Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_defaults_only(self):
        cfg = config.load_runtime_config(None)
        self.assertEqual(dict(cfg), config.DEFAULTS)
        self.assertEqual(cfg.sources, ())
        self.assertIsInstance(cfg, dict)
        self.assertEqual(cfg.get_int("RAW_PORT"), 9100)
        self.assertFalse(cfg.get_bool("RAW_BIDIRECTIONAL"))
        self.assertEqual(cfg["MEDIA_DEFAULT"], "62red")
        self.assertEqual(cfg["STATE_FILE"], "/run/ql810wc-proxy/state.json")

    def test_runtime_file_and_sibling_identity(self):
        runtime = self.write("runtime.env", 'RAW_PORT="9101"\nRAW_BIDIRECTIONAL=yes\n'
                                            "LOG_LEVEL=debug\nSNMP_AGENT_MODE=PASS_PERSIST\n"
                                            "CUPS_QUEUE=QL810Wc\n")
        identity = self.write("identity.env", f'PRINTER_UUID="{UUID}"\n')
        cfg = config.load_runtime_config(runtime)
        self.assertEqual(cfg.get_int("RAW_PORT"), 9101)
        self.assertTrue(cfg.get_bool("RAW_BIDIRECTIONAL"))
        self.assertEqual(cfg["LOG_LEVEL"], "DEBUG", "choices are normalised")
        self.assertEqual(cfg["SNMP_AGENT_MODE"], "pass_persist")
        self.assertEqual(cfg["PRINTER_UUID"], UUID)
        self.assertEqual(cfg["CUPS_QUEUE"], "QL810Wc", "keys outside SETTINGS are kept")
        self.assertEqual(cfg.sources, (str(runtime), str(identity)))

    def test_identity_is_optional_and_overridable(self):
        runtime = self.write("runtime.env", "RAW_PORT=9100\n")
        cfg = config.load_runtime_config(runtime)
        self.assertEqual(cfg["PRINTER_UUID"], "")
        self.assertEqual(cfg.sources, (str(runtime),))
        other = self.dir / "elsewhere"
        other.mkdir()
        identity = other / "id.env"
        identity.write_text(f"PRINTER_UUID={UUID}\n", encoding="utf-8")
        cfg = config.load_runtime_config(runtime, identity_path=identity)
        self.assertEqual(cfg["PRINTER_UUID"], UUID)
        cfg = config.load_runtime_config(None, identity_path=identity)
        self.assertEqual(cfg.sources, (str(identity),))

    def test_missing_or_broken_runtime_file(self):
        with self.assertRaises(ConfigError):
            config.load_runtime_config(self.dir / "absent.env")
        broken = self.write("runtime.env", "RAW_PORT=9100\nraw_port=1\n")
        with self.assertRaises(ConfigError) as ctx:
            config.load_runtime_config(broken)
        self.assertIn(f"{broken}:2:", str(ctx.exception))

    def test_validation_reports_every_problem(self):
        bad = {
            "RAW_PORT": "0", "RAW_BIDIRECTIONAL": "maybe", "MEDIA_DEFAULT": "DK-22251",
            "USB_PRODUCT_ID": "20 9c", "USB_VENDOR_ID": "", "PERSONA_MAC": "00:00:5E:00:53",
            "PRINTER_UUID": "not-a-uuid", "RAW_BIND": "localhost", "PROXY_IP": "2001:db8::1",
            "STATE_FILE": "relative/state.json", "SNMP_AGENT_MODE": "proxy",
            "STATUS_POLL_SECONDS": "ten", "STATUS_MAX_AGE_SECONDS": "1",
            "RAW_IDLE_TIMEOUT": "5", "MAX_JOB_BYTES": "16M", "MEDIA_SECOND": "DK-11201",
        }
        runtime = self.write("runtime.env", "".join(f'{k}="{v}"\n' for k, v in bad.items()))
        with self.assertRaises(ConfigError) as ctx:
            config.load_runtime_config(runtime)
        message = str(ctx.exception)
        for key in bad:
            self.assertIn(f"{key}=", message)
        cfg = config.load_runtime_config(runtime, validate=False)
        self.assertEqual(cfg["RAW_PORT"], "0")

    def test_status_age_must_exceed_two_polls(self):
        runtime = self.write("runtime.env", "STATUS_POLL_SECONDS=30\nSTATUS_MAX_AGE_SECONDS=45\n")
        with self.assertRaises(ConfigError) as ctx:
            config.load_runtime_config(runtime)
        self.assertIn("at least twice STATUS_POLL_SECONDS", str(ctx.exception))
        runtime.write_text("STATUS_POLL_SECONDS=30\nSTATUS_MAX_AGE_SECONDS=60\n", encoding="utf-8")
        self.assertEqual(config.load_runtime_config(runtime).get_int("STATUS_MAX_AGE_SECONDS"), 60)

    def test_valid_examples(self):
        good = {"RAW_BIND": "2001:db8::10", "PROXY_IP": "192.0.2.10", "PERSONA_MAC": "00:00:5E:00:53:01",
                "USB_PRODUCT_ID": "209c", "MEDIA_DEFAULT": "29x90", "MEDIA_SECOND": "62red",
                "PRINTER_UUID": UUID.upper(),
                "RAW_IDLE_TIMEOUT": "600", "MAX_JOB_BYTES": "33554432"}
        runtime = self.write("runtime.env", "".join(f"{k}={v}\n" for k, v in good.items()))
        cfg = config.load_runtime_config(runtime)
        for key, value in good.items():
            self.assertEqual(cfg[key], value)

    def test_installer_style_runtime_env_loads_without_warnings(self):
        # What qlp_sync_runtime_env writes: a comment header and KEY="value" for each
        # runtime key, including the data-plane limits ql810wc-proxyd reads.
        written = {
            "PROXY_IP": "", "RAW_PORT": "9100", "RAW_BIND": "", "RAW_BIDIRECTIONAL": "no",
            "RAW_IDLE_TIMEOUT": "120", "MAX_JOB_BYTES": "1048576", "STATUS_POLL_SECONDS": "3",
            "CUPS_QUEUE": "QL810Wc-proxy", "USB_VENDOR_ID": "04f9", "USB_PRODUCT_ID": "", "USB_SERIAL": "",
            "MEDIA_DEFAULT": "62red", "MEDIA_SECOND": "", "SNMP_AGENT_MODE": "agentx",
            "PRINTER_NAME_SUFFIX": "", "PERSONA_LOCATION": "Front office", "PERSONA_CONTACT": "",
        }
        runtime = self.write("runtime.env", "# ql810wc-print-proxy runtime settings (non-secret), generated\n"
                                            "# from config.env by the installer.\n"
                             + "".join(f'{k}="{v}"\n' for k, v in written.items()))
        self.write("identity.env", f'# printer identity\nPRINTER_UUID="{UUID}"\n')
        with self.assertLogs("ql810wc", level="DEBUG") as logs:
            cfg = config.load_runtime_config(runtime)
            logging.getLogger("ql810wc.tests").debug("loaded")   # assertLogs needs one record
        self.assertEqual([r.getMessage() for r in logs.records if r.levelno >= logging.WARNING], [])
        self.assertEqual(cfg.get_int("RAW_IDLE_TIMEOUT"), 120)
        self.assertEqual(cfg.get_int("MAX_JOB_BYTES"), 1048576)
        self.assertEqual(cfg["PRINTER_UUID"], UUID)
        self.assertEqual(config.validate_config(cfg), [])

    def test_data_plane_limits_are_validated_on_load(self):
        cases = {
            "RAW_IDLE_TIMEOUT": {"9": "between 10 and 86400", "86401": "between 10 and 86400",
                                 "soon": "not an integer", "1.5": "not an integer"},
            "MAX_JOB_BYTES": {"65535": "between 65536 and 1073741824",
                              "1073741825": "between 65536 and 1073741824",
                              "16M": "not an integer", "0x1000000": "not an integer"},
        }
        for key, values in cases.items():
            for value, reason in values.items():
                with self.subTest(key=key, value=value):
                    runtime = self.write("runtime.env", f'{key}="{value}"\n')
                    with self.assertRaises(ConfigError) as ctx:
                        config.load_runtime_config(runtime)
                    self.assertIn(f"{key}={value!r}: ", str(ctx.exception))
                    self.assertIn(reason, str(ctx.exception))
        runtime = self.write("runtime.env", 'RAW_IDLE_TIMEOUT=""\nMAX_JOB_BYTES=""\n')
        cfg = config.load_runtime_config(runtime)
        self.assertEqual((cfg.get_int("RAW_IDLE_TIMEOUT"), cfg.get_int("MAX_JOB_BYTES")), (300, 16 * 1024 * 1024),
                         "empty means the default, as ql810wc-proxyd reads it")


class GettersTest(unittest.TestCase):
    def test_get_int(self):
        cfg = {"N": "42", "SP": " 7 ", "BLANK": "", "F": "4.2", "H": "0x10", "RAW_PORT": "", "NEG": "-3"}
        self.assertEqual(config.get_int(cfg, "N"), 42)
        self.assertEqual(config.get_int(cfg, "SP"), 7)
        self.assertEqual(config.get_int(cfg, "NEG"), -3)
        self.assertEqual(config.get_int(cfg, "BLANK", 5), 5)
        self.assertEqual(config.get_int(cfg, "ABSENT", 6), 6)
        self.assertEqual(config.get_int(cfg, "RAW_PORT"), 9100, "blank falls back to the built-in default")
        self.assertEqual(config.get_int({}, "STATUS_POLL_SECONDS"), 3)
        for key in ("BLANK", "ABSENT", "F", "H"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    config.get_int(cfg, key)

    def test_get_int_bounds(self):
        with self.assertRaises(ConfigError):
            config.get_int({"RAW_PORT": "70000"}, "RAW_PORT")
        with self.assertRaises(ConfigError):
            config.get_int({"STATUS_POLL_SECONDS": "0"}, "STATUS_POLL_SECONDS")
        self.assertEqual(config.get_int({"X": "5"}, "X", minimum=1, maximum=9), 5)
        with self.assertRaises(ConfigError):
            config.get_int({"X": "10"}, "X", minimum=1, maximum=9)
        with self.assertRaises(ConfigError):
            config.get_int({"X": "-1"}, "X", minimum=0)

    def test_get_float(self):
        self.assertEqual(config.get_float({"X": "2.5"}, "X"), 2.5)
        self.assertEqual(config.get_float({"X": " 3 "}, "X"), 3.0)
        self.assertEqual(config.get_float({}, "X", 1.5), 1.5)
        self.assertEqual(config.get_float({}, "STATUS_POLL_SECONDS"), 3.0)
        for bad in ("nan", "inf", "-inf", "abc", "1e999"):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError):
                    config.get_float({"X": bad}, "X")
        with self.assertRaises(ConfigError):
            config.get_float({"X": "0.5"}, "X", minimum=1)

    def test_get_bool(self):
        for word in ("1", "y", "YES", "True", "on", "On"):
            self.assertTrue(config.get_bool({"B": word}, "B"), word)
        for word in ("", "0", "n", "No", "FALSE", "off"):
            self.assertFalse(config.get_bool({"B": word}, "B"), word)
        for word in ("maybe", " yes", "2", "enabled"):
            with self.subTest(word=word):
                with self.assertRaises(ConfigError):
                    config.get_bool({"B": word}, "B")
        self.assertTrue(config.get_bool({}, "B", True))
        self.assertFalse(config.get_bool({}, "RAW_BIDIRECTIONAL"))
        with self.assertRaises(ConfigError):
            config.get_bool({}, "B")

    def test_runtime_config_methods(self):
        cfg = config.RuntimeConfig({"N": "3", "F": "1.25", "B": "on"}, ["x.env"])
        self.assertEqual(cfg.get_int("N"), 3)
        self.assertEqual(cfg.get_int("N", minimum=1, maximum=3), 3)
        self.assertEqual(cfg.get_float("F"), 1.25)
        self.assertTrue(cfg.get_bool("B"))
        self.assertEqual(cfg.sources, ("x.env",))
        self.assertEqual(config.RuntimeConfig(), {})


class SettingsTableTest(unittest.TestCase):
    KINDS = {"str", "int", "bool", "hex16", "path", "choice", "ip", "ipv4", "uuid", "mac", "label"}

    def test_every_setting_is_documented_and_typed(self):
        required = {"RAW_PORT", "RAW_BIND", "RAW_BIDIRECTIONAL", "STATUS_POLL_SECONDS", "USB_VENDOR_ID",
                    "USB_PRODUCT_ID", "USB_SERIAL", "MEDIA_DEFAULT", "PRINTER_UUID", "PRINTER_NAME_SUFFIX",
                    "RAW_IDLE_TIMEOUT", "MAX_JOB_BYTES", "RAW_COLOR_CHECK"}
        self.assertTrue(required <= set(config.SETTINGS))
        self.assertTrue(any(key.startswith("PERSONA_") for key in config.SETTINGS))
        for key, setting in config.SETTINGS.items():
            with self.subTest(key=key):
                self.assertRegex(key, r"^[A-Z][A-Z0-9_]*$")
                self.assertGreater(len(setting.doc), 20)
                self.assertIn(setting.kind, self.KINDS)
                if setting.kind == "choice":
                    self.assertIn(setting.default, setting.choices)
                if setting.kind == "int":
                    self.assertIsNotNone(setting.minimum)
                    self.assertIsNotNone(setting.maximum)
        self.assertEqual(config.DEFAULTS, {k: s.default for k, s in config.SETTINGS.items()})

    def test_defaults_are_valid(self):
        self.assertEqual(config.validate_config(config.DEFAULTS), [])
        self.assertEqual(config.validate_config({}), [], "missing keys fall back to defaults")

    def test_integers_are_validated_as_get_int_reads_them(self):
        self.assertEqual(config.validate_config({"RAW_PORT": " 9101\t"}), [])
        self.assertEqual(config.get_int({"RAW_PORT": " 9101\t"}, "RAW_PORT"), 9101)
        self.assertEqual(config.validate_config({"STATUS_POLL_SECONDS": " 30 ",
                                                 "STATUS_MAX_AGE_SECONDS": "60 "}), [])
        self.assertEqual(config.validate_config({"RAW_IDLE_TIMEOUT": "   "}), [], "blank = the default")
        self.assertEqual(config.validate_config({"RAW_PORT": "  "}), ["RAW_PORT='  ': must not be empty"])
        self.assertEqual(config.validate_config({"RAW_PORT": "9 1"}), ["RAW_PORT='9 1': not an integer"])


class DataPlaneLimitsTest(unittest.TestCase):
    """RAW_IDLE_TIMEOUT and MAX_JOB_BYTES are read by ql810wc-proxyd
    (``ProxySettings.from_config``); the config table must judge them exactly
    as it does, so the installer's check rejects what the daemon would."""

    @classmethod
    def setUpClass(cls):
        from ql810wc import proxyd
        cls.proxyd = proxyd

    def test_table_entries(self):
        idle, size = config.SETTINGS["RAW_IDLE_TIMEOUT"], config.SETTINGS["MAX_JOB_BYTES"]
        self.assertEqual((idle.default, idle.kind, idle.minimum, idle.maximum, idle.required),
                         ("300", "int", 10, 86400, False))
        self.assertEqual((size.default, size.kind, size.minimum, size.maximum, size.required),
                         ("16777216", "int", 65536, 1073741824, False))
        self.assertEqual(config.get_int({}, "RAW_IDLE_TIMEOUT"), self.proxyd.DEFAULT_RAW_IDLE_TIMEOUT)
        self.assertEqual(config.get_int({}, "MAX_JOB_BYTES"), self.proxyd.DEFAULT_MAX_JOB_BYTES)
        self.assertEqual(size.minimum, self.proxyd.MIN_MAX_JOB_BYTES)

    def test_same_verdict_and_value_as_proxyd(self):
        common = ["", " ", "\t", "abc", "1.5", "1e3", "0x10", "+300", "--5", "-1", "0", "3 0",
                  "99999999999999999999999"]
        samples = {
            "RAW_IDLE_TIMEOUT": common + ["9", "10", "300", "86400", "86401", " 300 ", "\u00a0120", "0300"],
            "MAX_JOB_BYTES": common + ["65535", "65536", "16777216", "1073741824", "1073741825",
                                       " 1048576\t", "16M", "16 MiB"],
        }
        field = {"RAW_IDLE_TIMEOUT": "raw_idle_timeout", "MAX_JOB_BYTES": "max_job_bytes"}
        for key, values in samples.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    cfg = config.load_runtime_config(None)
                    cfg[key] = value
                    problems = config.validate_config(cfg)
                    try:
                        settings = self.proxyd.ProxySettings.from_config(cfg)
                    except ConfigError:
                        settings = None
                    self.assertEqual(problems == [], settings is not None,
                                     f"config.py: {problems or 'valid'}; proxyd: "
                                     f"{'accepted' if settings is not None else 'rejected'}")
                    if settings is not None:
                        self.assertEqual(getattr(settings, field[key]), config.get_int(cfg, key))


if __name__ == "__main__":
    unittest.main()
