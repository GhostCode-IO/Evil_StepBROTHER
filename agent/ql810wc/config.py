"""Runtime configuration of the ql810wc-proxy daemons.

Two layers:

``parse_env_file(path, known_keys=None)``
    The safe ``KEY=value`` reader. It applies exactly the rules of
    ``qlp_parse_env_file`` in ``scripts/lib/common.sh``, so the shell
    installer and the daemons always read a file the same way:

    * blank lines, and lines whose first non-blank character is ``#``, are
      skipped;
    * every other line must be ``KEY=value``: ``KEY`` matches
      ``[A-Z][A-Z0-9_]*``, may be indented, and ``=`` follows it directly;
    * ``"value"`` and ``'value'`` are taken literally (no escapes; the quote
      character cannot occur inside), optionally followed by blanks and a
      ``# comment``;
    * any other value is everything before the first ``#``, minus trailing
      whitespace (leading whitespace is kept);
    * a value containing ``$(`` or a backtick is rejected;
    * with ``known_keys``, other keys are ignored with a warning;
    * the last assignment of a key wins. Nothing is ever expanded or run.

    Whitespace means the ASCII blanks ``[ \\t\\n\\r\\v\\f]`` (the shell's
    ``[[:space:]]`` under ``LC_ALL=C.UTF-8``); lines end at ``\\n`` only, so a
    CRLF file reads like an LF one. Files must be UTF-8.

``load_runtime_config(path)``
    :data:`DEFAULTS` overlaid with ``/etc/ql810wc-proxy/runtime.env`` (generated
    by the installer from ``config.env``) and the ``identity.env`` next to it,
    validated against :data:`SETTINGS`. Values stay strings; use the typed
    getters :func:`get_int`, :func:`get_float` and :func:`get_bool` (also
    available as methods of the returned :class:`RuntimeConfig`).
"""
from __future__ import annotations

import ipaddress
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Collection, Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple, Union

from . import media, statecache

log = logging.getLogger(__name__)

PathArg = Union[str, "os.PathLike[str]"]

#: Installed runtime settings (non-secret), generated from config.env.
RUNTIME_ENV = "/etc/ql810wc-proxy/runtime.env"
#: Per-install identity (PRINTER_UUID), read from the same directory as the runtime file.
IDENTITY_ENV_NAME = "identity.env"

#: Accepted spellings of booleans, compared case-insensitively. True matches
#: the shell's ``qlp_is_yes``; anything in neither set is a config error.
TRUE_WORDS = frozenset({"1", "y", "yes", "true", "on"})
FALSE_WORDS = frozenset({"", "0", "n", "no", "false", "off"})


class ConfigError(ValueError):
    """A configuration file or value is invalid (message says where and why)."""


class ParsedEnv(NamedTuple):
    """Result of :func:`parse_env_file`."""

    values: Dict[str, str]
    warnings: List[str]


@dataclass(frozen=True)
class Setting:
    """One runtime setting: its default (as text), its kind and what it does."""

    default: str
    kind: str          # str | int | bool | hex16 | path | choice | ip | ipv4 | uuid | mac | label
    doc: str
    choices: Tuple[str, ...] = ()
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    required: bool = False   # an empty value is an error


