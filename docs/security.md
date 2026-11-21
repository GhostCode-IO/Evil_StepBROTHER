# Security

What the print server exposes, to whom, and why. Install details are in the
[README](../README.md); the design contract is
[architecture.md](architecture.md).

## Threat model

The box is a small server, possibly running other services such as Cockpit
and CUPS, that presents itself as a network label printer. The design
protects:

- **the host**: its own SNMP data (process list, storage, interfaces) and its
  other services;
- **the SNMPv3 credentials** of the monitoring system;
- **the printer**: a bad job can waste labels or leave it in an error until it
  is power-cycled;
- **label content**, which may contain personal data.

Who can reach what:

| Party | Can | Cannot |
|---|---|---|
| Any host that reaches TCP 9100 (by default the whole firewalld zone) | send print jobs | read anything back; reach USB other than through a job |
| Hosts in `SNMP_PRINTER_ALLOWED_CIDRS` | read the printer persona (v1/v2c, cleartext); send Bonjour queries (default) | write anything over SNMP; see any host data with a community |
| The monitoring system in `SNMP_V3_ALLOWED_CIDRS` | read the host's MIBs and the persona with SNMPv3 authPriv | write anything over SNMP |
| Everyone else | send print jobs while TCP 9100 is open zone-wide (the default) | reach SNMP or Bonjour: the firewall drops UDP 161 and 5353 from them |
| Local unprivileged users | read `/run/ql810wc-proxy/state.json`; print through the local CUPS queue | talk to the printer's USB device (root or group `lp` only); register SNMP subtrees (root or group `ql810wc` only) |

Out of scope, as with a real network printer: authentication of print jobs,
encryption of the print stream or of SNMPv1/v2c, and protection against
someone with physical access to the printer or its USB cable.

## Exposed surfaces

| Surface | Served by | Scope | Authentication |
|---|---|---|---|
| TCP `RAW_PORT` (9100) | `ql810wc-proxyd` (systemd socket) | the whole zone, or only `RAW_ALLOWED_CIDRS` | none |
| UDP 161, SNMPv1/v2c | snmpd → context `printer` → `ql810wc-snmp-agent` | `SNMP_PRINTER_ALLOWED_CIDRS` + loopback (snmpd checks the source; the firewall too) | community name (cleartext), read-only |
| UDP 161, SNMPv3 | snmpd, default context (host) and `printer` | `SNMP_V3_ALLOWED_CIDRS` (firewall only) | USM authPriv |
| UDP 5353 | avahi-daemon | `MDNS_ALLOWED_CIDRS` (default: `SNMP_PRINTER_ALLOWED_CIDRS`) | none (mDNS) |
| `/var/agentx/master` | snmpd (AgentX master) | local: root and group `ql810wc` (socket 0660, directory 0750), unless another snmpd setup already sets `agentXPerms` or owns `/var/agentx`: then its group, which `ql810wc-snmp-agent` joins | file permissions |
| CUPS queue `CUPS_QUEUE` | CUPS (raw, not shared) | local users | CUPS defaults |

Nothing else is opened or changed. Cockpit (9090) and CUPS's IPP port (631)
are never touched, and neither are firewall rules the installer did not add.

### TCP 9100

Like a real printer, the port accepts jobs from anyone who can reach it, and
nobody can read anything back (`RAW_BIDIRECTIONAL="no"`). Jobs are streamed to
USB and never stored. The journal records only metadata: the client's
address, the size and duration, the colour mode, the requested media and the
result.

Residual risks within the allowed networks: wasted labels; a mono job on
DK-22251, which leaves the printer in an error until a power cycle; and,
because jobs are served one at a time, a client that occupies the printer. A
client that sends nothing is dropped after `RAW_IDLE_TIMEOUT` (default
300 s), a vanished one after about a minute of failed TCP keepalives, and a
job larger than `MAX_JOB_BYTES` (default 16 MiB) is cut off. Restrict the port with
`RAW_ALLOWED_CIDRS` if the zone is wider than your printing clients. P-touch
for Mac connects to the Bonjour host name and may use IPv6, so include the
IPv6 client ranges.

### SNMP

- **No write access at all.** No community or user is given write access, and
  the persona refuses every SET.
