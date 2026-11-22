#!/usr/bin/env bash
# scripts/20-cups-queue.sh — the data plane (tiers T0/T1): ql810wc-proxyd
# forwards raw jobs from TCP RAW_PORT to the USB printer, byte for byte.
#
#   sudo scripts/20-cups-queue.sh        (normally run by ./install.sh)
#
# What it sets up (all idempotent, all recorded in the manifest):
#   * system user ql810wc (group lp) and the code in /usr/local/libexec/ql810wc-proxy
#   * /etc/ql810wc-proxy/runtime.env (non-secret settings) and identity.env
#   * udev rule: group lp may use the Brother USB device (libusb fallback)
#   * keeps ipp-usb and CUPS' own usb backend away from the printer
#   * ql810wc-proxyd.socket/.service (systemd socket activation on RAW_PORT)
#   * firewalld: RAW_PORT/tcp zone-wide, or only from RAW_ALLOWED_CIDRS
#   * optional raw CUPS queue CUPS_QUEUE -> socket://127.0.0.1:RAW_PORT; a
#     CUPS that step 10 installed is started for it (cups.socket). A CUPS that
#     was there before and is stopped stays stopped: the step then ends with
#     exit status 3 and says what to do.
#   * ql810wc-snmp-agent (when running) restarted for new code, and reloaded
#     (same AgentX session, no SNMP gap) when only its settings, the printer
#     identity or the OID map changed
#   * SNMP_AGENT_MODE=pass_persist: snmpd restarted when the persona's code or
#     settings changed (its pass_persist children load them only once)
# Other CUPS queues, Cockpit (9090) and CUPS' own port (631) are never touched.
# A missing printer (or one in Editor Lite mode) is reported, not fatal:
# ql810wc-proxyd waits for it.
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

readonly UDEV_RULE="/etc/udev/rules.d/60-ql810wc-proxy.rules"
readonly IPP_USB_QUIRK="$QLP_IPP_USB_QUIRK"
readonly IPP_USB_DEFAULT_DEVICE="Brother QL-810W"
readonly CUPS_USB_QUIRK_DIR="$QLP_CUPS_USB_QUIRK_DIR"
readonly CUPS_USB_QUIRK="$QLP_CUPS_USB_QUIRK"
readonly CUPS_QUEUE_INFO="QL-810Wc via ql810wc-proxy (raw)"
readonly CUPS_TIMEOUT_S=30   # bound every CUPS client call (a wedged cupsd must not hang the install)
CUPS_START_WAIT_S=15         # how long a just-started cups.socket may take to answer (tests: shorter)
# Every TCP rich rule this installer adds is for RAW_PORT.
readonly RAW_RULE_SELECTOR='protocol="tcp"'

