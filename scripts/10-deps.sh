#!/usr/bin/env bash
# scripts/10-deps.sh — install the packages the proxy needs (only the missing
# ones) and, optionally, the brother_ql_next validation tool in its own venv.
#
#   sudo scripts/10-deps.sh        (normally run by ./install.sh)
#
# Idempotent. Packages we install are recorded in the manifest (uninstall.sh
# lists them and removes them only with --remove-packages). CUPS, Avahi and
# everything already installed are left as they are.
# shellcheck source=lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

# Runtime: CUPS (raw queue), net-snmp (+ tools for selftest), Avahi (+ tools for
# selftest), nss-mdns, lsusb, Python 3 with PyYAML and pyusb (libusb fallback),
# semanage (SELinux labels).
readonly PACKAGES=(cups cups-client net-snmp net-snmp-utils avahi avahi-tools nss-mdns
                   usbutils python3 python3-pyyaml python3-pyusb policycoreutils-python-utils)
readonly MIN_FREE_MB=300
# Validation prints only (never used by the service). GPL-3.0: installed
# separately from PyPI, never vendored. The pin is the version that printed
# the black and the black/red test labels on the tested unit
# (dev/notes/usb-ground-truth.md).
readonly BROTHER_QL_DIST="brother_ql_next"
readonly BROTHER_QL_VERSION="0.12.0"
readonly PIP_TIMEOUT_S=900

check_disk_space() {
  local free_mb
  free_mb="$(df -Pm / | awk 'NR == 2 { print $4 }')"
  if [[ ! "$free_mb" =~ ^[0-9]+$ ]]; then
    warn "could not read free space on / (df said '$free_mb')"
  elif ((free_mb < MIN_FREE_MB)); then
    warn "only ${free_mb} MB free on / (recommended: ${MIN_FREE_MB} MB) — package installs may fail"
  else
    log "free space on /: ${free_mb} MB"
  fi
}

check_python() {
  require_cmd python3
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "python3 $(python3 -c 'import platform; print(platform.python_version())') is too old (3.9+ needed)"
  if python3 -I -c 'import yaml' 2>/dev/null; then ok "python3 $(python3 -c 'import platform; print(platform.python_version())') with PyYAML"
  else warn "PyYAML not importable by python3 — the daemons fall back to their JSON reader"; fi
  if ! python3 -I -c 'import usb.core' 2>/dev/null; then
    warn "pyusb not importable by python3 — only the usblp (/dev/usb/lp*) transport will work"
  fi
}

check_commands() {
  local c missing=()
  for c in lpadmin lpstat snmpd snmpget avahi-daemon avahi-browse semanage lsusb udevadm; do
    have "$c" || missing+=("$c")
  done
  if ((${#missing[@]})); then die "still missing after package install: ${missing[*]}"; fi
  ok "required commands present"
}

# Version of brother_ql_next in the venv, or nothing.
venv_brother_ql_version() {
  "$QLP_VENV_DIR/bin/python" -I -c '
import importlib.metadata as md
for name in ("brother_ql_next", "brother-ql-next"):
    try:
        print(md.version(name))
        break
    except md.PackageNotFoundError:
        pass' 2>/dev/null || true
}

install_brother_ql() {
  local have_version
  if ! qlp_is_yes "${INSTALL_BROTHER_QL:-no}"; then
    if qlp_manifest_has "$(printf 'tree\t%s' "$QLP_VENV_DIR")"; then
      # qlp_remove_tree refuses (and warns about) anything that is not our own
      # directory under $QLP_OPT_DIR; the record then stays for uninstall.sh.
      if qlp_remove_tree "$QLP_VENV_DIR"; then
        qlp_manifest_del tree "$QLP_VENV_DIR"
        log "INSTALL_BROTHER_QL=no — removed the validation venv we created earlier"
      fi
    else
      log "INSTALL_BROTHER_QL=no — skipping the optional validation tool"
    fi
    return 0
  fi
  if [[ ! -x "$QLP_VENV_DIR/bin/python" ]]; then
    qlp_ensure_dir "$QLP_OPT_DIR" 0755
    # --system-site-packages: reuse Fedora's Pillow and pyusb instead of
    # downloading and building them.
    if ! run python3 -m venv --system-site-packages "$QLP_VENV_DIR"; then
      qlp_remove_tree "$QLP_VENV_DIR" || true   # the partial venv (never recorded)
      warn "could not create $QLP_VENV_DIR (python3 venv/ensurepip unavailable?) — optional validation tool skipped"
      return 0
    fi
    qlp_manifest_add tree "$QLP_VENV_DIR"
    qlp_restorecon "$QLP_VENV_DIR"
  fi
  have_version="$(venv_brother_ql_version)"
  if [[ "$have_version" == "$BROTHER_QL_VERSION" ]]; then
    ok "$BROTHER_QL_DIST $BROTHER_QL_VERSION already installed in $QLP_VENV_DIR"
    return 0
  fi
  [[ -z "$have_version" ]] || log "replacing $BROTHER_QL_DIST $have_version with the pinned $BROTHER_QL_VERSION"
  if run timeout "$PIP_TIMEOUT_S" "$QLP_VENV_DIR/bin/python" -I -m pip install \
       --disable-pip-version-check --no-input --timeout 60 --retries 2 \
       "$BROTHER_QL_DIST==$BROTHER_QL_VERSION"; then
    qlp_restorecon "$QLP_VENV_DIR"
    ok "$BROTHER_QL_DIST $BROTHER_QL_VERSION installed in $QLP_VENV_DIR (validation prints only)"
  else
    warn "could not install $BROTHER_QL_DIST $BROTHER_QL_VERSION (no access to PyPI?) — optional; the proxy does not need it"
  fi
}

main() {
  qlp_init 10-deps
  require_root
  qlp_load_config
  qlp_require_bool INSTALL_BROTHER_QL
  require_cmd rpm dnf df
  check_disk_space
  log "checking packages (missing ones are installed from the Fedora repositories)"
  qlp_ensure_packages "${PACKAGES[@]}"
  check_commands
  check_python
  install_brother_ql
  ok "dependencies ready"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
