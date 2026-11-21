#!/usr/bin/env bash
# selftest.sh — check the installed ql810wc-print-proxy end to end and report
# the capability tier it reaches: T0 printing over TCP 9100, T1 black/red, T2
# discovery (SNMP persona + Bonjour), T3 live media report, plus the bonus
# host monitoring over SNMPv3. Read-only: it changes nothing on the system
# (--print-test prints one label).
#
#   sudo ./selftest.sh                      every check (install.sh runs this)
#   sudo ./selftest.sh --print-test         also print a black/red test label and
#                                           ask you whether it came out right
#   sudo ./selftest.sh --print-test --mono  a black-only test label instead (for
#                                           single-colour continuous rolls only)
#   ./selftest.sh --json                    JSON summary on stdout (report on stderr)
#   ./selftest.sh --quiet                   only problems and the verdict
#   ./selftest.sh --no-color                no colours (also when NO_COLOR is set)
#
# Run it as root. As another user the firewall rules and the SELinux audit log
# are not checked (they need root), nor SNMPv3 while config.env is root-only;
# the report says so.
# The report ends with "TIER REACHED: NONE|T0|T1|T2|T3", plus " (+host SNMP)"
# when SNMPv3 host monitoring works too. The tiers build on each other; T1 is
# verified only while a black/red roll (DK-22251) is loaded, and T2 only while
# the allowed client ranges admit real clients (not the template's example).
# Exit codes: 0 tier T0 (printing) reached, 1 not reached, 64 usage error.
set -euo pipefail

usage() { # [exit-code]   (prints the comment block above)
  awk 'NR == 1 { next } /^# shellcheck/ || !/^#/ { exit } { sub(/^# ?/, ""); print }' "$0"
  exit "${1:-0}"
}
usage_error() { printf 'selftest.sh: %s (see --help)\n' "$*" >&2; exit 64; }

JSON=0 QUIET=0 PRINT_TEST=0 MONO=0
while (($#)); do
  case "$1" in
    --json)                 JSON=1 ;;
    -q|--quiet)             QUIET=1 ;;
    --print-test)           PRINT_TEST=1 ;;
    --mono)                 MONO=1 ;;
    --no-color|--no-colour) NO_COLOR=1; export NO_COLOR ;;
    -h|--help)              usage 0 ;;
    *)                      usage_error "unknown option: $1" ;;
  esac
  shift
done
if ((MONO && !PRINT_TEST)); then usage_error "--mono only goes with --print-test"; fi

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib/common.sh"
qlp_init selftest

# qlp_init's ERR trap logs every failing command, including those inside
# command substitutions (errtrace), and a self-test runs many probes that are
# expected to fail. Record the last failure silently instead: on_exit reports
# it if the script stops before its verdict.
ERR_AT=""
trap 'ERR_AT="${BASH_SOURCE[0]##*/}:${LINENO}: ${BASH_COMMAND}"' ERR
# Messages of common.sh helpers are written into files here: keep them plain.
# shellcheck disable=SC2034  # read by common.sh's logger
_QLP_RED='' _QLP_YEL='' _QLP_GRN='' _QLP_BLU='' _QLP_OFF=''

# The report goes to fd 3: stdout, or stderr with --json (stdout is the JSON).
if ((JSON)); then exec 3>&2; else exec 3>&1; fi
if [[ -z "${NO_COLOR:-}" && -t 3 ]]; then
  C_RED=$'\e[31m' C_YEL=$'\e[33m' C_GRN=$'\e[32m' C_BOLD=$'\e[1m' C_OFF=$'\e[0m'
else
  C_RED='' C_YEL='' C_GRN='' C_BOLD='' C_OFF=''
fi

# ---------------------------------------------------------------------------
# Constants (paths as the install steps create them)
# ---------------------------------------------------------------------------
readonly MODEL="QL-810W"                       # the persona's model (MDL, Bonjour usb_MDL)
readonly CPU_SAMPLE_SECONDS=5 CPU_LIMIT_PERCENT=20
readonly WARMUP_SECONDS=15                     # a just-started ql810wc-proxyd may not have the printer yet
readonly ANSWER_TIMEOUT_SECONDS=600
readonly SNMP_HOST="127.0.0.1"                 # always allowed by the snmpd drop-in, for this test
readonly OID_1284=".1.3.6.1.4.1.2435.2.3.9.1.1.7.0"
readonly OID_STATUS=".1.3.6.1.4.1.2435.3.3.9.1.6.1.0"
readonly OID_SYSDESCR=".1.3.6.1.2.1.1.1.0"
# The Brother Windows driver installer's network search: one SNMPv1 GET of
# these 5 OIDs, all-or-nothing (oid-map.yaml clients: win-installer).
readonly WIN_SEARCH=(.1.3.6.1.2.1.1.1.0 .1.3.6.1.2.1.1.5.0 .1.3.6.1.2.1.2.2.1.6.1 "$OID_1284"
                     .1.3.6.1.4.1.2435.2.4.3.1240.1.3.0)
# This host's own data, which v1/v2c communities must never see: hrStorage,
# hrSWRun and UCD-SNMP-MIB.
readonly HOST_ONLY_SUBTREES=(.1.3.6.1.2.1.25.2 .1.3.6.1.2.1.25.4 .1.3.6.1.4.1.2021)
readonly QLCTL="$QLP_LIB_DIR/qlctl"
readonly AGENT_SCRIPT="$QLP_LIB_DIR/brother_snmp_agent.py"
readonly OID_MAP="$QLP_LIB_DIR/oid-map.yaml"
readonly IPP_USB_QUIRK="$QLP_IPP_USB_QUIRK"           # the installed paths: scripts/lib/common.sh
readonly CUPS_USB_QUIRK_DIR="$QLP_CUPS_USB_QUIRK_DIR"
readonly CUPS_USB_QUIRK="$QLP_CUPS_USB_QUIRK"
readonly SNMPD_UNIT="snmpd.service"
readonly SNMPD_DROPIN="$QLP_SNMPD_DROPIN"
readonly AVAHI_UNIT="avahi-daemon.service"
readonly AVAHI_SERVICE="$QLP_AVAHI_SERVICE"
readonly MDNS_TYPE="_pdl-datastream._tcp"
REPO_VERSION="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$QLP_REPO_ROOT/agent/ql810wc/__init__.py" 2>/dev/null || true)"

# ---------------------------------------------------------------------------
# State of this run
# ---------------------------------------------------------------------------
IS_ROOT=0
if [[ "$(id -u 2>/dev/null || true)" == 0 ]]; then IS_ROOT=1; fi
PY="$QLP_PYTHON"                 # the daemons' interpreter (python3-pyyaml is installed for it)
if [[ ! -x "$PY" ]]; then PY="$(command -v python3 || true)"; fi
TMPD="" RECORDS="" JSON_FILE="" VERDICT_DONE=0
CONFIG_FILE="" CONFIG_READABLE=0 CONFIG_WHY="" INSTALLED=0
PORT=9100 POLL=3
COMMUNITIES=()
SNMPD_UP=0 SNMP_OK=0 T0_READY=0
FW_READY="" FW_ZONE=""
ANSWER=""
SNMP_OUT="" SNMP_ERR="" SNMP_CONF_DIR="" SNMP_PERSIST_DIR=""
declare -A ST=() T3R=() OM=() AV=() RT=()

# ---------------------------------------------------------------------------
# Embedded helpers. tests/test_selftest_static.py runs them directly.
# ---------------------------------------------------------------------------

# qlctl "status --json" on stdin -> "key<TAB>value" lines describing the state
# ql810wc-proxyd last wrote. argv: <lib dir> <STATUS_POLL_SECONDS>.
PY_STATE="$(cat <<'PY'
import json
import math
import sys
import time

sys.path.insert(0, sys.argv[1])
from ql810wc import media, rasterproto  # noqa: E402

LIMIT = 3.0 * float(sys.argv[2])


def text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return "%.1f" % value
    return " ".join(str(value).split())


def emit(key, value):
    print("%s\t%s" % (key, text(value)))


def get(node, dotted):
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def ago(seconds):
    if seconds is None:
        return "?"
    s = int(max(0.0, seconds))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    if s < 86400:
        return "%dh%02dm" % (s // 3600, s % 3600 // 60)
    return "%dd%02dh" % (s // 86400, s % 86400 // 3600)


def status(state):
    raw = get(state, "status.raw_hex")
    if not isinstance(raw, str) or not raw.strip():
        emit("status_valid", False)
        emit("status_problem", "no status block read from the printer yet")
        return
    try:
        data = bytes.fromhex("".join(raw.split()))
    except ValueError:
        data = b""
    emit("status_header_mismatch", len(data) == rasterproto.STATUS_LEN
         and data[:3] != rasterproto.STATUS_HEADER)
    try:
        frame = rasterproto.Status.from_hex(raw)
    except ValueError as exc:
        emit("status_valid", False)
        emit("status_problem", "state.json holds an invalid status block (%s)" % exc)
        return
    emit("status_valid", True)
    emit("status_hex", frame.raw.hex())
    emit("status_hex_spaced", frame.raw.hex(" "))
    emit("series_code", "%02x" % frame.series_code)
    emit("model_code", "%02x" % frame.model_code)
    emit("model_name", frame.model_name)
    emit("status_type_name", rasterproto.STATUS_TYPES.get(frame.status_type, "type 0x%02x" % frame.status_type))
    emit("phase_name", rasterproto.PHASE_TYPES.get(frame.phase_type, "phase 0x%02x" % frame.phase_type))
    emit("errors", "; ".join(frame.errors()))
    emit("has_media", frame.has_media)
    emit("two_color_bit", frame.two_color)
    emit("media_type_raw", "%02x" % frame.media_type_raw)
    found = media.describe(frame) if frame.has_media else None
    emit("media_known", found is not None)
    if found is not None:
        emit("media_desc", found.description)
        emit("media_label", found.label_id)
        emit("media_two_color", found.needs_two_color)
    elif frame.has_media:
        emit("media_desc", "unsupported roll (width %d mm, type 0x%02x, length %d mm)"
             % (frame.media_width_mm, frame.media_type_raw, frame.media_length_mm))
        emit("media_two_color", False)
    else:
        emit("media_desc", "no roll")
        emit("media_two_color", False)
    # What qlctl test-print accepts (its _check_roll): continuous tape, colour
    # mode matching byte 25 of the loaded roll.
    lenient = media.describe(frame, exact=False) if frame.has_media else None
    if not frame.has_media:
        roll = "none"
    elif lenient is None:
        roll = "unsupported"
    elif not lenient.is_continuous:
        roll = "die-cut"
    elif frame.two_color:
        roll = "two-color"
    else:
        roll = "mono"
    emit("print_roll", roll)
    emit("print_roll_desc", lenient.description if lenient is not None else
         ("no roll" if not frame.has_media else "an unsupported roll"))


def main():
    raw = sys.stdin.read()
    try:
        doc = json.loads(raw) if raw.strip() else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        emit("available", False)
        emit("problem", "qlctl printed no status document")
        return
    state = doc.get("state")
    if doc.get("available") is not True or not isinstance(state, dict):
        emit("available", False)
        emit("problem", "no readable state at %s: is ql810wc-proxyd running?" % (doc.get("state_file") or "?"))
        return
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else {}
    now = time.time()
    emit("available", True)
    emit("state_file", doc.get("state_file"))
    updated_age = number(doc.get("age_seconds"))
    emit("updated_age", updated_age)
    emit("fresh_limit", LIMIT)
    emit("fresh", updated_age is not None and updated_age <= LIMIT)
    proxy = state.get("proxy") if isinstance(state.get("proxy"), dict) else {}
    pid = proxy.get("pid")
    emit("proxy_stopped", "pid" in proxy and pid is None)
    emit("proxy_pid", pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None)
    emit("proxy_version", proxy.get("version"))
    started = number(proxy.get("started"))
    emit("proxy_age", now - started if started is not None else None)
    emit("proxy_uptime", ago(now - started) if started is not None else "")
    for key in ("state", "detail", "vid", "pid", "product", "manufacturer", "transport", "node", "firmware"):
        emit("device_" + key, get(state, "device." + key))
    mdl = ""
    ieee = get(state, "device.ieee1284_id")
    if isinstance(ieee, str):
        for field in ieee.split(";"):
            name, _, value = field.partition(":")
            if name.strip().upper() in ("MDL", "MODEL"):
                mdl = value.strip()
                break
    emit("device_mdl", mdl)
    job_active = get(state, "job.active") is True
    status_age = number(summary.get("status_age_seconds"))
    emit("status_age_text", ago(status_age))
    emit("stale", summary.get("stale") is True)
    emit("status_fresh", job_active or (status_age is not None and status_age <= LIMIT))
    status(state)
    emit("job_active", job_active)
    emit("job_peer", get(state, "job.peer"))
    count = get(state, "job.bytes")
    emit("job_bytes", count if isinstance(count, int) and not isinstance(count, bool) else 0)
    two = get(state, "job.two_color")
    emit("job_two_color", "1" if two is True else "0" if two is False else "")
    emit("job_last_result", get(state, "job.last_result"))
    emit("job_last_detail", get(state, "job.last_detail"))
    finished = number(get(state, "job.last_finished"))
    emit("job_last_age", now - finished if finished is not None else None)
    emit("job_last_ago", ago(now - finished) if finished is not None else "")


main()
PY
)"

# The status block as P-touch reads it over SNMP, compared with what the proxy
# read from the printer. stdin: snmpget stdout (-Ox). argv: <lib dir>
# <expected model> <snmpget stderr> <state hex:job-active>...
PY_T3="$(cat <<'PY'
import re
import sys

sys.path.insert(0, sys.argv[1])
from ql810wc import media, rasterproto  # noqa: E402

EXPECTED_MODEL = sys.argv[2]
STDERR = sys.argv[3]
SYNTHESISED = range(18, 22)   # status type, phase type, phase number: rewritten during a job
HEX = re.compile(r"Hex-STRING:\s*((?:[0-9A-Fa-f]{2}(?:\s+|$))+)")


def text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return " ".join(str(value).split())


def emit(key, value):
    print("%s\t%s" % (key, text(value)))


def snapshots(args):
    out = []
    for arg in args:
        hexpart, _, active = arg.partition(":")
        try:
            data = bytes.fromhex(hexpart) if hexpart else b""
        except ValueError:
            data = b""
        if len(data) == rasterproto.STATUS_LEN:
            out.append((data, active == "1"))
    return out


def parse(out, err):
    match = HEX.search(out)
    if match:
        return bytes.fromhex("".join(match.group(1).split())), ""
    if re.search(r'=\s*""\s*$', out, re.M):
        return b"", ""
    both = out + "\n" + err
    if "noSuchName" in both or "No Such" in both:
        return None, "the agent does not know the status OID (noSuchName): P-touch would fail at once"
    if "Timeout" in both:
        return None, "no answer (timeout)"
    flat = " ".join(both.split())
    return None, ("unexpected answer: %s" % flat[:200]) if flat else "no answer"


def main():
    data, problem = parse(sys.stdin.read(), STDERR)
    if data is None:
        emit("snmp_ok", False)
        emit("problem", problem)
        return
    emit("snmp_hex", data.hex(" "))
    if len(data) != rasterproto.STATUS_LEN:
        emit("snmp_ok", False)
        emit("problem", "the status OID returned %d bytes, not %d" % (len(data), rasterproto.STATUS_LEN))
        return
    if data[:3] != rasterproto.STATUS_HEADER:
        emit("snmp_ok", False)
        emit("problem", "not a Brother status block (header %s, expected 80 20 42)" % data[:3].hex(" "))
        return
    emit("snmp_ok", True)
    frame = rasterproto.Status(data)
    states = snapshots(sys.argv[4:])
    match = "nostate" if not states else "mismatch"
    for state, _active in states:
        if state == data:
            match = "exact"
            break
    if match == "mismatch":
        for state, active in states:
            differ = [i for i in range(rasterproto.STATUS_LEN) if state[i] != data[i]]
            if active and differ and all(i in SYNTHESISED for i in differ):
                match = "job"
                break
    if match in ("mismatch", "nostate") and frame.status_type == rasterproto.ST_ERROR \
            and frame.err1 & 0x20 and not frame.has_media:
        match = "offline"
    emit("match", match)
    if match == "mismatch":
        reference = states[-1][0]
        emit("diff", ",".join(str(i) for i in range(rasterproto.STATUS_LEN) if reference[i] != data[i]))
        emit("state_hex", reference.hex(" "))
    emit("series_code", "%02x" % frame.series_code)
    emit("model_code", "%02x" % frame.model_code)
    emit("model_name", frame.model_name)
    emit("model_ok", frame.model_name == EXPECTED_MODEL)
    emit("errors", "; ".join(frame.errors()))
    emit("has_media", frame.has_media)
    emit("media_type_raw", "%02x" % frame.media_type_raw)
    emit("media_encoding_ok", not frame.has_media or frame.media_type_raw in (rasterproto.MEDIA_CONTINUOUS,
                                                                              rasterproto.MEDIA_DIE_CUT))
    found = media.describe(frame) if frame.has_media else None
    emit("media_known", found is not None)
    if found is not None:
        emit("media_desc", found.description)
        emit("media_label", found.label_id)
    elif frame.has_media:
        emit("media_desc", "width %d mm, type 0x%02x, length %d mm"
             % (frame.media_width_mm, frame.media_type_raw, frame.media_length_mm))


main()
PY
)"

