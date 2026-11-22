#!/usr/bin/env bash
# scripts/lib/common.sh — shared helpers for ql810wc-print-proxy.
#
# Sourced (never executed) by install.sh, uninstall.sh, selftest.sh and
# scripts/NN-*.sh. Requires bash >= 4.2 (Fedora ships bash 5).
#
# Conventions
#   * Every mutating helper is idempotent: it checks current state first and
#     only changes what differs, so every script is safe to re-run.
#   * Everything the installer creates or modifies is recorded in a manifest
#     ($QLP_STATE_DIR/manifest) so uninstall.sh can reverse exactly that, and
#     nothing else (Cockpit/9090, CUPS/631 and unrelated config stay untouched).
#   * Site-specific values come only from config.env (see config.sample.env);
#     hardware identity is detected at runtime.
#   * Secrets (SNMPv3 passphrases) never appear on a command line: helpers pass
#     them through stdin or the environment, never argv (visible in ps).

# shellcheck disable=SC2034  # QLP_CHANGED, QL_USB_* and the QLP_* names are read by callers
if [[ -n "${QLP_COMMON_LOADED:-}" ]]; then return 0; fi
QLP_COMMON_LOADED=1

# ---------------------------------------------------------------------------
# Paths (one place to change them)
# ---------------------------------------------------------------------------
QLP_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
QLP_NAME="ql810wc-print-proxy"
QLP_TAG="ql810wc-print-proxy"                  # marker for managed blocks
QLP_ETC_DIR="/etc/ql810wc-proxy"               # deployed runtime config
QLP_LIB_DIR="/usr/local/libexec/ql810wc-proxy" # deployed code
QLP_STATE_DIR="/var/lib/ql810wc-proxy"         # manifest + backups
QLP_RUN_DIR="/run/ql810wc-proxy"               # live printer status cache
QLP_LOG_DIR="/var/log/ql810wc-proxy"
QLP_MANIFEST="$QLP_STATE_DIR/manifest"
QLP_BACKUP_DIR="$QLP_STATE_DIR/backups"
export QLP_REPO_ROOT QLP_NAME QLP_TAG QLP_ETC_DIR QLP_LIB_DIR QLP_STATE_DIR \
       QLP_RUN_DIR QLP_LOG_DIR QLP_MANIFEST QLP_BACKUP_DIR
# Held by setup.sh, install.sh and uninstall.sh while they change the box
# (qlp_lock). setup.sh names the same file in its one-line mode.
QLP_LOCK_FILE="/run/ql810wc-proxy.lock"

# Names fixed by the architecture contract (docs/architecture.md §3).
QLP_USER="ql810wc"                              # daemon user; also the AgentX socket group
QLP_SYSTEMD_DIR="/etc/systemd/system"
QLP_RUNTIME_ENV="$QLP_ETC_DIR/runtime.env"      # non-secret settings for the daemons
QLP_IDENTITY_ENV="$QLP_ETC_DIR/identity.env"    # PRINTER_UUID (generated once)
QLP_OPT_DIR="/opt/ql810wc-proxy"
QLP_VENV_DIR="$QLP_OPT_DIR/venv"                # optional brother_ql_next (validation prints)
QLP_UNIT_SOCKET="ql810wc-proxyd.socket"
QLP_UNIT_PROXYD="ql810wc-proxyd.service"
QLP_UNIT_AGENT="ql810wc-snmp-agent.service"
QLP_SNMP_PERSIST="/var/lib/net-snmp/snmpd.conf" # net-snmp persistent data (USM users)
# Files one install step writes and other scripts (selftest.sh, uninstall.sh,
# the other steps) look at: named once, here.
QLP_SNMPD_DROPIN_DIR="/etc/snmp/snmpd.conf.d"                         # step 30
QLP_SNMPD_DROPIN="$QLP_SNMPD_DROPIN_DIR/ql810wc-proxy.conf"            # step 30
QLP_AVAHI_SERVICE="/etc/avahi/services/ql810wc-proxy.service"         # step 40
QLP_IPP_USB_QUIRK="/etc/ipp-usb/quirks/ql810wc-proxy.conf"             # step 20
QLP_CUPS_USB_QUIRK_DIR="/usr/share/cups/usb"                          # step 20
QLP_CUPS_USB_QUIRK="$QLP_CUPS_USB_QUIRK_DIR/ql810wc-proxy.usb-quirks"  # step 20
# The daemons run as "$QLP_PYTHON -I -B <script>": /usr/local/libexec is usr_t
# under SELinux, so scripts there are never executed directly.
QLP_PYTHON="/usr/bin/python3"

# Brother Industries USB vendor id (public constant, not site-specific).
QLP_BROTHER_VID="04f9"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  _QLP_RED=$'\e[31m' _QLP_YEL=$'\e[33m' _QLP_GRN=$'\e[32m' _QLP_BLU=$'\e[34m' _QLP_OFF=$'\e[0m'
else
  _QLP_RED='' _QLP_YEL='' _QLP_GRN='' _QLP_BLU='' _QLP_OFF=''
fi

_qlp_ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

_qlp_emit() { # level color message...
  local level="$1" color="$2"; shift 2
  local step="${QLP_STEP:-$(basename "${0:-ql810wc}")}"
  printf '%s %s%-5s%s [%s] %s\n' "$(_qlp_ts)" "$color" "$level" "$_QLP_OFF" "$step" "$*" >&2
  if [[ -n "${QLP_LOG_FILE:-}" ]]; then
    printf '%s %-5s [%s] %s\n' "$(_qlp_ts)" "$level" "$step" "$*" >>"$QLP_LOG_FILE" 2>/dev/null || true
  fi
}
log()   { _qlp_emit INFO  "$_QLP_BLU" "$@"; }
ok()    { _qlp_emit OK    "$_QLP_GRN" "$@"; }
warn()  { _qlp_emit WARN  "$_QLP_YEL" "$@"; }
error() { _qlp_emit ERROR "$_QLP_RED" "$@"; }
die()   { error "$@"; exit 1; }

# Ask the human to do something physical (we can't), then stop with a clear code.
needs_human() { # message...
  _qlp_emit HUMAN "$_QLP_YEL" "$@"
  exit 3
}

# Tell the human about something physical without stopping (the install goes on).
qlp_human() { _qlp_emit HUMAN "$_QLP_YEL" "$@"; }

# Standard prologue for every script: strict mode + an ERR trap that names
# the failing command and line.
qlp_init() { # step-name
  set -Eeuo pipefail
  # Parse tool output in a fixed locale: the box may run a non-English locale
  # (seen: non-English dates from lpstat), which breaks every grep/awk below.
  export LC_ALL=C.UTF-8 LANG=C.UTF-8
  QLP_STEP="${1:-$(basename "$0")}"
  trap 'error "failed (exit $?) at ${BASH_SOURCE[0]##*/}:${LINENO}: ${BASH_COMMAND}"' ERR
}

