#!/usr/bin/env bash
# dev/snmpwalk/walk.sh: read-only SNMP walk of a QL-810W(c) on Wi-Fi.
#
# A development tool (see dev/snmpwalk/README.md); installing or running the
# proxy never needs it. Walks the printer's Brother private MIB (enterprise
# 2435) and MIB-2 with GET, GETNEXT and GETBULK only, never SET.
#
#   dev/snmpwalk/walk.sh <printer-ip> <label> [community]
#
#   label      what state the printer is in, e.g. dk22251, diecut, cover-open,
#              no-media, after-print (letters, digits, '-', '_')
#   community  SNMP v2c/v1 read community (default: public)
#
# Output (gitignored: it contains serials, MACs, IPs):
#   dev/walks/<UTC>_<label>.<tree>.hex.walk  values as hex: exact, for diffing
#   dev/walks/<UTC>_<label>.<tree>.txt.walk  human-readable
# where <tree> is 2435 or mib2.
set -euo pipefail

# The header comment above, without its '#' marks.
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 64; }
[[ $# -ge 2 ]] || usage
ip="$1" label="$2" community="${3:-public}"
[[ "$label" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "bad label: $label" >&2; exit 64; }
command -v snmpbulkwalk >/dev/null || { echo "need net-snmp tools (snmpbulkwalk)" >&2; exit 69; }

root="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
out="$root/dev/walks"
mkdir -p "$out"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
base="$out/${ts}_${label}"

# Timeouts: Brother NICs can be slow; 3 s x 2 retries.
v2=(-v2c -c "$community" -t 3 -r 2)
v1=(-v1 -c "$community" -t 3 -r 2)
# -On numeric OIDs, -Oe numeric enums, -Ot raw timeticks, -OU no units.
fmt=(-On -Oe -Ot -OU)

# Abort a hung walk after N seconds (portable: no coreutils timeout on macOS).
with_timeout() { local s="$1"; shift; perl -e 'alarm shift; exec @ARGV' "$s" "$@"; }

walk_tree() { # oid name
  local oid="$1" name="$2" f mode
  for mode in hex txt; do
    f="$base.$name.$mode.walk"
    # Never expand an empty array: bash before 4.4 (macOS has 3.2) treats
    # "${empty[@]}" as an unbound variable under set -u.
    local out_fmt=("${fmt[@]}")
    [[ "$mode" == hex ]] && out_fmt+=(-Ox)
    if with_timeout 600 snmpbulkwalk "${v2[@]}" -Cr20 "${out_fmt[@]}" "$ip" "$oid" >"$f" 2>"$f.err"; then :
    elif with_timeout 900 snmpwalk "${v2[@]}" "${out_fmt[@]}" "$ip" "$oid" >"$f" 2>"$f.err"; then :
    elif with_timeout 900 snmpwalk "${v1[@]}" "${out_fmt[@]}" "$ip" "$oid" >"$f" 2>"$f.err"; then :
    elif grep -q 'not increasing' "$f.err" \
         && with_timeout 900 snmpwalk "${v2[@]}" -Cc "${out_fmt[@]}" "$ip" "$oid" >"$f" 2>"$f.err"; then
      echo "note: $name walk needed -Cc (agent returned non-increasing OIDs)" >&2
    else
      echo "walk of $oid failed:" >&2; cat "$f.err" >&2; return 1
    fi
    [[ -s "$f.err" ]] || rm -f "$f.err"
  done
  printf '%-5s %6d varbinds  -> %s.*.walk\n' "$name" "$(grep -c '^\.' "$base.$name.hex.walk" || true)" "$base.$name"
}

echo "walking $ip as '$label' (read-only) ..."
walk_tree 1.3.6.1.4.1.2435 2435
walk_tree 1.3.6.1.2.1 mib2
echo "done. Raw walks are gitignored. Compare states with: dev/snmpwalk/diffwalks.py <walks...>"
