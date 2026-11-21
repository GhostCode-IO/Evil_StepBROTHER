#!/usr/bin/env bash
# uninstall.sh — reverse exactly what install.sh recorded, and nothing else.
#
#   sudo ./uninstall.sh                 remove our units, files, queue, firewall
#                                       rules, SELinux labels and SNMPv3 user;
#                                       restore every file we modified from its
#                                       pristine backup
#   sudo ./uninstall.sh --dry-run       print what would be done
#   sudo ./uninstall.sh --purge         also delete logs and the state/backup dir
#   sudo ./uninstall.sh --remove-packages   also dnf-remove packages WE installed
#
# --purge and --remove-packages also work after a completed uninstall, which
# archives the manifest: --remove-packages then removes the packages that the
# archived manifests (manifest.uninstalled-*) list and that are still
# installed, and --purge deletes the logs and the state directory.
#
# Cockpit (9090), CUPS itself (631), other queues and any rule or file the
# installer did not create are never touched. A service that reads something
# we removed or restored is restarted (avahi-daemon: reloaded) only if it runs.
# What cannot be reversed right now (the CUPS scheduler or firewall-offline-cmd
# unavailable, a failed userdel or systemctl disable) stays in the manifest:
# the run then ends with exit status 1, and running it again finishes the job.
# Exit codes: 0 done (or nothing to do), 1 error or partially uninstalled, 64 usage error.
# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib/common.sh"

# Which service must notice a change: "PREFIX|SERVICE". A PREFIX ending in "/"
# covers everything below it; any other PREFIX names one file.
UNINSTALL_WATCH=(
  "/etc/snmp/|snmpd"
  "/etc/sysconfig/snmpd|snmpd"
  "/etc/avahi/|avahi"
  "/etc/cups/|cups"
  "/etc/ipp-usb/|ipp-usb"
  "/etc/udev/rules.d/|udev"
  "$QLP_SYSTEMD_DIR/|systemd"
)
readonly SNMPD_UNIT="snmpd.service"
readonly AVAHI_UNIT="avahi-daemon.service"
readonly CUPS_TIMEOUT_S=30     # a wedged cupsd must not hang the uninstall
# Edits firewalld's permanent configuration while firewalld is stopped (part
# of the firewalld package). Not readonly: the tests name a missing one.
FW_OFFLINE_CMD="firewall-offline-cmd"

DRY=0 PURGE=0 RMPKGS=0
SNMPD_STOPPED=0       # this run stopped snmpd to edit its persistent file
FOREIGN_INCLUDES=()   # set by find_foreign_includes
PKGS=()
KEEP=0                # set by keep(): the record being undone could not be reversed now
LATE_DIRS=()          # our directories that still hold a socket in use: tried again after finish_services
declare -gA TOUCHED=()    # service -> 1: something it reads was removed or restored
declare -gA DISABLED=()   # unit -> 1: this run disabled it (never started again)
declare -gA KEEP_WHY=()   # why records stay in the manifest -> 1