# Run a command, logging it first.
run() { log "+ $*"; "$@"; }

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
require_root() { [[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root (use sudo)"; }
have() { command -v "$1" >/dev/null 2>&1; }
require_cmd() { local c; for c in "$@"; do have "$c" || die "required command not found: $c"; done; }

# One run at a time: setup.sh, install.sh and uninstall.sh hold an exclusive
# flock on QLP_LOCK_FILE, through file descriptor 9, while they change the
# box. A run that one of them starts (setup.sh runs install.sh) inherits
# descriptor 9, and with it the lock; QLP_LOCK_HELD=1 tells it so. Call after
# require_root.
qlp_lock() {
  if [[ "${QLP_LOCK_HELD:-}" == 1 ]] && { : >&9; } 2>/dev/null \
     && { [[ ! -d /proc/self/fd ]] || [[ /proc/self/fd/9 -ef "$QLP_LOCK_FILE" ]]; }; then
    flock -n 9 || die "could not take over the lock on $QLP_LOCK_FILE from the run that started this one"
    return 0
  fi
  have flock || die "flock is missing (package util-linux-core), so this run cannot make sure that no other" \
                    "setup.sh, install.sh or uninstall.sh changes the box at the same time: sudo dnf install util-linux-core"
  if ! { exec 9>>"$QLP_LOCK_FILE"; } 2>/dev/null; then
    die "cannot open the lock file $QLP_LOCK_FILE (run as root)"
  fi
  if ! flock -n 9; then
    die "another setup.sh, install.sh or uninstall.sh is changing this box right now (it holds" \
        "$QLP_LOCK_FILE). Let it finish, then run this again."
  fi
  QLP_LOCK_HELD=1
  export QLP_LOCK_HELD
}

# "unknown" when /etc/os-release is missing (the preflight reports it, never aborts).
# shellcheck source=/dev/null
qlp_os_id()      { (. /etc/os-release 2>/dev/null || true; printf '%s' "${ID:-unknown}"); }
# shellcheck source=/dev/null
qlp_os_version() { (. /etc/os-release 2>/dev/null || true; printf '%s' "${VERSION_ID:-unknown}"); }

# ---------------------------------------------------------------------------
# Configuration — a SAFE parser: KEY="value" lines only, never sourced/eval'd.
# ---------------------------------------------------------------------------
QLP_CONFIG_TEMPLATE="$QLP_REPO_ROOT/config.sample.env"

qlp_config_path() {
  if [[ -n "${QL_PROXY_CONFIG:-}" ]]; then printf '%s' "$QL_PROXY_CONFIG"
  elif [[ -f "$QLP_REPO_ROOT/config.env" ]]; then printf '%s' "$QLP_REPO_ROOT/config.env"
  elif [[ -f "$QLP_ETC_DIR/config.env" ]]; then printf '%s' "$QLP_ETC_DIR/config.env"
  fi
}

# Parse one env file into shell variables (exported). Accepts:
#   KEY="value"   KEY='value'   KEY=value   (+ optional trailing # comment)
# Rejects command substitution and anything that is not KEY=VALUE.
qlp_parse_env_file() { # file [known-keys-regex]
  local file="$1" known="${2:-}" line key val n=0
  local re_kv='^[[:space:]]*([A-Z][A-Z0-9_]*)=(.*)$'
  local re_dq='^"([^"]*)"[[:space:]]*(#.*)?$'
  local re_sq="^'([^']*)'[[:space:]]*(#.*)?$"
  [[ -r "$file" ]] || die "config not readable: $file"
  while IFS= read -r line || [[ -n "$line" ]]; do
    n=$((n + 1))
    [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue
    [[ "$line" =~ $re_kv ]] || die "$file:$n: expected KEY=\"value\""
    key="${BASH_REMATCH[1]}" val="${BASH_REMATCH[2]}"
    if   [[ "$val" =~ $re_dq ]]; then val="${BASH_REMATCH[1]}"
    elif [[ "$val" =~ $re_sq ]]; then val="${BASH_REMATCH[1]}"
    else
      val="${val%%#*}"                      # strip comment
      val="${val%"${val##*[![:space:]]}"}"  # rtrim
    fi
    # shellcheck disable=SC2016  # literal match on $( and backtick
    case "$val" in *'$('*|*'`'*) die "$file:$n: command substitution is not allowed in config";; esac
    if [[ -n "$known" && ! "$key" =~ ^($known)$ ]]; then
      warn "$file:$n: unknown setting '$key' (typo?) — ignored"
      continue
    fi
    printf -v "$key" '%s' "$val"
    export "${key?}"
  done <"$file"
}

# Load template defaults, then overlay the site config.
qlp_load_config() {
  local keys cfg
  keys="$(grep -oE '^[A-Z][A-Z0-9_]*=' "$QLP_CONFIG_TEMPLATE" | tr -d '=' | paste -sd '|' -)"
  qlp_parse_env_file "$QLP_CONFIG_TEMPLATE"
  cfg="$(qlp_config_path)"
  if [[ -n "$cfg" ]]; then
    qlp_check_secret_perms "$cfg"
    qlp_parse_env_file "$cfg" "$keys"
    QLP_CONFIG_FILE="$cfg"
    log "config: $cfg"
  else
    QLP_CONFIG_FILE=""
    warn "no config.env found — using template defaults (placeholders). Create it with: sudo ./setup.sh --config-only"
  fi
  export QLP_CONFIG_FILE
}

# Which config.env an install used. install.sh records its real path, or
# "none" for the template defaults, in $QLP_STATE_DIR/config-path; uninstall.sh
# removes the record. setup.sh and install.sh read it before they would apply
# the template defaults to a box that is installed already.
qlp_record_config() {
  local path=none
  if [[ -n "${QLP_CONFIG_FILE:-}" ]]; then
    path="$(readlink -f -- "$QLP_CONFIG_FILE" 2>/dev/null || printf '%s' "$QLP_CONFIG_FILE")"
  fi
  install -d -m 0750 "$QLP_STATE_DIR"
  printf '%s\n' "$path" >"$QLP_STATE_DIR/config-path"
}

# The config.env the last install used ("none": the template defaults), or
# nothing when that is not known. An install made before the record existed
# left only the "config: <path>" line in its install log.
qlp_recorded_config() {
  local line="" i
  local -a logs=()
  if [[ -r "$QLP_STATE_DIR/config-path" ]]; then
    IFS= read -r line <"$QLP_STATE_DIR/config-path" || true
    printf '%s' "$line"
    return 0
  fi
  logs=("$QLP_LOG_DIR"/install-*.log)
  for ((i = ${#logs[@]} - 1; i >= 0; i--)); do
    [[ -f "${logs[i]}" && -r "${logs[i]}" ]] || continue
    line="$(sed -n '/^[^ ]* INFO  \[install\] config: /{s///p;q;}' "${logs[i]}" 2>/dev/null || true)"
    if [[ -n "$line" ]]; then printf '%s' "$line"; return 0; fi
  done
}

# True when a run would apply the template defaults to a box that is
# installed with settings of its own: runtime.env exists, CFG (the config.env
# the run would read) does not, and the install did not run on the template
# defaults itself. Then qlp_settings_hint says what to do.
qlp_settings_would_be_lost() { # cfg
  [[ ! -e "$1" && -e "$QLP_RUNTIME_ENV" ]] || return 1
  [[ "$(qlp_recorded_config)" != none ]]
}

# HUMAN lines: how to keep the settings an installed box uses, as a copy at
# TARGET, or by running SCRIPT (setup.sh, install.sh) where they are.
qlp_settings_hint() { # target script
  local target="$1" script="$2" used dir
  used="$(qlp_recorded_config)"
  if [[ -n "$used" && -e "$used" ]]; then
    qlp_human "this box is already installed with the settings in $used, but $target does not exist:" \
              "going on would replace those settings with the template defaults (the printer name, the" \
              "client ranges, the SNMPv3 user and every other setting)."
    qlp_human "To keep them, copy them here and run $script again: sudo install -m 600 -o root -g root $used $target"
    dir="$(dirname -- "$used")"
    if [[ -f "$dir/$script" ]]; then
      qlp_human "or run $script in the checkout you installed from: sudo $dir/$script"
    else
      qlp_human "or name the file: sudo env QL_PROXY_CONFIG=$used $QLP_REPO_ROOT/$script"
    fi
  else
    if [[ -n "$used" ]]; then used="$used, which is gone"; else used="which config.env is not recorded"; fi
    qlp_human "this box is already installed with the settings of another config.env ($used), and" \
              "$target does not exist: going on would replace those settings with the template defaults (the" \
              "printer name, the client ranges, the SNMPv3 user and every other setting)."
    qlp_human "To keep them, copy that config.env to $target (as root, mode 0600), or run $script in the" \
              "checkout you installed from."
  fi
}

# config.env holds SNMPv3 passphrases: it must not be group/world readable.
qlp_check_secret_perms() { # file
  local mode
  mode="$(stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1")"
  if [[ "${mode: -2}" != "00" ]]; then
    warn "$1 is mode $mode; it contains secrets — run: chmod 600 '$1'"
  fi
}

# Fail if a setting still holds a template placeholder (only where it matters).
qlp_require_real() { # VAR...
  local v
  for v in "$@"; do
    [[ -n "${!v:-}" ]] || die "config: $v is empty — set it in config.env"
    [[ "${!v}" != *CHANGE-ME* ]] || die "config: $v still has the template placeholder — set it in config.env"
  done
}

qlp_is_yes() { [[ "${1,,}" =~ ^(1|y|yes|true|on)$ ]]; }

# Die unless each VAR holds a yes/no style value (empty counts as "no").
qlp_require_bool() { # VAR...
  local v val
  for v in "$@"; do
    val="${!v:-}"
    # "(...)?" rather than an empty alternative "(|...)": BSD regcomp rejects those.
    [[ "${val,,}" =~ ^(1|0|y|n|yes|no|true|false|on|off)?$ ]] \
      || die "config: $v='$val' is not a yes/no value"
  done
}

# Die unless VAR equals one of the given words.
qlp_require_enum() { # VAR word...
  local v="$1" w
  shift
  for w in "$@"; do
    if [[ "${!v:-}" == "$w" ]]; then return 0; fi
  done
  die "config: $v='${!v:-}' must be one of: $*"
}

# Die unless VAR is a whole number within [min, max]. At most 18 digits are
# read, so the shell's 64-bit arithmetic never overflows.
qlp_require_int() { # VAR min max
  local v="$1" val="${!1:-}"
  if [[ "$val" =~ ^[0-9]{1,18}$ ]] && ((10#$val >= $2 && 10#$val <= $3)); then return 0; fi
  die "config: $v='$val' must be a whole number from $2 to $3"
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
qlp_valid_port() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535)); }

qlp_valid_cidr() { # IPv4 or IPv6, with or without /prefix
  local c="$1"
  python3 -c 'import ipaddress,sys; ipaddress.ip_network(sys.argv[1], strict=False)' "$c" 2>/dev/null
}

qlp_cidr_family() { # prints ipv4|ipv6
  python3 -c 'import ipaddress,sys; print("ipv%d" % ipaddress.ip_network(sys.argv[1], strict=False).version)' "$1"
}

# Validate, normalise and de-duplicate an address/CIDR list. Prints one
# "family<TAB>network" line per entry (family ipv4|ipv6; host bits cleared, as
# snmpd's com2sec requires). Warns when host bits had to be cleared; dies on
# an invalid entry. Call as  var="$(qlp_norm_cidrs LABEL "${list[@]}")".
_QLP_PY_CIDRS='
import ipaddress, sys
label, seen = sys.argv[1], set()
for raw in sys.argv[2:]:
    try:
        net = ipaddress.ip_network(raw, strict=False)
    except ValueError as exc:
        print("E\t%s: %r is not an IPv4/IPv6 address or CIDR (%s)" % (label, raw, exc))
        sys.exit(1)
    try:
        ipaddress.ip_network(raw, strict=True)
    except ValueError:
        print("W\t%s: %s has host bits set; using %s" % (label, raw, net))
    if str(net) not in seen:
        seen.add(str(net))
        print("C\tipv%d\t%s" % (net.version, net))
'
qlp_norm_cidrs() { # label [cidr...]
  local label="$1" out kind a b rc=0
  shift
  if (($# == 0)); then return 0; fi
  out="$(python3 -c "$_QLP_PY_CIDRS" "$label" "$@")" || rc=$?
  while IFS=$'\t' read -r kind a b; do
    case "$kind" in
      C) printf '%s\t%s\n' "$a" "$b" ;;
      W) warn "$a" ;;
      E) error "$a" ;;
    esac
  done <<<"$out"
  if ((rc != 0)); then die "$label: invalid address list"; fi
}

# Is the network a loopback range (127.0.0.0/8, ::1)? Loopback is never filtered.
qlp_cidr_is_loopback() { # cidr
  python3 -c 'import ipaddress,sys; sys.exit(0 if ipaddress.ip_network(sys.argv[1], strict=False).is_loopback else 1)' "$1"
}

# Which entries of an allowed-sources list (SNMP_PRINTER_ALLOWED_CIDRS, ...)
# real network clients can come from. Loopback never reaches the firewall, and
# the documentation-only ranges (RFC 5737, RFC 3849) that config.sample.env
# uses as placeholders hold no real hosts, so neither counts. Sets
#   QLP_SRC_USABLE       every other entry (normalised, space-separated)
#   QLP_SRC_PLACEHOLDER  the entries inside documentation ranges
#   QLP_SRC_SUBNETS      this host's own subnets ("ip -o addr"; empty = unknown)
#   QLP_SRC_LOCAL        the usable entries that overlap one of those subnets
# Entries that are no address at all are skipped (the install steps refuse
# them). Returns 1 when python3 fails.
_QLP_PY_SOURCES='
import ipaddress, sys
DOCS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]
subnets = []
for line in sys.stdin:
    fields = line.split()
    if len(fields) > 3 and fields[2] in ("inet", "inet6"):
        try:
            net = ipaddress.ip_interface(fields[3]).network
        except ValueError:
            continue
        if not net.is_loopback and net not in subnets:
            subnets.append(net)
def inside(net, others):
    return any(net.version == o.version and net.subnet_of(o) for o in others)
def overlaps(net, others):
    return any(net.version == o.version and net.overlaps(o) for o in others)
usable, placeholder, local = [], [], []
for raw in sys.argv[1:]:
    try:
        net = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        continue
    if net.is_loopback:
        continue
    if inside(net, DOCS):
        placeholder.append(str(net))
        continue
    usable.append(str(net))
    if overlaps(net, subnets):
        local.append(str(net))
for key, nets in (("usable", usable), ("placeholder", placeholder), ("local", local), ("subnets", subnets)):
    print("%s\t%s" % (key, " ".join(str(n) for n in nets)))
'
qlp_classify_sources() { # [cidr...]
  local out key value
  QLP_SRC_USABLE="" QLP_SRC_PLACEHOLDER="" QLP_SRC_LOCAL="" QLP_SRC_SUBNETS=""
  out="$({ ip -o addr show 2>/dev/null || true; } | python3 -c "$_QLP_PY_SOURCES" "$@")" || return 1
  while IFS=$'\t' read -r key value; do
    case "$key" in
      usable)      QLP_SRC_USABLE="$value" ;;
      placeholder) QLP_SRC_PLACEHOLDER="$value" ;;
      local)       QLP_SRC_LOCAL="$value" ;;
      subnets)     QLP_SRC_SUBNETS="$value" ;;
    esac
  done <<<"$out"
}