# The shipped OID map: model, validated firmware, the status block entry.
# argv: <lib dir> <oid-map path> <printer firmware or "">.
PY_OIDMAP="$(cat <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from ql810wc import persona  # noqa: E402

STATUS_OID = "1.3.6.1.4.1.2435.3.3.9.1.6.1.0"


def emit(key, value):
    if isinstance(value, bool):
        value = "1" if value else "0"
    print("%s\t%s" % (key, " ".join(str(value).split())))


try:
    oid_map = persona.load_oid_map(sys.argv[2])
except persona.OidMapError as exc:
    emit("ok", False)
    emit("problem", exc)
else:
    emit("ok", True)
    emit("model", oid_map.model)
    emit("firmware", ", ".join(oid_map.validated_firmware))
    emit("firmware_match", bool(sys.argv[3]) and sys.argv[3] in oid_map.validated_firmware)
    emit("entries", len(oid_map.entries))
    emit("verified", sum(1 for entry in oid_map.entries if entry.verified))
    entry = oid_map.get(STATUS_OID)
    emit("status_entry", entry is not None and entry.type == "octets" and entry.source.kind == "provider"
         and entry.source.arg == "status_block")
PY
)"

# The Bonjour record: the installed Avahi service file, and what
# "avahi-browse --resolve --parsable" (stdin) shows of it.
# argv: <service file> <PRINTER_UUID> <port> <1 if avahi-browse ran>.
PY_AVAHI="$(cat <<'PY'
import sys
import xml.etree.ElementTree as ET

PATH, UUID, PORT, RAN = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
MODEL = "QL-810W"
TYPE = "_pdl-datastream._tcp"


def emit(key, value):
    if isinstance(value, bool):
        value = "1" if value else "0"
    print("%s\t%s" % (key, " ".join(str(value).split())))


def unescape(text):
    """avahi-browse --parsable escapes as \\DDD (decimal byte) and \\X."""
    out, i = bytearray(), 0
    while i < len(text):
        code = text[i + 1:i + 4]
        if text[i] == "\\" and len(code) == 3 and code.isdigit():
            out.append(int(code) & 0xFF)
            i += 4
        elif text[i] == "\\" and i + 1 < len(text):
            out += text[i + 1].encode()
            i += 2
        else:
            out += text[i].encode()
            i += 1
    return out.decode("utf-8", "replace")


def txt_items(text):
    """'"a=1" "b=2"' -> {"a": "1", "b": "2"} (quoted, backslash-escaped strings)."""
    items, i, n = {}, 0, len(text)
    while i < n:
        if text[i] != '"':
            i += 1
            continue
        i += 1
        start = i
        while i < n and text[i] != '"':
            i += 2 if text[i] == "\\" else 1
        key, _, value = unescape(text[start:i]).partition("=")
        items.setdefault(key, value)
        i += 1
    return items


def read_file():
    try:
        root = ET.parse(PATH).getroot()
    except (OSError, ET.ParseError) as exc:
        return None, "cannot read %s: %s" % (PATH, exc)
    service = root.find("service")
    if root.tag != "service-group" or service is None:
        return None, "%s is not an Avahi service group" % PATH
    txt = {}
    for record in service.findall("txt-record"):
        key, _, value = (record.text or "").partition("=")
        txt.setdefault(key, value)
    return {"name": (root.findtext("name") or "").strip(), "type": (service.findtext("type") or "").strip(),
            "port": (service.findtext("port") or "").strip(), "txt": txt}, ""


def main():
    info, problem = read_file()
    problems = [problem] if problem else []
    if info is not None:
        if info["type"] != TYPE:
            problems.append("service type %s, expected %s" % (info["type"] or "(none)", TYPE))
        if info["port"] != PORT:
            problems.append("port %s, expected %s" % (info["port"] or "(none)", PORT))
        if info["txt"].get("usb_MDL") != MODEL:
            problems.append("TXT usb_MDL=%s, but P-touch for Mac needs exactly %s"
                            % (info["txt"].get("usb_MDL", "(missing)"), MODEL))
        if not UUID or info["txt"].get("UUID") != UUID:
            problems.append("TXT UUID does not match PRINTER_UUID in identity.env")
        if not info["name"].startswith("Brother " + MODEL):
            problems.append("instance name %r does not start with 'Brother %s'" % (info["name"], MODEL))
        emit("file_name", info["name"])
    emit("file_ok", not problems)
    emit("file_problem", "; ".join(problems))
    emit("browse_ran", RAN)
    ours, others = [], set()
    for line in sys.stdin.read().splitlines():
        fields = line.split(";")
        if len(fields) < 10 or fields[0] != "=" or fields[4] != TYPE:
            continue
        txt = txt_items(";".join(fields[9:]))
        if UUID and txt.get("UUID") == UUID:
            ours.append((unescape(fields[3]), fields[2], fields[8], txt))
        else:
            others.add(unescape(fields[3]))
    emit("browse_found", bool(ours))
    emit("browse_others", len(others))
    if ours:
        emit("browse_name", " | ".join(sorted({o[0] for o in ours})))
        emit("browse_protocols", ", ".join(sorted({o[1] for o in ours})))
        emit("browse_port_ok", all(o[2] == PORT for o in ours))
        emit("browse_mdl_ok", all(o[3].get("usb_MDL") == MODEL for o in ours))


main()
PY
)"

# The verdict: tiers from the check records. argv: <records file> <JSON output
# path or ""> <key=value>... Prints SUMMARY / NOTE / DEGRADED / REMARK / TIER /
# EXIT lines (a REMARK line is printed as it is: no problem, no tier).
PY_VERDICT="$(cat <<'PY'
import json
import sys
import time

RECORDS, JSON_PATH = sys.argv[1], sys.argv[2]
META = dict(arg.partition("=")[::2] for arg in sys.argv[3:])
FIRMWARE_NOTE = ("NOTE: identity strings in oid-map.yaml (sysDescr, sysObjectID, ...) are not yet verified "
                 "on a real QL-810Wc; P-touch Editor for Mac and iPrint&Label for iOS work with them as they are")

checks, facts = [], {}
with open(RECORDS, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        parts = line.rstrip("\n").split("\t", 3)
        if len(parts) != 4:
            continue
        section, status, cid, message = parts
        if section == "fact":
            facts[cid] = message
        else:
            checks.append({"section": section, "status": status, "id": cid, "message": message})


def failed(*sections):
    return [c["message"] for c in checks if c["section"] in sections and c["status"] == "fail"]


def last(cid):
    found = [c for c in checks if c["id"] == cid]
    return found[-1] if found else None


def tier(ok, status, problems=()):
    return {"ok": ok, "status": status, "problems": list(problems)}


problems = failed("setup", "t0")
t0 = tier(not problems, "not reached" if problems else "reached", problems)

roll, printed, problems = last("t1.roll"), last("t1.print"), failed("t1")
if problems:
    t1 = tier(False, "not reached", problems)
elif roll is None or roll["status"] != "pass":
    t1 = tier(False, facts.get("t1_summary") or "not verified", [roll["message"]] if roll else [])
elif printed is not None and printed["status"] == "pass":
    t1 = tier(True, "confirmed (test label printed and checked)")
elif printed is not None and printed["status"] == "info":
    t1 = tier(True, "capable (test label printed, not confirmed)")
else:
    t1 = tier(True, "capable (not printed)")

problems = failed("t2")
t2 = tier(not problems, "not reached" if problems else "reached", problems)

entry = last("t3.degraded")
degraded = entry is not None and entry["status"] == "fail"
problems = failed("t3")
if degraded:
    t3 = tier(False, "degraded: T0-T2 with manual media", problems)
else:
    t3 = tier(not problems, "not reached" if problems else "reached", problems)
entry = last("t3.firmware")
firmware_unvalidated = t3["ok"] and (entry is None or entry["status"] != "pass")

entry = last("host.v3")
problems = failed("host") + [c["message"] for c in checks
                             if c["id"].startswith("t2.leak") and c["status"] == "fail"]
if entry is None or entry["status"] == "skip":
    host = tier(False, facts.get("host_summary") or "not checked")
elif problems:
    host = tier(False, "not reached", problems)
else:
    host = tier(entry["status"] == "pass", "reached" if entry["status"] == "pass" else "not reached")

ladder = [("T0", t0), ("T1", t1), ("T2", t2), ("T3", t3)]
reached = "NONE"
notes = []
for index, (name, info) in enumerate(ladder):
    if not info["ok"]:
        above = [n for n, i in ladder[index + 1:] if i["ok"]]
        if above:
            names = ", ".join(above[:-1]) + " and " + above[-1] if len(above) > 1 else above[0]
            notes.append("%s passed as well, but the tiers build on each other and %s comes first"
                         % (names, name))
        break
    reached = name
degraded_lines, remarks = [], []
if degraded:
    degraded_lines.append("DEGRADED: T0-T2 with manual media")
elif firmware_unvalidated:
    remarks.append(FIRMWARE_NOTE)
verdict = "TIER REACHED: " + reached + (" (+host SNMP)" if host["ok"] else "")
code = 0 if t0["ok"] else 1

for label, info in (("T0 data plane", t0), ("T1 two-colour", t1), ("T2 discovery", t2),
                    ("T3 live media", t3), ("host SNMP (bonus)", host)):
    print("SUMMARY\t%s\t%s" % (label, info["status"]))
for note in notes:
    print("NOTE\t" + note)
for line in degraded_lines:
    print("DEGRADED\t" + line)
for line in remarks:
    print("REMARK\t" + line)
print("TIER\t" + verdict)
print("EXIT\t%d" % code)

if JSON_PATH:
    doc = {
        "schema": 1,
        "tool": "ql810wc-print-proxy selftest.sh",
        "version": META.get("version", ""),
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": META.get("root") == "1",
        "options": {"print_test": META.get("print_test") == "1", "mono": META.get("mono") == "1"},
        "tier_reached": reached,
        "host_snmp": host["ok"],
        "degraded": degraded,
        "firmware_validated": not firmware_unvalidated and t3["ok"],
        "verdict": verdict,
        "exit_code": code,
        "tiers": {"T0": t0, "T1": t1, "T2": t2, "T3": t3, "host_snmp": host},
        "notes": notes + degraded_lines + remarks,
        "counts": {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail", "skip", "info")},
        "facts": facts,
        "checks": checks,
    }
    with open(JSON_PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
PY
)"

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
emit_line() { # status text
  local status="$1" text="$2" label color=""
  case "$status" in
    pass) label=PASS color="$C_GRN" ;;
    warn) label=WARN color="$C_YEL" ;;
    fail) label=FAIL color="$C_RED" ;;
    skip) label=SKIP ;;
    *)    label=INFO ;;
  esac
  if ((!QUIET)) || [[ "$status" == warn || "$status" == fail ]]; then
    printf '%s%-4s%s  %s\n' "$color" "$label" "${color:+$C_OFF}" "$text" >&3
  fi
  log_file_line "$label" "$text"
}

