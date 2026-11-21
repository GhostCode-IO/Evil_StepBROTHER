#!/usr/bin/env bash
# setup.sh — set up ql810wc-print-proxy on Fedora 44 with one command.
#
#   curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash
#   sudo ./setup.sh [options]                  (in a checkout of the repository)
#
# "setup.sh --help" explains both ways and every option. Everything below is
# a function and the last line calls main, so a download that is cut short is
# a syntax error or does nothing: it never runs half of the script.
# Exit codes: 0 installed (install.sh finished; its selftest, unless left out,
# reached at least T0), 1 error, 3 a person must act first, 64 usage error.
#
# Test-only hooks: tests/test_setup.py sets these environment variables to
# run setup.sh without root, Fedora or the real installer. Never set them on
# a real system.
#   QLP_TEST_EUID         the effective user id to assume instead of $EUID
#   QLP_TEST_OS_RELEASE   the file to read instead of /etc/os-release
#   QLP_TEST_ROOT         a directory that stands in for / in every path outside
#                         the checkout that setup.sh uses (/etc/ql810wc-proxy,
#                         /var/lib/ql810wc-proxy, the lock file, /sys/class/net, ...)
#   QLP_TEST_INSTALL_CMD  the command to run instead of ./install.sh

setup_usage() {
  cat <<'EOF'
setup.sh — set up ql810wc-print-proxy on Fedora 44 with one command

A Fedora 44 box (tested: a Raspberry Pi 4 with Fedora 44 Server) with a
Brother QL-810Wc on USB becomes a network label printer that P-touch Editor
(Mac) and iPrint&Label (iOS) find and print to.

Run it in one of these ways:
  1. On a fresh Fedora 44, as one line:
       curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash
     With options:
       curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash -s -- --clients 192.0.2.0/24 --name office
     192.0.2.0/24 is a documentation-only example: replace it with your
     clients' network in CIDR form, or leave --clients out to use the box's
     own network.
  2. From a checkout of the repository:
       sudo ./setup.sh [options]
  3. Run either one again to upgrade, for the checkout you installed from
     (the one-liner's is /usr/local/src/ql810wc-print-proxy). That is safe:
     setup.sh keeps the checkout's config.env, and the installer changes only
     what differs.

Without a checkout next to it (the one-liner), setup.sh installs git-core
when git is missing, clones
https://github.com/GhostCode-IO/Evil_StepBROTHER.git (branch main) into
/usr/local/src/ql810wc-print-proxy, or updates that checkout with a
fast-forward, and then runs the checkout's setup.sh with the same options.
It stops with an error, and leaves the files and the branch in that
directory as they were, when the directory is not owned by root, holds files
but is not a git checkout, is a checkout of another repository, or cannot be
fast-forwarded. Nothing else has changed by then, except that git-core may
have been installed: the last two checks need git.

In a checkout, setup.sh installs python3 and iproute when they are missing,
writes config.env when there is none yet, runs ./install.sh (every step, then
./selftest.sh) and tells you what to do next. Only one setup.sh, install.sh or
uninstall.sh runs at a time: a second one stops with an error.

A new config.env is a copy of config.sample.env (mode 0600, owned by root)
with these settings filled in; everything else keeps the template default:
  SNMP_PRINTER_ALLOWED_CIDRS  the IPv4 network of the default-route
                              interface (setup.sh names the box's other
                              networks, which this leaves out), or the
                              networks given by --clients
  PRINTER_NAME_SUFFIX         --name (empty: the host name)
  SNMP_V3_ENABLE              "no", or "yes" with --snmpv3
An existing config.env is kept as it is: nothing is detected, and only the
settings an option names (--clients, --name, --snmpv3) are rewritten in it;
setup.sh lists each value it rewrites.

On a box that is installed already, a checkout without config.env (another
clone, or another --dir) makes setup.sh stop with exit status 3 before it
writes a config.env: a new one from the template would replace the settings
the box was installed with. The message names the config.env the
installation uses and how to keep it; --new-config starts from new settings
instead.

Options:
  --clients "CIDR [CIDR...]"
                  the networks of the computers and phones that print (IPv4
                  and/or IPv6; space- or comma-separated; may be repeated).
                  Sets SNMP_PRINTER_ALLOWED_CIDRS: the sources that may read
                  the printer's status over SNMP and, unless
                  MDNS_ALLOWED_CIDRS says otherwise, find it over Bonjour.
  --name NAME     the printer appears as "Brother QL-810W [NAME]" (sets
                  PRINTER_NAME_SUFFIX; at most 45 bytes; "" means the host
                  name).
  --snmpv3        SNMPv3 monitoring of this host: SNMP_V3_ENABLE="yes" and two
                  random passphrases, which later runs keep. They are never
                  printed: read them in config.env. SNMP_V3_ALLOWED_CIDRS (the
                  monitoring system's sources) becomes the client networks
                  while it is empty, the template's example or the same as
                  the client networks; a range of your own is kept.
  --new-config    write a new config.env although this box is installed with
                  settings from elsewhere (see above).
  --config-only   write or update config.env, say where it is, and stop
                  before ./install.sh. The one-liner still clones or updates
                  its checkout, and git-core, python3 and iproute are still
                  installed when they are missing (recorded in the install
                  manifest, which uninstall.sh reads).
  --print-config  print the config.env setup.sh would write, or the existing
                  one, and change nothing. The SNMPv3 passphrases, the serial
                  numbers (USB_SERIAL, PERSONA_SERIAL) and community names
                  other than public and internal are masked.
  --dir DIR       the checkout the one-liner uses (default
                  /usr/local/src/ql810wc-print-proxy).
  --ref REF       the branch or tag the one-liner checks out (default main).
  -- ARGS...      hand ARGS to ./install.sh, for example -- --data-plane-only
                  (see ./install.sh --help).
  -h, --help      show this help.

Environment, for forks and tests: QLP_REPO_URL (the repository to clone),
QLP_SRC_DIR (the checkout directory) and QLP_REPO_REF (branch or tag);
--dir and --ref take precedence.

setup.sh warns when the host name is localhost or fedora: the printer would
then appear as "Brother QL-810W [fedora]". Name the box with
"sudo hostnamectl set-hostname NAME", or pass --name.

Exit codes:
  0   installed: install.sh finished, and its selftest reached at least T0
      ("-- --only N" and "-- --no-selftest" leave the selftest out); with
      --config-only or --print-config: done
  1   error (the install logs are in /var/log/ql810wc-proxy/)
  3   a person must act first, for example connect the printer by USB and
      switch it on, turn Editor Lite off, or choose which settings to keep;
      the message says what, and which command to run again
  64  usage error
EOF
}

# ---------------------------------------------------------------------------
# Settings and messages
# ---------------------------------------------------------------------------
setup_globals() {
  S_REPO_URL_DEFAULT="https://github.com/GhostCode-IO/Evil_StepBROTHER.git"
  S_RAW_URL="https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh"
  S_SRC_DIR_DEFAULT="/usr/local/src/ql810wc-print-proxy"
  S_REF_DEFAULT="main"
  S_ARGS=() S_INSTALL_ARGS=() S_CLIENTS=()
  S_CLIENTS_SET=0 S_NAME="" S_NAME_SET=0 S_SNMPV3=0 S_CONFIG_ONLY=0 S_PRINT_CONFIG=0 S_NEW_CONFIG=0
  S_DIR="" S_REF=""
  S_SCRIPT=""        # this file, when bash runs one (empty: read from stdin)
  S_CLIENT_LIST=""   # the client networks, normalised, space-separated
  S_STATE=""         # the one-liner's checkout directory: new, empty or checkout
  S_GIT_INSTALLED=0  # this run installed git-core
  # Paths outside the checkout (QLP_TEST_ROOT: test-only hook, see the header).
  # The lock file is the one scripts/lib/common.sh names (QLP_LOCK_FILE).
  S_LOCK_FILE="${QLP_TEST_ROOT:-}/run/ql810wc-proxy.lock"
  S_NET_DIR="${QLP_TEST_ROOT:-}/sys/class/net"
}

setup_colors() {
  if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
    S_RED=$'\e[31m' S_YEL=$'\e[33m' S_GRN=$'\e[32m' S_BLU=$'\e[34m' S_OFF=$'\e[0m'
  else
    S_RED='' S_YEL='' S_GRN='' S_BLU='' S_OFF=''
  fi
}

# Messages look like those of scripts/lib/common.sh ("<time> LEVEL [setup]
# text" on stderr). In a checkout, sourcing common.sh replaces these
# functions with its own; the one-liner has nothing but this file.
setup_emit() { # level color text...
  local level="$1" color="$2"
  shift 2
  printf '%s %s%-5s%s [setup] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$color" "$level" "$S_OFF" "$*" >&2
}
log()   { setup_emit INFO "$S_BLU" "$@"; }
ok()    { setup_emit OK "$S_GRN" "$@"; }
warn()  { setup_emit WARN "$S_YEL" "$@"; }
error() { setup_emit ERROR "$S_RED" "$@"; }
die()   { error "$@"; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }

usage_error() { # message...
  error "$@"
  error "usage: setup.sh [options] [-- install.sh options]; see setup.sh --help"
  exit 64
}

# The effective user id (QLP_TEST_EUID: test-only hook, see the header).
setup_euid() { printf '%s' "${QLP_TEST_EUID:-${EUID:-$(id -u)}}"; }

# A word as a shell would need it quoted (for commands shown to the user).
setup_quote() { # word
  local escaped="'\\''"
  if [[ "$1" =~ ^[A-Za-z0-9_./:=@%+,-]+$ ]]; then
    printf '%s' "$1"
  else
    printf "'%s'" "${1//\'/$escaped}"
  fi
}

# One run at a time. setup.sh, install.sh and uninstall.sh take an exclusive
# flock on the same lock file through file descriptor 9 (qlp_lock in
# scripts/lib/common.sh; the one-liner repeats it here, since it has nothing
# else yet). The checkout's setup.sh, and the install.sh it runs, inherit the
# descriptor and with it the lock: QLP_LOCK_HELD=1 tells them so.
setup_lock() {
  local lock="$S_LOCK_FILE"
  if [[ "${QLP_LOCK_HELD:-}" == 1 ]] && { : >&9; } 2>/dev/null \
     && { [[ ! -d /proc/self/fd ]] || [[ /proc/self/fd/9 -ef "$lock" ]]; }; then
    flock -n 9 || die "could not take over the lock on $lock from the setup.sh that started this one"
    return 0
  fi
  have flock || die "flock is missing (package util-linux-core), so setup.sh cannot make sure that no other" \
                    "setup.sh, install.sh or uninstall.sh changes the box at the same time: sudo dnf install util-linux-core"
  if ! { exec 9>>"$lock"; } 2>/dev/null; then
    die "cannot open the lock file $lock (run as root)"
  fi
  if ! flock -n 9; then
    die "another setup.sh, install.sh or uninstall.sh is changing this box right now (it holds $lock)." \
        "Let it finish, then run setup.sh again."
  fi
  QLP_LOCK_HELD=1
  export QLP_LOCK_HELD
}

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
setup_parse_args() {
  local opt
  S_ARGS=("$@")
  while (($#)); do
    opt="$1"
    case "$opt" in
      --clients|--name|--dir|--ref)
        if (($# < 2)); then usage_error "$opt needs a value"; fi
        if [[ "$opt" != --clients && "$2" == -* ]]; then
          usage_error "$opt needs a value (write $opt=VALUE for one that starts with \"-\")"
        fi
        setup_set_option "$opt" "$2"
        shift ;;
      --clients=*|--name=*|--dir=*|--ref=*)
        setup_set_option "${opt%%=*}" "${opt#*=}" ;;
      --snmpv3)       S_SNMPV3=1 ;;
      --new-config)   S_NEW_CONFIG=1 ;;
      --config-only)  S_CONFIG_ONLY=1 ;;
      --print-config) S_PRINT_CONFIG=1 ;;
      -h|--help)      setup_usage; exit 0 ;;
      --)             shift; S_INSTALL_ARGS=("$@"); break ;;
      *)              usage_error "unknown option: $opt" ;;
    esac
    shift
  done
  if ((S_CONFIG_ONLY && S_PRINT_CONFIG)); then
    usage_error "--config-only and --print-config exclude each other"
  fi
  if ((S_CONFIG_ONLY || S_PRINT_CONFIG)) && ((${#S_INSTALL_ARGS[@]})); then
    usage_error "options after \"--\" are for install.sh, which --config-only and --print-config do not run"
  fi
}

setup_set_option() { # option value
  case "$1" in
    --clients) S_CLIENTS+=("$2"); S_CLIENTS_SET=1 ;;
    --name)    S_NAME="$2"; S_NAME_SET=1 ;;
    --dir)     if [[ -z "$2" ]]; then usage_error "--dir needs a directory"; fi
               S_DIR="$2" ;;
    --ref)     S_REF="$2" ;;
  esac
}

