#!/usr/bin/env bash
# scripts/30-snmp-agent.sh — the control plane (tiers T2/T3 + host monitoring):
# net-snmp answers as the printer for SNMPv1/v2c clients and keeps this host's
# own MIBs for SNMPv3 authPriv users.
#
#   sudo scripts/30-snmp-agent.sh        (normally run by ./install.sh)
#
# What it sets up (all idempotent, all recorded in the manifest):
#   * "includeDir /etc/snmp/snmpd.conf.d" as a managed block in snmpd.conf
#   * /etc/snmp/snmpd.conf.d/ql810wc-proxy.conf (0600): AgentX master, every
#     community x source mapped read-only to SNMP context "printer" (view
#     printerView), optional SNMPv3 user (view hostView). No agentaddress, so
#     snmpd keeps its default UDP 161 wildcard (broadcast discovery needs it);
#     only IPv6 sources add "agentaddress udp:161,udp6:161", and only while no
#     other snmpd configuration sets listening addresses (they add up, and a
#     second bind of UDP 161 makes snmpd exit)
#   * the SNMPv3 user's keys (createUser, written while snmpd is stopped)
#   * snmpd enabled and restarted when its configuration changed, or in
#     pass_persist mode the persona's code or settings did (never "reload":
#     net-snmp 5.9.5.2 mishandles pass_persist on SIGHUP), then a check (ss)
#     that it listens on the UDP 161 wildcard
#   * ql810wc-snmp-agent.service (SNMP_AGENT_MODE=agentx): restarted for a new
#     unit file, reloaded (same AgentX session, no SNMP gap) for new settings;
#     or pass_persist lines + an snmpd_var_run_t label on /run/ql810wc-proxy
#     (pass_persist)
#   * AgentX access: agentXPerms (socket root:ql810wc 0660) and /var/agentx
#     (root:ql810wc 0750) when nothing else configures them. An agentXPerms of
#     another snmpd configuration, or a /var/agentx of another group, is left
#     as it is: the subagent joins that group (a unit drop-in), or the step
#     stops with what to change. snmpd's own root-only /var/agentx gets group
#     ql810wc r-x; the original owner and mode are recorded for uninstall.sh.
#   * firewalld: UDP 161 only from SNMP_PRINTER_ALLOWED_CIDRS (+ the SNMPv3
#     sources when SNMPv3 is on), as source-scoped rich rules
# Requires step 20 (user ql810wc and the deployed code).
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

readonly SNMPD_UNIT="snmpd.service"
readonly SNMPD_CONF="/etc/snmp/snmpd.conf"
readonly SNMPD_DROPIN_DIR="$QLP_SNMPD_DROPIN_DIR"
readonly SNMPD_DROPIN="$QLP_SNMPD_DROPIN"
readonly SNMPD_TEMPLATE="$QLP_REPO_ROOT/agent/snmpd/ql810wc-proxy.conf.tmpl"
# The files net-snmp reads (its search path, and what they include), then the
# other drop-ins, which are read once our include is in place. Not readonly
# (like SNMPD_SYSCONFIG): the tests point them at sample files.
SNMPD_OTHER_CONFIGS=("$SNMPD_CONF" /etc/snmp/snmpd.local.conf /usr/share/snmp/snmpd.conf
                     /usr/share/snmp/snmpd.local.conf /usr/lib64/snmp/snmpd.conf
                     /usr/lib64/snmp/snmpd.local.conf "$SNMPD_DROPIN_DIR")
# snmpd.service runs "snmpd $OPTIONS -f"; OPTIONS may name listening addresses too.
SNMPD_SYSCONFIG="/etc/sysconfig/snmpd"
AGENTX_DIR="/var/agentx"                  # not readonly either: the tests point it elsewhere
readonly AGENTX_SOCKET="/var/agentx/master"
# Our agentXPerms: socket root:ql810wc 0660, directory mode 0750 (only applied
# when snmpd itself creates the directory; ensure_agentx_dir creates it first).
readonly OWN_AGENTX_PERMS="0660 0750 root $QLP_USER"
# Drop-in of ql810wc-snmp-agent.service with the groups it joins (if any).
readonly AGENT_GROUPS_DROPIN="ql810wc-proxy-agentx-groups.conf"
readonly AGENT_SCRIPT="$QLP_LIB_DIR/brother_snmp_agent.py"
readonly PERSONA_CONTEXT="printer"
readonly PERSONA_SECNAME="ql810wcPersona"
readonly PRINTER_VIEW="printerView"   # what v1/v2c communities may read
readonly HOST_VIEW="hostView"         # what the SNMPv3 user may read
# Subtrees v1/v2c clients may read. In agentx mode the "printer" context holds
# nothing but the persona; pass_persist serves only Printer-MIB and Brother.
readonly VIEW_AGENTX=(.1.3.6.1.2.1.1 .1.3.6.1.2.1.2 .1.3.6.1.2.1.25 .1.3.6.1.2.1.43 .1.3.6.1.4.1.2435)
readonly VIEW_PASS_PERSIST=(.1.3.6.1.2.1.43 .1.3.6.1.4.1.2435)
readonly RUN_FCONTEXT_TYPE="snmpd_var_run_t"
readonly RUN_FCONTEXT_REGEX="$QLP_RUN_DIR(/.*)?"
readonly SNMP_RULE_SELECTOR='port port="161" protocol="udp"'
readonly V3_MARKER="$QLP_STATE_DIR/snmpv3-user"   # user, salt, sha256 of the v3 settings
readonly PROBE_OID=".1.3.6.1.4.1.2435.2.3.9.1.1.7.0" # brieee1284id: every client reads it

COMMUNITIES=()        # effective v1/v2c communities (deduplicated)
PRINTER_CIDRS=""      # "family<TAB>network" lines (qlp_norm_cidrs)
V3_CIDRS=""
V3_ENABLED=0
NEED_UDP6=0           # an allowed source is IPv6: snmpd must listen on udp6:161 too
ADMIN_LISTEN=()       # "where: addresses" — listening addresses set outside our drop-in
AGENTX_PERMS=()       # "where<TAB>fields" of each agentXPerms line outside our drop-in
AGENTX_PERMS_WHERE="" # those lines, for messages
AGENTX_OWN_PERMS=1    # the drop-in sets agentXPerms (nothing else does)
AGENTX_ACCESS=""      # how ql810wc-snmp-agent reaches the AgentX socket (for messages)
AX_SOCK=0 AX_DIR=0 AX_USER=0 AX_GROUP=0   # the agentXPerms net-snmp will apply (modes, uid, gid)
AGENT_GROUPS=()       # groups ql810wc-snmp-agent joins (SupplementaryGroups=) for the socket or /var/agentx
AGENT_GIDS=()         # its group ids: ql810wc's own, then those of AGENT_GROUPS
AGENT_DROPIN_CHANGED=0
SNMPD_HAS_INCLUDE=0   # another config already includes our drop-in directory
SNMPD_CHANGED=0       # configuration or users changed -> restart snmpd
SNMPD_STOPPED=0       # we stopped snmpd to edit its persistent file
SNMP_LISTEN_TRIES=5   # seconds to wait for snmpd's UDP 161 sockets after a start