CODE_CHANGED=0 SOCKET_CHANGED=0 SERVICE_CHANGED=0
# Which daemon runs what changed: ql810wc-proxyd runs ql810wc-proxyd and the
# ql810wc package, ql810wc-snmp-agent runs brother_snmp_agent.py and the same
# package, and only the agent reads oid-map.yaml (it reloads that). qlctl is a
# command, not a daemon.
PROXYD_CODE_CHANGED=0 AGENT_CODE_CHANGED=0 OIDMAP_CHANGED=0
RAW_BIND_KIND=""          # empty | any | loopback | ipv4 | ipv6
CUPS_RESULT="not configured"
CUPS_HUMAN=""             # what a human must do so the CUPS queue can be set up (exit 3 at the end)
declare -A DEPLOYED=()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
validate_config() {
  local re_queue='^[A-Za-z0-9_.-]{1,127}$'
  qlp_valid_port "${RAW_PORT:-}" || die "config: RAW_PORT='${RAW_PORT:-}' is not a TCP port (1-65535)"
  qlp_require_bool CUPS_QUEUE_SHARED IPP_USB_EXCLUDE
  # RAW_BIDIRECTIONAL, RAW_COLOR_CHECK, RAW_MEDIA_CHECK, STATUS_POLL_SECONDS, RAW_IDLE_TIMEOUT,
  # MAX_JOB_BYTES: checked before anything changes (qlp_sync_runtime_env checks them again).
  qlp_validate_runtime_settings
  [[ "${USB_VENDOR_ID:-}" =~ ^[0-9A-Fa-f]{4}$ ]] || die "config: USB_VENDOR_ID='${USB_VENDOR_ID:-}' must be 4 hex digits"
  [[ -z "${USB_PRODUCT_ID:-}" || "$USB_PRODUCT_ID" =~ ^[0-9A-Fa-f]{4}$ ]] \
    || die "config: USB_PRODUCT_ID='$USB_PRODUCT_ID' must be empty or 4 hex digits"
  if [[ -n "${PROXY_IP:-}" ]]; then
    python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$PROXY_IP" 2>/dev/null \
      || die "config: PROXY_IP='$PROXY_IP' is not an IPv4 address"
  fi
  if [[ -n "${RAW_BIND:-}" ]]; then
    RAW_BIND_KIND="$(python3 -c '
import ipaddress, sys
a = ipaddress.ip_address(sys.argv[1])
print("any" if a.is_unspecified else "loopback" if a.is_loopback else "ipv%d" % a.version)' "$RAW_BIND" 2>/dev/null)" \
      || die "config: RAW_BIND='$RAW_BIND' is not an IP address"
    case "$RAW_BIND_KIND" in
      loopback) warn "RAW_BIND=$RAW_BIND: only this host can print; network clients cannot connect" ;;
      ipv4|ipv6)
        local addrs
        addrs="$(ip -o addr show 2>/dev/null | awk '{ split($4, a, "/"); print a[1] }' || true)"
        if ! grep -qixF -- "$RAW_BIND" <<<"$addrs"; then
          warn "RAW_BIND=$RAW_BIND is not (yet) an address of this host; the listener binds it anyway (FreeBind)"
        fi ;;
    esac
  fi
  if [[ -n "${CUPS_QUEUE:-}" && ! "$CUPS_QUEUE" =~ $re_queue ]]; then
    die "config: CUPS_QUEUE='$CUPS_QUEUE' — use letters, digits, '.', '_' and '-' only (or leave it empty)"
  fi
}

# ---------------------------------------------------------------------------
# Printer presence (informational: the daemon waits for the printer)
# ---------------------------------------------------------------------------
report_printer() {
  if ! qlp_detect_printer; then
    qlp_human "No Brother QL printer on USB right now. Installing anyway; ql810wc-proxyd waits for it." \
              "Connect the QL-810Wc by USB and power it on, with Editor Lite OFF (green LED dark)."
    return 0
  fi
  log "found ${QL_USB_MANUFACTURER:-?} '$QL_USB_PRODUCT' $QL_USB_VID:$QL_USB_PID (serial $(qlp_mask "$QL_USB_SERIAL"))"
  case "$QL_USB_MODE" in
    printer)      ok "printer interface present (USB class 07)" ;;
    mass-storage) qlp_human "Editor Lite is ON (the printer shows up as a USB drive). Press and hold the" \
                            "Editor Lite button until its green LED turns off; ql810wc-proxyd then picks the printer up by itself." ;;
    *)            warn "the printer shows no printer interface right now (unexpected USB classes)" ;;
  esac
}

# ---------------------------------------------------------------------------
# Code
# ---------------------------------------------------------------------------
deploy_one() { # src dest mode [flag...]   (flags set when the file changed)
  local flag
  [[ -f "$1" ]] || die "missing $1 (incomplete checkout?)"
  qlp_install_file "$1" "$2" "$3"
  if ((QLP_CHANGED)); then
    CODE_CHANGED=1
    for flag in "${@:4}"; do printf -v "$flag" '%s' 1; done
  fi
  DEPLOYED["$2"]=1
}