# Normalise --clients into S_CLIENT_LIST; a bad entry is a usage error.
setup_check_clients() {
  local out kind text rc=0
  ((S_CLIENTS_SET)) || return 0
  out="$(setup_py clients ${S_CLIENTS[@]+"${S_CLIENTS[@]}"})" || rc=$?
  S_CLIENT_LIST=""
  while IFS=$'\t' read -r kind text; do
    case "$kind" in
      C) S_CLIENT_LIST+="${S_CLIENT_LIST:+ }$text" ;;
      W) warn "$text" ;;
      E) error "$text" ;;
    esac
  done <<<"$out"
  if ((rc == 2)); then usage_error "--clients takes networks such as 192.0.2.0/24 (several: space- or comma-separated)"; fi
  if ((rc != 0)); then die "could not check --clients (python3 failed)"; fi
}

setup_check_name() {
  local out rc=0 tab=$'\t'
  ((S_NAME_SET)) || return 0
  out="$(setup_py name "$S_NAME")" || rc=$?
  if ((rc == 2)); then usage_error "${out#E"$tab"}"; fi
  if ((rc != 0)); then die "could not check --name (python3 failed)"; fi
}

# ---------------------------------------------------------------------------
# Python helpers (python3 -I: isolated mode, so no PYTHON* variable and no
# user site directory changes them). Secret values never leave this program:
# it generates the SNMPv3 passphrases itself and prints them masked at most.
#   clients RAW...        normalise --clients: "C\t<net>", "W\t<msg>", "E\t<msg>" (exit 2)
#   name NAME             check --name ("E\t<msg>", exit 2)
#   route                 stdin "ip -4 route show default" -> the interface to use
#   nets                  stdin "ip -4 -o addr show dev IFACE scope global" -> "C\t<net>"
#   others CHOSEN NETDIR  stdin "ip -4 -o addr show scope global" -> the networks
#                         outside CHOSEN: "O\t<net>\t<interface>\t<1: has a device>"
#   print|write AGENT TEMPLATE TARGET   config.env (settings come in QLPS_* variables)
#   get AGENT TEMPLATE TARGET KEY       the effective value of one non-secret setting
# ---------------------------------------------------------------------------
setup_py_source() {
  cat <<'PY'
import ipaddress
import os
import re
import secrets
import signal
import stat
import string
import sys
import tempfile
import time
import unicodedata

NAME_PREFIX = "Brother QL-810W"
NAME_MAX_BYTES = 63 - len(NAME_PREFIX) - 3   # "Brother QL-810W [NAME]" is one DNS label
SECRET_KEYS = ("SNMP_V3_AUTH_PASS", "SNMP_V3_PRIV_PASS")
SERIAL_KEYS = ("USB_SERIAL", "PERSONA_SERIAL")
COMMUNITY_KEYS = ("SNMP_PRINTER_COMMUNITIES", "SNMP_PRINTER_COMMUNITY")
PLAIN_COMMUNITIES = ("public", "internal")
MASK = "********"
PASS_CHARS = string.ascii_letters + string.digits   # safe in every parser that reads them
PASS_LENGTH = 32
# A temporary config file: ".config.<random>.tmp.env". Git ignores "*.env", and
# the hygiene check refuses to commit one.
TMP_PREFIX, TMP_SUFFIX = ".config.", ".tmp.env"
TMP_STALE_SECONDS = 60
WS = " \t\r\x0b\x0c"
LINE = re.compile(r"^([ \t\r\x0b\x0c]*)([A-Z][A-Z0-9_]*)=(.*)$")
QUOTED = re.compile(r"""^(?:"([^"]*)"|'([^']*)')([ \t\r\x0b\x0c]*(?:#.*)?)$""")
HEADER = """\
# ql810wc-print-proxy — the site configuration (config.env), written by
# setup.sh from config.sample.env. It may hold SNMPv3 passphrases: keep it
# private (mode 0600, owned by root). Git ignores it.
#
# To change a setting, edit this file, then run "sudo ./setup.sh" (or
# "sudo ./install.sh") again. setup.sh keeps this file as it is and rewrites
# only the settings an option names (--clients, --name, --snmpv3).
"""


def fail(message):
    sys.stderr.write("setup.sh: %s\n" % message)
    sys.exit(1)


def emit(kind, text):
    sys.stdout.write("%s\t%s\n" % (kind, text))


def cmd_clients(raws):
    entries = [entry for raw in raws for entry in re.split(r"[\s,]+", raw) if entry]
    if not entries:
        emit("E", "--clients needs at least one network")
        return 2
    nets = []
    for raw in entries:
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            emit("E", "--clients: %r is not an IPv4 or IPv6 network (CIDR) or address" % raw)
            return 2
        try:
            ipaddress.ip_network(raw, strict=True)
        except ValueError:
            emit("W", "--clients: %s has host bits set; using %s" % (raw, net))
        if net not in nets:
            nets.append(net)
    for net in nets:
        emit("C", str(net))
    return 0


def cmd_name(name):
    try:
        size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        emit("E", "--name is not valid UTF-8 text")
        return 2
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        emit("E", "--name must not contain control characters")
        return 2
    if any(ch in name for ch in "\"\\$`"):
        emit("E", "--name must not contain a double quote, a backslash, \"$\" or a backtick")
        return 2
    if size > NAME_MAX_BYTES:
        emit("E", "--name is %d bytes long; at most %d fit into \"%s [NAME]\"" % (size, NAME_MAX_BYTES, NAME_PREFIX))
        return 2
    return 0


def cmd_route():
    """The interface of the IPv4 default route with the lowest metric. The
    nexthop lines of a multipath route belong to the route above them."""
    routes = []
    for line in sys.stdin:
        words = line.split()
        if not words:
            continue
        if not line[0].isspace():
            routes.append({"default": words[0] == "default", "metric": 0, "devs": []})
        if not routes:
            continue
        for index, word in enumerate(words[:-1]):
            if word == "dev":
                routes[-1]["devs"].append(words[index + 1])
            elif word == "metric" and words[index + 1].isdigit():
                routes[-1]["metric"] = int(words[index + 1])
    candidates = [route for route in routes if route["default"] and route["devs"]]
    if not candidates:
        return 1
    print(min(candidates, key=lambda route: route["metric"])["devs"][0])
    return 0


def client_net(text):
    """The network of an "inet ADDRESS/PREFIX" word that clients can be on:
    IPv4, neither loopback nor link-local. None for anything else."""
    try:
        net = ipaddress.ip_interface(text).network
    except ValueError:
        return None
    if net.version != 4 or net.is_loopback or net.is_link_local:
        return None
    return net


def cmd_nets():
    """The networks of the interface's global IPv4 addresses, in order."""
    nets = []
    for line in sys.stdin:
        words = line.split()
        for index, word in enumerate(words[:-1]):
            if word != "inet":
                continue
            net = client_net(words[index + 1])
            if net is not None and net not in nets:
                nets.append(net)
    for net in nets:
        if net.prefixlen == 32:
            emit("W", "the detected client network %s holds only this host; pass --clients with your clients' network" % net)
        emit("C", str(net))
    return 0 if nets else 1


def cmd_others(chosen, net_dir):
    """This host's other IPv4 networks: not inside one of CHOSEN, each once.
    An interface without a device in NET_DIR (/sys/class/net) is virtual: a
    bridge, a container network or a tunnel."""
    taken = []
    for word in chosen.split():
        try:
            taken.append(ipaddress.ip_network(word, strict=False))
        except ValueError:
            pass
    seen = []
    for line in sys.stdin:
        words = line.split()
        if len(words) < 4 or words[2] != "inet":
            continue
        name = words[1].rstrip(":").split("@", 1)[0]
        net = client_net(words[3])
        if net is None or net in seen or any(net.version == t.version and net.subnet_of(t) for t in taken):
            continue
        seen.append(net)
        device = os.path.exists(os.path.join(net_dir, name, "device")) if "/" not in name else False
        emit("O", "%s\t%s\t%d" % (net, name, device))
    return 0


def read_text(path):
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except (OSError, UnicodeError) as exc:
        fail("cannot read %s: %s" % (path, exc))


def values_of(agent_dir, path):
    """KEY -> value of the env file PATH, read with the installer's own rules."""
    if agent_dir not in sys.path:
        sys.path.insert(0, agent_dir)
    from ql810wc.config import ConfigError, parse_env_file
    try:
        return dict(parse_env_file(path).values)
    except ConfigError as exc:
        fail(str(exc))


def value_of(raw):
    """The value in the right-hand side RAW of KEY=RAW, as the installer reads it."""
    match = QUOTED.match(raw)
    if match:
        return match.group(1) if match.group(1) is not None else match.group(2)
    return raw.split("#", 1)[0].rstrip(WS)


def tail_of(raw):
    """What follows the value in RAW (blanks, a comment): kept on a rewrite."""
    match = QUOTED.match(raw)
    if match:
        return match.group(3)
    return raw[len(raw.split("#", 1)[0].rstrip(WS)):]


def rewrite(text, settings):
    """TEXT with each assignment of a key in SETTINGS rewritten where it is
    (indentation and trailing comment kept); keys it lacks are appended."""
    lines = text.split("\n")
    found = set()
    for index, line in enumerate(lines):
        match = LINE.match(line)
        if match and match.group(2) in settings:
            key = match.group(2)
            lines[index] = '%s%s="%s"%s' % (match.group(1), key, settings[key], tail_of(match.group(3)))
            found.add(key)
    missing = [key for key in settings if key not in found]
    if missing:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", "# Added by setup.sh:"] + ['%s="%s"' % (key, settings[key]) for key in missing] + [""]
    return "\n".join(lines)


def mask_word(value):
    """As the installer's qlp_mask: the first 3 characters, then "***"."""
    return value[:3] + "***" if len(value) > 3 else value


def masked(text):
    """TEXT for the terminal, which gets pasted into public issues: no
    passphrase, no serial number and no community name of the site."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        match = LINE.match(line)
        if not match:
            continue
        key, raw = match.group(2), match.group(3)
        value, shown = value_of(raw), None
        if key in SECRET_KEYS:
            if value and "CHANGE-ME" not in value:
                shown = MASK
        elif key in SERIAL_KEYS:
            if len(value) > 3:
                shown = mask_word(value)
        elif key in COMMUNITY_KEYS:
            words = value.split()
            if any(word not in PLAIN_COMMUNITIES for word in words):
                shown = " ".join(word if word in PLAIN_COMMUNITIES else mask_word(word) for word in words)
        if shown is not None:
            lines[index] = '%s%s="%s"%s' % (match.group(1), key, shown, tail_of(raw))
    return "\n".join(lines)


def usable(passphrase):
    return len(passphrase) >= 8 and "CHANGE-ME" not in passphrase and not set("\r\n") & set(passphrase)


def new_config(template):
    """config.sample.env with its first paragraph (about the template) replaced."""
    _head, sep, body = template.partition("\n\n")
    return HEADER + "\n" + (body if sep else template)


def remove_stale(directory):
    """Temporary files of an interrupted run: regular files of this user (root)
    older than a minute. Only setup.sh, which holds the lock that keeps other
    runs out, calls this."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    now = time.time()
    for name in names:
        if not (name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX)):
            continue
        path = os.path.join(directory, name)
        try:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and now - info.st_mtime > TMP_STALE_SECONDS:
                os.unlink(path)
        except OSError:
            pass


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)   # so that write_private removes its temporary file