# Finds what in the existing snmpd configuration would defeat ours: another
# mapping of one of our communities (the first com2sec match wins, so the
# request would never reach context "printer"), another definition of our
# SNMPv3 user, or a view of the same name as ours (views merge). It also
# reports listening addresses set elsewhere (agentaddress lines, or addresses
# on the snmpd command line: OPTIONS in /etc/sysconfig/snmpd), because any
# address we set would add to them, and every agentXPerms line, because
# net-snmp keeps one agentXPerms setting for the whole agent (each field from
# the last line that sets it). Tokenizes like net-snmp (copy_nword); follows
# includes.
# Output lines: "E<TAB>problem", "W<TAB>warning", "I<TAB>file:line" (that line
# already includes our drop-in directory), "A<TAB>where<TAB>addresses",
# "P<TAB>where<TAB>agentXPerms fields".
# Keep bare apostrophes out of the program: bash 3.2 (macOS, where the tests
# also run) mis-parses a quote in a heredoc inside a command substitution.
SCAN_PY="$(cat <<'PY'
import glob, os, re, sys

own = os.path.realpath(os.environ["QLP_OWN"])
dropin_dir = os.path.realpath(os.environ["QLP_DROPIN_DIR"])
tag = os.environ["QLP_TAG"]
communities = set(os.environ.get("QLP_COMMUNITIES", "").split())
user = os.environ.get("QLP_V3_USER", "")
socket_path = os.environ.get("QLP_AGENTX_SOCKET", "")
views = set(os.environ.get("QLP_VIEWS", "").split())
sysconfig = os.environ.get("QLP_SYSCONFIG", "")

# snmpd(8) options (net-snmp 5.9) that take an argument. Any other word on its
# command line that is not an option is a listening address.
ARG_OPTIONS = set("cgIlmMnpPSuxY")
# A syslog priority or range, as decode_priority in net-snmp reads it.
PRIORITY = re.compile(r"[0-7!aAcCeEwWnNiIdD](-[0-7!aAcCeEwWnNiIdD])?")

def unix_socket(spec):
    """An agentXSocket value as a path: "unix:/x" and "/x" are the same socket."""
    return spec[5:] if spec.lower().startswith("unix:") else spec

def log_option_words(spec, rest):
    """How many of the words REST that follow "-L<spec>" the option consumes
    (snmp_log_options in net-snmp): "-Lf FILE", "-Ls FACILITY" and "-LE PRIORITY"
    read their value from the next word when none is attached; "-LF PRIORITY FILE"
    and "-LS PRIORITY FACILITY" do so for each part not attached; "-L" alone takes
    its spec from the next word first."""
    used = 0
    if not spec:
        if not rest:
            return 0
        spec, used = rest[0], 1
    letter, value = spec[0], spec[1:].lstrip("=").lstrip()
    if letter in "FS":
        if not value:
            if used >= len(rest):
                return used
            value, used = rest[used], used + 1
        match = PRIORITY.match(value)
        tail = value[match.end():].lstrip() if match else value
        return used if tail else min(used + 1, len(rest))
    if letter in "fsEON" and not value:
        return min(used + 1, len(rest))
    return used

def listening_words(words):
    """The listening addresses among the words of the snmpd command line (getopt rules)."""
    found, i = [], 0
    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            found.extend(words[i:])
            break
        if word.startswith("--"):
            continue                    # --token=value: the argument is attached
        if not word.startswith("-") or word == "-":
            found.append(word)
            continue
        for j in range(1, len(word)):
            if word[j] == "D":          # -D[TOKEN,...]: optional argument, attached only
                break
            if word[j] == "L":          # -L<spec>: may read one or two more words
                i += log_option_words(word[j + 1:], words[i:])
                break
            if word[j] in ARG_OPTIONS:
                if j == len(word) - 1:
                    i += 1              # its argument is the next word
                break
    return found

def sysconfig_words(path):
    """OPTIONS of a systemd EnvironmentFile (last assignment wins), split into
    words as systemd splits an unquoted $OPTIONS."""
    value = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                match = re.match(r"[ \t]*OPTIONS[ \t]*=(.*)$", line.rstrip("\n"))
                if match:
                    value = match.group(1).strip()
    except OSError:
        return []
    if value is None:
        return []
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.split()

def words(line):
    out, i, n = [], 0, len(line)
    while True:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            return out
        if line[i] in "\"'":
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

def strip_flags(args, flags):
    for flag, width in flags:
        if args[:1] == [flag]:
            args = args[width:]
    return args

seen = set()

# "unread": the file is scanned only because it sits in the drop-in directory;
# snmpd reads it only if something includes that directory, so an includeDir
# found there does not count as including it.
def scan(path, depth=0, unread=False):
    real = os.path.realpath(path)
    if real in seen or real == own or depth > 8 or not os.path.exists(real):
        return
    seen.add(real)
    if os.path.isdir(real):
        for name in sorted(glob.glob(os.path.join(real, "*.conf"))):
            scan(name, depth + 1, unread)
        return
    try:
        with open(real, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError as exc:
        print("W\tcannot read %s: %s" % (path, exc))
        return
    managed = False
    for number, line in enumerate(lines, 1):
        text = line.strip()
        if text.startswith("# BEGIN " + tag):
            managed = True
            continue
        if managed:
            managed = text != "# END " + tag
            continue
        if not text or text.startswith("#"):
            continue
        w = words(text)
        if w and w[0].startswith("[") and w[0].endswith("]"):
            w = w[1:]
        if not w:
            continue
        d, a, where = w[0].lower(), w[1:], "%s:%d" % (path, number)
        if d in ("com2sec", "com2sec6", "com2secunix"):
            a = strip_flags(a, [("-Cn", 2)])
            if len(a) > 2 and a[2] in communities:
                print("E\t%s: %s already maps community \"%s\"" % (where, w[0], a[2]))
        elif d in ("rocommunity", "rwcommunity", "rocommunity6", "rwcommunity6", "authcommunity"):
            a = strip_flags(a[1:] if d == "authcommunity" else a, [("-v", 2)])
            if a and a[0] in communities:
                print("E\t%s: %s already grants community \"%s\"" % (where, w[0], a[0]))
        elif user and d in ("rouser", "rwuser", "authuser"):
            a = strip_flags(a[1:] if d == "authuser" else a, [("-s", 2)])
            if a and a[0] == user:
                print("E\t%s: %s already configures SNMPv3 user \"%s\"" % (where, w[0], user))
        elif user and d == "group":
            if len(a) > 2 and a[1].lower() == "usm" and a[2] == user:
                print("E\t%s: group already places SNMPv3 user \"%s\" in group %s" % (where, user, a[0]))
        elif user and d == "createuser":
            a = strip_flags(a, [("-M", 1), ("-e", 2)])
            if a and a[0] == user:
                print("E\t%s: createUser for \"%s\" (this installer manages that user)" % (where, user))
        elif d == "view" and a and a[0] in views:
            print("E\t%s: defines view \"%s\", which the ql810wc-proxy drop-in uses (the definitions would merge)"
                  % (where, a[0]))
        elif d == "agentaddress":
            print("A\t%s\t%s" % (where, " ".join(a)))
        elif d == "agentxperms":
            print("P\t%s\t%s" % (where, " ".join(a[:4])))
        elif d == "agentxsocket" and a and unix_socket(a[0]) != unix_socket(socket_path):
            print("W\t%s: agentXSocket %s - the ql810wc-proxy drop-in sets %s, the socket"
                  " ql810wc-snmp-agent connects to. agentXSocket is last-wins: whichever line snmpd"
                  " reads last, the subagents of the other socket cannot connect" % (where, a[0], socket_path))
        elif d in ("includedir", "includefile") and a:
            target = " ".join(a)
            if not target.startswith("/"):
                target = os.path.join(os.path.dirname(real), target)
            if d == "includedir" and os.path.realpath(target) == dropin_dir and not unread:
                print("I\t%s" % where)
            scan(target, depth + 1, unread)

for item in sys.argv[1:]:
    scan(item, unread=os.path.realpath(item) == dropin_dir)
if sysconfig:
    addresses = listening_words(sysconfig_words(sysconfig))
    if addresses:
        print("A\t%s (OPTIONS)\t%s" % (sysconfig, " ".join(addresses)))
PY
)"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
v3_is_set() { [[ -n "${!1:-}" && "${!1}" != *CHANGE-ME* ]]; }