# Why the list SETTING holds no network client at all (nothing when it does);
# run qlp_classify_sources on the same entries first.
qlp_sources_problem() { # setting [cidr...]
  local setting="$1"
  shift
  if [[ -n "$QLP_SRC_USABLE" ]]; then return 0; fi
  if (($# == 0)); then
    printf '%s is empty' "$setting"
  elif [[ -n "$QLP_SRC_PLACEHOLDER" ]]; then
    printf '%s holds only documentation-only example ranges (%s, RFC 5737/3849), which no real client uses' \
      "$setting" "$QLP_SRC_PLACEHOLDER"
  else
    printf '%s holds no address a network client can come from (%s)' "$setting" "$*"
  fi
}

# The setting whose ranges UDP 5353 (Bonjour) is opened for: MDNS_ALLOWED_CIDRS,
# or SNMP_PRINTER_ALLOWED_CIDRS while that is empty.
qlp_mdns_source_setting() {
  if [[ -n "${MDNS_ALLOWED_CIDRS//[[:space:]]/}" ]]; then printf 'MDNS_ALLOWED_CIDRS'
  else printf 'SNMP_PRINTER_ALLOWED_CIDRS'; fi
}

# Address of the interface holding the default route (IPv4).
qlp_default_ipv4() {
  ip -4 route get 192.0.2.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}'
}

qlp_proxy_ip() { if [[ -n "${PROXY_IP:-}" ]]; then printf '%s' "$PROXY_IP"; else qlp_default_ipv4; fi; }

# Short host name (no domain) — the default Bonjour name suffix.
qlp_short_hostname() { local n; n="$(uname -n)"; printf '%s' "${n%%.*}"; }

# Truncate a string to at most MAX bytes of UTF-8 without splitting a character.
qlp_utf8_trunc() { # max-bytes string
  python3 -c '
import sys
data = sys.argv[2].encode("utf-8", "surrogateescape")[: int(sys.argv[1])]
sys.stdout.write(data.decode("utf-8", "ignore"))' "$1" "$2"
}

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
# Render TEMPLATE, replacing each @KEY@ with the VALUE given as KEY=VALUE.
# Values are inserted literally (XML-escaped with --xml). Fails, naming them,
# if the template holds placeholders that were given no value. Values travel
# through the environment, not argv. Use as: text="$(qlp_render ...)".
_QLP_PY_RENDER='
import os, re, sys
from xml.sax.saxutils import escape
mode, path = sys.argv[1], sys.argv[2]
vals = {k[5:]: v for k, v in os.environ.items() if k.startswith("QLPV_")}
with open(path, encoding="utf-8") as fh:
    text = fh.read()
pat = re.compile(r"@([A-Z][A-Z0-9_]*)@")
missing = sorted({m for m in pat.findall(text) if m not in vals})
if missing:
    sys.exit("render: %s: no value for %s" % (path, ", ".join("@%s@" % m for m in missing)))
def sub(match):
    value = vals[match.group(1)]
    return escape(value, {"\"": "&quot;", "\x27": "&apos;"}) if mode == "xml" else value
sys.stdout.write(pat.sub(sub, text))
'
qlp_render() { # [--xml] template KEY=VALUE...
  local mode=plain tpl kv
  if [[ "${1:-}" == --xml ]]; then mode=xml; shift; fi
  tpl="$1"; shift
  [[ -r "$tpl" ]] || die "template not readable: $tpl"
  (
    for kv in "$@"; do
      [[ "$kv" == *=* && "${kv%%=*}" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "qlp_render: bad argument '${kv%%=*}'"
      export "QLPV_${kv%%=*}=${kv#*=}"
    done
    exec python3 -c "$_QLP_PY_RENDER" "$mode" "$tpl"
  )
}

# Render a systemd unit shipped in agent/systemd/. In every unit, @RAW_PORT@
# becomes RAW_PORT and @RAW_BIND@ becomes RAW_BIND: plain text substitution
# (the .service units mention them in comments at most). Only a .socket unit
# listens, so only there does a set RAW_BIND also turn each
# "ListenStream=@RAW_PORT@" line into ADDRESS:PORT ([ADDRESS]:PORT for IPv6)
# and add FreeBind=yes, so the socket can bind before the address is
# configured at boot. Any other @PLACEHOLDER@ fails loudly instead of
# producing a broken unit.
_QLP_PY_UNIT='
import os, re, sys
path = sys.argv[1]
port, bind = os.environ["RAW_PORT"], os.environ.get("RAW_BIND", "")
with open(path, encoding="utf-8") as fh:
    text = fh.read()
if path.endswith(".socket"):
    if re.search(r"(?m)^[ \t]*ListenStream[ \t]*=.*@RAW_BIND@", text):
        sys.exit("%s: write the listen line as \"ListenStream=@RAW_PORT@\" (the address comes from RAW_BIND)"
                 % path)
    if bind:
        listen = ("[%s]:%s" if ":" in bind else "%s:%s") % (bind, port)
        text, count = re.subn(r"(?m)^([ \t]*ListenStream[ \t]*=[ \t]*)@RAW_PORT@[ \t]*$",
                              lambda m: m.group(1) + listen, text)
        if not count:
            sys.exit("%s: RAW_BIND is set but the socket has no \"ListenStream=@RAW_PORT@\" line" % path)
        if not re.search(r"(?mi)^[ \t]*FreeBind[ \t]*=", text):
            text = re.sub(r"(?m)^([ \t]*ListenStream[ \t]*=.*)$", lambda m: m.group(1) + "\nFreeBind=yes",
                          text, count=1)
text = text.replace("@RAW_PORT@", port).replace("@RAW_BIND@", bind)
left = sorted(set(re.findall(r"@[A-Z][A-Z0-9_]*@", text)))
if left:
    sys.exit("%s: unknown placeholder(s) %s" % (path, ", ".join(left)))
sys.stdout.write(text)
'
qlp_render_unit() { # template   (uses RAW_PORT / RAW_BIND from the config)
  [[ -r "$1" ]] || die "unit template missing: $1 (incomplete checkout?)"
  RAW_PORT="${RAW_PORT:-9100}" RAW_BIND="${RAW_BIND:-}" python3 -c "$_QLP_PY_UNIT" "$1"
}

# ---------------------------------------------------------------------------
# Install manifest (drives uninstall.sh)
#   created  <path>                  file/dir we created  -> remove
#   modified <path> <backup>         file we replaced     -> restore backup
#   block    <path> <backup>         managed block added  -> remove the block only
#   tree     <path>                  directory tree we created (venv) -> rm -rf
#   attrs    <path> <uid:gid> <mode> directory we re-grouped -> its old owner/mode
#   fw-port  <zone> <port/proto>     firewalld port       -> remove
#   fw-rich  <zone> <rule>           firewalld rich rule  -> remove
#   fcontext <type> <regex>          semanage fcontext    -> delete
#   seport   <type> <proto> <port>   semanage port        -> delete
#   semodule <name>                  SELinux module       -> semodule -r
#   unit     <name>                  systemd unit enabled -> disable
#   cupsq    <queue>                 CUPS queue created   -> lpadmin -x
#   user     <name>                  system user created  -> userdel
#   usmuser  <name>                  SNMPv3 USM user      -> qlp_usm_remove_user (snmpd stopped)
#   pkg      <name>                  package we installed -> (left installed; listed)
# ---------------------------------------------------------------------------
qlp_manifest_has() { [[ -f "$QLP_MANIFEST" ]] && grep -qxF -- "$1" "$QLP_MANIFEST"; }

qlp_manifest_add() { # tab-joined record fields...
  local rec
  rec="$(printf '%s\t' "$@")"; rec="${rec%$'\t'}"
  install -d -m 0750 "$QLP_STATE_DIR"
  qlp_manifest_has "$rec" || printf '%s\n' "$rec" >>"$QLP_MANIFEST"
}

qlp_manifest_del() { # exact record (tab-joined)
  local rec tmp
  rec="$(printf '%s\t' "$@")"; rec="${rec%$'\t'}"
  [[ -f "$QLP_MANIFEST" ]] || return 0
  tmp="$(mktemp)"
  grep -vxF -- "$rec" "$QLP_MANIFEST" >"$tmp" || true
  cat "$tmp" >"$QLP_MANIFEST"; rm -f "$tmp"
}

# Records of one kind, one per line (tab-separated fields after the kind).
qlp_manifest_records() { # kind
  [[ -f "$QLP_MANIFEST" ]] || return 0
  grep "^$1"$'\t' "$QLP_MANIFEST" | cut -f2- || true
}

# Backup a pre-existing file exactly once (the pristine original).
qlp_backup_once() { # path -> prints backup path
  local path="$1" bak
  bak="$QLP_BACKUP_DIR/$(printf '%s' "$path" | sed 's#^/##; s#/#__#g').orig"
  install -d -m 0750 "$QLP_BACKUP_DIR"
  if [[ ! -e "$bak" ]]; then
    cp -a -- "$path" "$bak"
  fi
  printf '%s' "$bak"
}

# ---------------------------------------------------------------------------
# Idempotent file management
# ---------------------------------------------------------------------------
QLP_CHANGED=0   # set to 1 by helpers when they changed something

# Write stdin to DEST if content/mode/owner differ. Records created/modified.
qlp_write_file() { # dest mode [owner:group]
  local dest="$1" mode="$2" owner="${3:-root:root}" tmp bak
  tmp="$(mktemp)"
  cat >"$tmp"
  QLP_CHANGED=0
  if [[ -e "$dest" ]] && cmp -s "$tmp" "$dest" \
     && [[ "$(stat -c '%a %U:%G' "$dest")" == "${mode#0} $owner" ]]; then
    rm -f "$tmp"; return 0
  fi
  if [[ -e "$dest" ]]; then
    if ! qlp_manifest_has "$(printf 'created\t%s' "$dest")"; then
      bak="$(qlp_backup_once "$dest")"
      qlp_manifest_add modified "$dest" "$bak"
    fi
  else
    install -d -m 0755 "$(dirname "$dest")"
    qlp_manifest_add created "$dest"
  fi
  install -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
  rm -f "$tmp"
  qlp_restorecon "$dest"
  QLP_CHANGED=1
  log "wrote $dest"
}

# Copy a repo file to DEST (same semantics as qlp_write_file).
qlp_install_file() { # src dest mode [owner:group]
  qlp_write_file "$2" "$3" "${4:-root:root}" <"$1"
}

qlp_ensure_dir() { # dir mode [owner:group]
  local dir="$1" mode="$2" owner="${3:-root:root}"
  if [[ ! -d "$dir" ]]; then
    install -d -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$dir"
    qlp_manifest_add created "$dir"
    qlp_restorecon "$dir"
    log "created $dir"
  fi
}

# Remove a directory this installer created (manifest "created") once it is
# empty; one that still holds files stays, and so does its record.
qlp_remove_created_dir() { # dir
  qlp_manifest_has "$(printf 'created\t%s' "$1")" || return 0
  if [[ -d "$1" ]] && rmdir -- "$1" 2>/dev/null; then log "removed $1"; fi
  if [[ ! -d "$1" ]]; then qlp_manifest_del created "$1"; fi
}

# Owner, group and permission bits of PATH as "<uid> <gid> <mode> <group>"
# (mode in octal as stat prints it, e.g. 750). Returns 1 when PATH does not
# exist. GNU stat, else BSD stat.
qlp_path_attrs() { # path
  [[ -e "$1" ]] || return 1
  stat -c '%u %g %a %G' -- "$1" 2>/dev/null || stat -f '%u %g %Lp %Sg' -- "$1"
}

# Remove a file this installer created (manifest "created"). A file we did not
# create is never removed (we only say so). Sets QLP_CHANGED.
qlp_remove_created() { # path
  local path="$1"
  QLP_CHANGED=0
  if qlp_manifest_has "$(printf 'created\t%s' "$path")"; then
    if [[ -e "$path" || -L "$path" ]]; then
      rm -f -- "$path"
      QLP_CHANGED=1
      log "removed $path"
    fi
    qlp_manifest_del created "$path"
  elif [[ -e "$path" ]]; then
    warn "$path was not created by this installer — left in place"
  fi
}

# Insert or replace a marked block in a shared file we do not own.
qlp_ensure_block() { # file  (block body on stdin)
  local file="$1" body tmp bak
  body="$(cat)"
  local begin="# BEGIN $QLP_TAG (managed — do not edit inside this block)"
  local end="# END $QLP_TAG"
  if [[ -f "$file" ]]; then
    local nb ne
    nb="$(grep -c "^# BEGIN $QLP_TAG" "$file" || true)"
    ne="$(grep -cx "$end" "$file" || true)"
    [[ "$nb" == "$ne" && "$nb" -le 1 ]] \
      || die "$file: unbalanced/duplicate '$QLP_TAG' managed markers (BEGIN=$nb END=$ne) — fix by hand"
  fi
  tmp="$(mktemp)"
  if [[ -f "$file" ]]; then
    awk -v b="# BEGIN $QLP_TAG" -v e="$end" '
      index($0, b) == 1 { skip = 1; next }
      skip && $0 == e   { skip = 0; next }
      !skip' "$file" >"$tmp"
  fi
  printf '%s\n%s\n%s\n' "$begin" "$body" "$end" >>"$tmp"
  QLP_CHANGED=0
  if [[ -f "$file" ]] && cmp -s "$tmp" "$file"; then rm -f "$tmp"; return 0; fi
  if [[ -f "$file" ]]; then
    bak="$(qlp_backup_once "$file")"   # safety copy; uninstall removes just the block
    qlp_manifest_has "$(printf 'created\t%s' "$file")" || qlp_manifest_add block "$file" "$bak"
    cat "$tmp" >"$file"   # keep inode, mode, owner and SELinux label
  else
    install -m 0644 "$tmp" "$file"
    qlp_manifest_add created "$file"
    qlp_restorecon "$file"
  fi
  rm -f "$tmp"
  QLP_CHANGED=1
  log "updated managed block in $file"
}

qlp_remove_block() { # file
  local file="$1" tmp
  [[ -f "$file" ]] || return 0
  grep -qF "# BEGIN $QLP_TAG" "$file" || return 0
  tmp="$(mktemp)"
  awk -v b="# BEGIN $QLP_TAG" -v e="# END $QLP_TAG" '
    index($0, b) == 1 { skip = 1; next }
    skip && $0 == e   { skip = 0; next }
    !skip' "$file" >"$tmp"
  cat "$tmp" >"$file"; rm -f "$tmp"
  log "removed managed block from $file"
}

# The lines inside our managed block of FILE (nothing if it has none).
qlp_block_body() { # file
  [[ -f "$1" ]] || return 0
  awk -v b="# BEGIN $QLP_TAG" -v e="# END $QLP_TAG" '
    index($0, b) == 1 { inside = 1; next }
    inside && $0 == e { inside = 0; next }
    inside' "$1"
}

# Why PATH must not be removed as a directory tree this installer created
# (manifest "tree"); prints nothing when it may be. It has to be a real
# directory (not a symlink) strictly inside QLP_OPT_DIR or QLP_STATE_DIR, both
# compared after canonicalisation, so no symlinked component can redirect an
# "rm -rf" to somewhere else.
qlp_tree_refusal() { # path
  local path="$1" real root rroot
  if [[ "$path" != /* ]]; then printf 'not an absolute path'; return 0; fi
  case "$path/" in */../*|*/./*) printf 'has "." or ".." components'; return 0 ;; esac
  if [[ -L "$path" ]]; then printf 'a symbolic link'; return 0; fi
  if [[ ! -d "$path" ]]; then printf 'not a directory'; return 0; fi
  real="$(readlink -f -- "$path" 2>/dev/null || true)"
  for root in "$QLP_OPT_DIR" "$QLP_STATE_DIR"; do
    rroot="$(readlink -f -- "$root" 2>/dev/null || true)"
    if [[ -n "$real" && -n "$rroot" && "$rroot" != / && "$real" == "$rroot"/?* ]]; then return 0; fi
  done
  printf 'outside %s and %s' "$QLP_OPT_DIR" "$QLP_STATE_DIR"
}

# Remove a directory tree this installer created, never one qlp_tree_refusal
# objects to (returns 1 and warns instead). A tree that is already gone is fine.
qlp_remove_tree() { # path
  local path="$1" why
  local -a opts=(-rf)
  if [[ ! -e "$path" && ! -L "$path" ]]; then return 0; fi
  why="$(qlp_tree_refusal "$path")"
  if [[ -n "$why" ]]; then
    warn "refusing to remove $path ($why)"
    return 1
  fi
  # GNU rm: never cross into another file system mounted inside the tree.
  if rm --version >/dev/null 2>&1; then opts+=(--one-file-system); fi
  run rm "${opts[@]}" -- "$(readlink -f -- "$path")"
}

# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------
qlp_pkg_installed() { rpm -q --quiet "$1" 2>/dev/null || rpm -q --quiet --whatprovides "$1" 2>/dev/null; }

# Install the missing packages only. Weak dependencies are skipped: the box is
# a small production server and nothing here needs them.
qlp_ensure_packages() { # pkg...
  local p missing=()
  for p in "$@"; do qlp_pkg_installed "$p" || missing+=("$p"); done
  if ((${#missing[@]} == 0)); then log "packages present: $*"; return 0; fi
  run dnf -y --setopt=install_weak_deps=False install "${missing[@]}"
  for p in "${missing[@]}"; do qlp_manifest_add pkg "$p"; done
}

# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------
qlp_daemon_reload() { run systemctl daemon-reload; }

qlp_enable_now() { # unit
  if ! systemctl is-enabled --quiet "$1" 2>/dev/null; then
    run systemctl enable "$1"
    qlp_manifest_add unit "$1"
  fi
  systemctl is-active --quiet "$1" || run systemctl start "$1"
}

# Restart the given units that are currently active; leave the others alone.
qlp_try_restart() { # unit...
  local u
  for u in "$@"; do
    if systemctl is-active --quiet "$u" 2>/dev/null; then run systemctl restart "$u"; fi
  done
}

# Reload the given units that are currently active; leave the others alone.
# For ql810wc-snmp-agent (Type=notify-reload) "systemctl reload" sends SIGHUP
# and returns once the agent has applied runtime.env, identity.env and
# oid-map.yaml in its running AgentX session, so SNMP never pauses. A unit that
# cannot reload (an older unit file) is restarted instead, and so is one whose
# reload failed outright. "systemctl reload" also succeeds when the agent
# rejected the new files: it then keeps serving the previous ones and its status
# says "last reload failed: ...". That stops the step.
qlp_try_reload() { # unit...
  local u status
  for u in "$@"; do
    systemctl is-active --quiet "$u" 2>/dev/null || continue
    if [[ "$(systemctl show -p CanReload --value "$u" 2>/dev/null || true)" != yes ]]; then
      log "$u cannot reload (its unit file predates reloading) — restarting it instead"
      run systemctl restart "$u"
      continue
    fi
    if ! run systemctl reload "$u"; then
      warn "reloading $u failed — restarting it instead"
      run systemctl restart "$u"
      continue
    fi
    status="$(systemctl show -p StatusText --value "$u" 2>/dev/null | qlp_mask_serials || true)"
    if [[ "$status" == *"last reload failed"* ]]; then
      { journalctl -u "$u" -n 15 --no-pager 2>/dev/null || true; } | qlp_mask_serials >&2
      die "$u rejected the new settings or OID map and keeps serving the previous ones" \
          "(status: $status); journal above"
    fi
  done
}

# Die with the unit's recent journal (serial numbers masked) unless it is active.
qlp_require_active() { # unit
  if ! systemctl is-active --quiet "$1"; then
    { journalctl -u "$1" -n 25 --no-pager 2>/dev/null || true; } | qlp_mask_serials >&2
    die "$1 is not running (journal above; also: systemctl status $1)"
  fi
}

# ---------------------------------------------------------------------------
# CUPS
# ---------------------------------------------------------------------------
# Does the CUPS scheduler answer? "lpstat -r" exits 0 whether it does or not;
# only its message tells: "scheduler is not running" when it cannot reach
# cupsd (in the C locale qlp_init sets). A listening cups.socket starts cupsd
# for the request. Without lpstat, or when lpstat fails or times out: no.
qlp_cups_running() { # [timeout-seconds]
  local out
  have lpstat || return 1
  out="$(timeout "${1:-30}" lpstat -r 2>/dev/null)" || return 1
  [[ "$out" != *"scheduler is not running"* ]]
}

# ---------------------------------------------------------------------------
# firewalld — add/remove ONLY what we own. Never touches cockpit (9090),
# ipp (631) or any other existing rule. Runtime + permanent are applied
# separately so we never need a --reload (which would drop others' runtime rules).
# ---------------------------------------------------------------------------
qlp_fw_active() { systemctl is-active --quiet firewalld 2>/dev/null; }

qlp_fw_zone() { # zone serving PROXY_IP's interface, else the default zone
  local ip ifc zone=""
  ip="$(qlp_proxy_ip || true)"   # no default route: fall back to the default zone
  if [[ -n "$ip" ]]; then
    ifc="$(ip -o -4 addr show 2>/dev/null | awk -v ip="$ip" '{split($4, a, "/"); if (a[1] == ip) { print $2; exit }}' || true)"
    [[ -n "$ifc" ]] && zone="$(firewall-cmd --get-zone-of-interface="$ifc" 2>/dev/null || true)"
  fi
  [[ -n "$zone" ]] || zone="$(firewall-cmd --get-default-zone)"
  printf '%s' "$zone"
}

# Pre-existing ports/rules (opened by the admin before us) are left alone and
# NOT recorded, so uninstall never closes something it did not open.
qlp_fw_ensure_port() { # zone port/proto
  local zone="$1" pp="$2"
  qlp_fw_active || { warn "firewalld not active — skipping port $pp"; return 0; }
  if ! firewall-cmd --permanent --zone="$zone" --query-port="$pp" >/dev/null 2>&1; then
    run firewall-cmd --permanent --zone="$zone" --add-port="$pp" >/dev/null
    qlp_manifest_add fw-port "$zone" "$pp"
  fi
  firewall-cmd --zone="$zone" --query-port="$pp" >/dev/null 2>&1 \
    || run firewall-cmd --zone="$zone" --add-port="$pp" >/dev/null
}

qlp_fw_remove_port() { # zone port/proto
  local zone="$1" pp="$2"
  qlp_fw_active || return 0
  firewall-cmd --permanent --zone="$zone" --query-port="$pp" >/dev/null 2>&1 \
    && run firewall-cmd --permanent --zone="$zone" --remove-port="$pp" >/dev/null
  firewall-cmd --zone="$zone" --query-port="$pp" >/dev/null 2>&1 \
    && run firewall-cmd --zone="$zone" --remove-port="$pp" >/dev/null
  qlp_manifest_del fw-port "$zone" "$pp"
}

qlp_fw_ensure_rich() { # zone rule
  local zone="$1" rule="$2"
  qlp_fw_active || { warn "firewalld not active — skipping rich rule"; return 0; }
  if ! firewall-cmd --permanent --zone="$zone" --query-rich-rule="$rule" >/dev/null 2>&1; then
    run firewall-cmd --permanent --zone="$zone" --add-rich-rule="$rule" >/dev/null
    qlp_manifest_add fw-rich "$zone" "$rule"
  fi
  firewall-cmd --zone="$zone" --query-rich-rule="$rule" >/dev/null 2>&1 \
    || run firewall-cmd --zone="$zone" --add-rich-rule="$rule" >/dev/null
}

qlp_fw_remove_rich() { # zone rule
  local zone="$1" rule="$2"
  qlp_fw_active || return 0
  firewall-cmd --permanent --zone="$zone" --query-rich-rule="$rule" >/dev/null 2>&1 \
    && run firewall-cmd --permanent --zone="$zone" --remove-rich-rule="$rule" >/dev/null
  firewall-cmd --zone="$zone" --query-rich-rule="$rule" >/dev/null 2>&1 \
    && run firewall-cmd --zone="$zone" --remove-rich-rule="$rule" >/dev/null
  qlp_manifest_del fw-rich "$zone" "$rule"
}

# Source-scoped accept for one port, IPv4 or IPv6 per CIDR.
qlp_fw_rich_for() { # cidr port proto
  printf 'rule family="%s" source address="%s" port port="%s" protocol="%s" accept' \
    "$(qlp_cidr_family "$1")" "$1" "$2" "$3"
}

# Same, when the family is already known (see qlp_norm_cidrs).
qlp_fw_rich_rule() { # family cidr port proto
  printf 'rule family="%s" source address="%s" port port="%s" protocol="%s" accept' "$1" "$2" "$3" "$4"
}

# Make the rich rules WE added that contain SELECTOR (a fixed substring such as
# 'port port="161" protocol="udp"') exactly the given set in ZONE: missing ones
# are added; ones we added earlier (in any zone) that are no longer wanted are
# removed. Rules we did not add are never touched.
qlp_fw_sync_rich() { # zone selector [rule...]
  local zone="$1" sel="$2" rule recs z r want
  shift 2
  qlp_fw_active || { warn "firewalld not active — not managing rules for: $sel"; return 0; }
  for rule in "$@"; do qlp_fw_ensure_rich "$zone" "$rule"; done
  recs="$(qlp_manifest_records fw-rich)"
  while IFS=$'\t' read -r z r; do
    [[ -n "$z" && "$r" == *"$sel"* ]] || continue
    want=0
    if [[ "$z" == "$zone" ]]; then
      for rule in "$@"; do
        if [[ "$rule" == "$r" ]]; then want=1; break; fi
      done
    fi
    if ((want == 0)); then qlp_fw_remove_rich "$z" "$r"; fi
  done <<<"$recs"
}

# Same for zone-wide ports of one protocol: open PORT/PROTO for each given port,
# close the PORT/PROTO entries we opened earlier that are no longer wanted.
qlp_fw_sync_ports() { # zone proto [port...]
  local zone="$1" proto="$2" p recs z pp want
  shift 2
  qlp_fw_active || { warn "firewalld not active — not managing $proto ports"; return 0; }
  for p in "$@"; do qlp_fw_ensure_port "$zone" "$p/$proto"; done
  recs="$(qlp_manifest_records fw-port)"
  while IFS=$'\t' read -r z pp; do
    [[ -n "$z" && "$pp" == */"$proto" ]] || continue
    want=0
    if [[ "$z" == "$zone" ]]; then
      for p in "$@"; do
        if [[ "$pp" == "$p/$proto" ]]; then want=1; break; fi
      done
    fi
    if ((want == 0)); then qlp_fw_remove_port "$z" "$pp"; fi
  done <<<"$recs"
}

# ---------------------------------------------------------------------------
# SELinux — stays enforcing; we only add labels/policy we own.
# ---------------------------------------------------------------------------
qlp_selinux_on() { have selinuxenabled && selinuxenabled; }
qlp_selinux_mode() { have getenforce && getenforce || printf 'Disabled'; }
qlp_restorecon() { qlp_selinux_on && restorecon -RF -- "$@" 2>/dev/null || true; }

qlp_ensure_fcontext() { # type regex
  local type="$1" regex="$2"
  qlp_selinux_on || return 0
  require_cmd semanage
  if semanage fcontext -l -C 2>/dev/null | awk -v r="$regex" -v t="$type" \
       '$1 == r && index($0, ":" t ":") { found = 1 } END { exit !found }'; then
    return 0
  fi
  if semanage fcontext -l -C 2>/dev/null | awk -v r="$regex" '$1 == r { f = 1 } END { exit !f }'; then
    run semanage fcontext -m -t "$type" "$regex"
  else
    run semanage fcontext -a -t "$type" "$regex"
  fi
  qlp_manifest_add fcontext "$type" "$regex"
}

# Delete a file-context rule this installer added (and only such a rule).
qlp_remove_fcontext() { # type regex
  local type="$1" regex="$2"
  qlp_manifest_has "$(printf 'fcontext\t%s\t%s' "$type" "$regex")" || return 0
  if qlp_selinux_on && semanage fcontext -l -C 2>/dev/null \
       | awk -v r="$regex" '$1 == r { f = 1 } END { exit !f }'; then
    run semanage fcontext -d "$regex"
  fi
  qlp_manifest_del fcontext "$type" "$regex"
}

# ---------------------------------------------------------------------------
# Service user, identity and runtime settings (shared by steps 20, 30, 40)
# ---------------------------------------------------------------------------
# System user for both daemons: no login, no home, supplementary group lp
# (/dev/usb/lp* is root:lp 0660). Its own group owns the AgentX socket.
QLP_USER_CHANGED=0
qlp_ensure_service_user() {
  local shell
  QLP_USER_CHANGED=0
  getent group lp >/dev/null || die "group 'lp' does not exist (is cups installed? run step 10)"
  if ! getent passwd "$QLP_USER" >/dev/null; then
    shell="$(command -v nologin || printf '/usr/sbin/nologin')"
    if getent group "$QLP_USER" >/dev/null; then
      run useradd --system --gid "$QLP_USER" --groups lp --no-create-home --home-dir / \
        --shell "$shell" --comment "$QLP_NAME daemon" "$QLP_USER"
    else
      run useradd --system --user-group --groups lp --no-create-home --home-dir / \
        --shell "$shell" --comment "$QLP_NAME daemon" "$QLP_USER"
    fi
    qlp_manifest_add user "$QLP_USER"
    QLP_USER_CHANGED=1
  elif [[ " $(id -nG "$QLP_USER") " != *" lp "* ]]; then
    run usermod --append --groups lp "$QLP_USER"
    QLP_USER_CHANGED=1
  fi
}

# PRINTER_UUID, generated once and never regenerated: clients (P-touch Editor
# for Mac) recognise the printer by it. A copy in $QLP_STATE_DIR survives
# uninstall.sh (only --purge removes it), so a reinstall keeps the identity.
# Both files are read with the daemons' own rules (ql810wc.config: any
# quoting, upper- or lower-case hex, a trailing comment); the UUID comes out
# in lower case.
_QLP_PY_UUID='
import sys
sys.path.insert(0, sys.argv[1])
from ql810wc.config import ConfigError, _RE_KV, _RE_UUID, parse_env_file
path = sys.argv[2]
try:
    value = parse_env_file(path).values.get("PRINTER_UUID")
except ConfigError as exc:
    print("E\t%s" % exc)
    sys.exit(0)
if value is None:
    sys.exit(0)
if _RE_UUID.fullmatch(value):
    print("U\t" + value.lower())
    sys.exit(0)
line = 0
with open(path, encoding="utf-8", errors="replace") as fh:
    for number, text in enumerate(fh.read().split("\n"), 1):
        match = _RE_KV.fullmatch(text)
        if match and match.group(1) == "PRINTER_UUID":
            line = number
print("E\t%s:%d: PRINTER_UUID=%r is not a UUID (8-4-4-4-12 hex digits)" % (path, line, value))
'
# Sets QLP_READ_UUID to the UUID in FILE, or to nothing when FILE does not
# exist or sets none. A file the daemons could not use stops the step:
# falling back to the other copy, or to a new identity, would silently
# present a new printer to every client.
QLP_READ_UUID=""
_qlp_read_uuid() { # file
  local out kind text
  QLP_READ_UUID=""
  [[ -e "$1" || -L "$1" ]] || return 0
  out="$(python3 -I -B -c "$_QLP_PY_UUID" "$QLP_REPO_ROOT/agent" "$1")" || die "could not read $1 (python3 failed)"
  IFS=$'\t' read -r kind text <<<"$out" || true
  case "$kind" in
    U) QLP_READ_UUID="$text" ;;
    E) die "the printer identity cannot be read: $text. Correct the file, or delete it: the installer then" \
           "uses the other copy ($QLP_IDENTITY_ENV, $QLP_STATE_DIR/identity.env), or generates a new" \
           "identity, which clients see as a new printer" ;;
  esac
}

# The UUID in FILE, printed, for checks that report instead of stopping
# (selftest.sh). Prints nothing when FILE does not exist, cannot be read,
# sets no PRINTER_UUID or sets one the daemons could not use.
qlp_identity_uuid() { # file
  local out kind text
  [[ -e "$1" || -L "$1" ]] || return 0
  out="$(python3 -I -B -c "$_QLP_PY_UUID" "$QLP_REPO_ROOT/agent" "$1" 2>/dev/null)" || return 0
  IFS=$'\t' read -r kind text <<<"$out" || true
  if [[ "$kind" == U ]]; then printf '%s' "$text"; fi
  return 0
}

QLP_IDENTITY_CHANGED=0
qlp_ensure_identity() {
  local uuid="" src existing=""
  for src in "$QLP_IDENTITY_ENV" "$QLP_STATE_DIR/identity.env"; do
    if [[ -e "$src" ]]; then existing+="${existing:+, }$src"; fi
    _qlp_read_uuid "$src"
    uuid="$QLP_READ_UUID"
    if [[ -n "$uuid" ]]; then break; fi
  done
  if [[ -z "$uuid" ]]; then
    uuid="$(python3 -c 'import uuid; print(uuid.uuid4())')"
    if [[ -n "$existing" ]]; then
      warn "generated a new printer identity (PRINTER_UUID): $existing sets none. Clients see a new printer."
    else
      log "generated a new printer identity (PRINTER_UUID)"
    fi
  fi
  qlp_ensure_dir "$QLP_ETC_DIR" 0755
  qlp_write_file "$QLP_IDENTITY_ENV" 0644 <<EOF
# $QLP_NAME printer identity — generated once by the installer and kept across
# reinstalls (copy: $QLP_STATE_DIR/identity.env). Network clients recognise the
# printer by this UUID; changing it makes them see a new printer.
PRINTER_UUID="$uuid"
EOF
  QLP_IDENTITY_CHANGED=$QLP_CHANGED
  install -d -m 0750 "$QLP_STATE_DIR"
  if ! cmp -s "$QLP_IDENTITY_ENV" "$QLP_STATE_DIR/identity.env"; then
    install -m 0640 "$QLP_IDENTITY_ENV" "$QLP_STATE_DIR/identity.env"
  fi
  PRINTER_UUID="$uuid"
  export PRINTER_UUID
}

# Non-secret settings for the daemons, regenerated from config.env by every
# step (same input -> same file). SNMP communities and SNMPv3 credentials are
# deliberately absent. Values are written as KEY="value" (systemd
# EnvironmentFile= compatible), so a few characters are refused. The new file
# is first checked with the daemons' own loader (ql810wc.config, once deployed)
# so a value they would reject fails the install, not a later service start.
QLP_RUNTIME_KEYS=(PROXY_IP RAW_PORT RAW_BIND RAW_BIDIRECTIONAL RAW_IDLE_TIMEOUT MAX_JOB_BYTES
                  RAW_COLOR_CHECK RAW_MEDIA_CHECK STATUS_POLL_SECONDS CUPS_QUEUE
                  USB_VENDOR_ID USB_PRODUCT_ID USB_SERIAL
                  MEDIA_DEFAULT MEDIA_SECOND SNMP_AGENT_MODE PRINTER_NAME_SUFFIX
                  PERSONA_LOCATION PERSONA_CONTACT PERSONA_SYSNAME PERSONA_MAC PERSONA_SERIAL
                  LOG_LEVEL)
# Written to runtime.env as well, but computed from the settings above: they
# are no config.env settings (qlp_runtime_value).
QLP_RUNTIME_DERIVED=(STATUS_MAX_AGE_SECONDS)

# Same, but an empty value is accepted too (the daemons then use their default).
qlp_require_int_or_empty() { # VAR min max
  if [[ -z "${!1:-}" ]]; then return 0; fi
  qlp_require_int "$@"
}

# The daemons' limits, checked before anything is written (same bounds as
# ql810wc.config, so a value they would refuse never reaches runtime.env):
#   STATUS_POLL_SECONDS  1-60 seconds
#   RAW_IDLE_TIMEOUT     10-86400 seconds (empty = the daemon default, 300)
#   MAX_JOB_BYTES        65536-1073741824 bytes (empty = the daemon default, 16 MiB)
#   LOG_LEVEL            DEBUG, INFO, WARNING or ERROR, any case (empty = INFO)
qlp_validate_runtime_settings() {
  local level="${LOG_LEVEL:-}"
  qlp_require_bool RAW_BIDIRECTIONAL RAW_COLOR_CHECK RAW_MEDIA_CHECK
  qlp_require_int STATUS_POLL_SECONDS 1 60
  qlp_require_int_or_empty RAW_IDLE_TIMEOUT 10 86400
  qlp_require_int_or_empty MAX_JOB_BYTES 65536 1073741824
  case "${level^^}" in
    ""|DEBUG|INFO|WARNING|ERROR) ;;
    *) die "config: LOG_LEVEL='$level' must be one of: DEBUG INFO WARNING ERROR" ;;
  esac
}