def write_private(path, text):
    """Write TEXT through a new mode-0600 file in the same directory that is
    renamed over PATH: never readable by others, never half-written."""
    real = os.path.realpath(path)
    for signum in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(signum, interrupted)
    fd, tmp = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=os.path.dirname(real))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            os.fchmod(fh.fileno(), 0o600)
            if os.geteuid() == 0:
                os.fchown(fh.fileno(), 0, 0)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def same_list(a, b):
    return a.split() == b.split()


def cmd_config(mode, agent_dir, template_path, target):
    env = os.environ
    clients = env.get("QLPS_CLIENTS", "").strip()
    set_clients = env.get("QLPS_SET_CLIENTS") == "1"
    defaults = values_of(agent_dir, template_path)
    exists = os.path.exists(target)
    current = dict(defaults)
    if exists:
        text = read_text(target)
        current.update(values_of(agent_dir, target))
    else:
        text = new_config(read_text(template_path))
    wanted = {}
    if set_clients or not exists:
        if not clients:
            fail("no client network to write")
        wanted["SNMP_PRINTER_ALLOWED_CIDRS"] = clients
    if env.get("QLPS_SET_NAME") == "1":
        wanted["PRINTER_NAME_SUFFIX"] = env.get("QLPS_NAME", "")
    if env.get("QLPS_SNMPV3") == "1":
        wanted["SNMP_V3_ENABLE"] = "yes"
        for key in SECRET_KEYS:
            if not usable(current.get(key, "")):
                wanted[key] = "".join(secrets.choice(PASS_CHARS) for _ in range(PASS_LENGTH))
        # The monitoring system's sources follow the client networks only while
        # they are no range of their own: empty, the template's example, or the
        # client networks as they are now.
        v3 = current.get("SNMP_V3_ALLOWED_CIDRS", "")
        printers = current.get("SNMP_PRINTER_ALLOWED_CIDRS", "")
        if not exists or not v3.split() or same_list(v3, defaults.get("SNMP_V3_ALLOWED_CIDRS", "")) \
                or same_list(v3, printers):
            wanted["SNMP_V3_ALLOWED_CIDRS"] = wanted.get("SNMP_PRINTER_ALLOWED_CIDRS", printers).strip()
    elif not exists:
        wanted["SNMP_V3_ENABLE"] = "no"
    if exists:
        wanted = {key: value for key, value in wanted.items() if current.get(key) != value}
    for key, value in wanted.items():
        if '"' in value or "\n" in value or "$(" in value or "`" in value:
            fail("%s: this value cannot be written to config.env" % key)
    result = rewrite(text, wanted)
    if mode == "print":
        sys.stdout.write(masked(result))
        return 0
    remove_stale(os.path.dirname(os.path.realpath(target)))
    if exists and result == text:
        print("unchanged")
        return 0
    try:
        write_private(target, result)
    except OSError as exc:
        fail("cannot write %s: %s" % (target, exc))
    if not exists:
        print("created")
        return 0
    print("updated")
    for key, value in wanted.items():
        if key in SECRET_KEYS:
            print("secret\t%s" % key)
        else:
            print("change\t%s\t%s\t%s" % (key, current.get(key, ""), value))
    return 0


