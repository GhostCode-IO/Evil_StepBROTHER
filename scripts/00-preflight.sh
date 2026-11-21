#!/usr/bin/env bash
# scripts/00-preflight.sh — preflight check. READ-ONLY: changes nothing.
#
# Reports whether this box can run the proxy and what the later steps will
# do. Exit codes: 0 ready (warnings allowed), 1 blocking problem,
# 3 a human must do something physical first (e.g. turn Editor Lite off).
#
#   sudo scripts/00-preflight.sh      (root gives the complete picture)
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
qlp_init 00-preflight

declare -i N_FAIL=0 N_WARN=0 N_HUMAN=0
pass()  { ok   "PASS  $*"; }
note()  { log  "INFO  $*"; }
wfail() { warn "WARN  $*"; N_WARN+=1; }
bfail() { error "FAIL  $*"; N_FAIL+=1; }
human() { _qlp_emit HUMAN "$_QLP_YEL" "ACTION $*"; N_HUMAN+=1; }

IS_ROOT=0; [[ ${EUID:-$(id -u)} -eq 0 ]] && IS_ROOT=1

# --- configuration (warnings here; a failure only where a later step would stop)
section() { log "── $* ──"; }
section "configuration"
qlp_load_config
if [[ -n "$QLP_CONFIG_FILE" ]]; then
  pass "config loaded from $QLP_CONFIG_FILE"
  for v in SNMP_PRINTER_ALLOWED_CIDRS SNMP_V3_ALLOWED_CIDRS RAW_ALLOWED_CIDRS; do
    read -r -a cidrs <<<"${!v:-}"
    for c in "${cidrs[@]}"; do qlp_valid_cidr "$c" || bfail "$v: '$c' is not a valid CIDR"; done
  done
  qlp_valid_port "${RAW_PORT:-9100}" || bfail "RAW_PORT '${RAW_PORT:-}' is not a valid port"
else
  wfail "no config.env yet — create it with: sudo ./setup.sh --config-only (or by hand:" \
        "cp config.sample.env config.env && chmod 600 config.env)"
fi
# SNMPv3 host monitoring, decided as 30-snmp-agent decides it: "auto" turns it
# on once both passphrases are set, "yes" needs both (30-snmp-agent stops
# without them), "no" leaves it off whatever they hold.
v3_unset=()
for v in SNMP_V3_AUTH_PASS SNMP_V3_PRIV_PASS; do
  if [[ -z "${!v:-}" || "${!v}" == *CHANGE-ME* ]]; then v3_unset+=("$v"); fi
done
v3_mode="${SNMP_V3_ENABLE:-auto}"
if qlp_is_yes "$v3_mode"; then
  for v in ${v3_unset[@]+"${v3_unset[@]}"}; do
    bfail "$v is not set (empty, or still the template placeholder), and SNMP_V3_ENABLE=\"$v3_mode\" needs" \
          "it (30-snmp-agent would stop): set both SNMPv3 passphrases (sudo ./setup.sh --snmpv3 generates" \
          "them), or set SNMP_V3_ENABLE=\"no\""
  done
  if ((${#v3_unset[@]} == 0)); then note "SNMPv3 host monitoring on (SNMP_V3_ENABLE=\"$v3_mode\")"; fi
elif [[ "${v3_mode,,}" == auto ]]; then
  case "${#v3_unset[@]}" in
    0) note "SNMPv3 host monitoring on (SNMP_V3_ENABLE=auto, both passphrases set)" ;;
    1) wfail "${v3_unset[0]} is not set (empty, or still the template placeholder): with" \
             "SNMP_V3_ENABLE=auto, SNMPv3 host monitoring stays off until both passphrases are set" ;;
    *) note "SNMPv3 host monitoring off (SNMP_V3_ENABLE=auto, passphrases unset)" ;;
  esac
elif [[ "${v3_mode,,}" =~ ^(0|n|no|false|off)$ ]]; then
  note "SNMPv3 host monitoring off (SNMP_V3_ENABLE=\"$v3_mode\")"