# install.sh exports QLP_LOG_FILE: the report goes into its log too.
log_file_line() { # label text
  if [[ -n "${QLP_LOG_FILE:-}" ]]; then
    printf '%s %-5s [selftest] %s\n' "$(_qlp_ts)" "$1" "$2" >>"$QLP_LOG_FILE" 2>/dev/null || true
  fi
}

section() { # title
  if ((!QUIET)); then printf '\n%s== %s ==%s\n' "$C_BOLD" "$1" "$C_OFF" >&3; fi
  log_file_line "----" "== $1 =="
}

# One check result: printed, and recorded for the verdict and the JSON summary.
check() { # section(setup|t0|t1|t2|t3|host) status(pass|warn|fail|skip|info) id message...
  local sec="$1" status="$2" id="$3" msg
  shift 3
  msg="$*"
  msg="${msg//$'\t'/ }"
  msg="${msg//$'\n'/ }"
  printf '%s\t%s\t%s\t%s\n' "$sec" "$status" "$id" "$msg" >>"$RECORDS"
  emit_line "$status" "$msg"
}

fact() { # key value   (JSON summary only)
  local value="${2//$'\t'/ }"
  printf 'fact\tinfo\t%s\t%s\n' "$1" "${value//$'\n'/ }" >>"$RECORDS"
}

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
bounded() { # seconds command...   (with timeout(1) when it exists)
  local seconds="$1"
  shift
  if have timeout; then timeout "$seconds" "$@"; else "$@"; fi
}

oneline() { # text -> one line of at most 240 characters
  local t="${1//$'\n'/ }"
  t="${t//$'\t'/ }"
  printf '%s' "${t:0:240}"
}

# Serial numbers never appear in full in output that may be pasted into issues.
masked() { # text
  oneline "$(printf '%s\n' "$1" | qlp_mask_serials)"
}

# The messages of the common.sh log lines in FILE, "; "-joined.
logged_messages() { # file
  local out
  out="$(sed -n 's/^[^[]*\[[^]]*\] //p' "$1" 2>/dev/null || true)"
  oneline "${out//$'\n'/; }"
}

in_list() { # word list...
  local word="$1" item
  shift
  for item in "$@"; do
    if [[ "$word" == "$item" ]]; then return 0; fi
  done
  return 1
}

configured_community() { # community
  if ((${#COMMUNITIES[@]} == 0)); then return 1; fi
  in_list "$1" "${COMMUNITIES[@]}"
}

community_label() { # community -> 'public' / 'internal'; other names masked
  case "$1" in
    public|internal) printf "'%s'" "$1" ;;
    *)               printf "'%s'" "$(qlp_mask "$1")" ;;
  esac
}

community_id() { # community -> public / internal / cN (check ids never carry other names)
  local i
  case "$1" in public|internal) printf '%s' "$1"; return 0 ;; esac
  for i in "${!COMMUNITIES[@]}"; do
    if [[ "${COMMUNITIES[$i]}" == "$1" ]]; then printf 'c%d' "$((i + 1))"; return 0; fi
  done
  printf 'other'
}

same_file() { # a b
  if have cmp; then cmp -s -- "$1" "$2"
  else [[ "$(sha256sum <"$1")" == "$(sha256sum <"$2")" ]]; fi
}

pyrun() { # code args...   (stdin passes through)
  if [[ -z "$PY" ]]; then return 1; fi
  "$PY" -I -B -c "$@"
}

# "key<TAB>value" lines into the global associative array NAME (replaced).
load_kv() { # name text
  local name="$1" k v
  unset "$name"
  declare -gA "$name"
  while IFS=$'\t' read -r k v; do
    if [[ "$k" =~ ^[a-z][a-z0-9_]*$ ]]; then printf -v "${name}[$k]" '%s' "$v"; fi
  done <<<"$2"
}

now_ns() {
  local t
  t="$(date +%s%N 2>/dev/null || true)"
  if [[ "$t" =~ ^[0-9]{10,}$ ]]; then printf '%s' "$t"; else printf '%s000000000' "$(date +%s)"; fi
}

# State of a systemd unit, reported. Returns 0 when it is active.
unit_check() { # section id unit
  local sec="$1" id="$2" unit="$3" state enabled
  if ! have systemctl; then
    check "$sec" fail "$id" "$unit: systemctl not found (this is not a systemd host)"
    return 1
  fi
  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
  state="${state%%$'\n'*}" enabled="${enabled%%$'\n'*}"
  if [[ "$state" == active ]]; then
    case "$enabled" in
      enabled|enabled-runtime|static|indirect|alias|generated)
        check "$sec" pass "$id" "$unit is active" ;;
      *)
        check "$sec" warn "$id" "$unit is active but not enabled (${enabled:-unknown}): it will not start" \
                                "at boot; re-run sudo ./install.sh" ;;
    esac
    return 0
  fi
  check "$sec" fail "$id" "$unit is ${state:-not running} (journalctl -u $unit -n 50)"
  return 1
}

# ---------------------------------------------------------------------------
# Configuration: template defaults + config.env (as the installer reads them)
# ---------------------------------------------------------------------------
read_runtime_env() { # -> RT[] from runtime.env, which the installer writes as KEY="value"
  local line re='^([A-Z][A-Z0-9_]*)="([^"]*)"[[:space:]]*$'
  RT=()
  if [[ ! -r "$QLP_RUNTIME_ENV" ]]; then return 1; fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ $re ]]; then RT["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"; fi
  done <"$QLP_RUNTIME_ENV"
}

# Settings config.env could not provide: take the deployed ones where they exist.
overlay_runtime_env() {
  local k
  for k in "${QLP_RUNTIME_KEYS[@]}"; do
    if [[ -n "${RT[$k]+set}" ]]; then printf -v "$k" '%s' "${RT[$k]}"; fi
  done
}