def cmd_get(agent_dir, template_path, target, key):
    if key in SECRET_KEYS:
        fail("%s is never printed" % key)
    values = values_of(agent_dir, template_path)
    if os.path.exists(target):
        values.update(values_of(agent_dir, target))
    sys.stdout.write(values.get(key, ""))
    return 0


def main(argv):
    command, args = argv[1], argv[2:]
    if command == "clients":
        return cmd_clients(args)
    if command == "name":
        return cmd_name(args[0])
    if command == "route":
        return cmd_route()
    if command == "nets":
        return cmd_nets()
    if command == "others":
        return cmd_others(*args)
    if command in ("print", "write"):
        return cmd_config(command, *args)
    if command == "get":
        return cmd_get(*args)
    fail("unknown helper command %r" % command)


sys.exit(main(sys.argv))
PY
}

setup_py() { # command [args...]
  python3 -I -B -c "$(setup_py_source)" "$@"
}

# ---------------------------------------------------------------------------
# One-liner mode: no checkout next to this file, or read from stdin
# ---------------------------------------------------------------------------
setup_os_value() { # key file
  local line value=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$1="* ]]; then
      value="${line#*=}"
      value="${value#\"}" value="${value%\"}"
      value="${value#\'}" value="${value%\'}"
    fi
  done <"$2"
  printf '%s' "$value"
}