else
  bfail "SNMP_V3_ENABLE='$v3_mode' must be auto, yes or no"
fi
# Client ranges that admit no real client (empty, or the template's
# documentation-only examples): the persona and the Bonjour record would be
# installed but unreachable. Warnings only, so lab setups still install.
reach_warning() { # setting what
  local why=""
  local -a list=()
  read -r -a list <<<"${!1:-}"
  if qlp_classify_sources ${list[@]+"${list[@]}"}; then why="$(qlp_sources_problem "$1" ${list[@]+"${list[@]}"})"; fi
  if [[ -n "$why" ]]; then
    wfail "$why: no network client could reach $2 — set it to your clients' subnet(s) in config.env"
  fi
}
reach_warning SNMP_PRINTER_ALLOWED_CIDRS "the printer persona (P-touch discovery and status)"
if qlp_is_yes "${ENABLE_MDNS:-yes}" && qlp_fw_active; then
  reach_warning "$(qlp_mdns_source_setting)" "the Bonjour record (firewalld drops their mDNS queries)"
fi

# --- platform ---------------------------------------------------------------
section "platform"
os_id="$(qlp_os_id)"; os_ver="$(qlp_os_version)"; arch="$(uname -m)"
if [[ "$os_id" == fedora && "$os_ver" == 44 ]]; then pass "Fedora $os_ver"
elif [[ "$os_id" == fedora ]]; then wfail "Fedora $os_ver (target is 44; may work)"
else bfail "OS '$os_id $os_ver' is not Fedora"; fi
case "$arch" in
  x86_64|aarch64) pass "arch $arch" ;;   # the tested unit: a Raspberry Pi 4 (aarch64)
  *)              bfail "arch $arch unsupported" ;;
esac
if ((IS_ROOT)); then pass "running as root"; else wfail "not root — some checks skipped (re-run with sudo)"; fi
note "kernel $(uname -r); host $(hostname)"

ip4="$(qlp_proxy_ip || true)"
if [[ -n "${PROXY_IP:-}" ]]; then
  if ip -o -4 addr show 2>/dev/null | awk '{split($4, a, "/"); print a[1]}' | grep -qxF "$PROXY_IP"; then
    pass "PROXY_IP $PROXY_IP is configured on this host"
  else
    bfail "PROXY_IP $PROXY_IP is not an address of this host"
  fi
else
  if [[ -n "$ip4" ]]; then note "PROXY_IP empty — would use default-route address $ip4"
  else wfail "PROXY_IP empty and no default IPv4 route found"; fi
fi

# --- USB printer ------------------------------------------------------------
section "USB printer"
if qlp_detect_printer; then
  note "found ${QL_USB_MANUFACTURER:-?} '${QL_USB_PRODUCT}' ${QL_USB_VID}:${QL_USB_PID} serial $(qlp_mask "$QL_USB_SERIAL") at ${QL_USB_PATH##*/}"
  case "$QL_USB_MODE" in
    printer)
      pass "printer interface present (USB class 07)"
      [[ "$QL_USB_PRODUCT" == *QL-810W* ]] || wfail "product '$QL_USB_PRODUCT' is not a QL-810W(c); untested model"
      if [[ -n "$QL_USB_LP" ]]; then note "usblp device node: $QL_USB_LP"
      else note "no usblp node bound (fine: libusb access will be used)"; fi
      ;;
    mass-storage)
      human "Editor Lite is ON (the printer presents as a USB drive). Press and hold the" \
            "Editor Lite button until its green LED turns OFF, then re-run this script." ;;
    *) bfail "printer found but exposes no printer interface (USB classes unexpected)" ;;
  esac
else
  if have lsusb && lsusb -d "${QLP_BROTHER_VID}:" >/dev/null 2>&1; then
    bfail "a Brother USB device is attached but not recognised as a QL printer:"
    while IFS= read -r line; do note "        $line"; done \
      < <({ lsusb -d "${QLP_BROTHER_VID}:" 2>/dev/null || true; } | qlp_mask_serials)
  else
    human "No Brother QL printer on USB. Connect the QL-810Wc by USB, power it on," \
          "make sure Editor Lite is OFF (green LED dark), then re-run."
  fi
