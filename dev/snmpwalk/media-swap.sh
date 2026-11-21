#!/usr/bin/env bash
# dev/snmpwalk/media-swap.sh: walk a QL-810W(c) on Wi-Fi in several roll
# states, then diff the walks.
#
# A development tool (see dev/snmpwalk/README.md). It asks you to change the
# printer (load another roll, open the cover, ...), walks it with walk.sh
# after each change, then N-way diffs the Brother MIB walks with
# diffwalks.py. OIDs that change between states are the media/status
# fields; the byte-level diff shows their encoding.
#
#   dev/snmpwalk/media-swap.sh <printer-ip> [community]
#
# States: see STATES below (edit it to add more). The first state is repeated
# at the end to show that the values are stable (repeatability check).
set -euo pipefail

# The header comment above, without its '#' marks.
[[ $# -ge 1 ]] || { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 64; }
ip="$1" community="${2:-public}"
here="$(cd "$(dirname "$0")" && pwd)"
root="$(git -C "$here" rev-parse --show-toplevel)"

STATES=(
  "dk22251|DK-22251 (62 mm continuous, black/red) loaded, cover CLOSED, printer idle"
  "diecut|the DIE-CUT roll loaded, cover CLOSED, printer idle"
  "cover-open|any roll loaded, cover OPEN"
  "no-media|NO roll loaded, cover CLOSED"
  "dk22251-again|DK-22251 loaded again, cover CLOSED, idle (repeatability check)"
)

declare -a files=()
for entry in "${STATES[@]}"; do
  label="${entry%%|*}" desc="${entry#*|}"
  echo
  echo "=== Set the printer to: $desc"
  read -r -p "    Press Enter when done (or type 'skip'): " ans
  [[ "$ans" == "skip" ]] && continue
  # Give the printer's status a moment to settle.
  perl -e 'select(undef, undef, undef, 3)'
  "$here/walk.sh" "$ip" "$label" "$community"
  # Names start with a UTC timestamp, so lexical order is chronological.
  newest="$(printf '%s\n' "$root"/dev/walks/*_"$label".2435.hex.walk | sort | tail -1)"
  files+=("$newest")
done

((${#files[@]} >= 2)) || { echo "need at least two states to diff" >&2; exit 1; }
report="$root/dev/walks/$(date -u +%Y%m%dT%H%M%SZ)_media-diff.txt"
"$here/diffwalks.py" "${files[@]}" >"$report"
echo
echo "diff report: $report   (gitignored; scrub before you quote it)"
# grep -c prints 0 but exits 1 when nothing changed; that is still a result.
changed="$(grep -c '^\* ' "$report" || true)"
echo "$changed OID(s) changed across states"
