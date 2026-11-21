#!/usr/bin/env bash
# scripts/40-discovery.sh — Bonjour/mDNS discovery (tier T2).
#
#   sudo scripts/40-discovery.sh        (normally run by ./install.sh)
#
# ENABLE_MDNS="yes": publishes an Avahi static service "_pdl-datastream._tcp"
# on RAW_PORT named "Brother QL-810W [<suffix>]" with the TXT records P-touch
# Editor for Mac requires (usb_MDL=QL-810W, UUID=PRINTER_UUID), and opens UDP
# 5353 only to MDNS_ALLOWED_CIDRS (default: SNMP_PRINTER_ALLOWED_CIDRS) with
# source-scoped firewalld rich rules, warning when those ranges admit no
# client (empty, the template's documentation-only examples, or none on this
# host's own networks). The zone-wide "mdns" service is never opened: mDNS is
# accepted only from the client ranges.
# ENABLE_MDNS="no": removes our service file and our 5353 rules (only ours).
# Idempotent; everything is recorded in the manifest.
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

readonly AVAHI_UNIT="avahi-daemon.service"
readonly AVAHI_SERVICE="$QLP_AVAHI_SERVICE"
readonly AVAHI_SERVICES_DIR="${AVAHI_SERVICE%/*}"
readonly AVAHI_TEMPLATE="$QLP_REPO_ROOT/agent/avahi/ql810wc-proxy.service.tmpl"
readonly SERVICE_TYPE="_pdl-datastream._tcp"
readonly MDNS_RULE_SELECTOR='port port="5353" protocol="udp"'
# P-touch Editor for Mac derives the model from this prefix ("Brother QL-810W [..]").
readonly NAME_PREFIX="Brother QL-810W"
readonly MAX_LABEL_BYTES=63   # a DNS-SD instance name is one DNS label
readonly MAX_TXT_BYTES=255    # one TXT string ("note=" + location)

INSTANCE_NAME=""   # "Brother QL-810W [<suffix>]"
NOTE=""            # TXT note= (PERSONA_LOCATION)

# Read "avahi-browse --parsable --resolve" output and print the (unescaped)
# instance name of the resolved service whose TXT carries our UUID.
# avahi escapes name bytes as \DDD (decimal) and other specials as \X.
FIND_PY="$(cat <<'PY'
import sys

def unescape(text):
    out, i = bytearray(), 0
    while i < len(text):
        if text[i] == "\\" and len(text[i + 1:i + 4]) == 3 and text[i + 1:i + 4].isdigit():
            out.append(int(text[i + 1:i + 4]) & 0xFF)
            i += 4
        elif text[i] == "\\" and i + 1 < len(text):
            out += text[i + 1].encode()
            i += 2
        else:
            out += text[i].encode()
            i += 1
    return out.decode("utf-8", "replace")

wanted = '"UUID=%s"' % sys.argv[1]
for line in sys.stdin:
    fields = line.rstrip("\n").split(";")
    if len(fields) > 9 and fields[0] == "=" and wanted in ";".join(fields[9:]):
        print(unescape(fields[3]))
        break
PY
)"