setup_require_fedora() {
  local file="${QLP_TEST_OS_RELEASE:-/etc/os-release}" id ver   # QLP_TEST_OS_RELEASE: test-only hook
  [[ -r "$file" ]] || die "$file is missing: setup.sh runs on Fedora only (tested: Fedora 44 Server)"
  id="$(setup_os_value ID "$file")"
  ver="$(setup_os_value VERSION_ID "$file")"
  if [[ "$id" != fedora ]]; then
    die "this system is '${id:-unknown}', not Fedora: setup.sh runs on Fedora only (tested: Fedora 44 Server)"
  fi
  if [[ "$ver" != 44 ]]; then
    warn "this is Fedora ${ver:-of an unknown version}; ql810wc-print-proxy is tested on Fedora 44." \
         "Continuing: the installer's preflight check decides."
  fi
}

# What the one-liner's directory holds: S_STATE = new (nothing there), empty
# (an empty directory) or checkout (has .git). Anything else is refused here,
# before anything on the system has changed.
setup_inspect_dir() { # dir
  local dir="$1"
  local -a entries=()
  S_STATE=new
  if [[ -e "$dir" || -L "$dir" ]] && [[ ! -d "$dir" ]]; then
    die "$dir exists and is not a directory: not touching it (choose another place with --dir DIR)"
  fi
  [[ -d "$dir" ]] || return 0
  if [[ ! -O "$dir" ]]; then
    die "$dir is not owned by root: git and the installer run as root and do not use it." \
        "Move it away, or choose another place with --dir DIR."
  fi
  if [[ -e "$dir/.git" ]]; then
    S_STATE=checkout
    return 0
  fi
  shopt -s nullglob dotglob
  entries=("$dir"/*)
  shopt -u nullglob dotglob
  if ((${#entries[@]} == 0)); then
    S_STATE=empty
    return 0
  fi
  die "$dir exists, is not empty and is not a git checkout: not changing it." \
      "Move it away, or choose another place with --dir DIR."
}

setup_ensure_git() {
  have git && return 0
  have dnf || die "git is missing, and there is no dnf to install it with"
  log "git is not installed (a fresh Fedora Server has none): installing git-core"
  dnf -y --setopt=install_weak_deps=False install git-core || die "could not install git-core with dnf (is the network up?)"
  have git || die "git-core is installed, but there is still no git command"
  S_GIT_INSTALLED=1
}

setup_norm_url() { # url -> the form two URLs of the same repository share
  local url="$1"
  while [[ "$url" == */ ]]; do url="${url%/}"; done
  url="${url%.git}"
  case "$url" in
    http://*|https://*) url="${url,,}" ;;
  esac
  printf '%s' "$url"
}

setup_clone() { # dir url ref
  local dir="$1" url="$2" ref="$3" parent
  parent="${dir%/*}"
  mkdir -p -- "${parent:-/}"
  log "cloning $url ($ref) into $dir"
  git clone --quiet --branch "$ref" -- "$url" "$dir" \
    || die "could not clone $url (branch or tag '$ref') into $dir"
  ok "cloned $url ($ref) into $dir"
}

setup_update() { # dir url ref
  local dir="$1" url="$2" ref="$3" origin head new
  if ! git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    die "$dir has a .git, but git does not take it as a checkout (see: git -C $dir status): not changing it"
  fi
  origin="$(git -C "$dir" remote get-url origin 2>/dev/null || true)"
  if [[ -z "$origin" ]]; then
    die "$dir is a git checkout without an \"origin\" remote: not changing it"
  fi
  if [[ "$(setup_norm_url "$origin")" != "$(setup_norm_url "$url")" ]]; then
    die "$dir is a checkout of $origin, not of $url: not changing it." \
        "Choose another place with --dir DIR, or set QLP_REPO_URL to $origin to update this one."
  fi
  log "updating $dir from $url ($ref, fast-forward only)"
  git -C "$dir" fetch --quiet origin "$ref" || die "could not fetch '$ref' from $url into $dir"
  new="$(git -C "$dir" rev-parse --verify --quiet 'FETCH_HEAD^{commit}' || true)"
  head="$(git -C "$dir" rev-parse --verify --quiet 'HEAD^{commit}' || true)"
  if [[ -z "$new" || -z "$head" ]]; then
    die "could not read the fetched '$ref' or the current commit of $dir"
  fi
  if [[ "$new" == "$head" ]]; then
    ok "$dir is up to date with $ref"
    return 0
  fi
  if ! git -C "$dir" merge-base --is-ancestor "$head" "$new"; then
    die "$dir cannot be fast-forwarded to $ref: it has commits that $ref does not have. Nothing was changed." \
        "Look at them with: git -C $dir log; or move $dir away and run setup.sh again for a fresh clone."
  fi
  git -C "$dir" merge --ff-only --quiet "$new" \
    || die "could not fast-forward $dir to $ref (local changes in the way? see: git -C $dir status). Nothing was changed."
  ok "updated $dir to $ref (${new:0:12})"
}