deploy_code() {
  local f path
  local -a pkg=()
  qlp_ensure_dir "$QLP_LIB_DIR" 0755
  qlp_ensure_dir "$QLP_LIB_DIR/ql810wc" 0755
  DEPLOYED["$QLP_LIB_DIR/ql810wc"]=1
  deploy_one "$QLP_REPO_ROOT/agent/ql810wc-proxyd" "$QLP_LIB_DIR/ql810wc-proxyd" 0755 PROXYD_CODE_CHANGED
  deploy_one "$QLP_REPO_ROOT/agent/qlctl" "$QLP_LIB_DIR/qlctl" 0755
  deploy_one "$QLP_REPO_ROOT/agent/brother_snmp_agent.py" "$QLP_LIB_DIR/brother_snmp_agent.py" 0755 \
    AGENT_CODE_CHANGED
  deploy_one "$QLP_REPO_ROOT/agent/oid-map.yaml" "$QLP_LIB_DIR/oid-map.yaml" 0644 OIDMAP_CHANGED
  shopt -s nullglob
  pkg=("$QLP_REPO_ROOT"/agent/ql810wc/*.py)
  shopt -u nullglob
  ((${#pkg[@]})) || die "no Python files in $QLP_REPO_ROOT/agent/ql810wc (incomplete checkout?)"
  for f in "${pkg[@]}"; do
    deploy_one "$f" "$QLP_LIB_DIR/ql810wc/${f##*/}" 0644 PROXYD_CODE_CHANGED AGENT_CODE_CHANGED
  done
  # The directory is ours alone: whatever this version does not ship is stale
  # (files of older versions, stray __pycache__). Either daemon may have used it.
  while IFS= read -r -d '' path; do
    if [[ -z "${DEPLOYED[$path]:-}" ]]; then
      rm -f -- "$path"
      qlp_manifest_del created "$path"
      CODE_CHANGED=1 PROXYD_CODE_CHANGED=1 AGENT_CODE_CHANGED=1
      log "removed stale $path"
    fi
  done < <(find "$QLP_LIB_DIR" -mindepth 1 ! -type d -print0)
  while IFS= read -r -d '' path; do
    if [[ -z "${DEPLOYED[$path]:-}" ]] && rmdir -- "$path" 2>/dev/null; then
      qlp_manifest_del created "$path"
      log "removed stale directory $path"
    fi
  done < <(find "$QLP_LIB_DIR" -mindepth 1 -depth -type d -print0)
  if ((CODE_CHANGED)); then ok "code deployed to $QLP_LIB_DIR"; else log "code in $QLP_LIB_DIR is up to date"; fi
}

# ---------------------------------------------------------------------------
# USB access: udev, ipp-usb, CUPS' usb backend, usblp
# ---------------------------------------------------------------------------
install_udev_rule() {
  local vid="${USB_VENDOR_ID,,}" text
  text="$(qlp_render "$QLP_REPO_ROOT/agent/udev/60-ql810wc-proxy.rules" "USB_VENDOR_ID=$vid")"
  qlp_write_file "$UDEV_RULE" 0644 <<<"$text"
  if ((QLP_CHANGED)); then
    run udevadm control --reload
    # "change" re-applies permissions to an attached printer without replaying
    # "add" (which would also wake ipp-usb's own rule).
    run udevadm trigger --action=change --subsystem-match=usb --attr-match="idVendor=$vid"
    udevadm settle --timeout=10 || warn "udevadm settle timed out"
  fi
}

ipp_usb_installed() {
  have ipp-usb || systemctl cat ipp-usb.service >/dev/null 2>&1
}