- **Context split.** Each pair of community and allowed source is mapped
  (`com2sec -Cn printer`) to the SNMP context `printer`. That context holds
  nothing but the persona registered by `ql810wc-snmp-agent`, and the view
  allows only its five subtrees (system, interfaces, host resources,
  Printer-MIB, Brother). The host's own MIBs live in the default context,
  which only the SNMPv3 user reaches. Checked on net-snmp 5.9.5.2 on the
  tested unit: `public` and `internal` saw no hrStorage, hrSWRun or UCD
  data, and a wrong community got no answer.
- **What the persona reveals** to the allowed networks, in cleartext, as a
  real Brother printer does: the model, live status and loaded roll, the
  printer's USB serial number (`prtGeneralSerialNumber`, `brInfoSerialNumber`),
  this box's MAC address (`ifPhysAddress.1` and the node name `BRW<mac>`),
  and `PERSONA_LOCATION` / `PERSONA_CONTACT`. To present other values, set
  `PERSONA_SERIAL`, `PERSONA_MAC` and `PERSONA_SYSNAME` in `config.env`.
- **SNMPv3.** One USM user, authPriv only (SHA-256 and AES-128 by default;
  DES is not available on Fedora). It may read everything in every context,
  including the host's process list, so treat its passphrases like a login.
  net-snmp cannot limit USM users by source, so the firewall does. The
  firewall cannot tell SNMP versions apart: UDP 161 is open to the union of
  `SNMP_PRINTER_ALLOWED_CIDRS` and `SNMP_V3_ALLOWED_CIDRS`, so the SNMPv3 user
  also works from the printer networks. The communities, in contrast, are
  accepted only from `SNMP_PRINTER_ALLOWED_CIDRS` and loopback.
- **Precedence.** net-snmp applies the first `com2sec` line that matches a
  request. The installer therefore refuses to configure snmpd while another
  snmpd configuration file maps one of the communities, defines the same
  SNMPv3 user or a view of the same name.
- **AgentX.** The subagent talks to snmpd over the unix socket
  `/var/agentx/master`, never over TCP (AgentX has no authentication). Anyone
  in group `ql810wc` can register subtrees, that is, answer SNMP on the box's
  behalf; keep that group to the service user.
- **Fallback mode.** With `SNMP_AGENT_MODE="pass_persist"` the persona serves
  only the Brother and Printer-MIB subtrees in the default context, and the
  communities' view is narrowed to exactly those two subtrees, so no host data
  is exposed in that mode either.

### Why the community `internal` is accepted

P-touch Editor for Mac (MacPrintSDK) reads printer status and the loaded roll
only over SNMPv1 with the community `internal`. The string `public` does not
occur in the SDK (static analysis of 5.3.23), and every request P-touch
made in the observed session used `internal`
([dev/notes/ptouch-mac-session.md](../dev/notes/ptouch-mac-session.md)).
Without it, Mac clients would find the printer but never see its status or
roll.

On Brother devices `internal` is the name Brother's own software uses for
read-write access (the SDK stores it as its read-write community string). On
this box it is read-only, sees only the persona and is accepted only from
`SNMP_PRINTER_ALLOWED_CIDRS`. Without Mac or iOS clients you can drop it
(which community iPrint&Label for iOS uses was not checked):
`SNMP_PRINTER_COMMUNITIES="public"`.

Community names are not secrets: they travel in cleartext and are built into
the clients. The snmpd drop-in that names them is still mode 0600, and they
are kept out of `runtime.env`.

## Scoping

- Firewall changes are additive. Each rule is applied at runtime and
  permanently, without `--reload` (which would drop other runtime-only rules),
  in the zone of the interface that holds `PROXY_IP` (else the default zone).
  Every rule is recorded in the install manifest and removed by
  `uninstall.sh`. A rule that existed before the install is never recorded,
  so it is never removed.
- TCP 9100: zone-wide by default, like a real printer, or per
  `RAW_ALLOWED_CIDRS` as source-scoped rich rules (IPv4 and IPv6).