fi
if ((IS_ROOT)); then
  # Serial numbers masked: this output gets pasted into public issues.
  note "recent kernel USB messages:"
  while IFS= read -r line; do note "        $line"; done < <(qlp_kernel_usb_messages 6)
fi
lp_nodes=(/dev/usb/lp*)
[[ -e "${lp_nodes[0]}" ]] && note "printer device nodes: ${lp_nodes[*]}"

# --- CUPS -------------------------------------------------------------------
section "CUPS"
if systemctl is-active --quiet cups 2>/dev/null; then
  pass "cups.service active"
  if qlp_cups_running 30; then pass "CUPS scheduler responding"; else wfail "the CUPS scheduler is not running"; fi
  if have lpstat; then
    # A usb:// device URI carries the printer's serial number: masked.
    while IFS= read -r line; do note "        existing queue: $line"; done \
      < <({ lpstat -v 2>/dev/null || true; } | { grep -i 'usb://brother' || true; } | qlp_mask_serials)
    lpstat -v "${CUPS_QUEUE:-QL810Wc}" >/dev/null 2>&1 && note "queue '${CUPS_QUEUE:-QL810Wc}' already exists (20-cups-queue will reconcile it)"
  fi
elif systemctl is-active --quiet cups.socket 2>/dev/null; then
  pass "cups.socket active (cupsd starts on the first request)"
elif ! qlp_pkg_installed cups; then
  note "cups not installed (10-deps installs it, and 20-cups-queue starts it for the local queue)"
elif [[ -z "${CUPS_QUEUE:-}" ]]; then
  note "cups installed but not running (fine: CUPS_QUEUE is empty, no local queue)"
elif qlp_manifest_has "$(printf 'pkg\tcups')"; then
  note "cups (installed by 10-deps) not running yet: 20-cups-queue starts cups.socket for the local queue"
else
  # Stopped by its admin: 20-cups-queue leaves it alone and ends with exit 3.
  wfail "cups installed but not running: start it (systemctl enable --now cups.socket) or set" \
        "CUPS_QUEUE=\"\" in config.env — 20-cups-queue needs it for the local queue '${CUPS_QUEUE}'"
fi
if have ss; then
  ss -Hltn 'sport = :631' 2>/dev/null | grep -q . && note "TCP 631 (IPP) is listening — left untouched"
fi

# --- SELinux ----------------------------------------------------------------
section "SELinux"
mode="$(qlp_selinux_mode)"
case "$mode" in
  Enforcing)  pass "SELinux Enforcing (stays enforcing)" ;;
  Permissive) wfail "SELinux Permissive — supported, but the target is Enforcing" ;;
  *)          wfail "SELinux $mode" ;;
esac

# --- firewalld --------------------------------------------------------------
section "firewall"
if qlp_fw_active; then
  if ((IS_ROOT)); then
    zone="$(qlp_fw_zone)"
    pass "firewalld active; zone for this host: $zone"
    svcs="$(firewall-cmd --zone="$zone" --list-services 2>/dev/null || true)"
    ports="$(firewall-cmd --zone="$zone" --list-ports 2>/dev/null || true)"
    note "zone services: ${svcs:-none}"
    note "zone ports:    ${ports:-none}"
    [[ " $svcs " == *" cockpit "* || " $ports " == *" 9090/tcp "* ]] && note "Cockpit (9090) open — will never be touched"
    [[ " $svcs " == *" ipp "* || " $ports " == *" 631/tcp "* ]] && note "IPP (631) open — will never be touched"
    if firewall-cmd --zone="$zone" --query-port="${RAW_PORT:-9100}/tcp" >/dev/null 2>&1; then
      note "${RAW_PORT:-9100}/tcp already open"
    else
      note "${RAW_PORT:-9100}/tcp not open yet (20-cups-queue opens it)"
    fi
    if [[ " $svcs " == *" mdns "* ]]; then note "mdns (5353/udp) open"; else note "mdns (5353/udp) not open in this zone"; fi
  else
    note "firewalld active (run as root for zone details)"
  fi