# ipp-usb matches quirk sections against "<USB manufacturer> <USB product>".
ipp_usb_device_name() {
  local re='^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,63}$' mfg prod cand
  mfg="$(printf '%s' "${QL_USB_MANUFACTURER:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  prod="$(printf '%s' "${QL_USB_PRODUCT:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  if [[ -n "$mfg" && -n "$prod" ]]; then
    cand="$mfg $prod"
    if [[ "$cand" =~ $re ]]; then printf '%s' "$cand"; return 0; fi
    warn "unusual USB name '$cand' — using '$IPP_USB_DEFAULT_DEVICE' for the ipp-usb quirk"
  fi
  printf '%s' "$IPP_USB_DEFAULT_DEVICE"
}

# Is any USB device other than the QL an IPP-over-USB printer (ipp-usb's own
# udev match), i.e. does ipp-usb still have work to do?
other_ipp_usb_devices() {
  local d props
  for d in /sys/bus/usb/devices/*; do
    [[ -f "$d/idVendor" && "$d" != "${QL_USB_PATH:-}" ]] || continue
    props="$(udevadm info --query=property --path="$d" 2>/dev/null || true)"
    if grep -q '^ID_USB_INTERFACES=.*:070104:' <<<"$props"; then
      return 0
    fi
  done
  return 1
}

exclude_from_ipp_usb() {
  local text changed name
  if ! qlp_is_yes "$IPP_USB_EXCLUDE"; then
    qlp_remove_created "$IPP_USB_QUIRK"
    if ((QLP_CHANGED)); then log "IPP_USB_EXCLUDE=no: ipp-usb may claim the printer again the next time it is plugged in"; fi
    return 0
  fi
  name="$(ipp_usb_device_name)"
  text="$(qlp_render "$QLP_REPO_ROOT/agent/ipp-usb/ql810wc-proxy.conf" "IPP_USB_DEVICE=$name")"
  # Written even when ipp-usb is absent: a later package update may pull it in
  # (it is a weak dependency of cups).
  qlp_ensure_dir /etc/ipp-usb 0755
  qlp_ensure_dir /etc/ipp-usb/quirks 0755
  qlp_write_file "$IPP_USB_QUIRK" 0644 <<<"$text"
  changed=$QLP_CHANGED
  if ! ipp_usb_installed; then
    log "ipp-usb is not installed; its exclusion of [$name] is in place should it be installed later"
    return 0
  fi
  ok "ipp-usb ignores [$name]"
  # A running ipp-usb read its quirks at start-up and may hold the printer.
  if systemctl is-active --quiet ipp-usb.service 2>/dev/null \
     && { ((changed)) || [[ "${QL_USB_MODE:-}" == printer && -z "${QL_USB_LP:-}" ]]; }; then
    log "stopping ipp-usb so it releases the printer (usblp can then bind) and re-reads its quirks"
    run systemctl stop ipp-usb.service
    if other_ipp_usb_devices; then
      log "other IPP-over-USB devices are attached — starting ipp-usb again for them"
      run systemctl start ipp-usb.service
    fi
  fi
}

# CUPS' usb backend opens every USB printer whenever devices are listed
# (lpinfo -v, the web interface's "Add Printer" page) and detaches usblp from
# it — in the middle of a job, for us. A blacklist quirk makes it skip the QL.
exclude_from_cups_usb() {
  local vid="${USB_VENDOR_ID,,}" pid="${USB_PRODUCT_ID,,}"
  if [[ -z "$pid" && "${QL_USB_MODE:-}" == printer ]]; then pid="${QL_USB_PID,,}"; fi
  if [[ ! -d "$CUPS_USB_QUIRK_DIR" ]]; then
    log "$CUPS_USB_QUIRK_DIR does not exist (CUPS usb backend absent) — nothing to exclude"
    return 0
  fi
  if [[ ! "$pid" =~ ^[0-9a-f]{4}$ ]]; then
    if [[ -f "$CUPS_USB_QUIRK" ]]; then log "keeping $CUPS_USB_QUIRK (printer not in printer mode right now)"
    else log "printer USB product id unknown right now — the CUPS usb exclusion is added by a re-run with the printer attached"; fi
    return 0
  fi
  qlp_write_file "$CUPS_USB_QUIRK" 0644 <<EOF
# Installed by $QLP_NAME (scripts/20-cups-queue.sh); removed by uninstall.sh.
# CUPS' usb backend must never open the QL label printer: listing USB devices
# (lpinfo -v, "Add Printer") would detach usblp from it in the middle of a job.
# CUPS reaches the printer through ql810wc-proxyd (socket://) instead.
0x$vid 0x$pid blacklist
EOF
}

# The proxy prefers the kernel usblp driver (/dev/usb/lpN). ipp-usb may have
# left the printer interface without a driver; bind it again.
ensure_usblp_bound() {
  local intf name drv
  [[ "${QL_USB_MODE:-}" == printer ]] || return 0
  if [[ -n "${QL_USB_LP:-}" ]]; then ok "printer bound to usblp ($QL_USB_LP)"; return 0; fi
  log "the printer has no /dev/usb/lp* node — binding the kernel usblp driver"
  if [[ ! -d /sys/bus/usb/drivers/usblp ]]; then
    run modprobe usblp || warn "could not load the usblp kernel module"
  fi
  for intf in "$QL_USB_PATH"/*:*; do
    [[ -f "$intf/bInterfaceClass" ]] || continue
    [[ "$(_qlp_read "$intf/bInterfaceClass")" == 07 && "$(_qlp_read "$intf/bInterfaceSubClass")" == 01 ]] || continue
    case "$(_qlp_read "$intf/bInterfaceProtocol")" in 01|02|03) ;; *) continue ;; esac
    name="${intf##*/}"
    if [[ -e "$intf/driver" ]]; then
      drv="$(basename "$(readlink -f "$intf/driver")")"
      log "printer interface $name is held by driver '$drv' — left alone"
      continue
    fi
    if { printf '%s' "$name" >/sys/bus/usb/drivers/usblp/bind; } 2>/dev/null; then
      log "bound usblp to printer interface $name"
    elif { printf '%s' "$name" >/sys/bus/usb/drivers_probe; } 2>/dev/null; then
      log "asked the kernel to probe drivers for printer interface $name"
    else
      log "re-triggering udev for printer interface $name"
      udevadm trigger --action=add "$intf" 2>/dev/null || warn "udevadm trigger failed for $name"
    fi
  done
  udevadm settle --timeout=10 2>/dev/null || true
  for _ in {1..20}; do
    qlp_detect_printer || break
    [[ -z "$QL_USB_LP" ]] || break
    sleep 0.25
  done
  if [[ -n "${QL_USB_LP:-}" ]]; then
    ok "printer bound to usblp ($QL_USB_LP)"
  else
    warn "no /dev/usb/lp* node for the printer; ql810wc-proxyd uses its libusb fallback." \
         "If printing fails, unplug and re-plug the printer's USB cable."
  fi
}

# ---------------------------------------------------------------------------
# systemd units
# ---------------------------------------------------------------------------
check_port_free() {
  local owner listen listeners
  have ss || return 0
  listeners="$(ss -Hltn "sport = :$RAW_PORT" 2>/dev/null || true)"
  [[ -n "$listeners" ]] || return 0
  listen="$(systemctl show -p Listen --value "$QLP_UNIT_SOCKET" 2>/dev/null || true)"
  if systemctl is-active --quiet "$QLP_UNIT_SOCKET" 2>/dev/null && [[ "$listen" == *":$RAW_PORT (Stream)"* ]]; then
    return 0   # our own listener from an earlier run
  fi
  owner="$(ss -Hltnp "sport = :$RAW_PORT" 2>/dev/null | grep -o 'users:(([^)]*' | head -n 1 || true)"
  die "TCP $RAW_PORT is already in use by ${owner:-another program} — stop it or choose another RAW_PORT" \
      "(P-touch Editor for Mac only prints to 9100)"
}

install_units() {
  local text
  # The .socket holds the listener, so it alone gets RAW_BIND in its
  # ListenStream= line; the .service only has its placeholders substituted.
  text="$(qlp_render_unit "$QLP_REPO_ROOT/agent/systemd/$QLP_UNIT_SOCKET")"
  qlp_write_file "$QLP_SYSTEMD_DIR/$QLP_UNIT_SOCKET" 0644 <<<"$text"
  SOCKET_CHANGED=$QLP_CHANGED
  text="$(qlp_render_unit "$QLP_REPO_ROOT/agent/systemd/$QLP_UNIT_PROXYD")"
  qlp_write_file "$QLP_SYSTEMD_DIR/$QLP_UNIT_PROXYD" 0644 <<<"$text"
  SERVICE_CHANGED=$QLP_CHANGED
  if ((SOCKET_CHANGED || SERVICE_CHANGED)); then qlp_daemon_reload; fi
}

start_data_plane() {
  local was_active=0
  if systemctl is-active --quiet "$QLP_UNIT_PROXYD"; then was_active=1; fi
  if ((SOCKET_CHANGED)) && systemctl is-active --quiet "$QLP_UNIT_SOCKET"; then
    log "listener settings changed — restarting the socket and the daemon"
    run systemctl stop "$QLP_UNIT_PROXYD" "$QLP_UNIT_SOCKET"
    was_active=0
  fi
  qlp_enable_now "$QLP_UNIT_SOCKET"
  qlp_enable_now "$QLP_UNIT_PROXYD"
  if ((was_active)) && ((SERVICE_CHANGED || PROXYD_CODE_CHANGED || QLP_RUNTIME_CHANGED || QLP_IDENTITY_CHANGED || QLP_USER_CHANGED)); then
    log "code or settings changed — restarting $QLP_UNIT_PROXYD"
    run systemctl restart "$QLP_UNIT_PROXYD"
  fi
  qlp_require_active "$QLP_UNIT_SOCKET"
  qlp_require_active "$QLP_UNIT_PROXYD"
  # The SNMP agent (when running). New code needs a new process: a process
  # older than its SIGHUP handler would even die of a reload. Settings, the
  # identity and the OID map are applied in its running AgentX session: a
  # restart would leave snmpd answering noSuchName until the agent registers
  # again, which fails a P-touch print that is polling the status.
  if ((AGENT_CODE_CHANGED)); then
    qlp_try_restart "$QLP_UNIT_AGENT"
  elif ((QLP_RUNTIME_CHANGED || QLP_IDENTITY_CHANGED || OIDMAP_CHANGED)); then
    qlp_try_reload "$QLP_UNIT_AGENT"
  fi
  ok "$QLP_UNIT_SOCKET listening on ${RAW_BIND:-*}:$RAW_PORT; $QLP_UNIT_PROXYD running"
}

# ---------------------------------------------------------------------------
# Firewall (RAW_PORT/tcp only)
# ---------------------------------------------------------------------------
configure_firewall() {
  local zone cidrs fam cidr
  local -a list=() rules=()
  if ! qlp_fw_active; then
    warn "firewalld is not active — TCP $RAW_PORT is not managed (open it in your firewall if needed)"
    return 0
  fi
  zone="$(qlp_fw_zone)"
  read -r -a list <<<"${RAW_ALLOWED_CIDRS:-}"
  if ((${#list[@]} == 0)); then
    qlp_fw_sync_ports "$zone" tcp "$RAW_PORT"
    qlp_fw_sync_rich "$zone" "$RAW_RULE_SELECTOR"
    ok "firewall: TCP $RAW_PORT open in zone '$zone'"
    return 0
  fi
  cidrs="$(qlp_norm_cidrs RAW_ALLOWED_CIDRS "${list[@]}")"
  while IFS=$'\t' read -r fam cidr; do
    [[ -n "$fam" ]] || continue
    if qlp_cidr_is_loopback "$cidr"; then continue; fi
    rules+=("$(qlp_fw_rich_rule "$fam" "$cidr" "$RAW_PORT" tcp)")
  done <<<"$cidrs"
  qlp_fw_sync_rich "$zone" "$RAW_RULE_SELECTOR" "${rules[@]}"
  qlp_fw_sync_ports "$zone" tcp
  if firewall-cmd --zone="$zone" --query-port="$RAW_PORT/tcp" >/dev/null 2>&1; then
    warn "firewall: TCP $RAW_PORT is open zone-wide by a rule this installer did not add;" \
         "RAW_ALLOWED_CIDRS has no effect until that rule is removed"
  fi
  ok "firewall: TCP $RAW_PORT open in zone '$zone' for ${list[*]}"
}

# ---------------------------------------------------------------------------
# Local CUPS queue (raw; its device is the proxy, never USB)
# ---------------------------------------------------------------------------
cups_device_uri() {
  local host="127.0.0.1"
  case "$RAW_BIND_KIND" in
    loopback|ipv4) host="$RAW_BIND" ;;
    ipv6)          host="[$RAW_BIND]" ;;
  esac
  printf 'socket://%s:%s' "$host" "$RAW_PORT"
}

# A CUPS client command, bounded in time.
cups() { timeout "$CUPS_TIMEOUT_S" "$@"; }

# device-uri, printer-is-shared and printer-info of a queue, tab-separated.
cups_queue_attrs() { # queue
  { cups lpoptions -p "$1" 2>/dev/null || true; } | python3 -c '
import shlex, sys
opts = {}
for token in shlex.split(sys.stdin.read()):
    key, _, value = token.partition("=")
    opts[key] = value
print("\t".join(opts.get(k, "") for k in ("device-uri", "printer-is-shared", "printer-info")))'
}

# Does the CUPS scheduler answer? lpstat -r exits 0 either way, so
# qlp_cups_running reads its message. (lpstat -r also socket-activates cupsd.)
cups_reachable() { have lpadmin && qlp_cups_running "$CUPS_TIMEOUT_S"; }

# A CUPS that step 10 installed has never run: its package presets enable
# cups.socket and cups.path but start neither before the next boot. Start the
# socket (cupsd then starts on the first request) and wait for the scheduler.
# Returns 1 when it does not answer in time.
start_our_cups() {
  local tries
  log "starting CUPS for the local queue (installed by step 10; cups.socket starts cupsd on demand)"
  if ! systemctl is-enabled --quiet cups.socket 2>/dev/null; then
    run systemctl enable cups.socket || return 1
    qlp_manifest_add unit cups.socket
  fi
  if ! systemctl is-active --quiet cups.socket 2>/dev/null; then run systemctl start cups.socket || return 1; fi
  for ((tries = 0; tries < CUPS_START_WAIT_S; tries++)); do
    if cups_reachable; then ok "the CUPS scheduler is running"; return 0; fi
    sleep 1
  done
  return 1
}

# The CUPS scheduler for the local queue. A CUPS that was installed before
# this installer is the admin's: when it is stopped, it stays stopped and
# CUPS_HUMAN says what to do. Returns 1 when the scheduler cannot be reached.
ensure_cups_reachable() {
  local q="${CUPS_QUEUE:-}"
  if cups_reachable; then return 0; fi
  if ! have lpadmin; then
    CUPS_HUMAN="CUPS_QUEUE='$q' needs CUPS, which is not installed: run sudo ./install.sh (step 10 installs it),"
    CUPS_HUMAN+=" or set CUPS_QUEUE=\"\" in config.env"
  elif qlp_manifest_has "$(printf 'pkg\tcups')"; then
    if start_our_cups; then return 0; fi
    CUPS_HUMAN="the CUPS scheduler (installed by step 10) does not answer after starting cups.socket: check"
    CUPS_HUMAN+=" 'systemctl status cups.socket cups.service', then re-run sudo ./install.sh (or set CUPS_QUEUE=\"\")"
  else
    CUPS_HUMAN="CUPS_QUEUE='$q' needs the CUPS scheduler, which is installed but not running (it was there"
    CUPS_HUMAN+=" before this installer, so it is left as it is): start it with 'sudo systemctl enable --now"
    CUPS_HUMAN+=" cups.socket', or set CUPS_QUEUE=\"\" in config.env; then re-run sudo ./install.sh"
  fi
  return 1
}

configure_cups_queue() {
  local q="${CUPS_QUEUE:-}" old uri shared attrs cur_uri cur_shared cur_info out line rc=0
  local -a args=()
  if [[ -z "$q" && -z "$(qlp_manifest_records cupsq)" ]]; then
    log "CUPS_QUEUE is empty — no local CUPS queue"
    CUPS_RESULT="none (CUPS_QUEUE empty)"
    return 0
  fi
  if ! ensure_cups_reachable; then
    if [[ -n "$q" ]]; then
      CUPS_RESULT="not set up (CUPS not reachable)"
      return 0
    fi
    # Only our earlier queues would be removed: no reason to stop the install.
    CUPS_HUMAN=""
    CUPS_RESULT="none (CUPS_QUEUE empty)"
    if ! have lpadmin; then
      while IFS= read -r old; do
        if [[ -n "$old" ]]; then qlp_manifest_del cupsq "$old"; fi
      done <<<"$(qlp_manifest_records cupsq)"
      log "CUPS is not installed any more: the CUPS queue(s) this installer created went with it"
    else
      warn "the CUPS scheduler is not reachable — the CUPS queue(s) this installer created earlier stay" \
           "until this step runs again with CUPS up"
    fi
    return 0
  fi
  # Queues we created earlier under another name (CUPS_QUEUE was changed).
  while IFS= read -r old; do
    [[ -n "$old" && "$old" != "$q" ]] || continue
    if cups lpstat -v "$old" >/dev/null 2>&1; then run cups lpadmin -x "$old"; fi
    qlp_manifest_del cupsq "$old"
    log "removed our earlier CUPS queue '$old'"
  done <<<"$(qlp_manifest_records cupsq)"
  if [[ -z "$q" ]]; then
    log "CUPS_QUEUE is empty — no local CUPS queue"
    CUPS_RESULT="none (CUPS_QUEUE empty)"
    return 0
  fi
  uri="$(cups_device_uri)"
  if qlp_is_yes "$CUPS_QUEUE_SHARED"; then shared=true; else shared=false; fi
  if cups lpstat -v "$q" >/dev/null 2>&1; then
    if ! qlp_manifest_has "$(printf 'cupsq\t%s' "$q")"; then
      warn "a CUPS queue named '$q' already exists and was not created by this installer — left untouched." \
           "Set CUPS_QUEUE to another name (or empty) in config.env."
      CUPS_RESULT="skipped (name '$q' taken by an existing queue)"
      return 0
    fi
    attrs="$(cups_queue_attrs "$q")"
    IFS=$'\t' read -r cur_uri cur_shared cur_info <<<"$attrs"
    if [[ "$cur_uri" != "$uri" ]]; then args+=(-v "$uri"); fi
    if [[ "$cur_shared" != "$shared" ]]; then args+=(-o "printer-is-shared=$shared"); fi
    if [[ "$cur_info" != "$CUPS_QUEUE_INFO" ]]; then args+=(-D "$CUPS_QUEUE_INFO"); fi
    if ((${#args[@]})); then
      run cups lpadmin -p "$q" "${args[@]}"
      ok "CUPS queue '$q' updated"
    fi
    out="$(cups lpstat -p "$q" 2>/dev/null || true)"
    if [[ "$out" == *disabled* ]]; then run cups cupsenable "$q"; fi
    out="$(cups lpstat -a "$q" 2>/dev/null || true)"
    if [[ "$out" == *"not accepting"* ]]; then run cups cupsaccept "$q"; fi
  else
    log "+ lpadmin -p $q -E -v $uri -m raw -o printer-is-shared=$shared -o printer-error-policy=retry-job"
    out="$(cups lpadmin -p "$q" -E -v "$uri" -m raw -o "printer-is-shared=$shared" \
             -o printer-error-policy=retry-job -D "$CUPS_QUEUE_INFO" 2>&1)" || rc=$?
    while IFS= read -r line; do
      # CUPS 2.4 warns that raw queues are deprecated; they still work.
      if [[ -n "$line" && "$line" != *"Raw queues are deprecated"* ]]; then warn "lpadmin: $line"; fi
    done <<<"$out"
    if ((rc != 0)); then die "lpadmin could not create the CUPS queue '$q' (exit $rc)"; fi
    qlp_manifest_add cupsq "$q"
    ok "created CUPS queue '$q' -> $uri (shared: $shared)"
  fi
  CUPS_RESULT="'$q' -> $uri"
}

# Driverless queues that reached the QL through ipp-usb stop working once the
# QL is excluded from ipp-usb. Say so (they are left unchanged).
note_ipp_usb_queues() {
  local re='^device for ([^:]+): (.*)$' line name uri opts
  qlp_is_yes "$IPP_USB_EXCLUDE" || return 0
  have lpstat || return 0
  while IFS= read -r line; do
    [[ "$line" =~ $re ]] || continue
    name="${BASH_REMATCH[1]}" uri="${BASH_REMATCH[2]}"
    case "$uri" in
      ipp://localhost:6[0-9][0-9][0-9][0-9]/*|ipp://127.0.0.1:6[0-9][0-9][0-9][0-9]/*|ippusb://*) ;;
      *) continue ;;
    esac
    opts="$(cups lpoptions -p "$name" 2>/dev/null || true)"
    if [[ "$opts" == *QL-8* ]]; then
      uri="$(printf '%s\n' "$uri" | qlp_mask_serials)"   # device URIs may carry the serial
      log "note: CUPS queue '$name' ($uri) reached the QL through ipp-usb and stops working while" \
          "IPP_USB_EXCLUDE=yes (left unchanged; print through TCP $RAW_PORT or '${CUPS_QUEUE:-}' instead)"
    fi
  done <<<"$(cups lpstat -v 2>/dev/null || true)"
}

summary() {
  local state="not attached"
  if qlp_detect_printer; then state="$QL_USB_MODE${QL_USB_LP:+ via $QL_USB_LP}"; fi
  ok "data plane ready: TCP ${RAW_BIND:-*}:$RAW_PORT -> ql810wc-proxyd -> USB printer ($state); CUPS queue: $CUPS_RESULT"
}

main() {
  qlp_init 20-cups-queue
  require_root
  qlp_load_config
  require_cmd python3 systemctl udevadm getent useradd usermod find
  validate_config
  report_printer
  qlp_ensure_service_user
  deploy_code
  qlp_ensure_identity
  qlp_sync_runtime_env
  install_udev_rule
  exclude_from_ipp_usb
  exclude_from_cups_usb
  ensure_usblp_bound
  check_port_free
  install_units
  start_data_plane
  configure_firewall
  configure_cups_queue
  note_ipp_usb_queues
  summary
  qlp_passpersist_refresh
  if [[ -n "$CUPS_HUMAN" ]]; then needs_human "$CUPS_HUMAN"; fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