setup_bootstrap() {
  local url ref dir
  url="${QLP_REPO_URL:-$S_REPO_URL_DEFAULT}"
  ref="${S_REF:-${QLP_REPO_REF:-$S_REF_DEFAULT}}"
  dir="${S_DIR:-${QLP_SRC_DIR:-$S_SRC_DIR_DEFAULT}}"
  [[ "$dir" == /* ]] || dir="$PWD/$dir"
  while [[ "$dir" == */ && "$dir" != / ]]; do dir="${dir%/}"; done
  if [[ "$dir" == / ]]; then usage_error "--dir cannot be the root directory"; fi
  if [[ ! "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ || "$ref" == *..* ]]; then
    usage_error "--ref '$ref' is not a branch or tag name"
  fi
  if [[ -n "${QLP_SETUP_BOOTSTRAPPED:-}" ]]; then
    die "$dir/setup.sh finds no install.sh and scripts/lib/common.sh next to it:" \
        "$dir is not a complete ql810wc-print-proxy checkout"
  fi
  if [[ "$(setup_euid)" != 0 && -z "$S_SCRIPT" ]]; then
    die "setup.sh must run as root. Pipe it into \"sudo bash\": curl -fsSL $S_RAW_URL | sudo bash"
  elif [[ "$(setup_euid)" != 0 ]]; then
    die "setup.sh must run as root: sudo bash $(setup_quote "$S_SCRIPT")" \
        "(or pipe it into \"sudo bash\": curl -fsSL $S_RAW_URL | sudo bash)"
  fi
  setup_require_fedora
  # Usage errors before anything changes, where python3 already exists (the
  # checkout's setup.sh checks again in any case).
  if have python3; then
    setup_check_clients
    setup_check_name
  fi
  # --print-config changes nothing: it needs no lock. Every other run holds it
  # from here on; the checkout's setup.sh inherits it.
  if ((!S_PRINT_CONFIG)); then setup_lock; fi
  setup_inspect_dir "$dir"
  export GIT_TERMINAL_PROMPT=0   # never ask for credentials: fail instead
  if ((S_PRINT_CONFIG)); then
    if [[ "$S_STATE" != checkout || ! -f "$dir/setup.sh" ]]; then
      die "--print-config changes nothing, so it needs the checkout in $dir, which is not there yet." \
          "Use --config-only to clone the checkout and write config.env without running install.sh."
    fi
    log "--print-config: using the checkout in $dir as it is (not updated)"
  else
    setup_ensure_git
    if [[ "$S_STATE" == checkout ]]; then
      setup_update "$dir" "$url" "$ref"
    else
      setup_clone "$dir" "$url" "$ref"
    fi
  fi
  if [[ ! -f "$dir/setup.sh" || ! -f "$dir/install.sh" || ! -f "$dir/scripts/lib/common.sh" ]]; then
    die "$dir ($ref of $url) has no setup.sh, install.sh and scripts/lib/common.sh: not a ql810wc-print-proxy checkout"
  fi
  export QLP_SETUP_BOOTSTRAPPED=1
  if ((S_GIT_INSTALLED)); then export QLP_SETUP_GIT_INSTALLED=1; fi
  log "continuing with $dir/setup.sh"
  exec "$BASH" "$dir/setup.sh" ${S_ARGS[@]+"${S_ARGS[@]}"}
}

# ---------------------------------------------------------------------------
# Checkout mode: config.env, then ./install.sh
# ---------------------------------------------------------------------------
# Test-only hook (see the header): QLP_TEST_ROOT stands in for / in the paths
# of scripts/lib/common.sh that setup.sh uses.
setup_test_root() {
  local r="${QLP_TEST_ROOT:-}"
  [[ -n "$r" ]] || return 0
  QLP_ETC_DIR="$r$QLP_ETC_DIR" QLP_STATE_DIR="$r$QLP_STATE_DIR" QLP_LOG_DIR="$r$QLP_LOG_DIR"
  QLP_RUN_DIR="$r$QLP_RUN_DIR" QLP_SYSTEMD_DIR="$r$QLP_SYSTEMD_DIR" QLP_LOCK_FILE="$r$QLP_LOCK_FILE"
  QLP_SNMPD_DROPIN="$r$QLP_SNMPD_DROPIN" QLP_AVAHI_SERVICE="$r$QLP_AVAHI_SERVICE"
  QLP_MANIFEST="$QLP_STATE_DIR/manifest" QLP_BACKUP_DIR="$QLP_STATE_DIR/backups"
  QLP_RUNTIME_ENV="$QLP_ETC_DIR/runtime.env" QLP_IDENTITY_ENV="$QLP_ETC_DIR/identity.env"
}

# python3 (config.env, the client network) and ip (the default route) are on
# every Fedora Server; a minimal system or container may lack them.
setup_ensure_tools() {
  local p
  local -a pkgs=() missing=()
  have python3 || pkgs+=(python3)
  have ip || pkgs+=(iproute)
  ((${#pkgs[@]})) || return 0
  if ((S_PRINT_CONFIG)); then
    die "setup.sh needs ${pkgs[*]}, and --print-config installs nothing: sudo dnf install ${pkgs[*]}"
  fi
  have dnf || die "setup.sh needs ${pkgs[*]}, and there is no dnf to install them with"
  for p in "${pkgs[@]}"; do qlp_pkg_installed "$p" || missing+=("$p"); done
  if ((${#missing[@]})); then
    log "setup.sh needs ${missing[*]}: installing"
    run dnf -y --setopt=install_weak_deps=False install "${missing[@]}" \
      || die "could not install ${missing[*]} with dnf (is the network up?)"
    for p in "${missing[@]}"; do qlp_manifest_add pkg "$p"; done   # uninstall.sh lists them
  fi
  have python3 || die "python3 is still missing"
  have ip || die "ip (package iproute) is still missing"
}

# The client network: the IPv4 network(s) of the default-route interface.
setup_detect_clients() {
  local routes ifc addrs out kind text
  routes="$(ip -4 route show default 2>/dev/null || true)"
  ifc="$(setup_py route <<<"$routes" 2>/dev/null || true)"
  if [[ -z "$ifc" ]]; then
    die "no IPv4 default route, so setup.sh cannot tell which network your computers and phones" \
        "print from. Pass it: --clients CIDR (for example --clients 192.0.2.0/24)"
  fi
  addrs="$(ip -4 -o addr show dev "$ifc" scope global 2>/dev/null || true)"
  out="$(setup_py nets <<<"$addrs" || true)"
  S_CLIENT_LIST=""
  while IFS=$'\t' read -r kind text; do
    case "$kind" in
      C) S_CLIENT_LIST+="${S_CLIENT_LIST:+ }$text" ;;
      W) warn "$text" ;;
    esac
  done <<<"$out"
  if [[ -z "$S_CLIENT_LIST" ]]; then
    die "the default-route interface $ifc has no IPv4 address, so setup.sh cannot tell which network" \
        "your computers and phones print from. Pass it: --clients CIDR (for example --clients 192.0.2.0/24)"
  fi
  log "client network: $S_CLIENT_LIST (the network of $ifc, which holds the default route; --clients sets another)"
  setup_other_networks
}

# The networks of this host that the detected client network leaves out. On
# an interface with a device (Ethernet, Wi-Fi) they can hold clients, which
# then do not find the printer: a warning. Bridges, container networks and
# tunnels have no device: a note only.
setup_other_networks() {
  local addrs out kind net ifc device list="" names=""
  addrs="$(ip -4 -o addr show scope global 2>/dev/null || true)"
  out="$(setup_py others "$S_CLIENT_LIST" "$S_NET_DIR" <<<"$addrs" 2>/dev/null || true)"
  while IFS=$'\t' read -r kind net ifc device; do
    [[ "$kind" == O ]] || continue
    if [[ "$device" == 1 ]]; then
      list+=" $net" names+="${names:+, }$net ($ifc)"
    else
      log "not a client network: $net on $ifc (no device: a bridge, container network or tunnel)"
    fi
  done <<<"$out"
  if [[ -n "$names" ]]; then
    warn "this host is also on $names, which the client network leaves out: computers and phones there" \
         "do not find the printer. To admit them too, pass --clients \"$S_CLIENT_LIST$list\""
  fi
}

setup_config() { # print|write config-path
  QLPS_CLIENTS="$S_CLIENT_LIST" QLPS_SET_CLIENTS="$S_CLIENTS_SET" QLPS_NAME="$S_NAME" \
  QLPS_SET_NAME="$S_NAME_SET" QLPS_SNMPV3="$S_SNMPV3" \
    python3 -I -B -c "$(setup_py_source)" "$1" "$QLP_REPO_ROOT/agent" "$QLP_CONFIG_TEMPLATE" "$2"
}

setup_config_get() { # config-path key   (never a passphrase)
  python3 -I -B -c "$(setup_py_source)" get "$QLP_REPO_ROOT/agent" "$QLP_CONFIG_TEMPLATE" "$1" "$2"
}

# Write config.env; for an existing one, say which values the options rewrote.
setup_write_config() { # config-path
  local cfg="$1" out status kind key old new keys=""
  local -a changes=()
  out="$(setup_config write "$cfg")" || die "could not write $cfg"
  status="${out%%$'\n'*}"
  while IFS=$'\t' read -r kind key old new; do
    case "$kind" in
      change) keys+="${keys:+ }$key" changes+=("$key: \"$old\" -> \"$new\"") ;;
      secret) keys+="${keys:+ }$key" changes+=("$key: a new random passphrase (never printed)") ;;
    esac
  done <<<"$out"
  case "$status" in
    created)   ok "wrote $cfg (mode 0600) from config.sample.env" ;;
    unchanged) log "config: $cfg (kept as it is)" ;;
    updated)   ok "config: $cfg (rewrote $keys)"
               for key in ${changes[@]+"${changes[@]}"}; do log "  $key"; done ;;
    *)         die "unexpected answer while writing $cfg: $status" ;;
  esac
}