# STATUS_MAX_AGE_SECONDS for a poll interval: the daemons treat an idle status
# older than that as stale and require it to be at least twice
# STATUS_POLL_SECONDS. Their default of 60 unless the interval needs more.
qlp_status_max_age() { # status-poll-seconds
  local poll="${1:-}"
  if [[ ! "$poll" =~ ^[0-9]{1,18}$ ]] || ((10#$poll < 1 || 10#$poll > 60)); then poll=3; fi
  poll=$((10#$poll))
  if ((2 * poll > 60)); then printf '%d' $((2 * poll)); else printf '60'; fi
}

# The value runtime.env gets for KEY: the configured value in the canonical
# form the installer writes ("0300" -> "300", "yes"/"no", lower-case USB ids,
# upper-case LOG_LEVEL), or for a derived key the value computed from the
# other settings. selftest.sh compares runtime.env against the same values.
qlp_runtime_value() { # key
  local v="${!1:-}"
  case "$1" in
    RAW_BIDIRECTIONAL|RAW_COLOR_CHECK|RAW_MEDIA_CHECK) if qlp_is_yes "$v"; then v=yes; else v=no; fi ;;
    USB_VENDOR_ID|USB_PRODUCT_ID) v="${v,,}" ;;
    STATUS_POLL_SECONDS|RAW_IDLE_TIMEOUT|MAX_JOB_BYTES)
      if [[ "$v" =~ ^[0-9]{1,18}$ ]]; then v="$((10#$v))"; fi ;;
    LOG_LEVEL) v="${v^^}"; v="${v:-INFO}" ;;
    STATUS_MAX_AGE_SECONDS) v="$(qlp_status_max_age "${STATUS_POLL_SECONDS:-}")" ;;
  esac
  printf '%s' "$v"
}
_QLP_PY_VALIDATE_RUNTIME='
import sys
sys.path.insert(0, sys.argv[1])
try:
    from ql810wc.config import ConfigError, load_runtime_config
    try:
        load_runtime_config(sys.argv[2], identity_path=sys.argv[3])
    except ConfigError as exc:
        print(exc)
        sys.exit(1)