load_config() {
  local keys cfg mode msg c seen=" "
  local -a words=()
  keys="$(grep -oE '^[A-Z][A-Z0-9_]*=' "$QLP_CONFIG_TEMPLATE" | tr -d '=' | paste -sd '|' -)"
  qlp_parse_env_file "$QLP_CONFIG_TEMPLATE"
  cfg="$(qlp_config_path)"
  CONFIG_FILE="$cfg"
  read_runtime_env || true
  if [[ -z "$cfg" ]]; then
    CONFIG_READABLE=1   # the installer used these same defaults
    check setup warn setup.config "no config.env found: checking against the template defaults, which" \
                                  "the installer used too (create one with: sudo ./setup.sh --config-only)"
  elif [[ ! -r "$cfg" ]]; then
    CONFIG_WHY="only root can read $cfg"
    check setup warn setup.config "$cfg is readable by root only: checking against $QLP_RUNTIME_ENV and" \
                                  "the template defaults (run sudo ./selftest.sh for the full test)"
    overlay_runtime_env
  elif ! (qlp_parse_env_file "$cfg" "$keys") >/dev/null 2>"$TMPD/config.err"; then
    CONFIG_WHY="$cfg is invalid"
    check setup warn setup.config "$cfg cannot be used ($(logged_messages "$TMPD/config.err")): checking against" \
                                  "$QLP_RUNTIME_ENV and the template defaults; fix it before the next ./install.sh"
    overlay_runtime_env
  else
    qlp_parse_env_file "$cfg" "$keys" 2>"$TMPD/config.err"
    CONFIG_READABLE=1
    check setup pass setup.config "configuration: $cfg"
    msg="$(logged_messages "$TMPD/config.err")"
    if [[ -n "$msg" ]]; then check setup warn setup.config.keys "$msg"; fi
    mode="$(stat -c '%a' "$cfg" 2>/dev/null || stat -f '%Lp' "$cfg" 2>/dev/null || true)"
    if [[ "$mode" =~ ^[0-7]+$ && "${mode: -2}" != 00 ]]; then
      check setup warn setup.config.mode "$cfg is mode $mode but holds the SNMPv3 passphrases: chmod 600 '$cfg'"
    fi
  fi
  # What the daemons were deployed with wins for the data plane.
  PORT="${RT[RAW_PORT]:-${RAW_PORT:-9100}}"
  if ! qlp_valid_port "$PORT"; then PORT=9100; fi
  PORT="$((10#$PORT))"
  POLL="${RT[STATUS_POLL_SECONDS]:-${STATUS_POLL_SECONDS:-3}}"
  if ! [[ "$POLL" =~ ^[0-9]{1,2}$ ]] || ((10#$POLL < 1 || 10#$POLL > 60)); then POLL=3; fi
  POLL="$((10#$POLL))"
  COMMUNITIES=()
  read -r -a words <<<"${SNMP_PRINTER_COMMUNITIES:-} ${SNMP_PRINTER_COMMUNITY:-}"
  if ((${#words[@]})); then
    for c in "${words[@]}"; do
      if [[ "$seen" != *" $c "* ]]; then COMMUNITIES+=("$c"); seen+="$c "; fi
    done
  fi
  fact raw_port "$PORT"
  fact snmp_agent_mode "${SNMP_AGENT_MODE:-}"
}

v3_is_set() { [[ -n "${!1:-}" && "${!1}" != *CHANGE-ME* ]]; }

V3_WHY=""
v3_enabled() { # the same decision as scripts/30-snmp-agent.sh (decide_v3)
  local mode="${SNMP_V3_ENABLE:-auto}"
  case "${mode,,}" in
    auto)
      if v3_is_set SNMP_V3_AUTH_PASS && v3_is_set SNMP_V3_PRIV_PASS; then return 0; fi
      V3_WHY="SNMP_V3_ENABLE=auto and the SNMPv3 passphrases are not set" ;;
    1|y|yes|true|on) return 0 ;;
    *) V3_WHY="SNMP_V3_ENABLE=$mode" ;;
  esac
  return 1
}

# ---------------------------------------------------------------------------
# Firewall (firewalld: what the install steps add, queried runtime + permanent)
# ---------------------------------------------------------------------------
fw_ready() { # section -> 0 when rules can be queried (FW_ZONE set); else says why
  local sec="$1"
  if [[ -z "$FW_READY" ]]; then
    if ((!IS_ROOT)); then
      FW_READY="skip:firewall rules not checked: firewalld answers root only"
    elif ! have firewall-cmd; then
      FW_READY="warn:firewalld is not installed: nothing filters the proxy's ports (the installer manages firewalld only)"
    elif ! qlp_fw_active; then
      FW_READY="warn:firewalld is not running: nothing filters the proxy's ports (the installer manages firewalld only)"
    else
      FW_ZONE="$(qlp_fw_zone 2>/dev/null || true)"
      if [[ -n "$FW_ZONE" ]]; then FW_READY=yes; else FW_READY="fail:cannot tell which firewalld zone serves this host"; fi
    fi
  fi
  if [[ "$FW_READY" == yes ]]; then return 0; fi
  check "$sec" "${FW_READY%%:*}" "$sec.fw" "${FW_READY#*:}"
  return 1
}

fw_expect() { # section id what firewall-cmd-query...
  local sec="$1" id="$2" what="$3" runtime=0 permanent=0
  shift 3
  if bounded 30 firewall-cmd --zone="$FW_ZONE" "$@" >/dev/null 2>&1; then runtime=1; fi
  if bounded 30 firewall-cmd --permanent --zone="$FW_ZONE" "$@" >/dev/null 2>&1; then permanent=1; fi
  if ((runtime && permanent)); then
    check "$sec" pass "$id" "firewall: $what (zone $FW_ZONE)"
  elif ((runtime)); then
    check "$sec" warn "$id" "firewall: $what is open now but not permanent: it is lost at the next firewalld" \
                            "reload or reboot (re-run sudo ./install.sh)"
  elif ((permanent)); then
    check "$sec" warn "$id" "firewall: $what is permanent but not in the running firewall (re-run sudo ./install.sh)"
  else
    check "$sec" fail "$id" "firewall: $what is missing in zone $FW_ZONE (re-run sudo ./install.sh)"
  fi
}

# One source-scoped rich rule per CIDR, as the install steps write them.
fw_expect_sources() { # section id setting proto port [cidr...]
  local sec="$1" id="$2" setting="$3" proto="$4" port="$5" cidrs fam cidr n=0
  shift 5
  if (($# == 0)); then
    check "$sec" warn "$id" "$setting is empty: only this host may use ${proto^^} $port"
    return 0
  fi
  if ! cidrs="$(qlp_norm_cidrs "$setting" "$@" 2>/dev/null)"; then
    check "$sec" fail "$id" "$setting holds an invalid address or CIDR"
    return 0
  fi
  while IFS=$'\t' read -r fam cidr; do
    if [[ -z "$fam" ]] || qlp_cidr_is_loopback "$cidr"; then continue; fi
    n=$((n + 1))
    fw_expect "$sec" "$id.$n" "${proto^^} $port from $cidr" \
      --query-rich-rule="$(qlp_fw_rich_rule "$fam" "$cidr" "$port" "$proto")"
  done <<<"$cidrs"
}

# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
installed_code_checks() {
  local f rel version=""
  local -a missing=() differ=()
  for f in qlctl ql810wc-proxyd brother_snmp_agent.py oid-map.yaml ql810wc/__init__.py; do
    if [[ ! -f "$QLP_LIB_DIR/$f" ]]; then missing+=("$f"); fi
  done
  if ((${#missing[@]})); then
    check setup fail setup.code "not installed: ${missing[*]} missing in $QLP_LIB_DIR (run sudo ./install.sh)"
    return 0
  fi
  INSTALLED=1
  if [[ -n "$PY" ]]; then
    version="$("$PY" -I -B "$QLCTL" --config "$QLP_RUNTIME_ENV" version 2>/dev/null || true)"
    version="${version##* }"
  fi
  check setup pass setup.code "installed code: $QLP_LIB_DIR${version:+ (version $version)}"
  fact installed_version "$version"
  if [[ ! -d "$QLP_REPO_ROOT/agent/ql810wc" ]]; then return 0; fi
  for f in ql810wc-proxyd qlctl brother_snmp_agent.py oid-map.yaml "$QLP_REPO_ROOT"/agent/ql810wc/*.py; do
    rel="${f#"$QLP_REPO_ROOT/agent/"}"
    if [[ ! -f "$QLP_LIB_DIR/$rel" ]] || ! same_file "$QLP_REPO_ROOT/agent/$rel" "$QLP_LIB_DIR/$rel"; then
      differ+=("$rel")
    fi
  done
  if ((${#differ[@]})); then
    check setup warn setup.code.drift "the installed code differs from this checkout (${differ[*]}):" \
                                      "re-run sudo ./install.sh to deploy it"
  else
    check setup pass setup.code.drift "the installed code matches this checkout"
  fi
}

runtime_env_checks() {
  local k want
  local -a drift=()
  if [[ ! -r "$QLP_RUNTIME_ENV" ]]; then
    check setup fail setup.runtime "$QLP_RUNTIME_ENV is missing: the daemons have no settings (run sudo ./install.sh)"
    return 0
  fi
  if ((!CONFIG_READABLE)); then return 0; fi
  # What the installer writes, derived keys (STATUS_MAX_AGE_SECONDS) included.
  for k in "${QLP_RUNTIME_KEYS[@]}" "${QLP_RUNTIME_DERIVED[@]}"; do
    want="$(qlp_runtime_value "$k")"
    if [[ -z "${RT[$k]+set}" || "${RT[$k]}" != "$want" ]]; then drift+=("$k"); fi
  done
  if ((${#drift[@]})); then
    check setup warn setup.runtime "$QLP_RUNTIME_ENV is out of date for ${drift[*]}: re-run sudo ./install.sh" \
                                   "to apply ${CONFIG_FILE:-the configuration}"
  else
    check setup pass setup.runtime "$QLP_RUNTIME_ENV matches the configuration"
  fi
}

selinux_checks() {
  local mode out n rc=0
  mode="$(qlp_selinux_mode 2>/dev/null || true)"
  case "$mode" in
    Enforcing)  check setup pass setup.selinux "SELinux is enforcing" ;;
    Permissive) check setup warn setup.selinux "SELinux is permissive; the target is enforcing (setenforce 1," \
                                               "and SELINUX=enforcing in /etc/selinux/config)" ;;
    Disabled)   if have getenforce; then
                  check setup warn setup.selinux "SELinux is disabled; the target posture is enforcing"
                else
                  check setup info setup.selinux "SELinux is not available on this host"
                fi ;;
    *)          check setup info setup.selinux "SELinux: ${mode:-not available}" ;;
  esac
  if [[ "$mode" == Disabled || -z "$mode" ]] || ((!IS_ROOT)) || ! have ausearch; then return 0; fi
  out="$(bounded 60 ausearch -m AVC,USER_AVC -ts boot 2>/dev/null)" || rc=$?
  if ((rc > 1)); then
    check setup skip setup.avc "could not search the audit log for SELinux denials (ausearch exit $rc)"
    return 0
  fi
  n="$(awk '/^type=(USER_)?AVC/ && /denied/ && /comm="(python3|snmpd)"|ql810wc/ { n++ } END { print n + 0 }' \
         <<<"$out")"
  if [[ "$n" =~ ^[1-9] ]]; then
    check setup warn setup.avc "$n SELinux denial(s) since boot for python3/snmpd (the proxy's processes):" \
                               "ausearch -m AVC -ts boot -i"
  else
    check setup pass setup.avc "no SELinux denials since boot for the proxy's processes (python3, snmpd)"
  fi
}

section_setup() {
  local who="as root"
  section "setup"
  if ((IS_ROOT)); then
    check setup info setup.user "running as root"
  else
    who="as a regular user"
    check setup warn setup.user "not running as root: the firewall rules and the SELinux audit log are not" \
                                "checked (sudo ./selftest.sh checks them)"
  fi
  fact user "$who"
  load_config
  if [[ -z "$PY" ]]; then check setup fail setup.python "python3 not found (the proxy's daemons need $QLP_PYTHON)"; fi
  installed_code_checks
  runtime_env_checks
  selinux_checks
}

# ---------------------------------------------------------------------------
# T0 / T1 data plane
# ---------------------------------------------------------------------------
# A fresh "qlctl status --json" into ST[]. Returns 0 when state.json was readable.
read_state() {
  local json="" kv="" rc=0
  if ((!INSTALLED)) || [[ -z "$PY" ]]; then
    load_kv ST $'available\t0\nproblem\tno state: ql810wc-proxy is not installed'
    return 1
  fi
  json="$("$PY" -I -B "$QLCTL" --config "$QLP_RUNTIME_ENV" status --json 2>"$TMPD/qlctl.err")" || rc=$?
  kv="$(pyrun "$PY_STATE" "$QLP_LIB_DIR" "$POLL" <<<"$json" 2>"$TMPD/pystate.err" || true)"
  load_kv ST "$kv"
  if [[ "${ST[available]:-0}" == 1 ]]; then return 0; fi
  if ((rc != 0 && rc != 1 && rc != 3)) || [[ -z "${ST[problem]:-}" ]]; then
    json="$(cat -- "$TMPD/qlctl.err" "$TMPD/pystate.err" 2>/dev/null || true)"
    ST[available]=0
    ST[problem]="cannot read the proxy's state (qlctl exit $rc: $(masked "$json"))"
  fi
  return 1
}

# Right after ql810wc-proxyd (re)starts, as at the end of ./install.sh, it has
# told systemd it is ready but may not have found the printer or read its
# status yet: give it a moment before judging.
wait_for_printer() {
  local waited=0
  while ((waited < WARMUP_SECONDS)) && [[ "${ST[device_state]:-}" != ready || "${ST[status_valid]:-0}" != 1 ]] \
        && awk -v a="${ST[proxy_age]:-}" -v l="$WARMUP_SECONDS" 'BEGIN { exit !(a != "" && a + 0 < l) }'; do
    sleep 1
    waited=$((waited + 1))
    if ! read_state; then break; fi
  done
  if ((waited)); then
    check t0 info t0.warmup "waited $waited s for ql810wc-proxyd, which had just started, to read the printer"
  fi
}

listener_checks() {
  local listen="" out
  if have systemctl; then
    listen="$(systemctl show -p Listen --value "$QLP_UNIT_SOCKET" 2>/dev/null || true)"
    listen="${listen//$'\n'/, }"
  fi
  if ! have ss; then
    check t0 skip t0.listen "TCP $PORT listener not checked (ss is not installed)"
  else
    out="$(ss -Hltn "sport = :$PORT" 2>/dev/null || true)"
    if [[ -n "$out" ]]; then
      check t0 pass t0.listen "TCP $PORT is listening${listen:+ ($QLP_UNIT_SOCKET: $listen)}"
    else
      check t0 fail t0.listen "nothing listens on TCP $PORT${listen:+ ($QLP_UNIT_SOCKET says: $listen)}"
    fi
  fi
  if [[ -n "$listen" && "$listen" != *":$PORT (Stream)"* ]]; then
    check t0 warn t0.listen.port "$QLP_UNIT_SOCKET listens on $listen, but RAW_PORT is $PORT: re-run sudo ./install.sh"
  fi
}

printer_error_hint() { # errors
  case "$1" in
    *"cover open"*)               printf 'close the printer cover' ;;
    *"no media"*|*"end of media"*) printf 'load a DK roll' ;;
    *"cutter jam"*)                printf 'clear the cutter jam' ;;
    *"replace media"*|*"does not support"*|*"unsupported media"*)
      printf 'the last job did not match the loaded roll; switch the printer off and on' ;;
    *)                             printf 'fix it at the printer, then re-run this test' ;;
  esac
}

state_checks() {
  local name lvl how detail
  if [[ "${ST[fresh]:-0}" == 1 ]]; then
    check t0 pass t0.state "state.json updated ${ST[updated_age]:-?} s ago (limit ${ST[fresh_limit]:-?} s:" \
                           "3 x STATUS_POLL_SECONDS)"
  else
    check t0 fail t0.state "state.json was last updated ${ST[updated_age]:-?} s ago (limit ${ST[fresh_limit]:-?} s):" \
                           "ql810wc-proxyd is not updating it"
  fi
  if [[ "${ST[proxy_stopped]:-0}" == 1 ]]; then
    check t0 fail t0.state.pid "state.json says ql810wc-proxyd has stopped"
  fi
  detail="$(masked "${ST[device_detail]:-}")"
  name="${ST[device_manufacturer]:-} ${ST[device_product]:-}"
  name="${name# }" name="${name% }"
  case "${ST[device_state]:-}" in
    ready)
      check t0 pass t0.printer "printer mode (not Editor Lite): ${name:-printer} ${ST[device_vid]:-?}:${ST[device_pid]:-?}" \
                               "via ${ST[device_transport]:-?}${ST[device_node]:+ ${ST[device_node]}}"
      if [[ "${ST[device_transport]:-}" == pyusb ]]; then
        check t0 info t0.transport "the printer is used through libusb: the usblp driver is not bound to it"
      fi ;;
    mass-storage)
      check t0 fail t0.printer "Editor Lite is ON (the printer shows up as a USB drive): press and hold its" \
                               "Editor Lite button until the green LED goes out" ;;
    absent)
      check t0 fail t0.printer "no printer on USB: connect the QL-810Wc and switch it on, Editor Lite off" \
                               "(it switches itself off after about an hour unused)${detail:+ [$detail]}" ;;
    error)
      check t0 fail t0.printer "ql810wc-proxyd cannot use the printer: ${detail:-USB error}" ;;
    *)
      check t0 fail t0.printer "unknown printer state '${ST[device_state]:-}' in state.json" ;;
  esac
  fact device_state "${ST[device_state]:-}"
  fact device "${name:-} ${ST[device_vid]:-}:${ST[device_pid]:-}"
  if [[ "${ST[device_state]:-}" != ready ]]; then return 0; fi

  if [[ "${ST[status_valid]:-0}" != 1 ]]; then
    check t0 fail t0.status "${ST[status_problem]:-no status block from the printer}"
    return 0
  fi
  if [[ "${ST[stale]:-0}" == 1 ]]; then
    check t0 fail t0.status "the last status block is ${ST[status_age_text]:-?} old: the printer stopped answering"
    return 0
  fi
  lvl=pass
  if [[ "${ST[status_fresh]:-0}" != 1 ]]; then lvl=warn; fi
  check t0 "$lvl" t0.status "status block valid (32 bytes, header 80 20 42), read ${ST[status_age_text]:-?} ago:" \
                            "${ST[status_type_name]:-?}, phase ${ST[phase_name]:-?}"
  fact status_block "${ST[status_hex_spaced]:-}"
  if [[ "${ST[series_code]:-}${ST[model_code]:-}" == 3439 ]]; then
    check t0 pass t0.model "model QL-810W/QL-810Wc (status bytes 3-4: 34 39)"
  else
    check t0 warn t0.model "unvalidated model: status bytes 3-4 are ${ST[series_code]:-?} ${ST[model_code]:-?}" \
                           "(${ST[model_name]:-unknown}); the proxy is validated with the QL-810W/QL-810Wc only"
  fi
  if [[ -n "${ST[errors]:-}" ]]; then
    check t0 fail t0.errors "the printer reports: ${ST[errors]} ($(printer_error_hint "${ST[errors]}"))"
  else
    check t0 pass t0.errors "no printer errors"
  fi
  if [[ "${ST[has_media]:-0}" != 1 ]]; then
    check t0 fail t0.media "no roll loaded: load a DK roll (DK-22251 for black/red)"
  elif [[ "${ST[media_known]:-0}" == 1 ]]; then
    check t0 pass t0.media "loaded roll: ${ST[media_desc]:-?}"
  else
    check t0 warn t0.media "loaded roll: ${ST[media_desc]:-?}, which P-touch does not know ('Unsupported media')"
  fi
  fact media "${ST[media_desc]:-}"
  if [[ "${ST[job_active]:-0}" == 1 ]]; then
    check t0 info t0.job "a job is printing now (from ${ST[job_peer]:-?}, ${ST[job_bytes]:-0} bytes so far)"
  elif [[ -n "${ST[job_last_result]:-}" ]]; then
    how="colour unknown"
    case "${ST[job_two_color]:-}" in 1) how="two-colour" ;; 0) how="black only" ;; esac
    lvl=info
    if [[ "${ST[job_last_result]}" != ok ]]; then lvl=warn; fi
    check t0 "$lvl" t0.job "last job: ${ST[job_last_result]}, ${ST[job_bytes]:-0} bytes, $how," \
                           "${ST[job_last_ago]:-?} ago${ST[job_last_detail]:+ (${ST[job_last_detail]})}"
  fi
}

proc_ticks() { # pid -> utime + stime of /proc/PID/stat, in clock ticks
  local stat rest
  local -a f=()
  stat="$(cat -- "/proc/$1/stat" 2>/dev/null)" || return 1
  rest="${stat##*) }"
  read -r -a f <<<"$rest"
  if ((${#f[@]} < 13)) || [[ ! "${f[11]}" =~ ^[0-9]+$ || ! "${f[12]}" =~ ^[0-9]+$ ]]; then return 1; fi
  printf '%s' "$((f[11] + f[12]))"
}

# Regression guard for the usblp readiness storm: proxyd once spun a core.
cpu_check() { # pid
  local pid="$1" hz a b t0 t1 pct
  hz="$(getconf CLK_TCK 2>/dev/null || true)"
  if [[ ! "$hz" =~ ^[1-9][0-9]*$ ]]; then hz=100; fi
  if ! a="$(proc_ticks "$pid")"; then
    check t0 skip t0.cpu "CPU use of ql810wc-proxyd not checked (cannot read /proc/$pid/stat)"
    return 0
  fi
  t0="$(now_ns)"
  sleep "$CPU_SAMPLE_SECONDS"
  t1="$(now_ns)"
  if ! b="$(proc_ticks "$pid")"; then
    check t0 warn t0.cpu "ql810wc-proxyd (pid $pid) exited during the ${CPU_SAMPLE_SECONDS} s CPU sample (restarting?)"
    return 0
  fi
  pct="$(awk -v d="$((b - a))" -v hz="$hz" -v ns="$((t1 - t0))" \
           'BEGIN { printf "%.1f", (ns > 0 ? 100 * d / hz / (ns / 1e9) : 0) }')"
  fact proxyd_cpu_percent "$pct"
  if ! awk -v p="$pct" -v l="$CPU_LIMIT_PERCENT" 'BEGIN { exit !(p > l) }'; then
    check t0 pass t0.cpu "ql810wc-proxyd used ${pct}% CPU over ${CPU_SAMPLE_SECONDS} s (limit ${CPU_LIMIT_PERCENT}%)"
    return 0
  fi
  read_state || true
  if [[ "${ST[job_active]:-0}" == 1 ]] || awk -v a="${ST[job_last_age]:-}" \
       'BEGIN { exit !(a != "" && a + 0 < 30) }'; then
    check t0 warn t0.cpu "ql810wc-proxyd used ${pct}% CPU over ${CPU_SAMPLE_SECONDS} s while a job was printing" \
                         "(limit ${CPU_LIMIT_PERCENT}% when idle): re-run when the printer is idle"
  else
    check t0 fail t0.cpu "ql810wc-proxyd used ${pct}% CPU over ${CPU_SAMPLE_SECONDS} s while idle (limit" \
                         "${CPU_LIMIT_PERCENT}%): it is spinning (see docs/architecture.md, usblp readiness)"
  fi
}

process_checks() {
  local pid label domain
  pid="$(systemctl show -p MainPID --value "$QLP_UNIT_PROXYD" 2>/dev/null || true)"
  pid="${pid%%$'\n'*}"
  if [[ ! "$pid" =~ ^[1-9][0-9]*$ ]]; then
    check t0 warn t0.pid "systemd reports no main process for $QLP_UNIT_PROXYD"
    return 0
  fi
  if [[ -n "${ST[proxy_pid]:-}" && "${ST[proxy_pid]}" != "$pid" ]]; then
    check t0 warn t0.pid "state.json was written by pid ${ST[proxy_pid]}, but $QLP_UNIT_PROXYD runs as pid $pid"
  fi
  if qlp_selinux_on && have ps; then
    label="$(ps -o label= -p "$pid" 2>/dev/null || true)"
    domain="$(cut -d: -f3 <<<"${label//[[:space:]]/}")"
    if [[ "$domain" == unconfined_service_t ]]; then
      check t0 pass t0.selinux "ql810wc-proxyd runs in the SELinux domain unconfined_service_t"
    elif [[ -n "$domain" ]]; then
      check t0 warn t0.selinux "ql810wc-proxyd runs in the SELinux domain $domain (expected" \
                               "unconfined_service_t): USB access may be denied"
    fi
  fi
  cpu_check "$pid"
}

usb_exclusion_checks() {
  local section_name want line vid pid
  if qlp_is_yes "${IPP_USB_EXCLUDE:-yes}"; then
    if [[ ! -f "$IPP_USB_QUIRK" ]]; then
      check t0 fail t0.ipp-usb "missing $IPP_USB_QUIRK: ipp-usb may take the printer over (re-run sudo ./install.sh)"
    elif ! grep -qiE '^[[:space:]]*blacklist[[:space:]]*=[[:space:]]*true[[:space:]]*$' "$IPP_USB_QUIRK"; then
      check t0 fail t0.ipp-usb "$IPP_USB_QUIRK has no 'blacklist = true' line (re-run sudo ./install.sh)"
    else
      section_name="$(sed -n 's/^[[:space:]]*\[\(.*\)\][[:space:]]*$/\1/p' "$IPP_USB_QUIRK" 2>/dev/null || true)"
      section_name="${section_name%%$'\n'*}"
      want="${ST[device_manufacturer]:-} ${ST[device_product]:-}"
      if [[ "${ST[device_state]:-}" == ready && "$want" != " " && -n "$section_name" && "$section_name" != "$want" ]]; then
        check t0 warn t0.ipp-usb "$IPP_USB_QUIRK names [$section_name], but the printer calls itself '$want':" \
                                 "re-run sudo ./install.sh with the printer attached"
      else
        check t0 pass t0.ipp-usb "ipp-usb leaves the printer alone ([${section_name:-?}] blacklist = true)"
      fi
    fi
  else
    check t0 info t0.ipp-usb "IPP_USB_EXCLUDE=no: ipp-usb is not kept away from the printer"
    if have systemctl && systemctl is-active --quiet ipp-usb.service 2>/dev/null; then
      check t0 warn t0.ipp-usb.running "ipp-usb is running and may switch the printer to IPP-over-USB, which" \
                                       "unbinds the raw printer interface"
    fi
  fi
  if [[ ! -d "$CUPS_USB_QUIRK_DIR" ]]; then
    check t0 info t0.cups-usb "$CUPS_USB_QUIRK_DIR does not exist (no CUPS usb backend): nothing to keep away"
  elif [[ ! -f "$CUPS_USB_QUIRK" ]]; then
    check t0 fail t0.cups-usb "missing $CUPS_USB_QUIRK: CUPS' usb backend can detach usblp from the printer in" \
                              "the middle of a job (re-run sudo ./install.sh with the printer attached)"
  else
    line="$(grep -iE '^[[:space:]]*0x[0-9a-f]{4}[[:space:]]+0x[0-9a-f]{4}[[:space:]]+blacklist' "$CUPS_USB_QUIRK" 2>/dev/null || true)"
    line="${line%%$'\n'*}"
    read -r vid pid _ <<<"$line"
    vid="${vid,,}" pid="${pid,,}"
    if [[ -z "$line" ]]; then
      check t0 fail t0.cups-usb "$CUPS_USB_QUIRK has no '0xVVVV 0xPPPP blacklist' line (re-run sudo ./install.sh)"
    elif [[ "${ST[device_state]:-}" == ready && -n "${ST[device_pid]:-}" \
            && "$vid $pid" != "0x${ST[device_vid],,} 0x${ST[device_pid],,}" ]]; then
      check t0 warn t0.cups-usb "$CUPS_USB_QUIRK excludes $vid:$pid, but the printer is" \
                                "0x${ST[device_vid]}:0x${ST[device_pid]} (re-run sudo ./install.sh)"
    else
      check t0 pass t0.cups-usb "CUPS' usb backend skips the printer ($vid $pid blacklist)"
    fi
  fi
}

cups_expected_uri() {
  local host="127.0.0.1" kind=""
  if [[ -n "${RAW_BIND:-}" && -n "$PY" ]]; then
    kind="$("$PY" -I -c 'import ipaddress, sys
a = ipaddress.ip_address(sys.argv[1])
print("any" if a.is_unspecified else "loopback" if a.is_loopback else "ipv%d" % a.version)' "$RAW_BIND" 2>/dev/null || true)"
  fi
  case "$kind" in
    loopback|ipv4) host="$RAW_BIND" ;;
    ipv6)          host="[$RAW_BIND]" ;;
  esac
  printf 'socket://%s:%s' "$host" "$PORT"
}

cups_queue_check() {
  local q="${CUPS_QUEUE:-}" out uri want state accept
  if [[ -z "$q" ]]; then
    check t0 info t0.cups-queue "no local CUPS queue configured (CUPS_QUEUE is empty)"
    return 0
  fi
  if ! have lpstat; then
    check t0 warn t0.cups-queue "CUPS queue '$q' not checked: lpstat is not installed"
    return 0
  fi
  out="$(bounded 30 lpstat -r 2>/dev/null || true)"
  if [[ "$out" != *"is running"* ]]; then
    check t0 warn t0.cups-queue "CUPS queue '$q' not checked: the CUPS scheduler is not running"
    return 0
  fi
  out="$(bounded 30 lpstat -v "$q" 2>/dev/null || true)"
  if [[ "$out" != *": "* ]]; then
    check t0 warn t0.cups-queue "the local CUPS queue '$q' does not exist (re-run sudo ./install.sh)"
    return 0
  fi
  uri="${out#*: }" uri="${uri%%$'\n'*}"
  want="$(cups_expected_uri)"
  state="$(bounded 30 lpstat -p "$q" 2>/dev/null || true)"
  accept="$(bounded 30 lpstat -a "$q" 2>/dev/null || true)"
  if [[ "$uri" != "$want" ]]; then
    check t0 warn t0.cups-queue "CUPS queue '$q' prints to $(masked "$uri"), not to the proxy ($want):" \
                                "local jobs bypass ql810wc-proxyd"
  elif [[ "$state" == *disabled* || "$accept" == *"not accepting"* ]]; then
    check t0 warn t0.cups-queue "CUPS queue '$q' is paused or rejecting jobs: cupsenable '$q'; cupsaccept '$q'"
  else
    check t0 pass t0.cups-queue "CUPS queue '$q' -> $uri (raw, for applications on this host)"
  fi
}

firewall_t0() {
  local -a list=()
  if ! fw_ready t0; then return 0; fi
  read -r -a list <<<"${RAW_ALLOWED_CIDRS:-}"
  if ((${#list[@]} == 0)); then
    fw_expect t0 t0.fw "TCP $PORT open (zone-wide, like a real printer)" --query-port="$PORT/tcp"
  else
    fw_expect_sources t0 t0.fw RAW_ALLOWED_CIDRS tcp "$PORT" "${list[@]}"
  fi
}

section_data_plane() {
  local socket_up=0 service_up=0
  section "T0/T1 data plane: TCP $PORT -> ql810wc-proxyd -> USB printer"
  if unit_check t0 t0.socket "$QLP_UNIT_SOCKET"; then socket_up=1; fi
  if unit_check t0 t0.service "$QLP_UNIT_PROXYD"; then service_up=1; fi
  listener_checks
  if read_state; then wait_for_printer; fi
  if [[ "${ST[available]:-0}" == 1 ]]; then
    state_checks
  else
    check t0 fail t0.state "${ST[problem]:-no state from ql810wc-proxyd}"
  fi
  if ((service_up)); then process_checks; fi
  usb_exclusion_checks
  cups_queue_check
  firewall_t0
  if ((socket_up && service_up)) && [[ "${ST[available]:-0}" == 1 && "${ST[fresh]:-0}" == 1 \
       && "${ST[device_state]:-}" == ready && "${ST[status_valid]:-0}" == 1 && "${ST[stale]:-1}" == 0 \
       && -z "${ST[errors]:-}" && "${ST[has_media]:-0}" == 1 ]]; then
    T0_READY=1
  fi
}

# ---------------------------------------------------------------------------
# T1 two-colour, and the optional test label
# ---------------------------------------------------------------------------
ask_human() { # question -> ANSWER=yes|no, or "" without a terminal or an answer
  local reply="" tries
  ANSWER=""
  if ! { exec 4<>/dev/tty; } 2>/dev/null; then return 0; fi
  printf '\n%s\n' "$1" >&4
  for tries in 1 2 3; do
    printf 'Is it right? [y/n] ' >&4
    reply=""
    if ! read -r -t "$ANSWER_TIMEOUT_SECONDS" reply <&4; then break; fi
    case "${reply,,}" in
      y|yes) ANSWER=yes; break ;;
      n|no)  ANSWER=no; break ;;
    esac
    if ((tries == 3)); then break; fi
  done
  exec 4>&-
}

print_test() {
  local want=two-color sec=t1 id=t1.print what="black/red" out rc=0 question
  if ((MONO)); then want=mono sec=t0 id=t0.print what="black-only"; fi
  if ((!T0_READY)); then
    check "$sec" fail "$id" "$what test label not printed: the data plane is not ready (see the FAIL lines above)"
    return 0
  fi
  case "${ST[print_roll]:-}:$want" in
    two-color:two-color|mono:mono) ;;
    two-color:mono)
      check t1 warn t1.print "--mono refused: a black-only job on the black/red DK-22251 roll puts the QL into an" \
                             "error that only switching it off and on clears. The default test label" \
                             "(--print-test without --mono) has black bands as well as red ones"
      return 0 ;;
    mono:two-color)
      check t1 warn t1.print "black/red test label refused: the loaded roll (${ST[print_roll_desc]:-?}) is" \
                             "single-colour and the printer rejects two-colour jobs on it. Load DK-22251, or" \
                             "use --print-test --mono for a black-only label"
      return 0 ;;
    die-cut:*)
      check "$sec" warn "$id" "$what test label not printed: it needs continuous tape, and a die-cut roll is" \
                              "loaded (${ST[print_roll_desc]:-?})"
      return 0 ;;
    *)
      check "$sec" warn "$id" "$what test label not printed: no supported continuous roll is loaded" \
                              "(${ST[print_roll_desc]:-unknown})"
      return 0 ;;
  esac
  emit_line info "printing a $what test label through TCP $PORT (qlctl test-print --$want) ..."
  out="$("$PY" -I -B "$QLCTL" --config "$QLP_RUNTIME_ENV" test-print "--$want" 2>&1)" || rc=$?
  out="$(masked "$out")"
  case "$rc" in
    0) ;;
    3) check "$sec" warn "$id" "qlctl refused the $what test label: $out"; return 0 ;;
    *) check t0 fail t0.print "printing a test label through TCP $PORT failed (qlctl exit $rc): $out"; return 0 ;;
  esac
  check "$sec" info "$id.sent" "$out"
  if ((MONO)); then
    question="Look at the label that just came out (about 5 cm long): a black frame and, inside it, black
stripes across the tape; every third stripe covers only half of the width. Is everything sharp and complete?"
  else
    question="Look at the label that just came out (62 mm wide, about 5 cm long): a black frame and, inside it,
stripes across the tape in this order, repeating: black, red, then half black and half red. Is every black
part black and every red part red, sharp and complete?"
  fi
  ask_human "$question"
  case "$ANSWER" in
    yes) check "$sec" pass "$id" "you confirmed the $what test label" ;;
    no)  check "$sec" fail "$id" "you reported the $what test label as wrong: check the roll, the cover and the print" \
                                 "head (docs/client-setup.md, Acceptance checklist; docs/troubleshooting.md)" ;;
    *)   check "$sec" info "$id" "$what test label printed but not confirmed (no terminal to ask, or no answer)" ;;
  esac
}

section_two_color() {
  section "T1 two-colour (black/red) printing"
  if [[ "${ST[available]:-0}" != 1 || "${ST[device_state]:-}" != ready || "${ST[status_valid]:-0}" != 1 ]]; then
    fact t1_summary "not verified: no live printer status"
    check t1 skip t1.roll "cannot tell which roll is loaded (no live printer status)"
  elif [[ "${ST[media_two_color]:-0}" == 1 ]]; then
    check t1 pass t1.roll "black/red roll loaded (${ST[media_desc]:-DK-22251}): the printer takes two-colour jobs," \
                          "and the proxy forwards them byte for byte"
  else
    fact t1_summary "not verified: no black/red roll loaded"
    check t1 skip t1.roll "the loaded roll is not black/red (${ST[media_desc]:-no roll}): load DK-22251 to print," \
                          "and check, two-colour labels"
  fi
  # ql810wc-proxyd's colour check, as runtime.env sets it (the daemon's default is on).
  if [[ -n "${RT[RAW_COLOR_CHECK]+set}" ]] && ! qlp_is_yes "${RT[RAW_COLOR_CHECK]}"; then
    local lvl=info
    if [[ "${ST[media_two_color]:-0}" == 1 ]]; then lvl=warn; fi
    check t1 "$lvl" t1.colorcheck "RAW_COLOR_CHECK is off: every job goes to the printer unchecked, and after a" \
                                  "black-only job on DK-22251 the printer needs a power cycle"
  fi
  if [[ "${ST[job_two_color]:-}" == 1 && "${ST[job_last_result]:-}" == ok ]]; then
    check t1 info t1.lastjob "the last job was two-colour and the printer reported it done (${ST[job_last_ago]:-?} ago)"
  fi
  if ((PRINT_TEST)); then
    print_test
    if ((MONO)) && [[ "${ST[print_roll]:-}" != two-color ]]; then
      check t1 skip t1.print "a black-only test label does not check two-colour printing"
    fi
  else
    check t1 skip t1.print "no test label printed (--print-test prints one and asks you to check it)"
  fi
}

# ---------------------------------------------------------------------------
# T2 discovery
# ---------------------------------------------------------------------------
snmp_with() { # client-config-dir snmp-command args...   -> SNMP_OUT, SNMP_ERR
  local dir="$1"
  shift
  SNMP_OUT="$(SNMPCONFPATH="$dir" SNMP_PERSISTENT_DIR="$SNMP_PERSIST_DIR" MIBS="" "$@" 2>"$TMPD/snmp.err" || true)"
  SNMP_ERR="$(cat -- "$TMPD/snmp.err" 2>/dev/null || true)"
}
snmp() { snmp_with "$SNMP_CONF_DIR" "$@"; }   # hermetic: no snmp.conf of this host or user

snmp_reason() { # why the last query gave no usable answer
  local why
  why="$(oneline "${SNMP_ERR:-$SNMP_OUT}")"
  printf '%s' "${why:-no answer}"
}

snmp_1284_check() { # community
  local c="$1" label mdl id
  label="$(community_label "$c")" id="t2.ieee1284.$(community_id "$c")"
  snmp snmpget -v1 -c "$c" -t 2 -r 1 -On "$SNMP_HOST" "$OID_1284"
  mdl="$(sed -n 's/.*MDL:\([^;"]*\).*/\1/p' <<<"$SNMP_OUT")"
  mdl="${mdl%%$'\n'*}"
  if [[ "$mdl" == "$MODEL" ]]; then
    check t2 pass "$id" "SNMPv1 community $label answers as the printer: the 1284 device ID has MDL:$MODEL"
  elif [[ -n "$mdl" ]]; then
    check t2 fail "$id" "SNMPv1 community $label: the 1284 device ID has MDL:$mdl, but P-touch needs" \
                                   "exactly MDL:$MODEL"
  else
    check t2 fail "$id" "SNMPv1 community $label: no 1284 device ID from $SNMP_HOST ($(snmp_reason))"
  fi
}

win_search_check() {
  local n
  snmp snmpget -v1 -c public -Cf -t 2 -r 1 -On "$SNMP_HOST" "${WIN_SEARCH[@]}"
  n="$(grep -cE '^\.1\.3\.6\.1[.0-9]* = ' <<<"$SNMP_OUT" || true)"
  if [[ "$n" == "${#WIN_SEARCH[@]}" && "$SNMP_OUT$SNMP_ERR" != *"Error in packet"* ]]; then
    check t2 pass t2.win-search "the Windows driver installer's search (one SNMPv1 GET of ${#WIN_SEARCH[@]} OIDs," \
                                "community 'public') is answered in full"
  else
    check t2 warn t2.win-search "the Windows driver installer's search (one SNMPv1 GET of ${#WIN_SEARCH[@]} OIDs)" \
                                "is not answered in full: $(snmp_reason)"
  fi
}

# v1/v2c communities must see the printer persona only, never this host.
leak_check() { # community
  local c="$1" label oid sub id leaked=""
  label="$(community_label "$c")" id="t2.leak.$(community_id "$c")"
  snmp snmpgetnext -v2c -c "$c" -t 2 -r 1 -On "$SNMP_HOST" "${HOST_ONLY_SUBTREES[@]}"
  if ! grep -qE '^\.1\.3\.6\.1[.0-9]* = ' <<<"$SNMP_OUT"; then
    check t2 warn "$id" "could not check what community $label can read ($(snmp_reason))"
    return 0
  fi
  while IFS= read -r oid; do
    for sub in "${HOST_ONLY_SUBTREES[@]}"; do
      if [[ "$oid" == "$sub".* ]]; then leaked+=" $oid"; fi
    done
  done < <(grep -oE '^\.1\.3\.6\.1[.0-9]*' <<<"$SNMP_OUT" || true)
  snmp snmpget -v2c -c "$c" -t 2 -r 1 -On "$SNMP_HOST" "$OID_SYSDESCR"
  if [[ "$SNMP_OUT" == *STRING* && "$SNMP_OUT" != *Brother* ]]; then leaked+=" sysDescr.0"; fi
  if [[ -n "$leaked" ]]; then
    check t2 fail "$id" "community $label can read this host's own data (${leaked# }): v1/v2c must see the" \
                               "printer persona only; look for other community, view or access lines in /etc/snmp"
  else
    check t2 pass "$id" "community $label sees only the printer persona (no hrStorage, hrSWRun, UCD or" \
                               "host sysDescr)"
  fi
}

snmp_checks() {
  local c
  for c in internal public; do
    if configured_community "$c"; then
      snmp_1284_check "$c"
    elif [[ "$c" == internal ]]; then
      check t2 warn t2.community.internal "community 'internal' is not in SNMP_PRINTER_COMMUNITIES: P-touch Editor" \
                                          "for Mac reads the printer status with 'internal' only"
    else
      check t2 warn t2.community.public "community 'public' is not in SNMP_PRINTER_COMMUNITIES: the Windows driver" \
                                        "installer, BRAdmin and CUPS look for printers with 'public'"
    fi
  done
  for c in "${COMMUNITIES[@]}"; do
    case "$c" in internal|public) ;; *) snmp_1284_check "$c" ;; esac
  done
  if [[ "${SNMP_AGENT_MODE:-agentx}" == agentx ]] && configured_community public; then win_search_check; fi
  for c in "${COMMUNITIES[@]}"; do leak_check "$c"; done
  snmp snmpget -v1 -c "ql810wc-selftest-$RANDOM$RANDOM" -t 1 -r 0 -On "$SNMP_HOST" "$OID_1284"
  if [[ "$SNMP_OUT" == *" = "* ]]; then
    check t2 fail t2.bad-community "snmpd answered an unconfigured community: anyone can read the persona with any" \
                                   "community (look for a 'default' community source in /etc/snmp)"
  else
    check t2 pass t2.bad-community "an unconfigured community gets no answer"
  fi
}

# The same rules as step 30 (verify_snmp_listeners): the IPv4 wildcard must be
# snmpd's (as a regular user ss cannot name the owner, which then counts as
# snmpd), and IPv6 client ranges need a [::]:161 or *:161 listener too.
snmp_listener_check() {
  local out fam owner v4="" v6=""
  if ! have ss; then
    check t2 skip t2.listen "snmpd's UDP 161 sockets not checked (ss is not installed)"
    return 0
  fi
  out="$(ss -Hlun 'sport = :161' 2>/dev/null || true)"
  while read -r fam owner; do
    case "$fam" in
      ipv4) if [[ -z "$v4" || "$owner" == snmpd ]]; then v4="$owner"; fi ;;
      ipv6) if [[ -z "$v6" || "$owner" == snmpd ]]; then v6="$owner"; fi ;;
    esac
  done < <(qlp_snmp_wildcard_listeners)
  if [[ -z "$v4" && -n "$out" ]]; then
    check t2 fail t2.listen "UDP 161 is bound to specific addresses only: SNMP broadcast discovery (Brother driver" \
                            "installer, BRAdmin) needs the 0.0.0.0:161 wildcard (see agentaddress in /etc/snmp)"
  elif [[ -z "$v4" ]]; then
    check t2 fail t2.listen "nothing listens on UDP 161"
  elif [[ "$v4" != snmpd && "$v4" != "?" ]]; then
    check t2 fail t2.listen "the UDP 161 wildcard belongs to '$v4', not snmpd: stop that program (or move it to" \
                            "another port), then re-run sudo ./install.sh"
  else
    check t2 pass t2.listen "snmpd listens on the UDP 161 wildcard (SNMP broadcast discovery reaches it)"
  fi
  if [[ -z "$v6" ]] && ipv6_sources_configured; then
    check t2 warn t2.listen.ipv6 "IPv6 client ranges are configured, but nothing listens on UDP 161 over IPv6" \
                                 "([::]:161): those clients get no answer (re-run sudo ./install.sh)"
  fi
}

# An IPv6 range in SNMP_PRINTER_ALLOWED_CIDRS, or in SNMP_V3_ALLOWED_CIDRS
# while SNMPv3 is on (step 30 then adds udp6:161).
ipv6_sources_configured() {
  if [[ "${SNMP_PRINTER_ALLOWED_CIDRS:-}" == *:* ]]; then return 0; fi
  [[ "${SNMP_V3_ALLOWED_CIDRS:-}" == *:* ]] && v3_enabled
}

bonjour_checks() {
  local uuid="" browse="" ran=0 avahi_up=0
  if ! qlp_is_yes "${ENABLE_MDNS:-yes}"; then
    check t2 warn t2.mdns "Bonjour is off (ENABLE_MDNS=no): P-touch Editor for Mac does not find the printer by" \
                          "itself (it can still use 'Connect to printer with an IP address', not tested yet)"
    return 0
  fi
  if unit_check t2 t2.avahi "$AVAHI_UNIT"; then avahi_up=1; fi
  uuid="$(qlp_identity_uuid "$QLP_IDENTITY_ENV")"
  if [[ -z "$uuid" ]]; then
    check t2 fail t2.uuid "no valid PRINTER_UUID in $QLP_IDENTITY_ENV: P-touch for Mac ignores a record without" \
                          "one (re-run sudo ./install.sh)"
  fi
  if [[ ! -f "$AVAHI_SERVICE" ]]; then
    check t2 fail t2.mdns.file "missing $AVAHI_SERVICE: nothing is published over Bonjour (re-run sudo ./install.sh)"
    return 0
  fi
  if ((avahi_up)) && [[ -n "$uuid" ]] && have avahi-browse; then
    browse="$(bounded 20 avahi-browse --resolve --parsable --terminate "$MDNS_TYPE" 2>/dev/null || true)"
    ran=1
  fi
  load_kv AV "$(pyrun "$PY_AVAHI" "$AVAHI_SERVICE" "$uuid" "$PORT" "$ran" <<<"$browse" 2>/dev/null || true)"
  if [[ "${AV[file_ok]:-0}" == 1 ]]; then
    check t2 pass t2.mdns.file "Bonjour record '${AV[file_name]:-?}': $MDNS_TYPE port $PORT, usb_MDL=$MODEL," \
                               "UUID from identity.env"
  else
    check t2 fail t2.mdns.file "$AVAHI_SERVICE: ${AV[file_problem]:-cannot be checked} (re-run sudo ./install.sh)"
  fi
  if ((!ran)); then
    if ! have avahi-browse; then
      check t2 skip t2.mdns.browse "what clients see over Bonjour not checked (avahi-browse is not installed:" \
                                   "dnf install avahi-tools)"
    fi
    return 0
  fi
  if [[ "${AV[browse_found]:-0}" != 1 ]]; then
    check t2 fail t2.mdns.browse "the Bonjour record does not resolve through avahi-browse (journalctl -u $AVAHI_UNIT)"
  elif [[ "${AV[browse_mdl_ok]:-0}" != 1 || "${AV[browse_port_ok]:-0}" != 1 ]]; then
    check t2 fail t2.mdns.browse "the resolved Bonjour record has the wrong usb_MDL or port (another" \
                                 "service file with our UUID?)"
  elif [[ "${AV[browse_name]:-}" != "${AV[file_name]:-}" ]]; then
    check t2 warn t2.mdns.browse "Avahi publishes the printer as '${AV[browse_name]:-?}', not '${AV[file_name]:-?}':" \
                                 "another device uses that name. Set a unique PRINTER_NAME_SUFFIX and re-run" \
                                 "sudo ./install.sh"
  else
    check t2 pass t2.mdns.browse "clients see '${AV[browse_name]}' over Bonjour (${AV[browse_protocols]:-?}) with" \
                                 "usb_MDL=$MODEL and the UUID"
  fi
  if [[ "${AV[browse_others]:-0}" =~ ^[1-9] ]]; then
    check t2 info t2.mdns.others "${AV[browse_others]} other $MDNS_TYPE printer(s) on the network"
  fi
}

# Who on the network can reach the persona and the Bonjour record at all. The
# checks above ask from this host (127.0.0.1, the local avahi-daemon), which
# no allowed range filters, so a range left at the template's
# documentation-only example would pass them while no real client gets in.
# Only with the real configuration: a non-root run with a root-only
# config.env falls back to the template, whose ranges would always fail here.
client_range_checks() {
  local setting
  local -a list=()
  if ((!CONFIG_READABLE)); then
    check t2 skip t2.reach "the allowed client ranges are not checked: ${CONFIG_WHY:-config.env cannot be read}" \
                           "(sudo ./selftest.sh checks them)"
    return 0
  fi
  read -r -a list <<<"${SNMP_PRINTER_ALLOWED_CIDRS:-}"
  reach_check snmp SNMP_PRINTER_ALLOWED_CIDRS "the printer persona (SNMP discovery and status)" \
    ${list[@]+"${list[@]}"}
  if ! qlp_is_yes "${ENABLE_MDNS:-yes}"; then return 0; fi
  if ! qlp_fw_active; then
    check t2 info t2.reach.mdns "firewalld is not running: nothing limits who may query the Bonjour record"
    return 0
  fi
  if [[ "$FW_READY" == yes ]] && bounded 30 firewall-cmd --zone="$FW_ZONE" --query-service=mdns >/dev/null 2>&1; then
    check t2 info t2.reach.mdns "firewalld zone $FW_ZONE admits mDNS from everyone (service mdns, not added by" \
                                "this installer)"
    return 0
  fi
  setting="$(qlp_mdns_source_setting)"
  read -r -a list <<<"${!setting:-}"
  reach_check mdns "$setting" "the Bonjour record" ${list[@]+"${list[@]}"}
}

reach_check() { # snmp|mdns setting what [cidr...]
  local kind="$1" setting="$2" what="$3" why
  shift 3
  if ! qlp_classify_sources "$@"; then
    check t2 warn "t2.reach.$kind" "$setting not checked: python3 could not classify its ranges"
    return 0
  fi
  why="$(qlp_sources_problem "$setting" "$@")"
  if [[ -n "$why" ]]; then
    check t2 fail "t2.reach.$kind" "no network client can reach $what: $why. Set $setting to your clients'" \
                                   "subnet(s) in config.env and re-run sudo ./install.sh"
    return 0
  fi
  check t2 pass "t2.reach.$kind" "$setting admits network clients for $what ($QLP_SRC_USABLE)"
  if [[ -z "$QLP_SRC_SUBNETS" || -n "$QLP_SRC_LOCAL" ]]; then return 0; fi
  if [[ "$kind" == mdns ]]; then
    check t2 warn "t2.reach.$kind.local" "no range in $setting is a subnet of this host ($QLP_SRC_SUBNETS):" \
                                         "mDNS queries come only from the local network, so the firewall drops" \
                                         "them and Macs do not discover the printer"
  else
    check t2 info "t2.reach.$kind.local" "no range in $setting is a subnet of this host ($QLP_SRC_SUBNETS):" \
                                         "fine as long as your SNMP clients are routed to it"
  fi
}

firewall_t2() {
  local out
  local -a list=()
  if ! fw_ready t2; then return 0; fi
  read -r -a list <<<"${SNMP_PRINTER_ALLOWED_CIDRS:-}"
  fw_expect_sources t2 t2.fw.snmp SNMP_PRINTER_ALLOWED_CIDRS udp 161 ${list[@]+"${list[@]}"}
  out=""
  if bounded 30 firewall-cmd --zone="$FW_ZONE" --query-port=161/udp >/dev/null 2>&1; then out="port 161/udp"; fi
  if bounded 30 firewall-cmd --zone="$FW_ZONE" --query-service=snmp >/dev/null 2>&1; then out+="${out:+, }service snmp"; fi
  if [[ -n "$out" ]]; then
    check t2 warn t2.fw.zone "firewall: UDP 161 is open to the whole zone $FW_ZONE ($out, not added by this" \
                             "installer), so the source-scoped rules have no effect"
  fi
  if qlp_is_yes "${ENABLE_MDNS:-yes}"; then
    read -r -a list <<<"${MDNS_ALLOWED_CIDRS:-}"
    if ((${#list[@]})); then
      fw_expect_sources t2 t2.fw.mdns MDNS_ALLOWED_CIDRS udp 5353 "${list[@]}"
    else
      read -r -a list <<<"${SNMP_PRINTER_ALLOWED_CIDRS:-}"
      fw_expect_sources t2 t2.fw.mdns "MDNS_ALLOWED_CIDRS (= SNMP_PRINTER_ALLOWED_CIDRS)" udp 5353 \
        ${list[@]+"${list[@]}"}
    fi
  fi
}

# How ql810wc-snmp-agent takes new settings. The installer reloads it (same
# AgentX session, no SNMP pause); a unit file that cannot reload is outdated,
# and the installer then has to restart the agent, which fails a P-touch print
# that is polling the status. A reload the agent rejected leaves the previous
# settings or OID map in service. argv: the agent's StatusText.
agent_reload_check() {
  local can
  can="$(systemctl show -p CanReload --value "$QLP_UNIT_AGENT" 2>/dev/null || true)"
  if [[ "$can" == no ]]; then
    check t2 warn t2.agent.canreload "$QLP_UNIT_AGENT cannot reload: its unit file is outdated, so new settings" \
                                     "need a restart (an SNMP pause): re-run sudo ./install.sh"
  fi
  if [[ "${1:-}" == *"last reload failed"* ]]; then
    check t2 warn t2.agent.reload "$QLP_UNIT_AGENT rejected its last reload and still serves the previous" \
                                  "settings or OID map (journalctl -u $QLP_UNIT_AGENT; then re-run sudo ./install.sh)"
  fi
}

section_discovery() {
  local agent_up=0 status
  section "T2 discovery: SNMP persona on UDP 161 and Bonjour"
  if unit_check t2 t2.snmpd "$SNMPD_UNIT"; then SNMPD_UP=1; fi
  if [[ -f "$SNMPD_DROPIN" ]]; then
    check t2 pass t2.dropin "snmpd drop-in present ($SNMPD_DROPIN)"
  else
    check t2 fail t2.dropin "missing $SNMPD_DROPIN: snmpd does not serve the printer persona (re-run sudo ./install.sh)"
  fi
  case "${SNMP_AGENT_MODE:-agentx}" in
    agentx)
      if unit_check t2 t2.agent "$QLP_UNIT_AGENT"; then
        agent_up=1
        status="$(systemctl show -p StatusText --value "$QLP_UNIT_AGENT" 2>/dev/null || true)"
        if [[ -n "$status" ]]; then check t2 info t2.agent.status "$QLP_UNIT_AGENT: $(masked "$status")"; fi
        agent_reload_check "$status"
      fi ;;
    pass_persist)
      agent_up=1
      check t2 warn t2.agent "SNMP_AGENT_MODE=pass_persist (degraded): only the Brother and Printer-MIB subtrees" \
                             "are served, so discovery over SNMP by the Windows driver installer, BRAdmin and" \
                             "mobile apps is expected to fail" ;;
    *)
      check t2 fail t2.agent "SNMP_AGENT_MODE='${SNMP_AGENT_MODE:-}' is neither agentx nor pass_persist" ;;
  esac
  if ((SNMPD_UP)); then snmp_listener_check; fi
  if ((${#COMMUNITIES[@]} == 0)); then
    check t2 fail t2.snmp "SNMP_PRINTER_COMMUNITIES is empty: no SNMP client can read the printer"
  elif ! have snmpget; then
    check t2 fail t2.snmp "snmpget not found: install net-snmp-utils to check the SNMP persona"
  elif ((SNMPD_UP && agent_up)); then
    SNMP_OK=1
  else
    check t2 skip t2.snmp "SNMP queries skipped: the printer persona is not running"
  fi
  if ((SNMP_OK)); then snmp_checks; fi
  bonjour_checks
  client_range_checks
  firewall_t2
}

# ---------------------------------------------------------------------------
# T3 live media
# ---------------------------------------------------------------------------
oidmap_checks() {
  local out rc=0
  if ((!INSTALLED)) || [[ -z "$PY" ]]; then
    check t3 fail t3.oidmap "the OID map and the SNMP agent are not installed"
    return 0
  fi
  out="$("$PY" -I -B "$AGENT_SCRIPT" --check --config "$QLP_RUNTIME_ENV" --identity "$QLP_IDENTITY_ENV" \
           --oid-map "$OID_MAP" 2>"$TMPD/agent.err")" || rc=$?
  if ((rc == 0)); then
    check t3 pass t3.oidmap "brother_snmp_agent.py --check: $(oneline "${out%%$'\n'*}")"
  else
    check t3 fail t3.oidmap "brother_snmp_agent.py --check failed (exit $rc):" \
                            "$(oneline "$(cat -- "$TMPD/agent.err" 2>/dev/null || true) $out")"
  fi
  load_kv OM "$(pyrun "$PY_OIDMAP" "$QLP_LIB_DIR" "$OID_MAP" "${ST[device_firmware]:-}" 2>/dev/null || true)"
  if [[ "${OM[ok]:-0}" != 1 ]]; then
    return 0
  fi
  if [[ "${OM[status_entry]:-0}" != 1 ]]; then
    check t3 fail t3.degraded "DEGRADED: T0-T2 with manual media — oid-map.yaml does not serve the status block" \
                              "($OID_STATUS) from the live printer"
  fi
  fact oidmap_model "${OM[model]:-}"
  fact oidmap_validated_firmware "${OM[firmware]:-}"
}

firmware_check() {
  local fw="${ST[device_firmware]:-}"
  if [[ "${OM[ok]:-0}" != 1 ]]; then return 0; fi
  if [[ -z "${OM[firmware]:-}" ]]; then
    check t3 info t3.firmware "oid-map.yaml lists no validated printer firmware yet (validated_firmware: [])"
  elif [[ -z "$fw" ]]; then
    check t3 info t3.firmware "oid-map.yaml is validated for firmware ${OM[firmware]}; this printer's firmware" \
                              "version cannot be read over USB"
  elif [[ "${OM[firmware_match]:-0}" == 1 ]]; then
    check t3 pass t3.firmware "oid-map.yaml is validated for this printer's firmware ($fw)"
  else
    check t3 warn t3.firmware "this printer's firmware ($fw) is not among oid-map.yaml's validated ones (${OM[firmware]})"
  fi
}

# "hex:job-active" of the state ql810wc-proxyd wrote just now.
state_snapshot() {
  local json kv
  json="$("$PY" -I -B "$QLCTL" --config "$QLP_RUNTIME_ENV" status --json 2>/dev/null || true)"
  kv="$(pyrun "$PY_STATE" "$QLP_LIB_DIR" "$POLL" <<<"$json" 2>/dev/null || true)"
  printf '%s:%s' "$(awk -F'\t' '$1 == "status_hex" { print $2 }' <<<"$kv")" \
                 "$(awk -F'\t' '$1 == "job_active" { print $2 }' <<<"$kv")"
}

section_live_media() {
  local c=internal label before after attempt
  section "T3 live media: the printer's status block over SNMP"
  oidmap_checks
  if ((!SNMP_OK)); then
    check t3 fail t3.status "not checked: the SNMP printer persona is not available (see T2)"
    return 0
  fi
  if ! configured_community internal; then
    c="${COMMUNITIES[0]}"
    check t3 warn t3.community "P-touch Editor for Mac reads the status block with community 'internal', which is" \
                               "not configured; checking with $(community_label "$c") instead"
  fi
  label="$(community_label "$c")"
  for attempt in 1 2; do
    before="$(state_snapshot)"
    snmp snmpget -v1 -c "$c" -t 2 -r 1 -On -Ox "$SNMP_HOST" "$OID_STATUS"
    after="$(state_snapshot)"
    load_kv T3R "$(pyrun "$PY_T3" "$QLP_LIB_DIR" "${OM[model]:-$MODEL}" "$SNMP_ERR" "$before" "$after" \
                   <<<"$SNMP_OUT" 2>/dev/null || true)"
    # The agent re-reads state.json at most every 0.5 s: retry once on a race.
    if [[ "${T3R[snmp_ok]:-0}" != 1 || "${T3R[match]:-}" == exact || "${T3R[match]:-}" == job ]]; then break; fi
    if ((attempt == 1)); then sleep 1; fi
  done
  if [[ "${T3R[snmp_ok]:-0}" != 1 ]]; then
    check t3 fail t3.status "SNMPv1 GET of the status block (community $label): ${T3R[problem]:-no answer}"
    return 0
  fi
  case "${T3R[match]:-}" in
    exact)
      check t3 pass t3.status "the status block P-touch reads ($OID_STATUS, SNMPv1 $label) is the printer's own," \
                              "byte for byte: ${T3R[snmp_hex]:-}" ;;
    job)
      check t3 pass t3.status "the status block over SNMP matches the printer's except bytes 18-21: a job is" \
                              "printing, and the printing phase is synthesised as designed" ;;
    offline)
      check t3 fail t3.status "the persona reports the printer as switched off (err1 0x20): it finds the" \
                              "proxy's state stale or the printer unavailable (see T0)" ;;
    nostate)
      check t3 fail t3.status "the persona serves a status block, but ql810wc-proxyd has none to compare it with (see T0)" ;;
    *)
      check t3 fail t3.status "the status block over SNMP differs from the printer's in bytes ${T3R[diff]:-?}:" \
                              "SNMP ${T3R[snmp_hex]:-?} / USB ${T3R[state_hex]:-?}" ;;
  esac
  if [[ "${T3R[match]:-}" == offline ]]; then return 0; fi
  if [[ "${T3R[model_ok]:-0}" != 1 ]]; then
    check t3 fail t3.degraded "DEGRADED: T0-T2 with manual media — the printer reports status bytes 3-4" \
                              "${T3R[series_code]:-?} ${T3R[model_code]:-?} (${T3R[model_name]:-unknown model})," \
                              "but oid-map.yaml describes ${OM[model]:-$MODEL}: select the media by hand in P-touch"
  elif [[ "${T3R[media_encoding_ok]:-1}" != 1 ]]; then
    check t3 fail t3.degraded "DEGRADED: T0-T2 with manual media — status byte 11 (media type) is" \
                              "0x${T3R[media_type_raw]:-?}, not 0x0a/0x0b: this firmware encodes media in a way" \
                              "P-touch does not read; select the media by hand in P-touch"
  elif [[ "${ST[status_header_mismatch]:-0}" == 1 ]]; then
    check t3 fail t3.degraded "DEGRADED: T0-T2 with manual media — the printer's status block has an unexpected header"
  fi
  if [[ "${T3R[has_media]:-0}" != 1 ]]; then
    check t3 fail t3.media "P-touch sees no roll: the printer reports no media"
  elif [[ "${T3R[media_known]:-0}" == 1 ]]; then
    check t3 pass t3.media "P-touch sees the installed roll: ${T3R[media_desc]:-?}"
  else
    check t3 warn t3.media "P-touch sees a roll it does not know (${T3R[media_desc]:-?}): it shows 'Unsupported media'"
  fi
  firmware_check
}

# ---------------------------------------------------------------------------
# Bonus: host monitoring over SNMPv3
# ---------------------------------------------------------------------------
snmp_quote() { # value -> "value", quoted the way net-snmp's config reader unquotes it
  local s="${1//\\/\\\\}"
  printf '"%s"' "${s//\"/\\\"}"
}

v3_auth_name() { # SNMP_V3_AUTH_PROTO -> the snmpcmd spelling
  case "${1^^}" in
    SHA|SHA1|SHA-1) printf 'SHA' ;;
    SHA224|SHA-224) printf 'SHA-224' ;;
    SHA256|SHA-256) printf 'SHA-256' ;;
    SHA384|SHA-384) printf 'SHA-384' ;;
    SHA512|SHA-512) printf 'SHA-512' ;;
    *)              printf '%s' "${1^^}" ;;
  esac
}