# The Bonjour name falls back to the short host name: warn about the names a
# fresh install has.
setup_hostname_warning() { # config-path
  local cfg="$1" name="" host
  if ((S_NAME_SET)); then
    name="$S_NAME"
  elif [[ -e "$cfg" ]]; then
    name="$(setup_config_get "$cfg" PRINTER_NAME_SUFFIX || true)"
  fi
  [[ -z "$name" ]] || return 0
  host="$(qlp_short_hostname)"
  case "$host" in
    localhost|fedora)
      warn "this host is called '$(uname -n)', so the printer would appear as \"Brother QL-810W [$host]\"." \
           "Give the box its own name (sudo hostnamectl set-hostname NAME, then run setup.sh again), or pass --name NAME." ;;
  esac
}

# The command that runs this setup again with the same options, and with the
# setup.sh options given here added (before any "--").
setup_rerun_command() { # [setup.sh option...]
  local out a x skip=0 rest=0
  out="sudo $(setup_quote "$QLP_REPO_ROOT/setup.sh")"
  for a in ${S_ARGS[@]+"${S_ARGS[@]}"}; do
    if ((skip)); then skip=0; continue; fi
    if ((!rest)); then
      case "$a" in
        --) rest=1
            for x in "$@"; do out+=" $(setup_quote "$x")"; done ;;
        --dir|--ref) skip=1; continue ;;
        --dir=*|--ref=*) continue ;;
      esac
    fi
    out+=" $(setup_quote "$a")"
  done
  if ((!rest)); then
    for x in "$@"; do out+=" $(setup_quote "$x")"; done
  fi
  printf '%s' "$out"
}

# How to upgrade, as commands run in the checkout.
setup_upgrade_hint() {
  local root="$QLP_REPO_ROOT" pull="git"
  if [[ -O "$root" && "$(setup_euid)" == 0 ]]; then pull="sudo git"; fi
  printf '%s pull --ff-only && sudo ./setup.sh' "$pull"
  if [[ -n "${QLP_SETUP_BOOTSTRAPPED:-}" || "$root" == "$S_SRC_DIR_DEFAULT" ]]; then
    printf '\n             or run the one-line install again: curl -fsSL %s | sudo bash' "$S_RAW_URL"
    if [[ "$root" != "$S_SRC_DIR_DEFAULT" ]]; then printf ' -s -- --dir %s' "$(setup_quote "$root")"; fi
  fi
}

# config.env as the commands in the checkout name it.
setup_config_shown() { # config-path
  if [[ "$1" == "$QLP_REPO_ROOT/config.env" ]]; then printf 'config.env'; else printf '%s' "$1"; fi
}

setup_snmpv3_note() { # config-path
  if [[ "$(setup_config_get "$1" SNMP_V3_ENABLE || true)" == yes ]]; then
    printf '  SNMPv3:    user %s; the passphrases are in %s (never printed)\n' \
      "$(setup_config_get "$1" SNMP_V3_USER || true)" "$(setup_config_shown "$1")"
  fi
}

setup_config_only_summary() { # config-path
  printf 'config.env: %s\n' "$1"
  setup_snmpv3_note "$1"
  printf 'Check or edit it, then install with: sudo %s\n' "$(setup_quote "$QLP_REPO_ROOT/setup.sh")"
}

# Did install.sh run its selftest? It does unless "--only N" or "--no-selftest".
setup_selftest_ran() {
  local a skip=0
  for a in ${S_INSTALL_ARGS[@]+"${S_INSTALL_ARGS[@]}"}; do
    if ((skip)); then skip=0; continue; fi
    case "$a" in
      --only|--no-selftest) return 1 ;;
      --from|--skip) skip=1 ;;
    esac
  done
  return 0
}

# What to do next. It speaks only of the parts that are installed: options
# after "--" may have left steps out (--data-plane-only, --only, --skip).
setup_what_now() { # config-path
  local cfg="$1" name addr mdns part listed="" data=0 persona=0 bonjour=0
  local -a missing=()
  name="$(setup_config_get "$cfg" PRINTER_NAME_SUFFIX || true)"
  if [[ -z "$name" ]]; then name="$(qlp_short_hostname)"; fi
  PROXY_IP="$(setup_config_get "$cfg" PROXY_IP || true)"
  addr="$(qlp_proxy_ip 2>/dev/null || true)"
  if [[ -z "$addr" ]]; then addr="the IPv4 address of this box"; fi
  mdns="$(setup_config_get "$cfg" ENABLE_MDNS || true)"
  mdns="${mdns:-yes}"
  if [[ -e "$QLP_SYSTEMD_DIR/$QLP_UNIT_SOCKET" ]]; then data=1; fi
  if [[ -e "$QLP_SNMPD_DROPIN" ]]; then persona=1; fi
  if [[ -e "$QLP_AVAHI_SERVICE" ]]; then bonjour=1; fi
  if setup_selftest_ran; then
    printf '\nql810wc-print-proxy is installed.\n\n'
  else
    printf '\ninstall.sh finished (install.sh %s); no selftest ran.\n\n' "${S_INSTALL_ARGS[*]}"
  fi
  if ((data && persona && bonjour)) && qlp_is_yes "$mdns"; then
    printf 'P-touch Editor (Mac) lists the printer as "Brother QL-810W [%s]";\n' "$name"
    printf 'iPrint&Label (iOS) finds it too. If P-touch Editor does not list it, use\n'
    printf '"Connect to printer with an IP address" there and enter %s\n' "$addr"
    printf '(not tested yet).\n\n'
  elif ((data && persona)) && ! qlp_is_yes "$mdns"; then
    printf 'Bonjour is off (ENABLE_MDNS="%s"): in P-touch Editor (Mac), use "Connect to\n' "$mdns"
    printf 'printer with an IP address" and enter %s (not tested yet).\n\n' "$addr"
  else
    ((data)) || missing+=("the data plane, TCP 9100 to the printer (step 20)")
    ((persona)) || missing+=("the SNMP persona (step 30)")
    if ((bonjour == 0)) && qlp_is_yes "$mdns"; then missing+=("the Bonjour record (step 40)"); fi
    for part in ${missing[@]+"${missing[@]}"}; do listed+="${listed:+; }$part"; done
    printf 'Not installed yet: %s.\n' "$listed"
    printf 'Until they are, P-touch Editor and iPrint&Label do not find the printer by\n'
    printf 'themselves. To install every step: sudo ./setup.sh\n\n'
  fi
  printf 'In %s:\n' "$QLP_REPO_ROOT"
  printf '  Settings:  %s; after a change, run: sudo ./setup.sh\n' "$(setup_config_shown "$cfg")"
  setup_snmpv3_note "$cfg"
  printf '  Upgrade:   %s\n' "$(setup_upgrade_hint)"
  printf '  Check:     sudo ./selftest.sh\n'
  printf '  Remove:    sudo ./uninstall.sh\n'
}