except Exception as exc:
    print("%s: %r" % (type(exc).__name__, exc))
    sys.exit(3)
'
QLP_RUNTIME_CHANGED=0
qlp_sync_runtime_env() {
  local k v body tmp out rc=0
  qlp_validate_runtime_settings
  body="# $QLP_NAME runtime settings (non-secret), generated from config.env by the
# installer. Do not edit: change config.env and re-run ./install.sh."
  for k in "${QLP_RUNTIME_KEYS[@]}" "${QLP_RUNTIME_DERIVED[@]}"; do
    if [[ "$k" == "${QLP_RUNTIME_DERIVED[0]}" ]]; then
      body+=$'\n'"# Derived from the settings above (no config.env setting):"
    fi
    v="$(qlp_runtime_value "$k")"   # validated above: canonical form
    case "$v" in
      *[\"\\\$\`]*|*$'\n'*) die "config: $k contains a character not allowed here (\" \\ \$ \` or a newline)" ;;
    esac
    body+=$'\n'"$k=\"$v\""
  done
  tmp="$(mktemp)"
  printf '%s\n' "$body" >"$tmp"
  if [[ -f "$QLP_LIB_DIR/ql810wc/config.py" ]]; then
    out="$("$QLP_PYTHON" -I -B -c "$_QLP_PY_VALIDATE_RUNTIME" "$QLP_LIB_DIR" "$tmp" "$QLP_IDENTITY_ENV" 2>&1)" || rc=$?
    case "$rc" in
      0) ;;
      1) rm -f "$tmp"; die "the daemons would reject these settings: $out" ;;
      *) warn "could not check the runtime settings with ql810wc.config: $out" ;;
    esac
  fi
  qlp_ensure_dir "$QLP_ETC_DIR" 0755
  qlp_write_file "$QLP_RUNTIME_ENV" 0644 <"$tmp"
  QLP_RUNTIME_CHANGED=$QLP_CHANGED
  rm -f "$tmp"
}