v3_check_pass() { # VAR
  local value="${!1}"
  ((${#value} >= 8)) || die "config: $1 needs at least 8 characters (net-snmp refuses shorter passphrases)"
  case "$value" in *$'\n'*|*$'\r'*) die "config: $1 contains a line break" ;; esac
}

decide_v3() {
  local mode="${SNMP_V3_ENABLE:-auto}"
  local -a list=()
  case "${mode,,}" in
    auto)
      if v3_is_set SNMP_V3_AUTH_PASS && v3_is_set SNMP_V3_PRIV_PASS; then V3_ENABLED=1
      else log "SNMPv3 host monitoring off (SNMP_V3_ENABLE=auto and the passphrases are still unset)"; fi ;;
    1|y|yes|true|on) qlp_require_real SNMP_V3_AUTH_PASS SNMP_V3_PRIV_PASS; V3_ENABLED=1 ;;
    0|n|no|false|off) log "SNMPv3 host monitoring off (SNMP_V3_ENABLE=$mode)" ;;
    *) die "config: SNMP_V3_ENABLE='$mode' must be auto, yes or no" ;;
  esac
  ((V3_ENABLED)) || return 0
  [[ "${SNMP_V3_USER:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$ ]] \
    || die "config: SNMP_V3_USER='${SNMP_V3_USER:-}' — use 1-32 letters, digits, '.', '_' or '-'"
  SNMP_V3_AUTH_PROTO="${SNMP_V3_AUTH_PROTO^^}"
  SNMP_V3_PRIV_PROTO="${SNMP_V3_PRIV_PROTO^^}"
  case "$SNMP_V3_AUTH_PROTO" in
    SHA|SHA-1|SHA1|SHA-224|SHA224|SHA-256|SHA256|SHA-384|SHA384|SHA-512|SHA512) ;;
    *) die "config: SNMP_V3_AUTH_PROTO='$SNMP_V3_AUTH_PROTO' — use SHA-256 (or SHA, SHA-224, SHA-384, SHA-512)" ;;
  esac
  case "$SNMP_V3_PRIV_PROTO" in
    AES|AES-128|AES128) ;;
    AES-192|AES192|AES-256|AES256)
      warn "SNMP_V3_PRIV_PROTO=$SNMP_V3_PRIV_PROTO: many monitoring systems only interoperate with AES (AES-128)" ;;
    *) die "config: SNMP_V3_PRIV_PROTO='$SNMP_V3_PRIV_PROTO' — use AES (DES is not available on Fedora)" ;;
  esac
  v3_check_pass SNMP_V3_AUTH_PASS
  v3_check_pass SNMP_V3_PRIV_PASS
  read -r -a list <<<"${SNMP_V3_ALLOWED_CIDRS:-}"
  V3_CIDRS="$(qlp_norm_cidrs SNMP_V3_ALLOWED_CIDRS "${list[@]}")"
  if [[ -z "$V3_CIDRS" ]]; then
    warn "SNMP_V3_ALLOWED_CIDRS is empty: the firewall admits SNMPv3 only from SNMP_PRINTER_ALLOWED_CIDRS"
  elif qlp_classify_sources "${list[@]}" && [[ -z "$QLP_SRC_USABLE" && -n "$QLP_SRC_PLACEHOLDER" ]]; then
    warn "SNMP_V3_ALLOWED_CIDRS holds only documentation-only example ranges ($QLP_SRC_PLACEHOLDER): the" \
         "firewall admits your monitoring system only from inside SNMP_PRINTER_ALLOWED_CIDRS. Set it to" \
         "the monitoring host's address in config.env."
  fi
}

validate_config() {
  local c seen=" " re_comm='^[A-Za-z0-9._@%+=:,~-]{1,64}$' why=""
  local -a words=() list=()
  qlp_require_enum SNMP_AGENT_MODE agentx pass_persist
  read -r -a words <<<"${SNMP_PRINTER_COMMUNITIES:-} ${SNMP_PRINTER_COMMUNITY:-}"
  for c in "${words[@]}"; do
    [[ "$c" =~ $re_comm ]] \
      || die "config: SNMP community '$c' — use 1-64 letters, digits or . _ @ % + = : , ~ -"
    if [[ "$seen" != *" $c "* ]]; then COMMUNITIES+=("$c"); seen+="$c "; fi
  done
  ((${#COMMUNITIES[@]})) \
    || die "config: SNMP_PRINTER_COMMUNITIES is empty (P-touch for Mac reads with \"internal\", Windows tools with \"public\")"
  if [[ -n "${SNMP_PRINTER_COMMUNITY:-}" ]]; then
    warn "SNMP_PRINTER_COMMUNITY is deprecated — list the community in SNMP_PRINTER_COMMUNITIES instead"
  fi
  read -r -a list <<<"${SNMP_PRINTER_ALLOWED_CIDRS:-}"
  PRINTER_CIDRS="$(qlp_norm_cidrs SNMP_PRINTER_ALLOWED_CIDRS "${list[@]}")"
  # Also the template's documentation-only range: com2sec and the firewall
  # then admit no real client. A warning only, so lab setups still install.
  if qlp_classify_sources ${list[@]+"${list[@]}"}; then
    why="$(qlp_sources_problem SNMP_PRINTER_ALLOWED_CIDRS ${list[@]+"${list[@]}"})"
  fi
  if [[ -n "$why" ]]; then
    warn "$why: only this host can read the printer persona (no P-touch discovery or status from the" \
         "network). Set SNMP_PRINTER_ALLOWED_CIDRS to your clients' subnet(s) in config.env and re-run."
  fi
  decide_v3
  if [[ "$PRINTER_CIDRS$V3_CIDRS" == *ipv6* ]]; then NEED_UDP6=1; fi
}

check_snmpd_conflicts() {
  local out kind msg detail errors=0 v3user="" where
  if ((V3_ENABLED)); then v3user="$SNMP_V3_USER"; fi
  out="$(QLP_OWN="$SNMPD_DROPIN" QLP_DROPIN_DIR="$SNMPD_DROPIN_DIR" QLP_TAG="$QLP_TAG" \
         QLP_COMMUNITIES="${COMMUNITIES[*]}" QLP_V3_USER="$v3user" QLP_AGENTX_SOCKET="$AGENTX_SOCKET" \
         QLP_VIEWS="$PRINTER_VIEW $HOST_VIEW" QLP_SYSCONFIG="$SNMPD_SYSCONFIG" \
         python3 -c "$SCAN_PY" "${SNMPD_OTHER_CONFIGS[@]}")" \
    || die "could not scan the existing snmpd configuration"
  ADMIN_LISTEN=() AGENTX_PERMS=()
  while IFS=$'\t' read -r kind msg detail; do
    case "$kind" in
      E) error "$msg"; errors=$((errors + 1)) ;;
      W) warn "$msg" ;;
      A) ADMIN_LISTEN+=("$msg: $detail") ;;
      P) AGENTX_PERMS+=("$msg"$'\t'"$detail") ;;
      I) SNMPD_HAS_INCLUDE=1; log "$msg already includes $SNMPD_DROPIN_DIR" ;;
    esac
  done <<<"$out"
  if ((errors)); then
    die "the existing snmpd configuration already uses the communities/user/views above. net-snmp" \
        "applies the first matching com2sec line (clients would never reach the printer persona) and" \
        "merges views of the same name. Remove those lines, or choose other SNMP_PRINTER_COMMUNITIES /" \
        "SNMP_V3_USER in config.env, then re-run."
  fi
  if ((${#ADMIN_LISTEN[@]})); then
    where="$(printf '%s; ' "${ADMIN_LISTEN[@]}")"
    warn "snmpd's listening addresses are set elsewhere (${where%; }). The ql810wc-proxy drop-in" \
         "therefore adds none: agentaddress lines add up, and a second bind of UDP 161 makes snmpd" \
         "exit. After the restart the installer checks that snmpd listens on the UDP 161 wildcard."
  fi
}

# ---------------------------------------------------------------------------
# snmpd configuration
# ---------------------------------------------------------------------------
ensure_include() {
  [[ -f "$SNMPD_CONF" ]] || die "$SNMPD_CONF not found — is net-snmp installed? (run step 10)"
  qlp_ensure_dir "$SNMPD_DROPIN_DIR" 0755
  if ((SNMPD_HAS_INCLUDE)); then
    if grep -qF "# BEGIN $QLP_TAG" "$SNMPD_CONF"; then
      qlp_remove_block "$SNMPD_CONF"   # avoid reading our drop-in twice
      SNMPD_CHANGED=1
    fi
    return 0
  fi
  qlp_ensure_block "$SNMPD_CONF" <<<"includeDir $SNMPD_DROPIN_DIR"
  if ((QLP_CHANGED)); then SNMPD_CHANGED=1; fi
}

# ---------------------------------------------------------------------------
# AgentX access for ql810wc-snmp-agent (user and group ql810wc)
# ---------------------------------------------------------------------------
# The numeric id net-snmp makes of an agentXPerms USER or GROUP field
# (netsnmp_str_to_uid/gid): a leading number as such, else the name looked
# up; 0 when that fails (net-snmp then warns and leaves that field unset).
agentx_id() { # user|group name-or-number
  local id=""
  if [[ "$2" =~ ^[0-9]{1,9} ]]; then id=$((10#${BASH_REMATCH[0]})); fi
  if [[ -z "$id" || "$id" == 0 ]]; then
    case "$1" in
      user)  id="$(id -u "$2" 2>/dev/null || true)" ;;
      group) id="$(getent group "$2" 2>/dev/null | cut -d: -f3 || true)" ;;
    esac
  fi
  if [[ "$id" =~ ^[0-9]+$ ]]; then printf '%s' "$((10#$id))"; else printf '0'; fi
}

# A group as SupplementaryGroups= names it: the name when it is a plain one
# that resolves to GID, else the number (GNU stat prints "UNKNOWN" for a gid
# without a name).
agentx_group_word() { # name gid
  if [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,31}$ && "$(agentx_id group "$1")" == "$2" ]]; then
    printf '%s' "$1"
  else
    printf '%s' "$2"
  fi
}