else
  wfail "firewalld not active — the proxy will not manage firewall rules"
fi

# --- port conflicts ---------------------------------------------------------
# Without root, ss cannot name the processes of other users (root's snmpd,
# systemd's socket): an owner it does not name is no conflict by itself.
section "port conflicts"
if have ss; then
  port="${RAW_PORT:-9100}"
  if ss -Hltn "sport = :$port" 2>/dev/null | grep -q .; then
    owner="$(ss -Hltnp "sport = :$port" 2>/dev/null | grep -o 'users:(([^)]*' | head -1 || true)"
    listen="$(systemctl show -p Listen --value "$QLP_UNIT_SOCKET" 2>/dev/null || true)"
    if systemctl is-active --quiet "$QLP_UNIT_SOCKET" 2>/dev/null && [[ "$listen" == *":$port (Stream)"* ]]; then
      note "$port/tcp held by $QLP_UNIT_SOCKET (previous run)"
    elif [[ "$owner" == *systemd* ]]; then
      note "$port/tcp held by systemd (likely our socket from a previous run)"
    elif [[ -z "$owner" ]] && ((!IS_ROOT)); then
      wfail "$port/tcp is in use by a process only root can name (re-run with sudo)"
    else
      bfail "$port/tcp already in use: ${owner:-unknown}"
    fi
  else
    pass "$port/tcp free"
  fi
  if ss -Hlun 'sport = :161' 2>/dev/null | grep -q .; then
    owner="$(ss -Hlunp 'sport = :161' 2>/dev/null | grep -o 'users:(([^)]*' | head -1 || true)"
    if [[ "$owner" == *snmpd* ]]; then note "UDP 161 held by snmpd (30-snmp-agent will reconfigure it)"
    elif [[ -z "$owner" ]] && ((!IS_ROOT)); then
      note "UDP 161 in use by a process only root can name (likely snmpd; re-run with sudo to see)"
    elif [[ -z "$owner" ]]; then
      note "UDP 161 in use by a process ss does not name (likely snmpd; 30-snmp-agent checks it)"
    else
      bfail "UDP 161 in use by a non-snmpd process: $owner"
    fi
  else
    pass "UDP 161 free"
  fi
fi

# --- other services ---------------------------------------------------------
section "services"
for svc in snmpd.service avahi-daemon.service cockpit.socket; do
  if systemctl cat "$svc" >/dev/null 2>&1; then
    note "$svc: $(systemctl is-enabled "$svc" 2>/dev/null || echo n/a)/$(systemctl is-active "$svc" 2>/dev/null || echo inactive)"
  else
    note "$svc: not installed"
  fi
done

# --- package repositories / internet ----------------------------------------
section "packages"
if have curl && curl -fsS --max-time 8 -o /dev/null "https://mirrors.fedoraproject.org/mirrorlist?repo=fedora-44&arch=$arch"; then
  pass "Fedora mirrors reachable"
else
  wfail "Fedora mirrors not reachable — 10-deps needs the network (or a local repo)"
fi
missing=()
for c in lsusb firewall-cmd semanage lpadmin snmpd avahi-daemon python3; do have "$c" || missing+=("$c"); done
((${#missing[@]})) && note "not yet installed (10-deps installs them): ${missing[*]}"

# --- verdict ----------------------------------------------------------------
section "verdict"
if ((N_HUMAN)); then
  _qlp_emit HUMAN "$_QLP_YEL" "$N_HUMAN item(s) need a human (see ACTION lines); $N_FAIL failure(s), $N_WARN warning(s)"
  exit 3
elif ((N_FAIL)); then
  error "$N_FAIL blocking problem(s), $N_WARN warning(s)"
  exit 1
fi
ok "ready ($N_WARN warning(s))"