# ---------------------------------------------------------------------------
# snmpd
# ---------------------------------------------------------------------------
# SNMP_AGENT_MODE=pass_persist: the persona runs as pass_persist children of
# snmpd, which load the deployed code, oid-map.yaml, runtime.env and
# identity.env once, when they are spawned, and live as long as snmpd. Unlike
# ql810wc-snmp-agent.service (restarted for new code, reloaded for new
# settings) they cannot be told about either, so whichever step changes those
# inputs (20, 30 or 40, also run alone with --only) restarts snmpd (restart,
# never reload: net-snmp 5.9.5.2 mishandles pass_persist on SIGHUP).
# $QLP_STATE_DIR/passpersist.digest holds a digest of the inputs snmpd last
# (re)started with.
_QLP_PY_DIGEST='
import hashlib, sys
digest = hashlib.sha256()
for path in sys.argv[1:]:
    digest.update(path.encode("utf-8", "surrogateescape") + b"\0")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        digest.update(b"-\0")
        continue
    digest.update(b"%d\0" % len(data) + data)
print(digest.hexdigest())
'
qlp_passpersist_digest() { # -> digest of the persona's inputs
  python3 -c "$_QLP_PY_DIGEST" "$QLP_LIB_DIR/brother_snmp_agent.py" "$QLP_LIB_DIR"/ql810wc/*.py \
    "$QLP_LIB_DIR/oid-map.yaml" "$QLP_RUNTIME_ENV" "$QLP_IDENTITY_ENV"
}

# Does snmpd run pass_persist children that may hold older inputs? Only while
# SNMP_AGENT_MODE=pass_persist and the snmpd drop-in has the pass_persist
# lines (step 30 writes them; until then no child exists).
qlp_passpersist_stale() {
  local old=""
  [[ "${SNMP_AGENT_MODE:-agentx}" == pass_persist ]] || return 1
  grep -qsF -- "brother_snmp_agent.py --mode pass_persist" "$QLP_SNMPD_DROPIN" || return 1
  if [[ -r "$QLP_STATE_DIR/passpersist.digest" ]]; then old="$(<"$QLP_STATE_DIR/passpersist.digest")"; fi
  [[ "$(qlp_passpersist_digest)" != "$old" ]]
}

# Note the inputs snmpd's pass_persist children now run with (call right after
# snmpd was restarted or started). Outside pass_persist mode the note goes.
qlp_passpersist_record() {
  if [[ "${SNMP_AGENT_MODE:-agentx}" != pass_persist ]]; then
    rm -f -- "$QLP_STATE_DIR/passpersist.digest"
    return 0
  fi
  install -d -m 0750 "$QLP_STATE_DIR"
  qlp_passpersist_digest >"$QLP_STATE_DIR/passpersist.digest"
}

# Steps 20 and 40: restart snmpd when its pass_persist children may run older
# code or settings than are deployed now (only if snmpd runs: a stopped snmpd
# loads the current ones when it starts).
qlp_passpersist_refresh() {
  qlp_passpersist_stale || return 0
  if systemctl is-active --quiet snmpd.service 2>/dev/null; then
    log "the pass_persist persona's code or settings changed — restarting snmpd (never reloading it)"
    run systemctl restart snmpd.service
    qlp_require_active snmpd.service
  fi
  qlp_passpersist_record
}

# Wildcard UDP 161 listeners from "ss -ulnp", as "<family> <process>" lines
# (family ipv4 or ipv6; process "?" when ss cannot name it, e.g. without
# root). A socket ss shows as "*:161" (IPv6, not v6-only) receives both
# families; one bound to a device ("0.0.0.0%eth0:161") is no wildcard.
# shellcheck disable=SC2016  # an awk program: its $i are awk fields
_QLP_SS_WILDCARD_AWK='
{
  addr = ""; owner = "?"
  for (i = 1; i <= NF; i++) {
    if (addr == "" && $i ~ /:161$/) addr = substr($i, 1, length($i) - 4)
    if ($i ~ /^users:\(\("/) { owner = $i; sub(/^users:\(\("/, "", owner); sub(/".*$/, "", owner) }
  }
  if (addr == "0.0.0.0" || addr == "*") print "ipv4", owner
  if (addr == "[::]" || addr == "::" || addr == "*") print "ipv6", owner
}'
qlp_snmp_wildcard_listeners() {
  { ss -Hulnp 'sport = :161' 2>/dev/null || true; } | awk "$_QLP_SS_WILDCARD_AWK"
}

# ---------------------------------------------------------------------------
# SNMPv3 USM users in net-snmp's persistent file. snmpd rewrites that file from
# memory when it stops, so it must be stopped while these helpers edit it.
# Lines are matched like net-snmp parses them: "usmUser ST ST ENGINE NAME ..."
# (NAME quoted or 0x-hex) and "createUser [-M] [-e ENGINE] NAME ...".
# ---------------------------------------------------------------------------
_QLP_PY_USM='
import sys
mode, user, path = sys.argv[1], sys.argv[2], sys.argv[3]
def words(line):
    out, i, n = [], 0, len(line)
    while True:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            return out
        if line[i] in "\"\x27":
            quote, i, buf = line[i], i + 1, []
            while i < n and line[i] != quote:
                if line[i] == "\\" and i + 1 < n:
                    buf.append(line[i + 1]); i += 2
                else:
                    buf.append(line[i]); i += 1
            out.append("".join(buf)); i += 1
        else:
            j = i
            while j < n and not line[j].isspace():
                j += 1
            out.append(line[i:j]); i = j
def is_user(tok):
    return tok == user or tok.lower() == "0x" + user.encode().hex()
def ours(line):
    w = words(line)
    if not w or w[0].startswith("#"):
        return False
    d = w[0].lower()
    if d == "usmuser":
        return len(w) > 4 and is_user(w[4])
    if d == "createuser":
        i = 1
        if i < len(w) and w[i] == "-M":
            i += 1
        if i < len(w) and w[i] == "-e":
            i += 2
        return i < len(w) and w[i] == user
    return False
try:
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        lines = fh.readlines()
except FileNotFoundError:
    lines = []
hits = [l for l in lines if ours(l)]
if mode == "has":
    sys.exit(0 if hits else 1)
if hits:
    with open(path, "r+", encoding="utf-8", errors="surrogateescape") as fh:
        fh.seek(0)
        fh.writelines(l for l in lines if not ours(l))
        fh.truncate()
print(len(hits))
'
qlp_usm_has_user() { # user [file]
  python3 -c "$_QLP_PY_USM" has "$1" "${2:-$QLP_SNMP_PERSIST}"
}

qlp_usm_remove_user() { # user [file]   (snmpd must be stopped)
  local n
  n="$(python3 -c "$_QLP_PY_USM" remove "$1" "${2:-$QLP_SNMP_PERSIST}")"
  if [[ "$n" != 0 ]]; then log "removed SNMPv3 user '$1' ($n line(s)) from ${2:-$QLP_SNMP_PERSIST}"; fi
}

# ---------------------------------------------------------------------------
# USB printer detection (sysfs; no root needed). Sets:
#   QL_USB_PATH QL_USB_VID QL_USB_PID QL_USB_SERIAL QL_USB_PRODUCT
#   QL_USB_MANUFACTURER QL_USB_MODE (printer|mass-storage|other)
#   QL_USB_LP (e.g. /dev/usb/lp0 when the usblp driver is bound, else empty)
# Honors USB_VENDOR_ID / USB_PRODUCT_ID / USB_SERIAL overrides from config.
# Returns 1 if no Brother QL printer is attached.
# ---------------------------------------------------------------------------
_qlp_read() { cat -- "$1" 2>/dev/null || true; }

qlp_detect_printer() {
  local want_vid="${USB_VENDOR_ID:-$QLP_BROTHER_VID}" d vid pid serial product classes lp
  QL_USB_PATH="" QL_USB_VID="" QL_USB_PID="" QL_USB_SERIAL="" QL_USB_PRODUCT=""
  QL_USB_MANUFACTURER="" QL_USB_MODE="" QL_USB_LP=""
  for d in /sys/bus/usb/devices/*; do
    [[ -f "$d/idVendor" ]] || continue
    vid="$(_qlp_read "$d/idVendor")"
    [[ "${vid,,}" == "${want_vid,,}" ]] || continue
    pid="$(_qlp_read "$d/idProduct")"
    [[ -z "${USB_PRODUCT_ID:-}" || "${pid,,}" == "${USB_PRODUCT_ID,,}" ]] || continue
    serial="$(_qlp_read "$d/serial")"
    [[ -z "${USB_SERIAL:-}" || "$serial" == "$USB_SERIAL" ]] || continue
    product="$(_qlp_read "$d/product")"
    # Unless the user pinned a PID, only accept QL-series products.
    if [[ -z "${USB_PRODUCT_ID:-}" && ! "$product" =~ QL- ]]; then continue; fi
    classes="$(cat "$d"/*:*/bInterfaceClass 2>/dev/null | sort -u | tr '\n' ' ')"
    QL_USB_PATH="$d" QL_USB_VID="$vid" QL_USB_PID="$pid" QL_USB_SERIAL="$serial"
    QL_USB_PRODUCT="$product" QL_USB_MANUFACTURER="$(_qlp_read "$d/manufacturer")"
    case " $classes " in
      *" 07 "*) QL_USB_MODE="printer" ;;
      *" 08 "*) QL_USB_MODE="mass-storage" ;;   # Editor Lite is ON
      *)        QL_USB_MODE="other" ;;
    esac
    for lp in "$d"/*:*/usbmisc/lp*; do
      [[ -e "$lp" ]] && QL_USB_LP="/dev/usb/$(basename "$lp")" && break
    done
    export QL_USB_PATH QL_USB_VID QL_USB_PID QL_USB_SERIAL QL_USB_PRODUCT \
           QL_USB_MANUFACTURER QL_USB_MODE QL_USB_LP
    return 0
  done
  return 1
}