usage() { awk 'NR > 1 { if (!/^#/ || /^# shellcheck/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; }

act() { if ((DRY)); then log "[dry-run] $*"; else run "$@"; fi; }

unit_active() { systemctl is-active --quiet "$1" 2>/dev/null; }

restart_if_running() { # unit
  if unit_active "$1"; then act systemctl try-restart "$1"; fi
}

# Note that the services reading PATH must see its change.
touch_for() { # path
  local entry prefix
  for entry in "${UNINSTALL_WATCH[@]}"; do
    prefix="${entry%|*}"
    if [[ "$prefix" == */ && "$1" == "$prefix"* ]] || [[ "$1" == "$prefix" ]]; then
      TOUCHED[${entry##*|}]=1
    fi
  done
}

# ---------------------------------------------------------------------------
# One handler per manifest record kind (see "Install manifest" in common.sh)
# ---------------------------------------------------------------------------
# The record being undone could not be reversed now, for a reason a later run
# may not have: it stays in the manifest. Deliberate keeps (a directory with
# files of others, an include block still needed, a refused tree) are no such
# case: they are reported and the record goes, or no run could ever finish.
keep() { # reason
  KEEP=1
  KEEP_WHY["$1"]=1
}

undo_unit() { # unit
  if ! systemctl cat "$1" >/dev/null 2>&1; then log "unit $1 is not installed any more"; return 0; fi
  if act systemctl disable --now "$1"; then
    DISABLED[$1]=1
  else
    warn "could not disable $1"
    keep "systemctl could not disable $1"
  fi
}

# lpstat -r exits 0 whether or not cupsd answers: qlp_cups_running reads its
# message. A CUPS whose scheduler is gone for good (the cups package removed,
# its client programs still there) took the queue with it.
undo_cupsq() { # queue
  if ! have lpstat; then log "CUPS is not installed any more: queue $1 went with it"; return 0; fi
  if ! qlp_cups_running "$CUPS_TIMEOUT_S"; then
    if ! qlp_pkg_installed cups && ! systemctl cat cups.service >/dev/null 2>&1; then
      log "CUPS is not installed any more (only its client programs are): queue $1 went with it"
      return 0
    fi
    warn "the CUPS scheduler is not running: queue $1 not removed"
    keep "the CUPS scheduler is not running"
    return 0
  fi
  if timeout "$CUPS_TIMEOUT_S" lpstat -v "$1" >/dev/null 2>&1; then
    if ! act timeout "$CUPS_TIMEOUT_S" lpadmin -x "$1"; then
      warn "could not delete CUPS queue $1"
      keep "lpadmin could not delete CUPS queue $1"
    fi
  else
    log "CUPS queue $1 is already gone"
  fi
}

undo_fw() { # fw-port|fw-rich zone port-or-rule
  local what="${1#fw-}"
  local -a query=() remove=()
  if qlp_fw_active; then
    if ((DRY)); then log "[dry-run] firewall: remove $what '$3' from zone $2"; return 0; fi
    case "$1" in
      fw-port) qlp_fw_remove_port "$2" "$3" ;;
      fw-rich) qlp_fw_remove_rich "$2" "$3" ;;
    esac
    return 0
  fi
  # firewalld is stopped: change its permanent configuration (what it loads
  # when it starts again) with its offline tool, as firewalld documents.
  if ! have "$FW_OFFLINE_CMD"; then
    warn "firewalld is not running and $FW_OFFLINE_CMD is not available: $what '$3' stays in zone $2" \
         "of its permanent configuration for now"
    keep "firewalld is not running (and $FW_OFFLINE_CMD is not available)"
    return 0
  fi
  case "$1" in
    fw-port) query=(--query-port="$3") remove=(--remove-port="$3") ;;
    fw-rich) query=(--query-rich-rule="$3") remove=(--remove-rich-rule="$3") ;;
  esac
  if ! "$FW_OFFLINE_CMD" --zone="$2" "${query[@]}" >/dev/null 2>&1; then
    log "firewall: $what '$3' is no longer in zone $2"
    return 0
  fi
  if ! act "$FW_OFFLINE_CMD" --zone="$2" "${remove[@]}" >/dev/null; then
    warn "$FW_OFFLINE_CMD could not remove $what '$3' from zone $2"
    keep "$FW_OFFLINE_CMD could not change the firewall"
  fi
}

