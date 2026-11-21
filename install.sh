#!/usr/bin/env bash
# install.sh — install or upgrade ql810wc-print-proxy. Idempotent: safe to re-run.
#
#   sudo ./install.sh                     every step (00..40), then selftest
#   sudo ./install.sh --data-plane-only   steps 00-20: printing only (tiers T0/T1)
#   sudo ./install.sh --only 20           a single step, by number
#   sudo ./install.sh --from 30           steps numbered >= 30
#   sudo ./install.sh --skip 40           skip a step (repeatable)
#   sudo ./install.sh --no-selftest
#
# Configuration: $QL_PROXY_CONFIG if set, else ./config.env (written by
# setup.sh), else /etc/ql810wc-proxy/config.env. On a box that is installed
# already, a run that finds none of them stops (exit 3) instead of applying
# the template defaults over the settings the box was installed with.
# Log: /var/log/ql810wc-proxy/install-<UTC>.log
# Exit codes: 0 ok, 1 error, 3 a human must act first (message says what), 64 usage error.
# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib/common.sh"
qlp_init install

# The header comment above, up to its shellcheck directive.
usage() { awk 'NR > 1 { if (!/^#/ || /^# shellcheck/) exit; sub(/^# ?/, ""); print }' "$0"; exit "${1:-0}"; }

only="" from="" max="" selftest=1
skips=()
while (($#)); do
  case "$1" in
    --only)            only="${2:?--only needs a step number}"; shift ;;
    --from)            from="${2:?--from needs a step number}"; shift ;;
    --skip)            skips+=("${2:?--skip needs a step number}"); shift ;;
    --data-plane-only) max=20 ;;
    --no-selftest)     selftest=0 ;;
    -h|--help)         usage 0 ;;
    *)                 error "unknown option: $1"; usage 64 ;;
  esac
  shift
done

require_root
qlp_lock   # one run at a time; a run setup.sh started inherits setup.sh's lock
install -d -m 0750 "$QLP_LOG_DIR"
QLP_LOG_FILE="$QLP_LOG_DIR/install-$(date -u +%Y%m%dT%H%M%SZ).log"
export QLP_LOG_FILE
log "ql810wc-print-proxy install starting (log: $QLP_LOG_FILE)"
qlp_load_config
if qlp_settings_would_be_lost "$QLP_CONFIG_FILE"; then
  qlp_settings_hint "$QLP_REPO_ROOT/config.env" install.sh
  needs_human "To start from new settings instead, write a new config.env first: sudo ./setup.sh --new-config" \
              "--config-only (check it), then run sudo ./install.sh again."
fi

shopt -s nullglob
steps=("$QLP_REPO_ROOT"/scripts/[0-9][0-9]-*.sh)
((${#steps[@]})) || die "no steps found under scripts/"

ran=0 recorded=0
for step in "${steps[@]}"; do
  name="$(basename "$step")"
  num="${name%%-*}"
  [[ -n "$only" && "$num" != "$only" ]] && continue
  if [[ -n "$from" ]] && ((10#$num < 10#$from)); then continue; fi
  if [[ -n "$max" ]] && ((10#$num > 10#$max)); then continue; fi
  if printf '%s\n' "${skips[@]}" | grep -qxF "$num"; then log "skipping $name (--skip)"; continue; fi
  # Which config.env this install applies (qlp_record_config), noted before
  # the first step that changes the box: the read-only preflight does not.
  if ((recorded == 0)) && [[ "$num" != 00 ]]; then qlp_record_config; recorded=1; fi

  log "════ step $name ════"
  set +e
  QLP_STEP="${name%.sh}" bash "$step"
  rc=$?
  set -e
  case "$rc" in
    0) ok "step $name done" ;;
    3) _qlp_emit HUMAN "$_QLP_YEL" "step $name needs a human action (see above); fix it and re-run ./install.sh"
       exit 3 ;;
    *) die "step $name failed (exit $rc) — see $QLP_LOG_FILE" ;;
  esac
  ran=$((ran + 1))
done
((ran)) || die "no step matched the selection"

if ((selftest)) && [[ -z "$only" ]]; then
  log "════ selftest ════"
  if "$QLP_REPO_ROOT/selftest.sh"; then ok "install complete"
  else warn "installed, but selftest reported problems (see above)"; exit 1; fi
else
  ok "done ($ran step(s))"
fi
