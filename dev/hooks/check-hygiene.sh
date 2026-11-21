#!/usr/bin/env bash
# dev/hooks/check-hygiene.sh — keep secrets and site-specific values out of git.
#
# This repository is public and git history is permanent. This guard runs as
# the pre-commit hook (staged blobs) and in CI (all tracked files).
#
#   check-hygiene.sh --staged   scan what is about to be committed (default)
#   check-hygiene.sh --all      scan every tracked file
#
# Use documentation-only values in examples instead of real ones:
#   IPv4 RFC 5737: 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24
#   IPv6 RFC 3849: 2001:db8::/32      MAC RFC 7042: 00:00:5E:00:53:00-FF
# A line containing the marker "hygiene-allow" is exempt from content checks.
# Binary files are refused: the content checks read text, and a binary file
# can hold addresses as raw bytes.
# Compatible with bash 3.2 (macOS) and bash 5 (Fedora).
set -euo pipefail

mode="${1:---staged}"
case "$mode" in --staged|--all) ;; *) echo "usage: $0 [--staged|--all]" >&2; exit 64;; esac

top="$(git rev-parse --show-toplevel)"
cd "$top"
# In the C locale every byte is a character, so grep -I passes over only files
# with NUL bytes, and those are refused as binary. In a UTF-8 locale grep would
# also pass over text that is not valid UTF-8.
export LC_ALL=C
tab="$(printf '\t')"

# Private / CGNAT IPv4 not embedded in a longer dotted string (so SNMP OIDs
# such as 1.3.6.1.2.1.10.1.2.3 do not trip it).
ip_re='(^|[^0-9.])(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3})([^0-9.]|$)'
mac_re='([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}'
mac_ok_re='00[:-]00[:-]5[Ee][:-]00[:-]53[:-][0-9A-Fa-f]{2}|00[:-]00[:-]00[:-]00[:-]00[:-]00|[Ff]{2}[:-][Ff]{2}[:-][Ff]{2}[:-][Ff]{2}[:-][Ff]{2}[:-][Ff]{2}'
key_re='-----BEGIN ([A-Z0-9]+ )*PRIVATE KEY-----'

fail=0
complain() { echo "hygiene: $*" >&2; fail=1; }

# True when git counts the indexed blob of path $1 as binary: ls-files --eol
# prints "i/-text" for a NUL byte anywhere, a lone CR, or more than one
# control character per 128 printable ones.
index_says_binary() {
  case "$(git --literal-pathspecs ls-files --eol -- "$1")" in "i/-text "*) return 0 ;; esac
  return 1
}

# One NUL-terminated record per file to check: "binary<TAB>path" or
# "text<TAB>path". -z output is never quoted, and a path may hold a TAB.
list_files() {
  local rec path kind
  if [ "$mode" = "--all" ]; then
    # Records are "i/<eol> w/<eol> attr/<attr><TAB>path".
    git ls-files --eol -z | while IFS= read -r -d '' rec; do
      case "$rec" in "i/-text "*) kind=binary ;; *) kind=text ;; esac
      printf '%s\t%s\0' "$kind" "${rec#*"$tab"}"
    done
  else
    # Records are "<added><TAB><deleted><TAB>path", with "-" for both counts
    # when the file is binary. With renames on, -z would print a rename as
    # "<counts><TAB>" followed by the old and the new path as two records.
    git diff --cached --numstat --no-renames -z --diff-filter=ACM | while IFS= read -r -d '' rec; do
      path="${rec#*"$tab"*"$tab"}"
      case "$rec" in "-$tab-$tab"*) kind=binary ;; *) kind=text ;; esac
      # diff looks for NUL bytes in the first 8000 bytes only. The --all check
      # reads the whole blob; applying it here too means the hook refuses
      # every binary file that CI refuses.
      if [ "$kind" = text ] && index_says_binary "$path"; then kind=binary; fi
      printf '%s\t%s\0' "$kind" "$path"
    done
  fi
}
# ":0:" names the index entry of the path exactly; a bare ":1:x" would be
# read as stage 1 of "x".
read_blob() {
  if [ "$mode" = "--all" ]; then cat -- "$1"; else git show ":0:$1"; fi
}

# Each blob is read once into a file and grep reads that file. (grep -q at the
# end of a pipe can exit before the writer is done, and with pipefail the
# writer's SIGPIPE would then hide the match.)
listing="$(mktemp "${TMPDIR:-/tmp}/check-hygiene.XXXXXX")"
blob="$(mktemp "${TMPDIR:-/tmp}/check-hygiene.XXXXXX")"
trap 'rm -f "$listing" "$blob"' EXIT
if ! list_files >"$listing"; then
  echo "hygiene: git could not list the files to check; commit blocked." >&2
  exit 1
fi

while IFS= read -r -d '' rec; do
  kind="${rec%%"$tab"*}" f="${rec#*"$tab"}"
  case "$f" in
    dev/walks/README.md|config.sample.env) ;;
    *.walk|*.snmpwalk|*.snmprec|*.env|*.key|*.pem|*.p12|*.pfx|dev/walks/*|venv/*|.venv/*|*/venv/*)
      complain "path not allowed in git (raw walk, real config, key or venv): $f"; continue ;;
  esac
  [ "$f" = "dev/hooks/check-hygiene.sh" ] && continue
  if [ "$kind" = binary ]; then
    complain "binary file not allowed in git (its content cannot be checked): $f"; continue
  fi
  if ! read_blob "$f" >"$blob" 2>/dev/null; then
    complain "could not read $f to check it"; continue
  fi

  if hits=$(grep -nIE -- "$key_re" "$blob" 2>/dev/null); then
    complain "private key material in $f:"; echo "$hits" | head -3 >&2
  fi
  if hits=$(grep -nIE -- "$ip_re" "$blob" 2>/dev/null | grep -v 'hygiene-allow'); then
    complain "private/site IPv4 address in $f (use RFC 5737 192.0.2.x etc.):"; echo "$hits" | head -5 >&2
  fi
  if hits=$(grep -nIE -- "$mac_re" "$blob" 2>/dev/null | grep -vE -- "$mac_ok_re" | grep -v 'hygiene-allow'); then
    complain "MAC address in $f (use RFC 7042 00:00:5E:00:53:xx):"; echo "$hits" | head -5 >&2
  fi
done <"$listing"

# Optional LOCAL deny-list of literal site values (printer serial, hostnames...).
# It lives inside .git/ so it is never committed: one literal per line, '#' comments.
# It reads binary files too.
deny_file="$(git rev-parse --git-dir)/hygiene-deny"
if [ -f "$deny_file" ]; then
  while IFS= read -r -d '' rec; do
    f="${rec#*"$tab"}"
    [ "$f" = "dev/hooks/check-hygiene.sh" ] && continue
    read_blob "$f" >"$blob" 2>/dev/null || continue
    while IFS= read -r pat || [ -n "$pat" ]; do
      case "$pat" in ''|'#'*) continue ;; esac
      if grep -qiF -- "$pat" "$blob" 2>/dev/null; then
        complain "site-specific value listed in .git/hygiene-deny found in $f"
      fi
    done <"$deny_file"
  done <"$listing"
fi

if [ "$fail" -ne 0 ]; then
  echo "hygiene: commit blocked. Git history is public; see dev/hooks/check-hygiene.sh." >&2
  exit 1
fi