undo_fcontext() { # type regex
  local base="${2%%"("*}"   # "/run/ql810wc-proxy(/.*)?" -> "/run/ql810wc-proxy"
  if ! qlp_selinux_on; then log "SELinux is disabled: file-context rule $2 not changed"; return 0; fi
  if ((DRY)); then log "[dry-run] semanage fcontext -d '$2'"; return 0; fi
  qlp_remove_fcontext "$1" "$2"
  if [[ "$base" == /* && -e "$base" ]]; then qlp_restorecon "$base"; fi
}

undo_seport() { # type proto port
  qlp_selinux_on || return 0
  act semanage port -d -t "$1" -p "$2" "$3" || warn "port label $3/$2 was already gone"
}

undo_semodule() { # name
  qlp_selinux_on || return 0
  if semodule -l 2>/dev/null | awk '{ print $1 }' | grep -qxF -- "$1"; then act semodule -r "$1"; fi
}

undo_user() { # name
  if ! id "$1" >/dev/null 2>&1; then return 0; fi
  if ! act userdel "$1"; then
    warn "could not delete user $1 (does a process still run as $1?)"
    keep "userdel could not delete user $1"
  fi
}

# A directory the installer gave another group and mode (/var/agentx): its
# original owner, group and mode again.
undo_attrs() { # path uid:gid mode
  if [[ ! -d "$1" || -L "$1" ]]; then log "$1 is gone: nothing to restore"; return 0; fi
  if [[ ! "$2" =~ ^[0-9]+:[0-9]+$ || ! "$3" =~ ^[0-7]{3,4}$ ]]; then
    warn "malformed attrs record for $1 ($2 $3) — left as is"
    return 0
  fi
  act chown -- "$2" "$1"
  act chmod -- "$3" "$1"
}

# snmpd rewrites its persistent file from memory when it stops, so it stays
# stopped while that file is edited; finish_services starts it again once the
# rest of its configuration is reverted too (unless this run disabled it).
stop_snmpd_for_edit() {
  if ((SNMPD_STOPPED == 0)) && unit_active "$SNMPD_UNIT"; then
    log "stopping snmpd: it rewrites $QLP_SNMP_PERSIST from memory when it stops"
    run systemctl stop "$SNMPD_UNIT"
    SNMPD_STOPPED=1
  fi
}

undo_usmuser() { # user
  local marker="$QLP_STATE_DIR/snmpv3-user"
  if ((DRY)); then
    log "[dry-run] remove SNMPv3 user '$1' from $QLP_SNMP_PERSIST (with snmpd stopped) and $marker"
    return 0
  fi
  stop_snmpd_for_edit
  if [[ -f "$QLP_SNMP_PERSIST" ]]; then qlp_usm_remove_user "$1"; fi
  rm -f -- "$marker"
  TOUCHED[snmpd]=1
}

undo_modified() { # path backup
  if [[ ! -f "$2" ]]; then warn "backup missing for $1 ($2) — left as is"; return 0; fi
  act cp -a -- "$2" "$1"
  if ((DRY == 0)); then qlp_restorecon "$1"; fi
  touch_for "$1"
}

# Set FOREIGN_INCLUDES to the files that our managed block in FILE still
# makes the service read although this installer did not create them. Only a
# block that does nothing but include directories counts (snmpd.conf:
# "includeDir /etc/snmp/snmpd.conf.d"); removing it would silently drop
# configuration that someone put into that directory later.
find_foreign_includes() { # file
  local line dir f
  local re='^[[:space:]]*[Ii][Nn][Cc][Ll][Uu][Dd][Ee][Dd][Ii][Rr][[:space:]]+(.*[^[:space:]])[[:space:]]*$'
  local -a dirs=()
  FOREIGN_INCLUDES=()
  while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]]; then continue; fi
    if [[ ! "$line" =~ $re ]]; then return 0; fi
    dirs+=("${BASH_REMATCH[1]}")
  done < <(qlp_block_body "$1")
  ((${#dirs[@]})) || return 0
  for dir in "${dirs[@]}"; do
    for f in "$dir"/*.conf; do
      [[ -e "$f" ]] || continue
      if qlp_manifest_has "$(printf 'created\t%s' "$f")"; then continue; fi
      FOREIGN_INCLUDES+=("$f")
    done
  done
}

undo_block() { # file
  local file="$1" list
  if [[ ! -f "$file" ]] || ! grep -qF "# BEGIN $QLP_TAG" "$file"; then
    log "$file holds no managed block any more"
    return 0
  fi
  find_foreign_includes "$file"
  if ((${#FOREIGN_INCLUDES[@]})); then
    list="$(printf '%s, ' "${FOREIGN_INCLUDES[@]}")"
    warn "kept the managed include block in $file: it still includes ${list%, }, which this" \
         "installer did not create. Remove the block by hand once nothing needs it."
    return 0
  fi
  if ((DRY)); then log "[dry-run] remove the managed block from $file"; else qlp_remove_block "$file"; fi
  touch_for "$file"
}

# A unit file about to be deleted must not leave its unit running or enabled
# (normally its "unit" record has already taken care of that).
stop_unit_of() { # unit file
  local unit="${1##*/}"
  if unit_active "$unit"; then act systemctl stop "$unit" || warn "could not stop $unit"; fi
  if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
    if act systemctl disable "$unit"; then DISABLED[$unit]=1; else warn "could not disable $unit"; fi
  fi
}

# Remove the Unix sockets directly in DIR that nothing listens on any more:
# snmpd leaves its AgentX master socket (/var/agentx/master) behind when it
# stops, and nobody can connect to such a socket. A socket something still
# listens on, and every other file, stays. Returns 1 if a socket in use is
# left. Without ss nothing is removed.
remove_stale_sockets() { # dir
  local f listening busy=0
  listening="$(ss -xlnH 2>/dev/null | awk '{ print $5 }')" || return 0
  for f in "$1"/* "$1"/.[!.]*; do
    [[ -S "$f" && ! -L "$f" ]] || continue
    if grep -qxF -- "$f" <<<"$listening"; then busy=1; continue; fi
    act rm -f -- "$f"
  done
  ((busy == 0))
}

# Remove a directory this installer created, once it is empty. A socket in it
# that something still listens on (snmpd still running on our configuration)
# is tried again after finish_services; anything else keeps the directory.
remove_created_dir() { # path late(0|1)
  local path="$1" busy=0
  remove_stale_sockets "$path" || busy=1
  if [[ -z "$(ls -A -- "$path" 2>/dev/null)" ]]; then
    if rmdir -- "$path" 2>/dev/null; then log "removed $path"; else warn "could not remove the empty directory $path"; fi
  elif ((busy && $2 == 0)); then
    LATE_DIRS+=("$path")
  elif ((busy)); then
    warn "kept $path: a socket in it is still in use (see: ss -xlp | grep $path)"
  else
    log "kept $path: it holds files this installer did not create"
  fi
}

undo_created() { # path
  local path="$1"
  if [[ -d "$path" && ! -L "$path" ]]; then
    if ((DRY)); then
      log "[dry-run] rmdir $path (if it is empty by then, after removing sockets nothing listens on)"
    else
      remove_created_dir "$path" 0
    fi
    return 0
  fi
  [[ -e "$path" || -L "$path" ]] || return 0
  case "$path" in
    "$QLP_SYSTEMD_DIR"/*.service|"$QLP_SYSTEMD_DIR"/*.socket) stop_unit_of "$path" ;;
  esac
  act rm -f -- "$path"
  touch_for "$path"
}

undo_tree() { # path
  local why
  if [[ ! -e "$1" && ! -L "$1" ]]; then log "$1 is already gone"; return 0; fi
  if ((DRY)); then
    why="$(qlp_tree_refusal "$1")"
    if [[ -n "$why" ]]; then warn "would refuse to remove $1 ($why)"; else log "[dry-run] rm -rf $1"; fi
    return 0
  fi
  qlp_remove_tree "$1" || true   # a refusal is a warning, the rest goes on
}

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
# Let the services that read what we removed or restored see it: only those
# that run, each once.
finish_services() {
  if [[ -n "${TOUCHED[systemd]:-}" ]]; then act systemctl daemon-reload; fi
  if [[ -n "${TOUCHED[udev]:-}" ]] && have udevadm; then act udevadm control --reload; fi
  if ((SNMPD_STOPPED)); then
    if [[ -n "${DISABLED[$SNMPD_UNIT]:-}" ]]; then
      log "snmpd stays stopped: this installer had enabled it, and it is disabled again"
    else
      run systemctl start "$SNMPD_UNIT"
    fi
    SNMPD_STOPPED=0
  elif [[ -n "${TOUCHED[snmpd]:-}" ]]; then
    restart_if_running "$SNMPD_UNIT"
  fi
  if [[ -n "${TOUCHED[avahi]:-}" ]] && unit_active "$AVAHI_UNIT"; then
    act systemctl reload "$AVAHI_UNIT" || warn "could not reload $AVAHI_UNIT (it notices the change by itself)"
  fi
  if [[ -n "${TOUCHED[cups]:-}" ]]; then restart_if_running cups.service; fi
  # A running ipp-usb read its quirks at start; it may serve the QL again.
  if [[ -n "${TOUCHED[ipp-usb]:-}" ]]; then restart_if_running ipp-usb.service; fi
}

# A run that stopped snmpd and then failed must not leave SNMP down.
restart_snmpd_on_exit() {
  if ((SNMPD_STOPPED)) && [[ -z "${DISABLED[$SNMPD_UNIT]:-}" ]]; then
    warn "starting snmpd again after an interrupted run"
    systemctl start "$SNMPD_UNIT" || error "could not start snmpd — see: journalctl -u snmpd"
  fi
}

parse_args() {
  while (($#)); do
    case "$1" in
      --dry-run) DRY=1 ;;
      --purge) PURGE=1 ;;
      --remove-packages) RMPKGS=1 ;;
      -h|--help) usage; exit 0 ;;
      *) error "unknown option: $1"; usage >&2; exit 64 ;;
    esac
    shift
  done
}

# --remove-packages and --purge after a completed uninstall, which archived
# the manifest as manifest.uninstalled-<UTC>. Each install records only the
# packages it installed itself, so the packages come from every archive; those
# still installed are removed (and their records dropped from the archives).
# --purge deletes the logs, the state directory (archives, backups and the
# saved printer identity) and the run directory.
after_uninstall() {
  local f kind name rest p tmp
  local -a archives=() pkgs=() still=()
  local -A seen=()
  archives=("$QLP_STATE_DIR"/manifest.uninstalled-*)
  if ((RMPKGS)); then
    for f in "${archives[@]}"; do
      [[ -f "$f" ]] || continue
      while IFS=$'\t' read -r kind name rest || [[ -n "$kind" ]]; do
        if [[ "$kind" == pkg && -n "$name" && -z "${seen[$name]:-}" ]]; then seen[$name]=1 pkgs+=("$name"); fi
      done <"$f"
    done
    for p in ${pkgs[@]+"${pkgs[@]}"}; do
      if qlp_pkg_installed "$p"; then still+=("$p"); fi
    done
    if ((${#still[@]})); then
      log "packages earlier installs installed and that are still installed: ${still[*]}"
      act dnf -y remove "${still[@]}"
      if ((DRY == 0 && PURGE == 0)); then
        for f in "${archives[@]}"; do
          [[ -f "$f" ]] || continue
          tmp="$(mktemp "$f.XXXXXX")"
          awk -F '\t' -v drop=" ${still[*]} " '!($1 == "pkg" && index(drop, " " $2 " "))' "$f" >"$tmp"
          chmod 0644 "$tmp"
          mv -f -- "$tmp" "$f"
        done
      fi
    elif ((${#pkgs[@]})); then
      log "the packages earlier installs installed are no longer installed: ${pkgs[*]}"
    else
      log "no earlier install recorded a package of its own"
    fi
  fi
  if ((PURGE)); then act rm -rf -- "$QLP_LOG_DIR" "$QLP_STATE_DIR" "$QLP_RUN_DIR"; fi
  ok "done: no install manifest at $QLP_MANIFEST (this box was uninstalled before, or never installed)"
}

# Replace the manifest with the records this run could not reverse, plus the
# packages still installed, in their original order (records[] and carry[]
# of main), so the next run takes up exactly those.
write_remaining_manifest() { # keep-packages(0|1)
  local i tmp
  tmp="$(mktemp "$QLP_STATE_DIR/manifest.XXXXXX")"
  for i in "${!records[@]}"; do
    case "${carry[i]:-}" in
      keep) printf '%s\n' "${records[i]}" ;;
      pkg)  if (($1)); then printf '%s\n' "${records[i]}"; fi ;;
    esac
  done >"$tmp"
  chmod 0644 "$tmp"
  mv -f -- "$tmp" "$QLP_MANIFEST"
}

main() {
  local -a records=() carry=()
  local i kind a b c kept=0 pkgs_left=1 why
  qlp_init uninstall
  parse_args "$@"
  require_root
  # One run at a time (qlp_lock). Sourced, as the tests do, it takes no lock.
  if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then qlp_lock; fi
  if [[ ! -f "$QLP_MANIFEST" ]]; then
    if ((PURGE || RMPKGS)); then
      after_uninstall
      return 0
    fi
    ok "nothing to do: no install manifest at $QLP_MANIFEST"
    return 0
  fi
  trap restart_snmpd_on_exit EXIT
  mapfile -t records <"$QLP_MANIFEST"
  # Newest change first: files go before their directories, a unit stops
  # before its file is deleted, an include target before its include block.
  for ((i = ${#records[@]} - 1; i >= 0; i--)); do
    [[ -n "${records[i]}" ]] || continue
    kind="" a="" b="" c="" KEEP=0
    IFS=$'\t' read -r kind a b c <<<"${records[i]}"
    case "$kind" in
      unit)            undo_unit "$a" ;;
      cupsq)           undo_cupsq "$a" ;;
      fw-port|fw-rich) undo_fw "$kind" "$a" "$b" ;;
      fcontext)        undo_fcontext "$a" "$b" ;;
      seport)          undo_seport "$a" "$b" "$c" ;;
      semodule)        undo_semodule "$a" ;;
      user)            undo_user "$a" ;;
      usmuser)         undo_usmuser "$a" ;;
      modified)        undo_modified "$a" "$b" ;;
      block)           undo_block "$a" ;;
      created)         undo_created "$a" ;;
      tree)            undo_tree "$a" ;;
      attrs)           undo_attrs "$a" "$b" "$c" ;;
      pkg)             PKGS+=("$a"); carry[i]=pkg ;;
      *)               warn "unknown manifest record (left alone): ${records[i]}" ;;
    esac
    if ((KEEP)); then carry[i]=keep kept=$((kept + 1)); fi
  done
  finish_services
  # snmpd has now stopped or restarted without our configuration.
  for i in "${!LATE_DIRS[@]}"; do remove_created_dir "${LATE_DIRS[i]}" 1; done

  if ((${#PKGS[@]})); then
    if ((RMPKGS)); then
      act dnf -y remove "${PKGS[@]}"
      pkgs_left=0
    else
      log "packages installed by us and left in place: ${PKGS[*]} (use --remove-packages to remove)"
    fi
  fi
  if ((DRY == 0)); then rm -f -- "$QLP_STATE_DIR/passpersist.digest" "$QLP_STATE_DIR/config-path"; fi
  if ((kept)); then
    why="$(printf '%s; ' "${!KEEP_WHY[@]}")"
    why="${why%; }"
    if ((DRY)); then
      warn "[dry-run] $kept change(s) could not be reversed now ($why): they would stay in $QLP_MANIFEST"
      return 0
    fi
    write_remaining_manifest "$pkgs_left"
    if ((PURGE)); then
      act rm -rf -- "$QLP_LOG_DIR" "$QLP_RUN_DIR"
      warn "--purge: kept $QLP_STATE_DIR, whose manifest still lists what is left to undo"
    fi
    error "partially uninstalled: $kept change(s) could not be reversed now ($why). They stay in" \
          "$QLP_MANIFEST: re-run sudo ./uninstall.sh once that is fixed (firewalld or CUPS running again)."
    exit 1
  fi
  if ((PURGE)); then
    act rm -rf -- "$QLP_LOG_DIR" "$QLP_STATE_DIR" "$QLP_RUN_DIR"
  elif ((DRY == 0)); then
    mv -f -- "$QLP_MANIFEST" "$QLP_MANIFEST.uninstalled-$(date -u +%Y%m%dT%H%M%SZ)"
    log "backups kept in $QLP_BACKUP_DIR (remove with --purge)"
  fi
  ok "uninstall complete"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