- UDP 161: only source-scoped rich rules, never the firewalld `snmp` service
  (which also opens TCP 161). snmpd itself listens on the wildcard address,
  because SNMP broadcast discovery (Brother's Windows tools) reaches only a
  wildcard socket; `com2sec` sources restrict the answers as well.
- UDP 5353: P-touch Editor for Mac finds printers only through Bonjour, so
  Avahi must receive the queries. The rules are source-scoped
  (`MDNS_ALLOWED_CIDRS`); the zone-wide `mdns` service is never opened.
  `ENABLE_MDNS="no"` removes the Bonjour record and these rules; Mac users
  then connect by IP address.
- The Bonjour record carries the printer name, port 9100, `PERSONA_LOCATION`
  and a `UUID`. The UUID is random, generated once per install; unlike
  Brother's own UUIDs it does not contain a MAC address.

## Installing with setup.sh

The one-liner (`curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh | sudo bash`)
runs a script from GitHub as root, and that script clones this repository
over HTTPS and installs from it. You trust GitHub, its TLS certificate and
the repository's `main` branch.

- To read the script before it runs, download it and start it yourself. A
  setup.sh outside a checkout works the same way as the piped one:

  ```bash
  curl -fsSLO https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh
  less setup.sh
  sudo bash setup.sh
  ```

- Or clone the repository, read what you like, and run `sudo ./setup.sh` in
  the checkout; nothing else is downloaded from GitHub then.
- `--ref REF` installs a given branch or tag instead of `main`.
- The script body consists of functions and the last line calls them, so a
  download that breaks off is a syntax error and runs nothing. It asks no
  questions, and when its standard input is not a terminal, the commands it
  runs get no input from it.
- An existing checkout in `/usr/local/src/ql810wc-print-proxy` is only ever
  fast-forwarded from the same repository. A directory there that another
  user owns is not used, because git and the installer run as root. If the
  directory is neither empty nor a git checkout, is a checkout of another
  repository or cannot be fast-forwarded, setup.sh stops and leaves its files
  and its branch as they were.
- On a box that is installed already, setup.sh does not replace its
  settings with the template's (that would, for example, reset the client
  ranges and remove the SNMPv3 user) unless you pass
  `--new-config`: it stops with exit status 3 and names the `config.env` the
  box was installed with. Only one setup.sh, `install.sh` or `uninstall.sh`
  changes the box at a time (a lock on `/run/ql810wc-proxy.lock`).

## Local hardening

- Both daemons run as the system user `ql810wc` (no login shell, no home),
  with the supplementary group `lp` for `/dev/usb/lp*` (`root:lp 0660`).
- systemd hardening for both: `NoNewPrivileges`, no capabilities,
  `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, kernel and control
  group protection, `RestrictNamespaces`, native system calls only.
  `ql810wc-proxyd` may write only `/run/ql810wc-proxy`. `ql810wc-snmp-agent`
  has no devices (`PrivateDevices`), no IP networking (`IPAddressDeny=any`,
  unix sockets only), a system-call filter and a 128 MB memory limit.
- Python runs as `/usr/bin/python3 -I -B`: no `PYTHON*` variables, no user
  site packages, no bytecode written next to the code. The runtime needs only
  the standard library and Fedora RPMs (`python3-pyyaml`, `python3-pyusb`).
- The udev rule gives group `lp` read-write access to USB devices of vendor
  `04f9` (every Brother USB device, not only the QL), for the libusb fallback.
- SELinux stays enforcing, with no custom policy module. The daemons run in
  `unconfined_service_t`; snmpd stays in `snmpd_t`. Only the `pass_persist`
  fallback, which runs inside snmpd, needs one file-context rule
  (`snmpd_var_run_t` on `/run/ql810wc-proxy`); the installer records it and
  `uninstall.sh` removes it. The tested unit ran every step with no AVC
  denial.
- `config.env` is parsed, never sourced or evaluated. Values containing
  `$(` or backticks are refused, and values handed to the daemons also refuse
  `"`, `\`, `$` and line breaks.
- `/run/ql810wc-proxy/state.json` is world-readable on the box. It contains
  the printer's USB serial and the address of the last client that printed.
- Logs mask the printer's serial number (only its first three characters
  remain): the daemon's journal lines, the installer's log and terminal
  output, including what it relays from `lsusb`, `dmesg`, `lpstat` and
  `journalctl`, and the report and JSON summary of `selftest.sh`.
  `qlctl status --json` and `qlctl probe --json` mask it the same way;
  `--show-serial` prints it in full, so leave that option out of output you
  paste anywhere public.
- Through the local CUPS queue, CUPS keeps job files for a while after
  printing (cupsd.conf `PreserveJobFiles`). The installer does not change
  cupsd.conf. Jobs sent to TCP 9100 never touch CUPS or the disk.

## Secrets handling

The only secrets are the SNMPv3 passphrases, `SNMP_V3_AUTH_PASS` and
`SNMP_V3_PRIV_PASS`.