#: Every runtime setting the daemons read, with its default and meaning.
SETTINGS: Dict[str, Setting] = {
    # --- data plane: ql810wc-proxyd ---------------------------------------
    "RAW_PORT": Setting(
        "9100", "int", "TCP port for raw print jobs. Used when the daemon binds the "
        "listener itself; under systemd, ql810wc-proxyd.socket listens on the same port.",
        minimum=1, maximum=65535, required=True),
    "RAW_BIND": Setting(
        "", "ip", "Address to bind when not socket-activated. Empty = every IPv4 and "
        "IPv6 address."),
    "RAW_BIDIRECTIONAL": Setting(
        "no", "bool", "yes = relay bytes the printer sends back to the 9100 client (for "
        "CUPS back-channel users). Real networked QLs never answer on 9100."),
    "RAW_IDLE_TIMEOUT": Setting(
        "300", "int", "Seconds without data from a connected print client before the proxy "
        "closes the connection. A job that has not ended yet is then torn (the printer is "
        "resynchronised); after a finished job it is an ordinary close. P-touch Editor for Mac "
        "keeps the connection open after a job and sends a short trailer on it once the label "
        "is done. Empty = 300.", minimum=10, maximum=86400),
    "MAX_JOB_BYTES": Setting(
        "16777216", "int", "Largest print job the proxy accepts, in bytes (64 KiB to 1 GiB). A "
        "larger job is torn and the printer resynchronised. The 16 MiB default is well above "
        "an uncompressed 1 m two-color label (about 2.2 MB, or 4.4 MB at 600 dpi). "
        "Empty = 16777216.", minimum=64 * 1024, maximum=1 << 30),
    "RAW_COLOR_CHECK": Setting(
        "yes", "bool", "yes = refuse a print job whose colour mode the loaded roll cannot print: "
        "a black-only job while a black/red roll (DK-22251) is loaded, or a two-color job while "
        "a single-colour roll is loaded. The printer rejects such a job itself, and after a "
        "black-only job on DK-22251 the tested QL-810Wc answered nothing until it was switched "
        "off and on. None of a refused job's print data reaches the printer, and the client "
        "sees the connection reset. Jobs whose colour mode or roll is not certain always go "
        "through. no = forward every job unchecked."),
    "STATUS_POLL_SECONDS": Setting(
        "3", "int", "Seconds between status polls (ESC i S over USB) while idle. The "
        "printer is never polled during a job.", minimum=1, maximum=60, required=True),
    "USB_VENDOR_ID": Setting(
        "04f9", "hex16", "USB vendor id of the printer (Brother).", required=True),
    "USB_PRODUCT_ID": Setting(
        "", "hex16", "USB product id. Empty = auto-detect (the QL-810W and QL-810Wc "
        "report 209c in printer mode)."),
    "USB_SERIAL": Setting(
        "", "str", "USB serial number to bind to when several QL printers are attached "
        "(the printer-mode serial: the QL reports a different one in Editor Lite mode). "
        "Empty = the first one found."),
    "STATE_FILE": Setting(
        statecache.DEFAULT_PATH, "path", "Live state written by the proxy and read by the "
        "SNMP agent (docs/architecture.md section 5).", required=True),
    "MEDIA_DEFAULT": Setting(
        "62red", "label", "brother_ql label id of the primary roll (62red = DK-22251, 62 mm "
        "continuous black/red). Validated, but not used yet: the persona reports the roll the "
        "printer itself reports over USB, and without live status it reports the printer as "
        "switched off. Reserved for a manual-media fallback.", required=True),
    "MEDIA_SECOND": Setting(
        "", "label", "brother_ql label id of a second roll that is swapped in, such as 29x90 "
        "(DK-11201, 29 x 90 mm die-cut). Empty = none. Validated, but not used yet (see "
        "MEDIA_DEFAULT)."),
    "LOG_LEVEL": Setting(
        "INFO", "choice", "Log level of the proxy and SNMP agent daemons (written to the "
        "journal).", choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    # --- identity and discovery ---------------------------------------------
    "PRINTER_UUID": Setting(
        "", "uuid", "Stable per-install id published as the Bonjour TXT record UUID "
        "(P-touch Editor for Mac requires one). Generated once into identity.env and kept "
        "across reinstalls."),
    "PRINTER_NAME_SUFFIX": Setting(
        "", "str", "Bonjour instance name suffix: 'Brother QL-810W [<suffix>]'. Empty = "
        "this host's short name."),
    "PROXY_IP": Setting(
        "", "ipv4", "IPv4 address clients use to reach this box (reported by the printer "
        "persona). Empty = the default-route address, detected at run time."),
    # --- SNMP persona: ql810wc-snmp-agent -----------------------------------
    "SNMP_AGENT_MODE": Setting(
        "agentx", "choice", "agentx = AgentX subagent serving the whole printer persona in "
        "SNMP context 'printer'. pass_persist = degraded fallback serving only the Brother "
        "and Printer-MIB subtrees in the default context.", choices=("agentx", "pass_persist")),
    "AGENTX_SOCKET": Setting(
        "/var/agentx/master", "path", "AgentX master socket of snmpd.", required=True),
    "STATUS_MAX_AGE_SECONDS": Setting(
        "60", "int", "A status frame older than this while no job is running is stale: "
        "the persona then reports the printer as unavailable instead of serving old "
        "media and state. The colour check of ql810wc-proxyd (RAW_COLOR_CHECK) asks the "
        "printer for a new status when the last one is older (not while the printer's "
        "input state is unknown), and lets a job through unchecked if none comes. Must "
        "be at least twice STATUS_POLL_SECONDS.",
        minimum=5, maximum=86400, required=True),
    "PERSONA_LOCATION": Setting(
        "", "str", "sysLocation.0 of the printer persona (Brother clients show it as the "
        "printer's location), also published as the Bonjour TXT record note."),
    "PERSONA_CONTACT": Setting(
        "", "str", "sysContact.0 of the printer persona."),
    "PERSONA_SYSNAME": Setting(
        "", "str", "sysName.0 (node name) of the printer persona. Empty = 'BRW' followed "
        "by the persona MAC as 12 hex digits, like a real Brother Wi-Fi printer."),
    "PERSONA_MAC": Setting(
        "", "mac", "MAC address presented as the printer's (ifPhysAddress.1, node name), "
        "e.g. 00:00:5E:00:53:01. Empty = the MAC of the interface holding PROXY_IP."),
    "PERSONA_SERIAL": Setting(
        "", "str", "Serial number the persona reports. Empty = the printer's own USB "
        "serial number, as the real printer reports it."),
}

#: Default value (text) of every runtime setting.
DEFAULTS: Dict[str, str] = {key: setting.default for key, setting in SETTINGS.items()}

_WS = " \t\n\r\x0b\x0c"
_RE_SKIP = re.compile(r"[ \t\n\r\x0b\x0c]*(#.*)?", re.DOTALL)
_RE_KV = re.compile(r"[ \t\n\r\x0b\x0c]*([A-Z][A-Z0-9_]*)=(.*)", re.DOTALL)
_RE_DQ = re.compile(r'"([^"]*)"[ \t\n\r\x0b\x0c]*(#.*)?', re.DOTALL)
_RE_SQ = re.compile(r"'([^']*)'[ \t\n\r\x0b\x0c]*(#.*)?", re.DOTALL)
_RE_INT = re.compile(r"-?[0-9]+")
_RE_HEX16 = re.compile(r"[0-9A-Fa-f]{4}")
_RE_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_RE_MAC = re.compile(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}")


def parse_env_file(path: PathArg, known_keys: Optional[Collection[str]] = None) -> ParsedEnv:
    """Read a ``KEY=value`` file with the shell installer's exact rules.

    ``known_keys``: names to accept; others are skipped with a warning (as
    the shell does). None or empty disables the check. Returns
    ``ParsedEnv(values, warnings)``. Raises :class:`ConfigError` (message
    ``<file>:<line>: ...`` like the shell's) for an unreadable file, a line
    that is not ``KEY=value``, or command substitution in a value.
    """
    name = os.fspath(path)
    try:
        with open(name, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ConfigError(f"config not readable: {name}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{name}: not valid UTF-8 ({exc})") from None
    warnings: List[str] = []
    if "\x00" in text:
        warnings.append(f"{name}: ignored NUL byte(s)")
        text = text.replace("\x00", "")
    known = frozenset(known_keys) if known_keys else None
    values: Dict[str, str] = {}
    for number, line in enumerate(text.split("\n"), 1):
        if _RE_SKIP.fullmatch(line):
            continue
        match = _RE_KV.fullmatch(line)
        if match is None:
            raise ConfigError(f'{name}:{number}: expected KEY="value"')
        key, value = match.group(1), match.group(2)
        quoted = _RE_DQ.fullmatch(value) or _RE_SQ.fullmatch(value)
        if quoted is not None:
            value = quoted.group(1)
        else:
            value = value.split("#", 1)[0].rstrip(_WS)
        if "$(" in value or "`" in value:
            raise ConfigError(f"{name}:{number}: command substitution is not allowed in config")
        if known is not None and key not in known:
            warnings.append(f"{name}:{number}: unknown setting '{key}' (typo?) — ignored")
            continue
        values[key] = value
    return ParsedEnv(values, warnings)


# ---------------------------------------------------------------------------
# Typed getters
# ---------------------------------------------------------------------------

def _text(cfg: Mapping[str, object], key: str) -> str:
    """The setting's text without surrounding blanks ("" when absent)."""
    value = cfg.get(key)
    return "" if value is None else str(value).strip()


def _builtin(key: str) -> str:
    """The built-in default of ``key``; ConfigError if it has none."""
    fallback = DEFAULTS.get(key, "")
    if not fallback:
        raise ConfigError(f"{key}: no value set")
    return fallback


Bound = Optional[float]


def _bounds(key: str, minimum: Bound, maximum: Bound) -> Tuple[Bound, Bound]:
    setting = SETTINGS.get(key)
    if minimum is None and setting is not None:
        minimum = setting.minimum
    if maximum is None and setting is not None:
        maximum = setting.maximum
    return minimum, maximum


def _check_range(key: str, value: float, minimum: Bound, maximum: Bound) -> None:
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        low = "-inf" if minimum is None else minimum
        high = "inf" if maximum is None else maximum
        raise ConfigError(f"{key}={value}: must be between {low} and {high}")


def get_int(cfg: Mapping[str, object], key: str, default: Optional[int] = None, *,
            minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    """Integer setting (base 10). Blank or absent: ``default`` if given, else
    the built-in default. Bounds default to the setting's own. Raises ConfigError."""
    text = _text(cfg, key)
    if not text:
        if default is not None:
            return default
        text = _builtin(key)
    if not _RE_INT.fullmatch(text):
        raise ConfigError(f"{key}={text!r}: not an integer")
    value = int(text)
    _check_range(key, value, *_bounds(key, minimum, maximum))
    return value


def get_float(cfg: Mapping[str, object], key: str, default: Optional[float] = None, *,
              minimum: Bound = None, maximum: Bound = None) -> float:
    """Decimal setting, same rules as :func:`get_int`. Raises ConfigError."""
    text = _text(cfg, key)
    if not text:
        if default is not None:
            return default
        text = _builtin(key)
    try:
        value = float(text)
    except ValueError:
        raise ConfigError(f"{key}={text!r}: not a number") from None
    if not math.isfinite(value):
        raise ConfigError(f"{key}={text!r}: not a finite number")
    _check_range(key, value, *_bounds(key, minimum, maximum))
    return value


def get_bool(cfg: Mapping[str, object], key: str, default: Optional[bool] = None) -> bool:
    """Boolean setting: 1/y/yes/true/on or 0/n/no/false/off/empty (any case).

    Absent: ``default`` if given, else the built-in default. Any other text
    raises ConfigError (the shell's ``qlp_is_yes`` would silently read it as no).
    """
    value = cfg.get(key)
    if value is None:
        if default is not None:
            return default
        if key not in DEFAULTS:
            raise ConfigError(f"{key}: no value set")
        value = DEFAULTS[key]
    word = str(value).lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ConfigError(f"{key}={value!r}: expected yes or no")


class RuntimeConfig(dict):
    """Settings as ``{KEY: text}`` plus the files they came from (``sources``)
    and the typed getters as methods."""

    def __init__(self, values: Optional[Mapping[str, str]] = None, sources: Iterable[str] = ()) -> None:
        super().__init__(values or {})
        self.sources: Tuple[str, ...] = tuple(sources)

    def get_int(self, key: str, default: Optional[int] = None, *,
                minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
        """See :func:`get_int`."""
        return get_int(self, key, default, minimum=minimum, maximum=maximum)

    def get_float(self, key: str, default: Optional[float] = None, *,
                  minimum: Bound = None, maximum: Bound = None) -> float:
        """See :func:`get_float`."""
        return get_float(self, key, default, minimum=minimum, maximum=maximum)

    def get_bool(self, key: str, default: Optional[bool] = None) -> bool:
        """See :func:`get_bool`."""
        return get_bool(self, key, default)


# ---------------------------------------------------------------------------
# Validation and loading
# ---------------------------------------------------------------------------

def _problem(setting: Setting, value: object) -> Optional[str]:
    """Why ``value`` is invalid for ``setting``, or None if it is fine.

    Integers are judged exactly as :func:`get_int` reads them (surrounding
    blanks ignored; blank = the default), so a value the daemons accept is
    never reported and one they reject always is.
    """
    kind = setting.kind
    value = "" if value is None else str(value)
    if kind == "int":
        value = value.strip()
    if value == "":
        return "must not be empty" if setting.required else None
    if kind == "int":
        if not _RE_INT.fullmatch(value):
            return "not an integer"
        number = int(value)
        if (setting.minimum is not None and number < setting.minimum) or \
           (setting.maximum is not None and number > setting.maximum):
            return f"must be between {setting.minimum} and {setting.maximum}"
    elif kind == "bool":
        if value.lower() not in TRUE_WORDS | FALSE_WORDS:
            return "expected yes or no"
    elif kind == "hex16":
        if not _RE_HEX16.fullmatch(value):
            return "expected 4 hex digits"
    elif kind == "path":
        if not value.startswith("/"):
            return "must be an absolute path"
    elif kind == "choice":
        if value not in setting.choices:
            return f"expected one of {', '.join(setting.choices)}"
    elif kind in ("ip", "ipv4"):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return "not an IP address"
        if kind == "ipv4" and address.version != 4:
            return "not an IPv4 address"
    elif kind == "uuid":
        if not _RE_UUID.fullmatch(value):
            return "not a UUID (8-4-4-4-12 hex digits)"
    elif kind == "mac":
        if not _RE_MAC.fullmatch(value):
            return "not a MAC address (six colon-separated hex pairs)"
    elif kind == "label":
        if media.lookup_label(value) is None:
            return f"unknown label id (one of: {', '.join(media.LABEL_IDS)})"
    return None


def validate_config(cfg: Mapping[str, str]) -> List[str]:
    """Problems with the known settings in ``cfg`` (empty list = valid)."""
    problems = []
    for key, setting in SETTINGS.items():
        value = cfg.get(key, setting.default)
        reason = _problem(setting, value)
        if reason:
            problems.append(f"{key}={value!r}: {reason}")
    if not problems:
        poll = get_int(cfg, "STATUS_POLL_SECONDS")
        max_age = get_int(cfg, "STATUS_MAX_AGE_SECONDS")
        if max_age < 2 * poll:
            problems.append(f"STATUS_MAX_AGE_SECONDS={max_age}: must be at least twice "
                            f"STATUS_POLL_SECONDS ({poll}), or idle status would look stale")
    return problems


def _canonical_choices(values: Dict[str, str]) -> None:
    """Accept choice values in any case ("debug" -> "DEBUG")."""
    for key, setting in SETTINGS.items():
        if setting.kind == "choice" and key in values:
            for choice in setting.choices:
                if values[key].lower() == choice.lower():
                    values[key] = choice


def load_runtime_config(path: Optional[PathArg] = RUNTIME_ENV, *,
                        identity_path: Optional[PathArg] = None,
                        validate: bool = True) -> RuntimeConfig:
    """Load the daemons' settings: :data:`DEFAULTS` overlaid with ``path`` and
    then the identity file.

    ``path`` must exist (ConfigError otherwise); None means built-in defaults
    only. ``identity_path`` defaults to ``identity.env`` in the directory of
    ``path``; the identity file is optional and skipped when absent. Keys not
    in :data:`SETTINGS` are kept as they are. Choice values are normalised
    to their canonical case. With ``validate`` (default), invalid known
    settings raise one ConfigError listing every problem.
    """
    values = dict(DEFAULTS)
    sources: List[str] = []
    identity: Optional[str] = None
    if identity_path is not None:
        identity = os.fspath(identity_path)
    if path is not None:
        main = os.fspath(path)
        parsed = parse_env_file(main)
        for warning in parsed.warnings:
            log.warning("%s", warning)
        values.update(parsed.values)
        sources.append(main)
        if identity is None:
            identity = os.path.join(os.path.dirname(main), IDENTITY_ENV_NAME)
    if identity is not None:
        if os.path.exists(identity):
            parsed = parse_env_file(identity)
            for warning in parsed.warnings:
                log.warning("%s", warning)
            values.update(parsed.values)
            sources.append(identity)
        else:
            log.debug("no identity file at %s", identity)
    _canonical_choices(values)
    cfg = RuntimeConfig(values, sources)
    if validate:
        problems = validate_config(cfg)
        if problems:
            where = ", ".join(sources) or "built-in defaults"
            raise ConfigError(f"invalid configuration ({where}): " + "; ".join(problems))
    return cfg