compose_names() {
  local suffix="${PRINTER_NAME_SUFFIX:-}" room short loc note
  if [[ -n "${PRINTER_DISPLAY_NAME:-}" ]]; then
    warn "PRINTER_DISPLAY_NAME is deprecated and ignored — the name is '$NAME_PREFIX [<PRINTER_NAME_SUFFIX>]'"
  fi
  if [[ -z "$suffix" ]]; then suffix="$(qlp_short_hostname)"; fi
  if [[ "$suffix" == *[[:cntrl:]]* || "${PERSONA_LOCATION:-}" == *[[:cntrl:]]* ]]; then
    die "config: PRINTER_NAME_SUFFIX / PERSONA_LOCATION must not contain control characters"
  fi
  room=$((MAX_LABEL_BYTES - ${#NAME_PREFIX} - 3))   # " [" + "]"
  short="$(qlp_utf8_trunc "$room" "$suffix")"
  if [[ "$short" != "$suffix" ]]; then
    warn "Bonjour name suffix '$suffix' is longer than $room bytes — shortened to '$short'"
  fi
  INSTANCE_NAME="$NAME_PREFIX [$short]"
  loc="${PERSONA_LOCATION:-}"
  note="$(qlp_utf8_trunc $((MAX_TXT_BYTES - 5)) "$loc")"
  if [[ "$note" != "$loc" ]]; then
    warn "PERSONA_LOCATION is longer than $((MAX_TXT_BYTES - 5)) bytes — shortened for the Bonjour note"
  fi
  NOTE="$note"
}

publish_service() {
  local text
  [[ -d "$AVAHI_SERVICES_DIR" ]] || die "$AVAHI_SERVICES_DIR not found — is Avahi installed? (run step 10)"
  [[ -n "${PRINTER_UUID:-}" ]] || die "no PRINTER_UUID (identity.env)"
  text="$(qlp_render --xml "$AVAHI_TEMPLATE" "INSTANCE_NAME=$INSTANCE_NAME" "RAW_PORT=$RAW_PORT" \
           "PRINTER_UUID=$PRINTER_UUID" "NOTE=$NOTE")"
  python3 -c 'import sys, xml.etree.ElementTree as ET; ET.fromstring(sys.stdin.buffer.read())' <<<"$text" \
    || die "the generated Avahi service file is not well-formed XML (template: $AVAHI_TEMPLATE)"
  qlp_write_file "$AVAHI_SERVICE" 0644 <<<"$text"
  if ((QLP_CHANGED)) && systemctl is-active --quiet "$AVAHI_UNIT"; then
    # avahi-daemon also notices the file by itself (inotify); reloading makes it immediate.
    run systemctl reload "$AVAHI_UNIT" || warn "could not reload $AVAHI_UNIT (it picks the file up by itself)"
  fi
  qlp_enable_now "$AVAHI_UNIT"
  qlp_require_active "$AVAHI_UNIT"
  ok "Bonjour: '$INSTANCE_NAME' ($SERVICE_TYPE, port $RAW_PORT)"
}

unpublish_service() {
  qlp_remove_created "$AVAHI_SERVICE"
  if ((QLP_CHANGED)) && systemctl is-active --quiet "$AVAHI_UNIT"; then
    run systemctl reload "$AVAHI_UNIT" || warn "could not reload $AVAHI_UNIT (it notices the removal by itself)"
  fi
  log "ENABLE_MDNS=no — no Bonjour record published"
}

# UDP 5353 from the client ranges only (never the zone-wide "mdns" service).
configure_firewall() { # publish|unpublish
  local zone src cidrs fam cidr
  local -a list=() rules=()
  if ! qlp_fw_active; then
    if [[ "$1" == publish ]]; then warn "firewalld is not active — UDP 5353 is not managed"; fi
    return 0
  fi
  zone="$(qlp_fw_zone)"
  if [[ "$1" == unpublish ]]; then
    qlp_fw_sync_rich "$zone" "$MDNS_RULE_SELECTOR"
    return 0
  fi
  src="$(qlp_mdns_source_setting)"
  read -r -a list <<<"${!src:-}"
  cidrs="$(qlp_norm_cidrs "$src" "${list[@]}")"
  while IFS=$'\t' read -r fam cidr; do
    [[ -n "$fam" ]] || continue
    if qlp_cidr_is_loopback "$cidr"; then continue; fi
    rules+=("$(qlp_fw_rich_rule "$fam" "$cidr" 5353 udp)")
  done <<<"$cidrs"
  qlp_fw_sync_rich "$zone" "$MDNS_RULE_SELECTOR" "${rules[@]}"
  if ((${#rules[@]})); then ok "firewall: UDP 5353 open in zone '$zone' for ${#rules[@]} source range(s) from $src"; fi
  warn_mdns_reach "$src" ${list[@]+"${list[@]}"}
}

# The rules above admit no Bonjour client when the ranges hold none (empty,
# or only the template's documentation-only examples), or when none of them
# is on this host's own networks: mDNS queries never cross a router. Warnings
# only, so lab setups still install.
warn_mdns_reach() { # setting [cidr...]
  local src="$1" why=""
  shift
  qlp_classify_sources "$@" || return 0
  why="$(qlp_sources_problem "$src" "$@")"
  if [[ -n "$why" ]]; then
    warn "$why: firewalld drops every Bonjour query, so Macs will not discover the printer. Set $src to" \
         "your clients' subnet in config.env and re-run."
  elif [[ -n "$QLP_SRC_SUBNETS" && -z "$QLP_SRC_LOCAL" ]]; then
    warn "no range in $src ($QLP_SRC_USABLE) is a subnet of this host ($QLP_SRC_SUBNETS): mDNS queries" \
         "come only from the local network, so firewalld drops them and Macs will not discover the printer"
  fi
}

# Look the record up the way a client would (through the local avahi-daemon).
verify_published() {
  local found=""
  have avahi-browse || { log "avahi-browse not installed — skipping the Bonjour check"; return 0; }
  for _ in 1 2 3; do
    found="$(timeout 15 avahi-browse --resolve --parsable --terminate "$SERVICE_TYPE" 2>/dev/null \
             | python3 -c "$FIND_PY" "$PRINTER_UUID" 2>/dev/null || true)"
    if [[ -n "$found" ]]; then break; fi
    sleep 2
  done
  if [[ -z "$found" ]]; then
    warn "the Bonjour record is not visible through avahi-browse yet (see: journalctl -u $AVAHI_UNIT)"
  elif [[ "$found" != "$INSTANCE_NAME" ]]; then
    warn "Avahi published the service as '$found': another device on the network already uses" \
         "'$INSTANCE_NAME'. Set a unique PRINTER_NAME_SUFFIX in config.env and re-run."
  else
    ok "Bonjour record visible: '$found'"
  fi
}

main() {
  qlp_init 40-discovery
  require_root
  qlp_load_config
  qlp_require_bool ENABLE_MDNS
  qlp_valid_port "${RAW_PORT:-}" || die "config: RAW_PORT='${RAW_PORT:-}' is not a TCP port (1-65535)"
  require_cmd python3 systemctl
  qlp_ensure_identity
  qlp_sync_runtime_env
  # ql810wc-proxyd restarts (a job in flight gets to finish first); the SNMP
  # agent applies new settings in its running AgentX session, so SNMP never
  # pauses (it does not read PRINTER_UUID at all).
  if ((QLP_RUNTIME_CHANGED || QLP_IDENTITY_CHANGED)); then
    qlp_try_restart "$QLP_UNIT_PROXYD"
    qlp_try_reload "$QLP_UNIT_AGENT"
  fi
  qlp_passpersist_refresh   # SNMP_AGENT_MODE=pass_persist: the persona lives inside snmpd
  if qlp_is_yes "$ENABLE_MDNS"; then
    compose_names
    publish_service
    configure_firewall publish
    verify_published
  else
    unpublish_service
    configure_firewall unpublish
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