- **`config.env`** holds them. It is gitignored; keep it owned by root with
  mode 0600. setup.sh creates it that way, with no moment at which others
  can read it, and keeps the mode when it rewrites keys. Every install step
  warns when the file is group- or world-readable. It may live next to
  `install.sh`, in `/etc/ql810wc-proxy/config.env`, or wherever
  `QL_PROXY_CONFIG` points.
- **Generated by `setup.sh --snmpv3`.** The passphrases are random, 32
  letters and digits long, and never printed or logged; `--print-config`
  shows them masked (with the serial numbers and the community names other
  than `public` and `internal`), and later runs keep them. Read them in
  `config.env` as root (`sudo cat config.env` in the checkout) to configure
  your monitoring system.
- **Never in `runtime.env`.** The daemons' settings file
  (`/etc/ql810wc-proxy/runtime.env`, mode 0644) is generated from a fixed list
  of non-secret keys. The communities and SNMPv3 settings are not on it.
- **Never on a command line.** The installer writes the `createUser` line with
  the shell's built-in `printf` and computes its change-detection digest from
  standard input, so the passphrases never appear in `ps`. They are present
  only in the environment of the root-owned install processes, which only root
  can read.
- **Never logged.** The install log records the commands it runs and the
  SNMPv3 user name and protocols, never a passphrase.
- **The selftest** reads them from `config.env` (as root) to query snmpd as
  the SNMPv3 user. It hands them to `snmpget` in a temporary client
  configuration file (mode 0600, in a private directory it deletes when it
  finishes), never on a command line, and its report never shows them. Run
  as another user while `config.env` is root-only, it skips that check.
- **At rest.** net-snmp's persistent file `/var/lib/net-snmp/snmpd.conf` (mode
  0600) receives the `createUser` line while snmpd is stopped. snmpd creates
  the user from it at start-up and replaces the line with localized keys
  (`usmUser`) when it next saves that file. To notice changes, the
  installer keeps a salted SHA-256 digest of the SNMPv3 settings in
  `/var/lib/ql810wc-proxy/snmpv3-user` (mode 0600, in a 0750 directory), not
  the passphrases.
- **Rotate** by changing the passphrases in `config.env` as root (for
  example with `sudo vi config.env`) and running
  `sudo ./install.sh --only 30`: it stops snmpd, replaces the user and starts
  snmpd again. `SNMP_V3_ENABLE="no"` removes the user the installer created.
- Typing passphrases on an `snmpget` command line puts them in the shell
  history and the process list; see
  [troubleshooting](troubleshooting.md#snmp-checks).

## Repository hygiene

The repository is public, and git history is permanent: nothing site-specific
or secret may ever be committed.

- `.gitignore` excludes `config.env` and every other `*.env` except
  `config.sample.env`, keys and certificates, raw SNMP walks (`*.walk`,
  `*.snmpwalk`, `*.snmprec`), everything in `dev/walks/` except its README,
  and virtual environments.
- `dev/hooks/check-hygiene.sh` runs as the pre-commit hook (enable it once
  per clone with `git config core.hooksPath dev/hooks`) and in CI over every
  tracked file (`--all`). It blocks those paths, every binary file (the
  content checks read text, and a binary file can hold addresses as raw
  bytes), private-key material, private and carrier-grade-NAT IPv4
  addresses, and MAC addresses other than the documentation range. An optional local deny-list, `.git/hygiene-deny` (one
  literal per line, such as the printer's serial number or a host name), never
  leaves the clone. A line containing `hygiene-allow` is exempt from the
  content checks.
- Examples use documentation values only: `192.0.2.0/24`, `198.51.100.0/24`,
  `203.0.113.0/24`, `2001:db8::/32` and `00:00:5E:00:53:xx`. The tests use
  them too, with two exceptions. Where a range must count as a real client
  network (the preflight and the selftest do not count documentation ranges
  as client networks), they
  use `198.18.0.0/15`, the benchmarking range of RFC 2544, which is not
  routed on the internet. Where a test is about a supernet of a
  documentation range, it uses `192.0.0.0/16`.
- Raw SNMP walks of a printer go to `dev/walks/` and nowhere else. Excerpts
  quoted in `dev/notes/` are scrubbed first (`dev/snmpwalk/scrub.py`). Only
  derived results are committed, such as `agent/oid-map.yaml`.