v3_priv_name() { # SNMP_V3_PRIV_PROTO -> the snmpcmd spelling
  case "${1^^}" in
    AES|AES128|AES-128) printf 'AES' ;;
    AES192|AES-192)     printf 'AES-192' ;;
    AES256|AES-256)     printf 'AES-256' ;;
    *)                  printf '%s' "${1^^}" ;;
  esac
}

# A private client snmp.conf (0600) holding the SNMPv3 credentials: they never
# appear on a command line, and printf is a shell builtin.
write_v3_conf() { # dir
  ( umask 077
    printf 'defVersion 3\ndefSecurityName %s\ndefSecurityLevel authPriv\ndefAuthType %s\n' \
      "${SNMP_V3_USER:-}" "$(v3_auth_name "${SNMP_V3_AUTH_PROTO:-SHA-256}")" >"$1/snmp.conf"
    printf 'defAuthPassphrase %s\ndefPrivType %s\ndefPrivPassphrase %s\n' "$(snmp_quote "${SNMP_V3_AUTH_PASS:-}")" \
      "$(v3_priv_name "${SNMP_V3_PRIV_PROTO:-AES}")" "$(snmp_quote "${SNMP_V3_PRIV_PASS:-}")" >>"$1/snmp.conf" )
}

section_host_snmp() {
  local dir="$TMPD/snmpv3" value
  local -a list=()
  section "host monitoring (bonus): SNMPv3 authPriv sees this host"
  if ((!CONFIG_READABLE)); then
    fact host_summary "not checked"
    check host skip host.v3 "not checked: the SNMPv3 settings are in config.env, and ${CONFIG_WHY:-it cannot be read}"
    return 0
  fi
  if ! v3_enabled; then
    fact host_summary "off"
    check host skip host.v3 "SNMPv3 host monitoring is off ($V3_WHY)"
    return 0
  fi
  # The template's example address for the monitoring system (as for T2, the
  # query below comes from this host, which no range filters).
  read -r -a list <<<"${SNMP_V3_ALLOWED_CIDRS:-}"
  if ((${#list[@]})) && qlp_classify_sources "${list[@]}" && [[ -z "$QLP_SRC_USABLE" && -n "$QLP_SRC_PLACEHOLDER" ]]; then
    check host warn host.reach "SNMP_V3_ALLOWED_CIDRS holds only documentation-only example ranges" \
                               "($QLP_SRC_PLACEHOLDER): the firewall admits your monitoring system only from" \
                               "inside SNMP_PRINTER_ALLOWED_CIDRS (set its address in config.env)"
  fi
  if ((!SNMPD_UP)); then
    check host fail host.v3 "SNMPv3 not checked: snmpd is not running"
    return 0
  fi
  if ! have snmpget; then
    check host fail host.v3 "SNMPv3 not checked: snmpget not found (install net-snmp-utils)"
    return 0
  fi
  mkdir -m 0700 "$dir"
  write_v3_conf "$dir"
  snmp_with "$dir" snmpget -t 2 -r 1 -On "$SNMP_HOST" "$OID_SYSDESCR"
  value="$(sed -n 's/^[^=]*= STRING: *"\{0,1\}\([^ "]*\).*/\1/p' <<<"$SNMP_OUT")"
  value="${value%%$'\n'*}"
  if [[ "$SNMP_OUT" == *STRING* && "$SNMP_OUT" != *Brother* ]]; then
    check host pass host.v3 "SNMPv3 user '${SNMP_V3_USER:-}' (authPriv) reads this host's own MIBs in the default" \
                            "context (sysDescr: ${value:-?} ...)"
  elif [[ "$SNMP_OUT" == *Brother* ]]; then
    check host fail host.v3 "SNMPv3: the default context answers as the printer persona, not as this host"
  else
    check host fail host.v3 "SNMPv3 user '${SNMP_V3_USER:-}' gets no answer from $SNMP_HOST ($(snmp_reason))"
  fi
  if [[ "${SNMP_AGENT_MODE:-agentx}" == agentx ]]; then
    snmp_with "$dir" snmpget -n printer -t 2 -r 1 -On "$SNMP_HOST" "$OID_SYSDESCR"
    if [[ "$SNMP_OUT" == *Brother* ]]; then
      check host pass host.v3.persona "SNMPv3 '-n printer' shows the printer persona"
    elif [[ "$SNMP_OUT" == *STRING* ]]; then
      check host warn host.v3.persona "SNMPv3 '-n printer' answers with this host's sysDescr, not the printer persona"
    else
      check host warn host.v3.persona "SNMPv3 '-n printer' does not show the printer persona ($(snmp_reason))"
    fi
  fi
  rm -rf -- "$dir"
  if ! fw_ready host; then return 0; fi
  read -r -a list <<<"${SNMP_V3_ALLOWED_CIDRS:-}"
  if ((${#list[@]})); then
    fw_expect_sources host host.fw SNMP_V3_ALLOWED_CIDRS udp 161 "${list[@]}"
  else
    check host info host.fw "SNMP_V3_ALLOWED_CIDRS is empty: the monitoring host must be inside" \
                            "SNMP_PRINTER_ALLOWED_CIDRS to pass the firewall"
  fi
}

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
verdict() {
  local out kind a b code=1 tier="" json_out=""
  section "verdict"
  if ((JSON)); then json_out="$JSON_FILE"; fi
  out="$(pyrun "$PY_VERDICT" "$RECORDS" "$json_out" "root=$IS_ROOT" "print_test=$PRINT_TEST" "mono=$MONO" \
           "version=${REPO_VERSION:-}" 2>"$TMPD/verdict.err" || true)"
  if [[ "$out" != *$'TIER\t'* ]]; then
    emit_line fail "could not compute the verdict: $(oneline "$(cat -- "$TMPD/verdict.err" 2>/dev/null || true)")"
    exit 1   # on_exit reports "TIER REACHED: NONE"
  fi
  while IFS=$'\t' read -r kind a b; do
    case "$kind" in
      SUMMARY)  if ((!QUIET)); then printf '%-18s %s\n' "$a" "$b" >&3; fi
                log_file_line INFO "$a: $b" ;;
      NOTE)     printf 'NOTE  %s\n' "$a" >&3   # explains the tier: shown with --quiet too
                log_file_line NOTE "$a" ;;
      DEGRADED) printf '%s%s%s\n' "$C_YEL" "$a" "$C_OFF" >&3
                log_file_line WARN "$a" ;;
      REMARK)   printf '%s\n' "$a" >&3   # no problem: printed as it is, no colour
                log_file_line NOTE "${a#NOTE: }" ;;
      TIER)     tier="$a" ;;
      EXIT)     code="$a" ;;
    esac
  done <<<"$out"
  printf '%s%s%s\n' "$C_BOLD" "$tier" "$C_OFF" >&3
  log_file_line INFO "$tier"
  if ((JSON)) && [[ -s "$JSON_FILE" ]]; then cat -- "$JSON_FILE"; fi
  VERDICT_DONE=1
  exit "$code"
}

# shellcheck disable=SC2329  # invoked by the EXIT trap set in main
on_exit() {
  local rc=$?
  if ((!VERDICT_DONE)); then
    printf '%sFAIL%s  selftest.sh stopped before its verdict (exit %s%s)\nTIER REACHED: NONE\n' \
      "$C_RED" "$C_OFF" "$rc" "${ERR_AT:+ at $ERR_AT}" >&3
    if ((JSON)); then
      printf '{"schema": 1, "tier_reached": "NONE", "host_snmp": false, "verdict": "TIER REACHED: NONE",'
      printf ' "exit_code": %d, "error": "selftest.sh stopped before its verdict"}\n' "$rc"
    fi
  fi
  if [[ -n "$TMPD" && -d "$TMPD" ]]; then rm -rf -- "$TMPD"; fi
}

main() {
  TMPD="$(mktemp -d "${TMPDIR:-/tmp}/ql810wc-selftest.XXXXXX")"
  trap on_exit EXIT
  RECORDS="$TMPD/records" JSON_FILE="$TMPD/summary.json"
  SNMP_CONF_DIR="$TMPD/snmp-conf" SNMP_PERSIST_DIR="$TMPD/snmp-persist"
  mkdir -m 0700 "$SNMP_CONF_DIR" "$SNMP_PERSIST_DIR"
  : >"$RECORDS"
  if ((!QUIET)); then
    printf '%sql810wc-print-proxy selftest%s %s, %s\n' "$C_BOLD" "$C_OFF" "${REPO_VERSION:-}" \
      "$(date '+%Y-%m-%d %H:%M:%S %z')" >&3
  fi
  section_setup
  section_data_plane
  section_two_color
  section_discovery
  section_live_media
  section_host_snmp
  verdict
}

main