setup_run_install() { # config-path
  local cfg="$1" rc=0
  local -a cmd=("$BASH" "$QLP_REPO_ROOT/install.sh")
  if [[ -n "${QLP_TEST_INSTALL_CMD:-}" ]]; then cmd=("$QLP_TEST_INSTALL_CMD"); fi   # test-only hook
  if ((${#S_INSTALL_ARGS[@]})); then
    log "running ./install.sh ${S_INSTALL_ARGS[*]}"
  else
    log "running ./install.sh (every install step, then ./selftest.sh)"
  fi
  "${cmd[@]}" ${S_INSTALL_ARGS[@]+"${S_INSTALL_ARGS[@]}"} || rc=$?
  case "$rc" in
    0)  setup_what_now "$cfg"
        exit 0 ;;
    3)  _qlp_emit HUMAN "$_QLP_YEL" "the install stopped: a person must act first (see the HUMAN lines above;" \
          "for example connect the printer by USB and switch it on, or turn Editor Lite off). Then run again:" \
          "$(setup_rerun_command)"
        exit 3 ;;
    64) error "install.sh does not accept the options after \"--\":${S_INSTALL_ARGS[*]+ ${S_INSTALL_ARGS[*]}}" \
              "(see: $QLP_REPO_ROOT/install.sh --help)"
        exit 64 ;;
    *)  error "install.sh stopped with exit $rc: see the messages above. The full log is the newest" \
              "install-*.log in $QLP_LOG_DIR/"
        exit 1 ;;
  esac
}

# This box is installed, but no config.env is found for this checkout: a new
# one from the template would replace the settings it was installed with
# (another checkout's config.env, or a QL_PROXY_CONFIG that sudo dropped).
# Stop before anything is written, unless --new-config; --print-config only
# warns.
setup_check_installed_settings() { # config-path
  local cfg="$1" used
  if ((S_NEW_CONFIG)); then
    if [[ -e "$cfg" ]]; then warn "--new-config: $cfg exists and is kept (it applies only where there is no config.env)"; fi
    return 0
  fi
  qlp_settings_would_be_lost "$cfg" || return 0
  if ((S_PRINT_CONFIG)); then
    used="$(qlp_recorded_config)"
    S_PRINT_WARNING="this box is installed with the settings in ${used:-a config.env elsewhere}, not with the file"
    S_PRINT_WARNING+=" above: setup.sh would stop instead of writing it (unless --new-config)"
    return 0
  fi
  qlp_settings_hint "$cfg" setup.sh
  needs_human "To start from new settings instead (the detected client network, the host name, SNMPv3 off):" \
              "$(setup_rerun_command --new-config)"
}

setup_checkout() { # checkout directory
  local root="$1" cfg
  S_PRINT_WARNING=""
  # shellcheck source=scripts/lib/common.sh
  source "$root/scripts/lib/common.sh"
  qlp_init setup
  setup_test_root
  if [[ -n "$S_DIR$S_REF" && -z "${QLP_SETUP_BOOTSTRAPPED:-}" ]]; then
    warn "--dir and --ref only apply to the one-line install; ignored in a checkout"
  fi
  if ((!S_PRINT_CONFIG)); then
    if [[ "$(setup_euid)" != 0 ]]; then die "setup.sh must run as root: sudo $(setup_quote "$root/setup.sh")"; fi
    qlp_lock   # one run at a time; inherited from the one-liner when it started this one
  fi
  setup_ensure_tools
  if [[ "${QLP_SETUP_GIT_INSTALLED:-}" == 1 ]] && ((!S_PRINT_CONFIG)); then
    qlp_manifest_add pkg git-core   # installed by the one-liner; uninstall.sh lists it
  fi
  setup_check_clients
  setup_check_name
  cfg="$(qlp_config_path)"
  if [[ -z "$cfg" ]]; then cfg="$QLP_REPO_ROOT/config.env"; fi
  setup_check_installed_settings "$cfg"
  if [[ ! -e "$cfg" ]] && ((!S_CLIENTS_SET)); then setup_detect_clients; fi
  if ((S_PRINT_CONFIG)); then
    if [[ -e "$cfg" ]]; then log "$cfg as setup.sh would leave it (secrets, serial numbers and site community names masked):"
    else log "the config.env setup.sh would write to $cfg (secrets, serial numbers and site community names masked):"; fi
    setup_config print "$cfg" || die "could not read $cfg"
    setup_hostname_warning "$cfg"
    if [[ -n "$S_PRINT_WARNING" ]]; then warn "$S_PRINT_WARNING"; fi
    exit 0
  fi
  setup_write_config "$cfg"
  setup_hostname_warning "$cfg"
  if ((S_CONFIG_ONLY)); then
    setup_config_only_summary "$cfg"
    exit 0
  fi
  setup_run_install "$cfg"
}

# ---------------------------------------------------------------------------
main() {
  set -Eeuo pipefail
  # The C locale for the messages and for every tool whose output is read, as
  # qlp_init in scripts/lib/common.sh sets it: under a non-English system
  # locale, date may print the time in another calendar.
  export LC_ALL=C.UTF-8 LANG=C.UTF-8
  # Read from a pipe ("curl ... | sudo bash"), bash takes the rest of this
  # script from stdin: no command may read it. Nothing here asks questions.
  if [[ ! -t 0 ]]; then exec </dev/null; fi
  setup_globals
  setup_colors
  setup_parse_args "$@"
  local here=""
  # Run from a file, BASH_SOURCE holds this file twice (main's own source and
  # its caller's); read from stdin, it holds "main" or nothing.
  if ((${#BASH_SOURCE[@]} >= 2)) && [[ -f "${BASH_SOURCE[0]}" ]]; then
    S_SCRIPT="${BASH_SOURCE[0]}"
    case "${BASH_SOURCE[0]}" in
      */*) here="${BASH_SOURCE[0]%/*}" ;;
      *)   here="." ;;
    esac
    here="$(cd -- "${here:-/}" && pwd)"
  fi
  if [[ -n "$here" && -f "$here/install.sh" && -f "$here/scripts/lib/common.sh" ]]; then
    setup_checkout "$here"
  else
    setup_bootstrap
  fi
}

main "$@"