# Can user ql810wc with the groups AGENT_GIDS connect to the socket net-snmp
# creates under AX_*? Connecting to a unix socket needs write permission; the
# owner bits apply to its owner, the group bits to members of its group (even
# when the other bits would allow more), the other bits to everyone else.
# Without a user or group in agentXPerms the socket belongs to root:root.
agentx_socket_reachable() {
  local g quid
  quid="$(agentx_id user "$QLP_USER")"
  if ((AX_USER != 0 && AX_USER == quid)); then ((AX_SOCK & 8#200)); return; fi
  for g in "${AGENT_GIDS[@]}"; do
    if ((AX_GROUP != 0 && AX_GROUP == g)); then ((AX_SOCK & 8#020)); return; fi
  done
  ((AX_SOCK & 8#002))
}

# How ql810wc-snmp-agent will reach the AgentX socket. net-snmp keeps ONE
# agentXPerms setting for the whole agent and takes each of its fields from
# the last line that sets it, so a line of our own would either change the
# socket for the subagents another snmpd configuration set it up for, or be
# overridden by that configuration (the subagent then times out at start with
# EACCES). So: no other agentXPerms -> the drop-in sets "agentXPerms
# $OWN_AGENTX_PERMS". Otherwise the drop-in sets none, and the subagent uses
# what that line grants: write access for others, group ql810wc, or another
# group that it then joins (SupplementaryGroups=; never group root). If it
# grants none of these, or several lines disagree, stop before changing
# anything.
plan_agentx_access() {
  local entry where fields sock dir user group f n=0 gid word="" seen_sock=0 seen_dir=0 conflict=""
  AGENTX_OWN_PERMS=1 AGENTX_PERMS_WHERE="" AGENT_GROUPS=()
  AGENT_GIDS=("$(agentx_id group "$QLP_USER")")
  if ((AGENT_GIDS[0] == 0)); then
    die "group $QLP_USER does not exist (does user $QLP_USER have another primary group?): the AgentX socket" \
        "is opened to that group. Create it (groupadd -r $QLP_USER; usermod -g $QLP_USER $QLP_USER), then" \
        "re-run ./install.sh --only 30."
  fi
  if ((${#AGENTX_PERMS[@]} == 0)); then
    AX_SOCK=$((8#0660)) AX_DIR=$((8#0750)) AX_USER=0 AX_GROUP="${AGENT_GIDS[0]}"
    AGENTX_ACCESS="group $QLP_USER (agentXPerms $OWN_AGENTX_PERMS in the drop-in)"
    return 0
  fi
  AGENTX_OWN_PERMS=0 AX_SOCK=0 AX_DIR=0 AX_USER=0 AX_GROUP=0
  for entry in "${AGENTX_PERMS[@]}"; do
    where="${entry%%$'\t'*}" fields="${entry#*$'\t'}"
    AGENTX_PERMS_WHERE+="${AGENTX_PERMS_WHERE:+; }$where: agentXPerms $fields"
    sock="" dir="" user="" group=""
    read -r sock dir user group _ <<<"$fields"
    # SOCKPERMS and DIRPERMS: octal (strtol base 8), set whenever given.
    if [[ -n "$sock" ]]; then
      f=0
      if [[ "$sock" =~ ^[0-7]{1,6} ]]; then f=$((8#${BASH_REMATCH[0]})); fi
      if ((seen_sock && f != AX_SOCK)); then conflict+=" SOCKPERMS"; fi
      AX_SOCK=$f seen_sock=1
    fi
    if [[ -n "$dir" ]]; then
      f=0
      if [[ "$dir" =~ ^[0-7]{1,6} ]]; then f=$((8#${BASH_REMATCH[0]})); fi
      if ((seen_dir && f != AX_DIR)); then conflict+=" DIRPERMS"; fi
      AX_DIR=$f seen_dir=1
    fi
    # USER and GROUP: only a known, non-root one is set (uid/gid 0 is ignored).
    if [[ -n "$user" ]]; then
      f="$(agentx_id user "$user")"
      if ((f == 0)) && [[ "$user" != root && ! "$user" =~ ^0+$ ]]; then
        warn "$where: agentXPerms names user '$user', which does not exist: net-snmp ignores it"
      fi
      if ((f != 0 && AX_USER != 0 && f != AX_USER)); then conflict+=" USER"; fi
      if ((f != 0)); then AX_USER=$f; fi
    fi
    if [[ -n "$group" ]]; then
      f="$(agentx_id group "$group")"
      if ((f == 0)) && [[ "$group" != root && ! "$group" =~ ^0+$ ]]; then
        warn "$where: agentXPerms names group '$group', which does not exist: net-snmp ignores it"
      fi
      if ((f != 0 && AX_GROUP != 0 && f != AX_GROUP)); then conflict+=" GROUP"; fi
      if ((f != 0)); then AX_GROUP=$f word="$(agentx_group_word "$group" "$f")"; fi
    fi
    n=$((n + 1))
  done
  if [[ -n "$conflict" ]]; then
    die "agentXPerms is set $n times with different values (${conflict# }): $AGENTX_PERMS_WHERE. net-snmp" \
        "keeps one agentXPerms setting and takes each field from the line it reads last, so the result" \
        "depends on the read order. Keep a single agentXPerms line that lets a group use the socket (for" \
        "example 'agentXPerms $OWN_AGENTX_PERMS'; $QLP_UNIT_AGENT joins any other group it names), then" \
        "re-run ./install.sh --only 30."
  fi
  if agentx_socket_reachable; then
    gid="${AGENT_GIDS[0]}"
    if ((AX_GROUP != 0 && AX_GROUP == gid)); then AGENTX_ACCESS="group $QLP_USER"
    elif ((AX_SOCK & 8#002)); then AGENTX_ACCESS="any local user (the socket is writable for others)"
    else AGENTX_ACCESS="its owner $QLP_USER"; fi
  elif ((AX_GROUP != 0)) && ((AX_SOCK & 8#020)); then
    AGENT_GROUPS+=("$word") AGENT_GIDS+=("$AX_GROUP")
    AGENTX_ACCESS="group $word, which $QLP_UNIT_AGENT joins"
  else
    die "agentXPerms ($AGENTX_PERMS_WHERE) does not let $QLP_UNIT_AGENT (user and group $QLP_USER) use the" \
        "AgentX socket, and net-snmp keeps one agentXPerms setting for every subagent, so the installer" \
        "cannot add its own. Let a group write to the socket instead, for example 'agentXPerms" \
        "$OWN_AGENTX_PERMS' or a group of your own (never root; $QLP_UNIT_AGENT then joins it), and re-run" \
        "./install.sh --only 30 — or set SNMP_AGENT_MODE=\"pass_persist\" (degraded) in config.env."
  fi
  log "agentXPerms is set elsewhere ($AGENTX_PERMS_WHERE): the drop-in sets none, and $QLP_UNIT_AGENT" \
      "reaches the AgentX socket as $AGENTX_ACCESS"
}

# Can user ql810wc with the groups AGENT_GIDS enter a directory?
agentx_dir_enterable() { # uid gid mode(number)
  local g quid
  quid="$(agentx_id user "$QLP_USER")"
  if ((quid != 0 && $1 == quid)); then (($3 & 8#100)); return; fi
  for g in "${AGENT_GIDS[@]}"; do
    if (($2 == g)); then (($3 & 8#010)); return; fi
  done
  (($3 & 8#001))
}

# The socket's directory must let ql810wc-snmp-agent through, and snmpd
# applies agentXPerms' directory mode only when it creates the directory, as
# root:root. So:
#   * no directory: create it with that mode plus group r-x, for group
#     ql810wc (or for the socket's group of another snmpd configuration, which
#     the subagent joins, so the subagents of that group get in as well);
#   * a directory the subagent can enter already: leave it;
#   * one that belongs to another (non-root) group: leave it and let the
#     subagent join that group, which needs group x (else stop with a clear
#     message rather than change someone else's directory);
#   * snmpd's own root-group directory (0700 by default), or one of group
#     ql810wc without group x: give group ql810wc r-x, after recording the
#     original owner, group and mode (manifest "attrs") for uninstall.sh.
ensure_agentx_dir() {
  local attrs uid gid mode name m word group="$QLP_USER"
  if ! attrs="$(qlp_path_attrs "$AGENTX_DIR")"; then
    m=$((8#0750))
    if ((AGENTX_OWN_PERMS == 0)); then
      if ((AX_DIR != 0)); then m=$(((AX_DIR & 8#0777) | 8#050)); fi
      if ((${#AGENT_GROUPS[@]})); then group="${AGENT_GROUPS[0]}"; fi
    fi
    qlp_ensure_dir "$AGENTX_DIR" "$(printf '%04o' "$m")" "root:$group"
    return 0
  fi
  read -r uid gid mode name <<<"$attrs"
  m=$((8#$mode))
  if agentx_dir_enterable "$uid" "$gid" "$m"; then
    log "$AGENTX_DIR ($uid:$gid, mode $mode) lets $QLP_UNIT_AGENT reach the AgentX socket"
    adopt_agentx_dir "$uid" "$gid"
  elif ((gid != 0 && gid != AGENT_GIDS[0])); then
    if (((m & 8#010) == 0)); then
      die "$AGENTX_DIR is owned by $uid:$name with mode $mode: neither its group nor others may enter it," \
          "so $QLP_UNIT_AGENT cannot reach the AgentX socket. Let group $name enter it (chmod g+x" \
          "$AGENTX_DIR; $QLP_UNIT_AGENT then joins that group) or allow others (chmod o+x), and re-run" \
          "./install.sh --only 30."
    fi
    word="$(agentx_group_word "$name" "$gid")"
    AGENT_GROUPS+=("$word") AGENT_GIDS+=("$gid")
    # As a member of that group the subagent may fall under the socket's group bits.
    if ! agentx_socket_reachable; then
      die "$QLP_UNIT_AGENT would join group $word to enter $AGENTX_DIR, but members of that group may not" \
          "use the AgentX socket (agentXPerms: ${AGENTX_PERMS_WHERE:-$OWN_AGENTX_PERMS}; the group bits of" \
          "a socket apply to every member of its group). Give the socket and $AGENTX_DIR one group that may" \
          "use both, and re-run ./install.sh --only 30."
    fi
    log "$AGENTX_DIR belongs to group $word (mode $mode): $QLP_UNIT_AGENT joins that group" \
        "instead of the installer changing the directory"
  else
    # The pristine original is recorded once; a re-run finds the directory adjusted.
    if ! { [[ -f "$QLP_MANIFEST" ]] && awk -F'\t' -v p="$AGENTX_DIR" '$1 == "attrs" && $2 == p { f = 1 }
                                                                     END { exit !f }' "$QLP_MANIFEST"; }; then
      qlp_manifest_add attrs "$AGENTX_DIR" "$uid:$gid" "$mode"
    fi
    run chgrp "$QLP_USER" "$AGENTX_DIR"
    run chmod g+rx "$AGENTX_DIR"
    log "adjusted $AGENTX_DIR (was $uid:$gid, mode $mode; now group $QLP_USER with g+rx) so $QLP_UNIT_AGENT" \
        "can reach the AgentX socket; uninstall.sh restores it"
  fi
}

# A root-owned directory of group ql810wc that the manifest knows nothing
# about: only this installer gives anything that group, so an earlier install
# created it, and an uninstall kept it because it still held snmpd's stale
# AgentX socket (early uninstall.sh versions did not remove that socket).
# Record it again, so that the next uninstall removes it.
adopt_agentx_dir() { # uid gid
  (($1 == 0 && $2 != 0 && $2 == AGENT_GIDS[0])) || return 0
  if [[ -f "$QLP_MANIFEST" ]] && awk -F'\t' -v p="$AGENTX_DIR" '($1 == "created" || $1 == "attrs") && $2 == p { f = 1 }
                                                                END { exit !f }' "$QLP_MANIFEST"; then
    return 0
  fi
  qlp_manifest_add created "$AGENTX_DIR"
  log "$AGENTX_DIR (group $QLP_USER) was left by an earlier install: recorded again, so uninstall.sh removes it"
}

# pass_persist children run inside snmpd_t, which may read snmpd_var_run_t but
# not the generic var_run_t of /run/ql810wc-proxy (state.json).
configure_run_label() {
  if [[ "$SNMP_AGENT_MODE" == pass_persist ]]; then
    qlp_ensure_fcontext "$RUN_FCONTEXT_TYPE" "$RUN_FCONTEXT_REGEX"
  elif qlp_manifest_has "$(printf 'fcontext\t%s\t%s' "$RUN_FCONTEXT_TYPE" "$RUN_FCONTEXT_REGEX")"; then
    qlp_remove_fcontext "$RUN_FCONTEXT_TYPE" "$RUN_FCONTEXT_REGEX"
  else
    return 0
  fi
  if [[ -d "$QLP_RUN_DIR" ]]; then qlp_restorecon "$QLP_RUN_DIR"; fi
}

# The drop-in's listening-address section. snmpd listens on the UDP 161
# wildcard as long as nothing names an address, and every agentaddress line
# (and every address on its command line) ADDS to the others: binding UDP 161 a
# second time makes snmpd exit. So the drop-in names addresses only when IPv6
# sources need udp6 and nothing else names any.
agent_address_section() {
  if ((${#ADMIN_LISTEN[@]})); then
    cat <<'EOF'
# None here: another snmpd configuration already sets the listening addresses
# (agentaddress, or addresses on the snmpd command line), and agentaddress
# lines add up: binding UDP 161 twice makes snmpd exit. The installer checks
# after each start that snmpd still listens on the UDP 161 wildcard.
EOF
  elif ((NEED_UDP6)); then
    cat <<'EOF'
# IPv6 sources are allowed, so snmpd listens on UDP 161 over IPv6 as well. Any
# agentaddress replaces snmpd's implicit default (the IPv4 wildcard), so that
# is listed again. Written only while no other snmpd configuration sets
# listening addresses: agentaddress lines add up, and binding UDP 161 twice
# makes snmpd exit.
agentaddress udp:161,udp6:161
EOF
  else
    cat <<'EOF'
# None: without any agentaddress, snmpd listens on the UDP 161 wildcard of
# every local IPv4 address. agentaddress lines add up and binding UDP 161
# twice makes snmpd exit, so that default is not repeated here.
EOF
  fi
}

# The drop-in's agentXPerms section (agentx mode): our own line unless another
# snmpd configuration sets agentXPerms (plan_agentx_access).
agentx_perms_section() {
  if ((AGENTX_OWN_PERMS)); then
    printf 'agentXPerms %s' "$OWN_AGENTX_PERMS"
    return 0
  fi
  printf '%s\n' \
    "# No agentXPerms here: another snmpd configuration sets it ($AGENTX_PERMS_WHERE)," \
    "# and net-snmp keeps a single agentXPerms setting for every subagent, so a" \
    "# second line would change that socket for the subagents relying on it, or be" \
    "# overridden by it. $QLP_UNIT_AGENT reaches the socket as ${AGENTX_ACCESS:-configured there}."
}

render_dropin() {
  local addr sources transport scope access host text fam cidr c directive oid seen
  local -a views=() com=()
  sources=$'ipv4\t127.0.0.1/32'
  if ((NEED_UDP6)); then sources+=$'\nipv6\t::1/128'; fi
  addr="$(agent_address_section)"
  if [[ -n "$PRINTER_CIDRS" ]]; then sources+=$'\n'"$PRINTER_CIDRS"; fi
  for c in "${COMMUNITIES[@]}"; do
    seen=" "
    while IFS=$'\t' read -r fam cidr; do
      [[ -n "$fam" && "$seen" != *" $cidr "* ]] || continue
      seen+="$cidr "
      directive=com2sec
      if [[ "$fam" == ipv6 ]]; then directive=com2sec6; fi
      if [[ "$SNMP_AGENT_MODE" == agentx ]]; then
        com+=("$directive -Cn $PERSONA_CONTEXT $PERSONA_SECNAME $cidr $c")
      else
        com+=("$directive $PERSONA_SECNAME $cidr $c")
      fi
    done <<<"$sources"
  done

  if [[ "$SNMP_AGENT_MODE" == agentx ]]; then
    for oid in "${VIEW_AGENTX[@]}"; do views+=("view   $PRINTER_VIEW included $oid"); done
    transport="# AgentX master: $QLP_UNIT_AGENT registers the printer persona in SNMP
# context \"$PERSONA_CONTEXT\" over this unix socket, which only root and those
# agentXPerms admits (normally group $QLP_USER) may use.
# agentXSocket repeats net-snmp's default path on purpose: it pins the master
# to the socket that the subagent connects to
# (its unit passes --socket $AGENTX_SOCKET) and that agentXPerms protects,
# whatever default this snmpd build has. agentXSocket is last-wins, so the
# installer warns about any other snmpd configuration that sets a different
# one. AgentX over TCP is not used: it has no authentication.
master agentx
agentXSocket $AGENTX_SOCKET
$(agentx_perms_section)"
    scope="# SNMP context \"$PERSONA_CONTEXT\" holds nothing but the persona (registered
# there by $QLP_UNIT_AGENT), so this host's own data stays invisible to them."
    access="access $PERSONA_SECNAME $PERSONA_CONTEXT any noauth exact $PRINTER_VIEW none none"
  else
    for oid in "${VIEW_PASS_PERSIST[@]}"; do views+=("view   $PRINTER_VIEW included $oid"); done
    transport="# Degraded fallback: two pass_persist children inside snmpd answer the
# Brother and Printer-MIB subtrees in the default context from the proxy's
# state.json (they never touch USB). There is no system/interfaces/host-resources
# persona in this mode.
pass_persist .1.3.6.1.4.1.2435 $QLP_PYTHON -I -B $AGENT_SCRIPT --mode pass_persist
pass_persist .1.3.6.1.2.1.43 $QLP_PYTHON -I -B $AGENT_SCRIPT --mode pass_persist"
    scope="# the Brother and Printer-MIB subtrees of the default context, which the
# pass_persist children answer for the printer."
    access="access $PERSONA_SECNAME \"\" any noauth exact $PRINTER_VIEW none none"
  fi

  if ((V3_ENABLED)); then
    host="# SNMPv3 user \"$SNMP_V3_USER\" (USM, authPriv): the whole tree in every context, i.e.
# the default context (this host's own MIBs) and \"$PERSONA_CONTEXT\" (the persona: -n printer).
# rouser without a CONTEXT field matches all contexts (snmpd.conf(5)). Its keys are
# in $QLP_SNMP_PERSIST (createUser, written by the installer); net-snmp cannot
# limit USM users by source, so the firewall does (SNMP_V3_ALLOWED_CIDRS).
view   $HOST_VIEW included .1
rouser -s usm $SNMP_V3_USER priv -V $HOST_VIEW"
  else
    host="# Disabled (SNMP_V3_ENABLE=${SNMP_V3_ENABLE:-auto}; with \"auto\", set both SNMPv3 passphrases)."
  fi

  text="$(qlp_render "$SNMPD_TEMPLATE" \
    "AGENT_ADDRESS=$addr" "AGENT_MODE=$SNMP_AGENT_MODE" "PERSONA_TRANSPORT=$transport" \
    "PERSONA_SCOPE=$scope" "PRINTER_VIEW_LINES=$(printf '%s\n' "${views[@]}")" \
    "COM2SEC=$(printf '%s\n' "${com[@]}")" "SEC_NAME=$PERSONA_SECNAME" \
    "PERSONA_ACCESS=$access" "HOST_ACCESS=$host")"
  qlp_write_file "$SNMPD_DROPIN" 0600 <<<"$text"
  if ((QLP_CHANGED)); then SNMPD_CHANGED=1; fi
}

# ---------------------------------------------------------------------------
# SNMPv3 user (keys in net-snmp's persistent file)
# ---------------------------------------------------------------------------
# sha256 over a salt and the v3 settings; secrets travel through stdin only.
v3_digest() { # salt
  printf '%s\0' "$1" "$SNMP_V3_USER" "$SNMP_V3_AUTH_PROTO" "$SNMP_V3_AUTH_PASS" \
                "$SNMP_V3_PRIV_PROTO" "$SNMP_V3_PRIV_PASS" \
    | python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

# Quote a value the way net-snmp's config reader unquotes it.
snmp_quote() { # value
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

stop_snmpd_for_edit() {
  if ((SNMPD_STOPPED == 0)) && systemctl is-active --quiet "$SNMPD_UNIT"; then
    log "stopping snmpd: it rewrites $QLP_SNMP_PERSIST from memory when it stops"
    run systemctl stop "$SNMPD_UNIT"
    SNMPD_STOPPED=1
  fi
}

# If this run stopped snmpd and then failed, do not leave SNMP down.
restart_snmpd_on_exit() {
  if ((SNMPD_STOPPED)); then
    warn "starting snmpd again after an interrupted run"
    systemctl start "$SNMPD_UNIT" || error "could not start snmpd — see: journalctl -u snmpd"
  fi
}

manage_v3_user() {
  local m_user="" m_salt="" m_digest="" salt persist_dir="${QLP_SNMP_PERSIST%/*}"
  if [[ -f "$V3_MARKER" ]]; then
    IFS=$'\t' read -r m_user m_salt m_digest <"$V3_MARKER" || true
  fi
  if ((V3_ENABLED == 0)); then
    if [[ -n "$m_user" ]]; then
      stop_snmpd_for_edit
      qlp_usm_remove_user "$m_user"
      rm -f -- "$V3_MARKER"
      qlp_manifest_del usmuser "$m_user"
      SNMPD_CHANGED=1
      log "SNMPv3 is off: removed the SNMPv3 user '$m_user' this installer had created"
    fi
    return 0
  fi
  if [[ "$m_user" == "$SNMP_V3_USER" && -n "$m_salt" ]] && [[ "$(v3_digest "$m_salt")" == "$m_digest" ]] \
     && qlp_usm_has_user "$SNMP_V3_USER"; then
    log "SNMPv3 user '$SNMP_V3_USER' is up to date"
    return 0
  fi
  if [[ "$m_user" != "$SNMP_V3_USER" ]] && qlp_usm_has_user "$SNMP_V3_USER"; then
    warn "SNMPv3 user '$SNMP_V3_USER' already exists in $QLP_SNMP_PERSIST but was not created by this" \
         "installer; no snmpd configuration uses it, so it is replaced"
  fi
  stop_snmpd_for_edit
  if [[ -n "$m_user" && "$m_user" != "$SNMP_V3_USER" ]]; then
    qlp_usm_remove_user "$m_user"
    qlp_manifest_del usmuser "$m_user"
  fi
  qlp_usm_remove_user "$SNMP_V3_USER"
  if [[ ! -d "$persist_dir" ]]; then install -d -m 0700 "$persist_dir"; fi
  if [[ -s "$QLP_SNMP_PERSIST" && -n "$(tail -c 1 "$QLP_SNMP_PERSIST")" ]]; then
    printf '\n' >>"$QLP_SNMP_PERSIST"
  fi
  # printf is a shell builtin: the passphrases never appear on a command line.
  ( umask 077
    printf 'createUser %s %s %s %s %s\n' "$SNMP_V3_USER" \
      "$SNMP_V3_AUTH_PROTO" "$(snmp_quote "$SNMP_V3_AUTH_PASS")" \
      "$SNMP_V3_PRIV_PROTO" "$(snmp_quote "$SNMP_V3_PRIV_PASS")" >>"$QLP_SNMP_PERSIST" )
  chmod 0600 "$QLP_SNMP_PERSIST"
  qlp_restorecon "$persist_dir"
  salt="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  install -d -m 0750 "$QLP_STATE_DIR"
  ( umask 077; printf '%s\t%s\t%s\n' "$SNMP_V3_USER" "$salt" "$(v3_digest "$salt")" >"$V3_MARKER" )
  qlp_manifest_add usmuser "$SNMP_V3_USER"
  SNMPD_CHANGED=1
  ok "SNMPv3 user '$SNMP_V3_USER' set up ($SNMP_V3_AUTH_PROTO / $SNMP_V3_PRIV_PROTO, authPriv)"
}

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
start_snmpd() {
  local since lines line
  since="$(date +%s)"
  if ((SNMPD_CHANGED)) && systemctl is-active --quiet "$SNMPD_UNIT"; then
    log "snmpd configuration changed — restarting snmpd (never reloading it)"
    run systemctl restart "$SNMPD_UNIT"
  fi
  qlp_enable_now "$SNMPD_UNIT"
  SNMPD_STOPPED=0
  qlp_require_active "$SNMPD_UNIT"
  # net-snmp starts even with bad lines; it only logs them. Surface those.
  have journalctl || return 0
  sleep 1
  lines="$(journalctl -u "$SNMPD_UNIT" --since "@$since" --no-pager -o cat 2>/dev/null \
           | grep -F "$SNMPD_DROPIN" || true)"
  if [[ -n "$lines" ]]; then
    warn "snmpd reported problems with $SNMPD_DROPIN:"
    while IFS= read -r line; do warn "  $line"; done <<<"$lines"
  fi
}

# pass_persist children load their code and settings once (qlp_passpersist_*
# in common.sh): when those changed, snmpd needs a restart as well.
check_passpersist_inputs() {
  if qlp_passpersist_stale; then
    log "the pass_persist persona's code or settings changed since snmpd last started"
    SNMPD_CHANGED=1
  fi
}

# snmpd must listen on the UDP 161 wildcard: SNMP broadcast discovery (Brother
# driver installer, BRAdmin) reaches nothing else, and an agentaddress set
# elsewhere may have narrowed it. Fails clearly if the IPv4 wildcard is
# missing; warns if IPv6 sources are allowed but nothing listens on udp6.
verify_snmp_listeners() {
  local out fam owner v4="" v6="" tries where="" hint
  if ! have ss; then
    warn "ss (iproute) not found — cannot check that snmpd listens on the UDP 161 wildcard"
    return 0
  fi
  for ((tries = 1; ; tries++)); do
    out="$(qlp_snmp_wildcard_listeners)"
    v4="" v6=""
    while read -r fam owner; do
      case "$fam" in
        ipv4) if [[ -z "$v4" || "$owner" == snmpd ]]; then v4="$owner"; fi ;;
        ipv6) if [[ -z "$v6" || "$owner" == snmpd ]]; then v6="$owner"; fi ;;
      esac
    done <<<"$out"
    if [[ -n "$v4" ]] && { ((NEED_UDP6 == 0)) || [[ -n "$v6" ]]; }; then break; fi
    if ((tries >= SNMP_LISTEN_TRIES)); then break; fi
    sleep 1
  done
  if ((${#ADMIN_LISTEN[@]})); then
    where="$(printf '%s; ' "${ADMIN_LISTEN[@]}")"
    where="${where%; }"
    hint="its listening addresses come from: $where. Add udp:161 to that setting (or remove it), then re-run ./install.sh --only 30"
  else
    hint="check 'systemctl status $SNMPD_UNIT' and 'ss -ulnp sport = :161'"
  fi
  if [[ -z "$v4" ]]; then
    die "snmpd does not listen on the UDP 161 wildcard (0.0.0.0:161), which SNMP broadcast discovery" \
        "(Brother driver installer, BRAdmin) needs: $hint"
  fi
  if [[ "$v4" != snmpd && "$v4" != "?" ]]; then
    die "the UDP 161 wildcard belongs to '$v4', not snmpd: stop that program (or move it to another port), then re-run ./install.sh --only 30"
  fi
  if ((NEED_UDP6)) && [[ -z "$v6" ]]; then
    if ((${#ADMIN_LISTEN[@]})); then hint="add udp6:161 to the listening addresses set in: $where"
    else hint="check 'systemctl status $SNMPD_UNIT'"; fi
    warn "IPv6 sources are allowed in SNMP_*_ALLOWED_CIDRS, but snmpd does not listen on UDP 161 over" \
         "IPv6, so they get no answer: $hint"
  fi
  if [[ -n "$v6" ]]; then ok "snmpd listens on the UDP 161 wildcard (IPv4 and IPv6)"
  else ok "snmpd listens on the UDP 161 wildcard (IPv4)"; fi
}

# The groups ql810wc-snmp-agent joins for the AgentX socket or its directory
# (AGENT_GROUPS), as a drop-in of its unit; without any, no drop-in. Sets
# AGENT_DROPIN_CHANGED.
configure_agent_groups() {
  local dir="$QLP_SYSTEMD_DIR/$QLP_UNIT_AGENT.d"
  local path="$dir/$AGENT_GROUPS_DROPIN"
  AGENT_DROPIN_CHANGED=0
  if ((${#AGENT_GROUPS[@]} == 0)); then
    qlp_remove_created "$path"
    AGENT_DROPIN_CHANGED=$QLP_CHANGED
    qlp_remove_created_dir "$dir"
    return 0
  fi
  qlp_ensure_dir "$dir" 0755
  qlp_write_file "$path" 0644 <<EOF
# $path
# Installed by $QLP_NAME (scripts/30-snmp-agent.sh); removed by uninstall.sh.
# The AgentX socket or its directory $AGENTX_DIR belongs to a group of this
# host's own snmpd setup, so the subagent joins that group instead of the
# installer changing them (agentXPerms: ${AGENTX_PERMS_WHERE:-$OWN_AGENTX_PERMS}).
[Service]
SupplementaryGroups=${AGENT_GROUPS[*]}
EOF
  AGENT_DROPIN_CHANGED=$QLP_CHANGED
}

configure_agent_unit() {
  local text changed was_active=0 unit_path="$QLP_SYSTEMD_DIR/$QLP_UNIT_AGENT"
  if [[ "$SNMP_AGENT_MODE" == pass_persist ]]; then
    if systemctl is-enabled --quiet "$QLP_UNIT_AGENT" 2>/dev/null \
       || systemctl is-active --quiet "$QLP_UNIT_AGENT" 2>/dev/null; then
      run systemctl disable --now "$QLP_UNIT_AGENT"
    fi
    qlp_manifest_del unit "$QLP_UNIT_AGENT"
    qlp_remove_created "$unit_path"
    changed=$QLP_CHANGED
    AGENT_GROUPS=()
    configure_agent_groups
    if ((changed || AGENT_DROPIN_CHANGED)); then qlp_daemon_reload; fi
    return 0
  fi
  text="$(qlp_render_unit "$QLP_REPO_ROOT/agent/systemd/$QLP_UNIT_AGENT")"
  qlp_write_file "$unit_path" 0644 <<<"$text"
  changed=$QLP_CHANGED
  configure_agent_groups
  if ((AGENT_DROPIN_CHANGED)); then changed=1; fi
  if ((changed)); then qlp_daemon_reload; fi
  if systemctl is-active --quiet "$QLP_UNIT_AGENT"; then was_active=1; fi
  qlp_enable_now "$QLP_UNIT_AGENT"
  # The subagent re-registers by itself after an snmpd restart. A new unit file
  # or groups drop-in needs a new process; new settings or a new identity are
  # applied in its running AgentX session (reload), without the SNMP gap of a
  # restart (snmpd answers noSuchName until the agent has registered again).
  if ((was_active)); then
    if ((changed)); then
      run systemctl restart "$QLP_UNIT_AGENT"
    elif ((QLP_RUNTIME_CHANGED || QLP_IDENTITY_CHANGED)); then
      qlp_try_reload "$QLP_UNIT_AGENT"
    fi
  fi
  qlp_require_active "$QLP_UNIT_AGENT"
}

# ---------------------------------------------------------------------------
# Firewall (UDP 161, source-scoped only)
# ---------------------------------------------------------------------------
configure_firewall() {
  local zone fam cidr all="$PRINTER_CIDRS" seen=" "
  local -a rules=()
  if ! qlp_fw_active; then
    warn "firewalld is not active — UDP 161 is not managed (allow it from your client ranges if needed)"
    return 0
  fi
  zone="$(qlp_fw_zone)"
  if ((V3_ENABLED)) && [[ -n "$V3_CIDRS" ]]; then all+=$'\n'"$V3_CIDRS"; fi
  while IFS=$'\t' read -r fam cidr; do
    [[ -n "$fam" && "$seen" != *" $cidr "* ]] || continue
    seen+="$cidr "
    if qlp_cidr_is_loopback "$cidr"; then continue; fi
    rules+=("$(qlp_fw_rich_rule "$fam" "$cidr" 161 udp)")
  done <<<"$all"
  qlp_fw_sync_rich "$zone" "$SNMP_RULE_SELECTOR" "${rules[@]}"
  ok "firewall: UDP 161 open in zone '$zone' for ${#rules[@]} source range(s)"
}

# One read of the 1284 device ID through the persona, as clients do.
probe_persona() {
  local out
  have snmpget || return 0
  for _ in {1..10}; do
    out="$(snmpget -v2c -c "${COMMUNITIES[0]}" -t 1 -r 0 -On -Oq 127.0.0.1 "$PROBE_OID" 2>/dev/null || true)"
    if [[ -n "$out" && "$out" != *"No Such"* ]]; then
      ok "the printer persona answers on 127.0.0.1 (1284 device ID)"
      return 0
    fi
    sleep 1
  done
  warn "the printer persona did not answer a test query yet — check 'systemctl status $SNMPD_UNIT" \
       "$QLP_UNIT_AGENT'; ./selftest.sh checks SNMP in detail"
}

main() {
  qlp_init 30-snmp-agent
  require_root
  qlp_load_config
  require_cmd python3 systemctl snmpd stat
  validate_config
  [[ -f "$AGENT_SCRIPT" ]] || die "$AGENT_SCRIPT is missing — run step 20 first (./install.sh --only 20)"
  qlp_ensure_service_user
  qlp_ensure_identity
  qlp_sync_runtime_env
  check_snmpd_conflicts
  # AgentX access first: both may stop the step, before anything else changes.
  if [[ "$SNMP_AGENT_MODE" == agentx ]]; then
    plan_agentx_access
    ensure_agentx_dir
  fi
  # ql810wc-proxyd first, as in steps 20 and 40: a job in flight gets to finish
  # (it waits for that) before snmpd or the agent restarts and SNMP pauses.
  if ((QLP_RUNTIME_CHANGED || QLP_IDENTITY_CHANGED)); then qlp_try_restart "$QLP_UNIT_PROXYD"; fi
  trap restart_snmpd_on_exit EXIT
  ensure_include
  configure_run_label
  render_dropin
  manage_v3_user
  check_passpersist_inputs
  start_snmpd
  qlp_passpersist_record
  verify_snmp_listeners
  configure_agent_unit
  configure_firewall
  probe_persona
  local v3_state="off"
  if ((V3_ENABLED)); then v3_state="on (user $SNMP_V3_USER)"; fi
  ok "control plane ready: snmpd on UDP 161, persona via $SNMP_AGENT_MODE," \
     "${#COMMUNITIES[@]} read-only community(ies), SNMPv3 $v3_state"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