# ---------------------------------------------------------------------------
# Masking. Install logs and terminal output end up pasted into public issues,
# so serial numbers never appear in them in full.
# ---------------------------------------------------------------------------
# Mask a serial for logs: keep the first 3 characters.
qlp_mask() { local s="${1:-}"; ((${#s} > 3)) && printf '%s***' "${s:0:3}" || printf '%s' "$s"; }

# Filter (stdin -> stdout) for output of other tools that we log or print:
# masks the printer's serial (QL_USB_SERIAL, USB_SERIAL) wherever it occurs,
# and any value after "SerialNumber:" (kernel log), "Serial Number:",
# "serial=" (CUPS usb:// URIs), "iSerial <index>" (lsusb -v) or "SN:"
# (IEEE-1284 device IDs). Like qlp_mask it keeps the first 3 characters;
# values of 3 characters or fewer (the string index in the kernel's
# "SerialNumber=3") stay as they are. Masking is idempotent.
_QLP_SERIAL_CHARS='[[:alnum:]_.-]'
_QLP_SED_SERIALS=(
  -e "s/([Ss]erial([ _-]?[Nn]umber)?[[:space:]]*[:=][[:space:]]*)(${_QLP_SERIAL_CHARS}{3})${_QLP_SERIAL_CHARS}+/\\1\\3***/g"
  -e "s/(iSerial[[:space:]]+[0-9]+[[:space:]]+)(${_QLP_SERIAL_CHARS}{3})${_QLP_SERIAL_CHARS}+/\\1\\2***/g"
  -e "s/(^|[^[:alnum:]])(SN:[[:space:]]*)(${_QLP_SERIAL_CHARS}{3})${_QLP_SERIAL_CHARS}+/\\1\\2\\3***/g"
)
qlp_mask_serials() {
  local line s
  local -a known=()
  for s in "${QL_USB_SERIAL:-}" "${USB_SERIAL:-}"; do
    if ((${#s} > 3)); then known+=("$s"); fi
  done
  sed -E "${_QLP_SED_SERIALS[@]}" | while IFS= read -r line || [[ -n "$line" ]]; do
    if ((${#known[@]})); then
      for s in "${known[@]}"; do line="${line//"$s"/"${s:0:3}***"}"; done
    fi
    printf '%s\n' "$line"
  done
}

# The last N kernel messages about USB devices, serial numbers masked. Prints
# nothing (and never fails) when the kernel log is not readable (non-root).
qlp_kernel_usb_messages() { # [n]
  { dmesg 2>/dev/null || true; } | { grep -iE 'usb|usblp|brother' || true; } \
    | tail -n "${1:-6}" | qlp_mask_serials
}
