# Troubleshooting

Run the commands on the print server unless a section says otherwise, and
run `./selftest.sh`, `./setup.sh`, `./install.sh` and `./uninstall.sh` from
the checkout you installed from (after the one-liner:
`cd /usr/local/src/ql810wc-print-proxy`). The one-liner's checkout and every
`config.env` that setup.sh writes belong to root: edit `config.env` as root
(for example `sudo vi config.env`), and write output files outside that
checkout (for example `sudo ./selftest.sh --json > ~/selftest.json`).
`<box-ip>` is the server's IPv4 address, `<zone>` its firewalld zone. The
examples call the operator CLI `qlctl`; define it first:

```bash
alias qlctl='python3 -I -B /usr/local/libexec/ql810wc-proxy/qlctl'
```

## Start here

```bash
sudo ./selftest.sh          # from the checkout: every check below, then a verdict
systemctl status ql810wc-proxyd.socket ql810wc-proxyd ql810wc-snmp-agent snmpd avahi-daemon
qlctl status
journalctl -u ql810wc-proxyd -n 30 --no-pager
```

- `sudo ./selftest.sh` checks the data plane, the SNMP persona, Bonjour, the
  firewall and SELinux, and ends with `TIER REACHED: …`. Each `FAIL` line
  says what to do; see [Reading the selftest report](#reading-the-selftest-report).
- `systemctl status ql810wc-proxyd` carries a one-line status from the daemon,
  for example `ready: 62mm continuous black/red (DK-22251)`, `printing job
  from …`, `waiting for the printer: …` or `printer in Editor Lite mode: …`.
- `qlctl status` shows what the daemon last saw: printer state (`ready`,
  `absent`, `mass-storage` or `error`) with the reason, the last status frame
  and its age, the loaded roll, error bits and the last job's result. It
  exits with 0 when the printer is ready without errors, 3 when it is not, and
  1 when there is no state file (`ql810wc-proxyd` is not running).
- Every print leaves one journal line:
  `job from <client>: <n> bytes in <t> s, two-color|black only, requested …,
  result ok|error|torn|refused (…)`.

| Symptom | Section |
|---|---|
| `setup.sh` stops with exit status 3, 1 or 64, or warns about the host name | [setup.sh problems](#setupsh-problems) |
| `selftest.sh` reports `FAIL` lines, or a lower tier than expected | [Reading the selftest report](#reading-the-selftest-report) |
| `install.sh` ends with "installed, but selftest reported problems" | [Reading the selftest report](#reading-the-selftest-report) |
| `Printer mass-storage`, a USB drive appears, green LED lit | [Editor Lite](#editor-lite-is-on) |
| `Printer error`, "device busy" or "another program has claimed the printer" | [ipp-usb](#ipp-usb-holds-the-printer) |
| "the printer disappeared" although nobody unplugged it | [CUPS's usb backend](#cups-usb-backend-detaches-the-printer) |
| P-touch says the printer is switched off | [Switched off](#p-touch-says-the-printer-is-switched-off) |
| The printer shows an error after a job | [Printer errors](#the-printer-shows-an-error-or-nothing-prints) |
| `ql810wc-proxyd` uses a whole CPU core | [High CPU](#high-cpu-from-ql810wc-proxyd) |
| P-touch finds the printer but shows no roll | [SNMP checks](#snmp-checks) |
| P-touch for Mac or iPrint&Label for iOS does not list the printer | [Bonjour checks](#bonjour-checks), [Firewall checks](#firewall-checks) |
| iPrint&Label (iOS) shows a 1.4" label instead of the loaded roll | Use the app's **Check Media** ([client setup](client-setup.md#ios-brother-iprintlabel)) |
| A job from PowerPoint or another Mac application prints nothing (macOS may show it as completed), or stops the printer | [Printer errors](#the-printer-shows-an-error-or-nothing-prints): `qlctl status` shows `refused`; set the driver's **Color** to **2-Color(Black/Red)** for DK-22251 |

## setup.sh problems

setup.sh writes `config.env` and then runs `install.sh`, so most problems
are the installer's, and the other sections apply. Its own cases:

| Case | What to do |
|---|---|
| Exit status 3 | A person must act first. The `HUMAN` lines above say what, for example connect the printer, turn Editor Lite off, start CUPS or choose which settings to keep, and setup.sh then names the command to run again. Do what they say, then run that command. |
| "this box is already installed with …" (exit status 3) | setup.sh (or `install.sh`) found no `config.env`, but the box was installed with one: another checkout's, for example after the one-liner on a box installed from a clone of your own, or one that has been deleted since. No `config.env` was written. To keep those settings, copy that `config.env` into this checkout as root with mode 0600 (while the file exists, the message shows the `sudo install -m 600 -o root -g root …` command), or run the scripts in the checkout you installed from. To start from new settings instead (the detected network, the host name, SNMPv3 off), run the `--new-config` command the message shows. |
| Exit status 1 | An error; the message names it. The install logs are in `/var/log/ql810wc-proxy/` ([Logs](#logs)). When the steps succeeded but the selftest found T0 not reached, see [Reading the selftest report](#reading-the-selftest-report). |
| Exit status 64 | Bad usage, for example an unknown option or a `--clients` value that is not a network. `./setup.sh --help` lists the options. |
| No IPv4 default route or address | setup.sh cannot work out the client network by itself. Pass your clients' network in CIDR form, for example `--clients 192.0.2.0/24` (a documentation-only example: use your own network). |
| A warning about the host name `localhost`, `localhost.localdomain` or `fedora` | The printer would appear as `Brother QL-810W [fedora]`. Set a name with `sudo hostnamectl set-hostname NAME` and run setup.sh again, or pass `--name NAME`. |
| Not run as root | Pipe the one-liner into `sudo bash`, or run `sudo ./setup.sh`. |
| "another setup.sh, install.sh or uninstall.sh is changing this box right now" | The run stopped with exit status 1 before it changed anything: only one of them changes the box at a time, and each holds a lock on `/run/ql810wc-proxy.lock` while it runs. Let the other run finish, then run yours again. The lock ends with the run that holds it. |
| "flock is missing (package util-linux-core)" | Every Fedora Server has `flock`; a minimal system may not. Install it with `sudo dnf install util-linux-core`, then run the command again. |
| "this host is also on … which the client network leaves out" (a warning) | setup.sh detected the client network from the default-route interface, and the box has another Ethernet or Wi-Fi network. Computers and phones on that network do not find the printer. To admit them too, run setup.sh again with the `--clients` value the warning shows. |
| "the printer identity cannot be read: … PRINTER_UUID=… is not a UUID" | Step 20, 30 or 40 found an `/etc/ql810wc-proxy/identity.env` (or its copy `/var/lib/ql810wc-proxy/identity.env`) that the daemons cannot use, for example after a hand edit, and stopped there. Correct the UUID in the file the message names (8-4-4-4-12 hex digits), or delete that file: the installer then uses the other copy, or generates a new identity, which P-touch sees as a new printer. Then run the command again. |
| Not Fedora | setup.sh runs only on Fedora. A Fedora other than 44 gets a warning, and the installer's preflight decides; it warns as well and goes on. Only Fedora 44 is tested. |
| `/usr/local/src/ql810wc-print-proxy` is not owned by root, is neither empty nor a git checkout, is a checkout of another repository, or cannot be fast-forwarded | The bootstrap left the files and the branch in that directory as they were. Look at it (`sudo git -C /usr/local/src/ql810wc-print-proxy status`, `sudo git -C /usr/local/src/ql810wc-print-proxy log`). Move it away and run the one-liner again, or clone somewhere else with `--dir DIR`. To keep the settings of a moved checkout, run the one-liner with `--config-only` (on a box that is installed already, it stops with exit status 3 once it has cloned), copy the old `config.env` into the new checkout with `sudo install -m 600 -o root -g root <moved checkout>/config.env /usr/local/src/ql810wc-print-proxy/config.env`, then run `sudo ./setup.sh` in the new checkout. |

To see the `config.env` setup.sh would write, or the existing one, without
changing anything: `sudo ./setup.sh --print-config`. It masks the SNMPv3
passphrases, the serial numbers and community names other than `public`
and `internal`, so its output can go into a bug report.

## Reading the selftest report

`selftest.sh` prints one line per check, grouped by tier, then the verdict.
Its options and tiers are described in the
[README](../README.md#selftest).

- `PASS` and `INFO` need nothing. `SKIP` names what was not checked and why,
  for example when not running as root. `WARN` is a risk worth fixing that
  does not lower the tier. `FAIL` lowers the tier and names the fix.
- The verdict is the highest tier that passed together with every tier below
  it. `T1 two-colour      not verified: no black/red roll loaded` is expected
  while another roll is loaded: the verdict then stays at T0, and a `NOTE`
  line names any higher tiers that passed.
- Exit status 1, and `install.sh`'s "installed, but selftest reported
  problems", mean that T0 is not reached: printing does not work right now,
  or something can break it, such as a missing firewall rule or USB
  exclusion. Fix the `FAIL` lines of the T0 section first; failures in T2 and
  T3 often follow from them.
- `sudo ./selftest.sh --quiet` shows only the problems and the verdict.
  `sudo ./selftest.sh --json > ~/selftest.json` writes a document to attach
  to a bug report on the project's
  [issue tracker](https://github.com/GhostCode-IO/Evil_StepBROTHER/issues);
  the printer's serial number is masked in it.

| Report line | What to do |
|---|---|
| `FAIL  no printer on USB` | Connect the printer and switch it on; it switches itself off after about an hour without `ql810wc-proxyd`'s status polls ([Switched off](#p-touch-says-the-printer-is-switched-off)). |
| `FAIL  Editor Lite is ON` | [Editor Lite](#editor-lite-is-on) |
| `FAIL  ql810wc-proxyd cannot use the printer` | [ipp-usb](#ipp-usb-holds-the-printer), [CUPS's usb backend](#cups-usb-backend-detaches-the-printer). When it says the printer does not answer status requests (for example after a mono job on DK-22251), switch the printer off and on ([Printer errors](#the-printer-shows-an-error-or-nothing-prints)). |
| `FAIL  the printer reports: …`, `FAIL  no roll loaded` | Close the cover, load a roll or clear the jam, as the line says ([Printer errors](#the-printer-shows-an-error-or-nothing-prints)). |
| `FAIL  missing /usr/share/cups/usb/ql810wc-proxy.usb-quirks` | Attach the printer in printer mode, then run `sudo ./install.sh --only 20` ([CUPS's usb backend](#cups-usb-backend-detaches-the-printer)). |
| `FAIL  ql810wc-proxyd used …% CPU … while idle` | [High CPU](#high-cpu-from-ql810wc-proxyd) |
| `FAIL  nothing listens on TCP …` | `journalctl -u ql810wc-proxyd -n 50`; `sudo ./install.sh --only 20` starts stopped units again. |
| `FAIL  state.json was last updated …` | `ql810wc-proxyd` hangs: `journalctl -u ql810wc-proxyd -n 50`, then `sudo systemctl restart ql810wc-proxyd`. |
| `FAIL  SNMPv1 community … no 1284 device ID` | "If the persona does not answer" under [SNMP checks](#snmp-checks). |
| `FAIL  community … can read this host's own data` | Another snmpd configuration gives the community more than the persona ([SNMP checks](#snmp-checks)). |
| `FAIL  UDP 161 is bound to specific addresses only` | [SNMP checks](#snmp-checks) |
| `FAIL  no network client can reach …` | The allowed client ranges still hold only the template's documentation range, or nothing: run `sudo ./setup.sh --clients "CIDR [CIDR...]"` with your clients' subnets, or set `SNMP_PRINTER_ALLOWED_CIDRS` (or `MDNS_ALLOWED_CIDRS`) in `config.env` as root and run `sudo ./install.sh`. |
| `FAIL  the Bonjour record does not resolve`, `WARN  Avahi publishes the printer as …`, `WARN  no range in … is a subnet of this host` | [Bonjour checks](#bonjour-checks) |
| `FAIL  firewall: … is missing in zone …` | Re-run `sudo ./install.sh` ([Firewall checks](#firewall-checks)). |
| `FAIL  the persona reports the printer as switched off` | Fix the T0 lines first ([Switched off](#p-touch-says-the-printer-is-switched-off)). |
| `WARN  last job: …` | The last print job did not print as it should; the line says why. `refused`: the box kept the job from the printer because the loaded roll cannot print its colour mode ([Printer errors](#the-printer-shows-an-error-or-nothing-prints)). |
| `WARN  RAW_COLOR_CHECK is off: …` | The colour check is switched off in `config.env`, so a black-only job on DK-22251 reaches the printer, which then needs a power cycle. Unless you need it off, set `RAW_COLOR_CHECK="yes"` (the default) as root and run `sudo ./install.sh`. When the printer does not report a black/red roll (another roll or no roll is loaded, or there is no status from the printer), the line is `INFO`. |
| `WARN  … cannot reload: its unit file is outdated`, `WARN  … rejected its last reload` | Re-run `sudo ./install.sh`; for a rejected reload, `journalctl -u ql810wc-snmp-agent` names the file and the problem ([Logs](#logs)). |
| `DEGRADED: T0-T2 with manual media` | The printer's status block does not fit the shipped OID map: select the roll by hand in P-touch, and attach the `--json` output to a bug report on the [issue tracker](https://github.com/GhostCode-IO/Evil_StepBROTHER/issues). |
| `NOTE: identity strings in oid-map.yaml …` | Nothing: this is information. The identity strings are placeholders until someone checks them on a real QL-810Wc ([dev/snmpwalk/](../dev/snmpwalk/README.md)); P-touch Editor for Mac and iPrint&Label for iOS work with them. |
| `WARN  … SELinux denial(s) since boot` | [SELinux](#selinux) |

## Editor Lite is on

With Editor Lite on, the QL-810Wc presents itself as a 2 MB USB drive
(`04f9:209f`, mass storage) and has no printer interface. In printer mode it
is `04f9:209c`. New units may ship with Editor Lite on.

```bash
lsusb -d 04f9:         # 04f9:209c = printer mode, 04f9:209f = Editor Lite
```

Fix: press and hold the **Editor Lite** button until its green LED goes out.
`ql810wc-proxyd` rescans every 2 seconds and opens the printer by itself; no
restart is needed. The printer's USB serial differs between the two modes:
if you pinned `USB_SERIAL`, use the printer-mode value.

## ipp-usb holds the printer

The QL-810Wc's IPP-over-USB interface is an alternate setting of its raw
printer interface. When ipp-usb selects it, the kernel's `usblp` driver is
unbound and raw Brother raster cannot reach the printer. Signs:
`ls /dev/usb/lp*` lists nothing while `lsusb` shows `04f9:209c`, `lsusb -t`
shows the printer interface with `Driver=usbfs` instead of `Driver=usblp`,
and `qlctl status` reports `error` ("device busy" or "another program has
claimed the printer").

```bash
cat /etc/ipp-usb/quirks/ql810wc-proxy.conf     # expect "[Brother QL-810W]" and "blacklist = true"
systemctl status ipp-usb
journalctl -u ipp-usb -b --no-pager | tail -n 30
```

- The quirk is installed while `IPP_USB_EXCLUDE="yes"` (the default). Its
  section name is the printer's USB manufacturer and product string,
  detected at install time.
- ipp-usb reads its quirks only at start-up. `sudo ./install.sh --only 20`
  stops ipp-usb when it holds the printer, starts it again if other
  IPP-over-USB devices are attached, and rebinds `usblp` to the printer.
  Unplugging and replugging the printer's USB cable also works once the quirk
  is in place.
- With the exclusion in place, a driverless CUPS queue that reached the QL
  through ipp-usb (an `ipp://localhost:60000/…` URI) no longer works. Step 20
  names such queues in its log and leaves them. Delete one you do not need
  with `sudo lpadmin -x <queue>`.

## CUPS usb backend detaches the printer

CUPS's usb backend detaches `usblp` from every USB printer whenever it lists
devices: `lpinfo -v`, CUPS's "Add Printer" page, and the rescans that a
`usb://` queue for an absent printer runs every 5 seconds. That can break a
job in progress. The installer prevents this with a blacklist quirk:

```bash
cat /usr/share/cups/usb/ql810wc-proxy.usb-quirks   # expect: 0x04f9 0x209c blacklist
```

- The file is written only when the printer's product ID is known, that is,
  when the printer was attached in printer mode while step 20 ran. Otherwise
  connect it and run `sudo ./install.sh --only 20`.
- With the quirk, `lpinfo -v` no longer lists a `usb://Brother/…` URI for the
  QL, and a `usb://` queue for the QL cannot print. That is intended: CUPS
  reaches the printer through the proxy (`CUPS_QUEUE`, `socket://127.0.0.1:9100`).
- The journal shows the symptom as `waiting for the printer: the printer
  disappeared (…)` or `… the printer was disconnected` right after someone
  listed devices.

## P-touch says the printer is switched off

P-touch for Mac shows its power-off message when the box serves the "turned
off" status block: status type `02` (error occurred), error 1 bit `0x20`
(printer turned off), no media. A real QL that is switched off does not
answer at all, and P-touch would retry for 60 seconds; the block makes it
report the problem at once. The persona serves that block whenever it cannot
vouch for the printer:

- the printer is unplugged, switched off or in Editor Lite mode, or
  `ql810wc-proxyd` cannot use it (USB error);
- the printer is attached but its first status has not been read yet;
- no status has been read from the printer for more than
  `STATUS_MAX_AGE_SECONDS` while no job runs (60 seconds, or twice
  `STATUS_POLL_SECONDS` if that is more);
- `ql810wc-proxyd` is stopped, or has not rewritten its state file for more
  than three times `STATUS_POLL_SECONDS`;
- a job has been in flight for more than 60 seconds since its last data
  without a word from the printer: the printer takes no more data, or the
  client went silent mid-job. `ql810wc-proxyd` ends such a job by itself
  (see the job lines below).

`qlctl status` and `systemctl status ql810wc-proxyd` give the reason. To see
what P-touch sees:

```bash
snmpget -v1 -c internal -On 127.0.0.1 1.3.6.1.4.1.2435.3.3.9.1.6.1.0
# turned-off block: byte 8 (the 9th) is 20, byte 18 (the 19th) is 02, media bytes 0
# live, DK-22251, idle: 80 20 42 34 39 30 04 00 00 00 3E 0A 00 00 23 00 … 81 …
```

**Auto power-off.** The tested unit switched itself off (a USB disconnect)
60 minutes after the last status request. `ql810wc-proxyd` polls an idle
printer every `STATUS_POLL_SECONDS` (3 s), which keeps it on: the tested
unit stayed on for almost four hours without a job. While
`ql810wc-proxyd` is stopped nothing polls, and after an hour the printer
switches off. If yours switches off, press its
power button: `ql810wc-proxyd` finds it again within a few seconds. The
timeout itself is a printer setting, changed with Brother's own tools; this
project never changes printer settings.

## The printer shows an error, or nothing prints

- **`refused a print job from …: a black-only job, but the loaded roll, …
  (DK-22251), takes two-color jobs only`**, and a job line with
  `result refused`: the job asked for black-only printing while DK-22251 was
  loaded, which the printer rejects (see the next point). None of its print
  data reached the printer, and the client's connection was reset; the
  application may still show the print as completed (macOS does when the
  reset arrives after its last write). Print it
  again in 2-colour mode (see [client setup](client-setup.md#media-rules)):
  with Brother's driver for macOS, set **Color** to **2-Color(Black/Red)**,
  as its default is **Monochrome**. `… a two-color job, but the loaded roll,
  …, cannot print red` is the reverse: print black only, or load DK-22251.
  `RAW_COLOR_CHECK="no"` turns this check off.
- **A mono job on DK-22251 that reached the printer** (with
  `RAW_COLOR_CHECK="no"`, or when the box could not tell the job's colour
  mode or the loaded roll)
  puts the printer into an error that only a power cycle clears. The job
  line then says `black only` with a 62 mm roll loaded, or no colour at all.
  On the tested unit it ended with
  `result error (the printer did not answer status requests after the job)`,
  and `printer unusable: the printer does not answer status requests (3 in a row); reopening it`
  followed. Switch the printer off and on, and print in 2-colour mode.
  `qlctl test-print --mono` refuses this combination for that reason.
- **`connection from …: 402 bytes, no print job (…)`** from an iPhone or
  iPad: iPrint&Label's short check before it prints. Nothing is wrong.
- **`result torn`**: the connection ended abnormally before the end of the
  job: reset by the client, no data for `RAW_IDLE_TIMEOUT` (default 300 s),
  or a job larger than `MAX_JOB_BYTES` (default 16 MiB). When the detail
  ends with `printer resynchronised`, the daemon resynchronised the printer
  (400 NUL bytes, then `ESC @`). When it ends with `printer
  resynchronisation pending`, the resync is not complete yet: while the
  printer shows an error, no roll, printing or busy, only the 400 NUL bytes
  and `ESC i S` go out, and the `ESC @` follows once two status polls in a
  row find it idle; if it did not answer, later status requests (or its
  reopen) repeat the resync. Either way, a partial label may have printed.
  When the detail ends with `none of its print data was sent to the
  printer`, the job was still held back for the colour check: nothing
  printed, and the printer needed no resync.
- **`closed the connection in the middle of a job (no 0x1A)`**, then a job
  line with `result error (the client closed the connection before the end
  of the job)`: the client closed its side before the `0x1A` that ends the
  job, and the printer was resynchronised (when the line ends with `printer
  resynchronisation pending`, that is still to come, as above). The same job
  line ending `none of its print data was sent to the printer`, with no line
  before it, is a job still held back for the colour check: nothing reached
  the printer.
- **`turned away a print job from … (printer not available: …)`**: the
  printer was absent, in Editor Lite mode or unusable when the client
  connected. The client got an immediate connection reset, and the job is
  recorded as `error`.
- **`printer unusable: the printer accepted no data for N s after the client
  finished; reopening it`**: the client half-closed, but the printer did not
  take the last of the job within `RAW_IDLE_TIMEOUT`. **`printer unusable: …
  the printer did not accept the end of the job …; reopening it`**: the same
  after the job's end, while waiting for the printer to finish: it had not
  taken the end of the job within 60 seconds, or within 5 seconds of
  reporting an error (named first). In both cases the job ends as `error`,
  the client gets a connection reset, and `ql810wc-proxyd` reopens the
  printer, which drops the stuck USB transfer and resynchronises it. Check
  the printer (cover, roll, jam) and print again.
- **`connection from X ended after its job: …`**: the job itself was
  recorded; what the client sent after it (P-touch's trailer) could not be
  delivered, for example because the printer stopped taking data.
- **`stream not fully parsed (…)`** in a job line is informational: the
  observer did not recognise a command. The job was still forwarded
  unchanged, unless the line says `result refused`: the job had shown its
  colour mode before that command, and none of its print data reached the
  printer.
- **Cover open** shows as `cover open` in `qlctl status` (the printer reports
  status type `02` with error 2 bit `0x10`). Third-party reports say a
  QL-810W holds a job during such a fault and prints it afterwards; not
  checked on the tested unit.
- **`result ok` but no label** on a new unit: a third-party report for the
  QL-810W says raster jobs need the printer's command mode set to raster
  (some units ship in P-touch Template mode). Brother's printer setting tools
  change it.

## High CPU from ql810wc-proxyd

The QL answers every USB bulk-IN request with a zero-length packet, so the
kernel reports `/dev/usb/lpN` as readable at all times and each read returns
nothing. The first build of `ql810wc-proxyd` deployed on the tested
Raspberry Pi 4 waited on that node with `select` and spun one CPU core (about
one CPU-second per second). It now never selects on the usblp node and reads
it on a timer: every 0.25 s while idle, every 0.05 s during a job.

```bash
top -b -n 1 -p "$(systemctl show -p MainPID --value ql810wc-proxyd)" | tail -n 2
grep -c 'zero-length packet' /usr/local/libexec/ql810wc-proxy/ql810wc/proxyd.py   # 1 or more = fixed code deployed
```

An idle daemon should use close to no CPU: `sudo ./selftest.sh` samples it
for 5 seconds and fails above 20 % while the printer is idle. If it uses a
whole core, upgrade as [Upgrade](../README.md#upgrade) describes: run the
one-liner again if you installed with it, otherwise update the checkout you
installed from and run `sudo ./setup.sh` there; both end with the selftest. The selftest also warns when the installed code differs from the
checkout; `cmp agent/ql810wc/proxyd.py
/usr/local/libexec/ql810wc-proxy/ql810wc/proxyd.py` compares one file by
hand.

## SNMP checks

The box always answers its own loopback address, so these work on the server
before any client is configured. From a client inside
`SNMP_PRINTER_ALLOWED_CIDRS`, replace `127.0.0.1` with `<box-ip>`.

```bash
# What P-touch for Mac reads (SNMPv1, community "internal"): the 32-byte status block
snmpget -v1 -c internal -On 127.0.0.1 1.3.6.1.4.1.2435.3.3.9.1.6.1.0

# "Connect to printer with an IP address": the IEEE-1284 device ID (must contain MDL:QL-810W;)
snmpget -v1 -c internal -On 127.0.0.1 1.3.6.1.4.1.2435.2.3.9.1.1.7.0

# Windows-style reads (community "public"): model, printer status (3 idle, 4 printing), node name
snmpget -v2c -c public -On 127.0.0.1 1.3.6.1.2.1.25.3.2.1.3.1 1.3.6.1.2.1.25.3.5.1.1.1 1.3.6.1.2.1.1.5.0

# The whole printer context: the persona's few dozen OIDs and nothing else
snmpwalk -v2c -c public -On 127.0.0.1 .1

# Isolation: no host data for the printer communities (expect no rows)
snmpwalk -v2c -c public -On 127.0.0.1 1.3.6.1.2.1.25.2    # hrStorage
snmpwalk -v2c -c public -On 127.0.0.1 1.3.6.1.2.1.25.4    # hrSWRun

# A community that is not configured gets no answer at all ("Timeout: No Response")
snmpget -v2c -c not-a-community -t 1 -r 0 127.0.0.1 1.3.6.1.2.1.1.5.0
```

SNMPv3 (when `SNMP_V3_ENABLE` is on; values from `config.env`). The default
context is the host itself; `-n printer` selects the persona:

```bash
snmpget -v3 -l authPriv -u nms -a SHA-256 -A '<auth passphrase>' -x AES -X '<priv passphrase>' \
  127.0.0.1 1.3.6.1.2.1.1.5.0              # this host's own name
snmpget -v3 -l authPriv -u nms -a SHA-256 -A '<auth passphrase>' -x AES -X '<priv passphrase>' \
  -n printer 127.0.0.1 1.3.6.1.2.1.1.5.0   # the persona's node name (BRW…)
```

Passphrases typed like this end up in the shell history and are visible in
the process list while the command runs. For repeated use, put them in a
`~/.snmp/snmp.conf` readable only by you (see `snmp.conf(5)`).

If the persona does not answer:

```bash
systemctl status ql810wc-snmp-agent      # status line: "live: <roll>, idle", or why not
journalctl -u ql810wc-snmp-agent -b --no-pager | tail -n 30
sudo ls -l /var/agentx/master            # socket: group ql810wc, mode srw-rw----
sudo ss -lunp 'sport = :161'             # snmpd is the only listener
python3 -I -B /usr/local/libexec/ql810wc-proxy/brother_snmp_agent.py --check   # settings and OID map
python3 -I -B /usr/local/libexec/ql810wc-proxy/brother_snmp_agent.py --dump    # every OID it would serve now
```

- Until the agent has registered with snmpd, the persona cannot answer.
  After an snmpd (re)start, snmpd drops requests for the printer context, so
  clients see timeouts, and P-touch retries for up to 60 seconds. While
  snmpd runs and the agent restarts (about a second) or crashed, snmpd
  answers SNMPv1 requests with noSuchName instead, which fails a P-touch
  print at once. That is why the installer reloads the agent for new
  settings (`systemctl reload ql810wc-snmp-agent`) and restarts it only for
  new code.
- snmpd must listen on the UDP 161 wildcard (`0.0.0.0:161`, plus `[::]:161`
  when `SNMP_PRINTER_ALLOWED_CIDRS` holds an IPv6 range, or
  `SNMP_V3_ALLOWED_CIDRS` does while SNMPv3 is on): SNMP broadcast discovery
  reaches nothing else. Step 30 checks this once snmpd runs: it fails without
  the IPv4 wildcard and warns without the IPv6 one, and `selftest.sh` checks
  the same. If your own snmpd configuration
  sets listening addresses (an `agentaddress` line, or addresses in `OPTIONS`
  of `/etc/sysconfig/snmpd`), the drop-in adds none, because a second bind of
  UDP 161 makes snmpd exit. Add `udp:161` (and `udp6:161`) to your setting
  instead, then re-run `sudo ./install.sh --only 30`.
- The installer refuses to configure snmpd while another snmpd configuration
  file maps one of the communities (net-snmp applies the first matching
  mapping, so requests would never reach the persona). Its message names the
  file and line.
- After editing SNMP settings, re-run `sudo ./install.sh --only 30`. When
  snmpd's configuration changed it restarts snmpd, and never reloads it:
  reloading net-snmp 5.9.5.2 mishandles `pass_persist`. `ql810wc-snmp-agent`
  is reloaded when only its settings changed, and restarted only for a new
  unit file.

## Bonjour checks

From a Mac (Terminal; stop each command with Ctrl-C):

```bash
dns-sd -B _pdl-datastream._tcp local.                               # lists "Brother QL-810W [<host>]"
dns-sd -L "Brother QL-810W [<host>]" _pdl-datastream._tcp local.    # port 9100, TXT with usb_MDL=QL-810W and UUID=…
```

On the server or another Linux host:

```bash
avahi-browse --resolve --terminate _pdl-datastream._tcp
cat /etc/avahi/services/ql810wc-proxy.service
journalctl -u avahi-daemon -b --no-pager | tail -n 20
```

- **A name with " #2"** means another device on the network already uses
  `Brother QL-810W [<host>]`, for example a real QL-810W on Wi-Fi. Avahi then
  renames ours, and P-touch's saved printer no longer matches. Give the box a
  unique name with `sudo ./setup.sh --name NAME`, or set
  `PRINTER_NAME_SUFFIX` in `config.env` as root and run
  `sudo ./install.sh --only 40`. Step 40 warns about such a rename.
- After changing `PRINTER_NAME_SUFFIX`, select the printer in P-touch again.
- The Mac must be allowed to send queries to UDP 5353 (see
  [Firewall checks](#firewall-checks)), and on macOS 15 and later P-touch
  Editor needs Local Network access. On iOS, iPrint&Label needs Local
  Network access too (Settings → Privacy & Security → Local Network).
- `ENABLE_MDNS="no"` removes the record and its firewall rules. Mac users then
  use "Connect to printer with an IP address".

## Firewall checks

The installer adds rules only to the zone of the interface that holds
`PROXY_IP` (else the default zone), applies each rule at runtime and
permanently, and never uses `--reload`.

```bash
sudo firewall-cmd --get-active-zones
sudo firewall-cmd --zone=<zone> --list-ports                 # 9100/tcp while RAW_ALLOWED_CIDRS is empty
sudo firewall-cmd --zone=<zone> --list-rich-rules            # udp 161, udp 5353 (and tcp 9100 when scoped)
sudo firewall-cmd --permanent --zone=<zone> --list-rich-rules   # the same set survives a reboot
sudo grep '^fw-' /var/lib/ql810wc-proxy/manifest             # the rules this installer added (and will remove)
sudo ss -ltnp 'sport = :9100'                                # held by systemd and the daemon (python3)
```

- A client outside `SNMP_PRINTER_ALLOWED_CIDRS` (or `MDNS_ALLOWED_CIDRS`) gets
  no SNMP (or Bonjour) answers: nothing is listed, or no roll is shown. Add its
  network with `sudo ./setup.sh --clients "CIDR [CIDR...]"` (list every client
  network), or edit `config.env` as root (`sudo vi config.env`) and re-run
  `sudo ./install.sh --only 30` and `--only 40`.
- When 9100 is scoped with `RAW_ALLOWED_CIDRS`, list IPv6 client ranges as
  well: P-touch for Mac connects to the Bonjour host name and may use IPv6.
- If 9100 was already open zone-wide before the install, that rule is not
  ours and `RAW_ALLOWED_CIDRS` has no effect until you remove it. Step 20
  warns about it.

## Logs

| Command | Shows |
|---|---|
| `journalctl -u ql810wc-proxyd -b` | jobs (one line each), printer found or lost, Editor Lite, USB errors, refused and turned-away jobs |
| `journalctl -u ql810wc-snmp-agent -b` | registration with snmpd, the persona's state, OID map problems |
| `journalctl -u snmpd -b` | snmpd's own warnings; the installer also repeats any line about its drop-in |
| `journalctl -u avahi-daemon -b` | the Bonjour service and name conflicts |
| `journalctl -u ipp-usb -b` | whether ipp-usb took a printer |
| `/var/log/ql810wc-proxy/install-<UTC time>.log` | everything each install run did |

For more detail, set `LOG_LEVEL="DEBUG"` in `config.env` (as root, for
example with `sudo vi config.env`) and run `sudo ./install.sh --only 20`: it
rewrites `/etc/ql810wc-proxy/runtime.env`, restarts `ql810wc-proxyd` and
reloads `ql810wc-snmp-agent` (no SNMP gap). Set it back to `INFO` the same
way when you are done.

- A quick, temporary change also works: edit `LOG_LEVEL` in
  `/etc/ql810wc-proxy/runtime.env` as root, then
  `sudo systemctl reload ql810wc-snmp-agent` (the agent applies it without a
  restart) and `sudo systemctl restart ql810wc-proxyd`. The next install
  overwrites the file, and until then `selftest.sh` warns that `runtime.env`
  is out of date.
- A reload the agent rejects (an invalid file) shows as `last reload failed:
  …` in `systemctl status ql810wc-snmp-agent` and as an ERROR in its journal;
  it keeps serving the previous settings.

## SELinux

SELinux stays enforcing. The tested unit ran every install step without a
single denial.

```bash
getenforce                                      # Enforcing
sudo ausearch -m avc -ts boot                   # expect "<no matches>"
ps -eZ | grep -e python3 -e snmpd               # the two daemons: unconfined_service_t; snmpd: snmpd_t
```

- Some denials are not logged (dontaudit rules). To see them: `sudo semodule
  -DB`, reproduce the problem, `sudo ausearch -m avc -ts recent`, then restore
  the rules with `sudo semodule -B`.
- Never switch the whole system to permissive. To test one domain, use
  `sudo semanage permissive -a <domain>` and undo it with
  `sudo semanage permissive -d <domain>`.
- In `SNMP_AGENT_MODE="pass_persist"` the persona runs inside snmpd
  (`snmpd_t`) and reads `/run/ql810wc-proxy/state.json` through a file-context
  rule the installer adds: `ls -Zd /run/ql810wc-proxy` shows
  `snmpd_var_run_t`. If it does not, run `sudo restorecon -Rv /run/ql810wc-proxy`.

## Uninstall and reinstall

```bash
sudo ./uninstall.sh --dry-run    # what would be removed or restored
sudo ./uninstall.sh
sudo ./setup.sh                  # reinstall: keeps config.env, ends with the selftest's verdict
```

- `install.sh`, and so `setup.sh`, exits 1 when the final selftest finds T0
  not reached, for example while the printer is switched off. Fix what its
  `FAIL` lines say, then run `sudo ./selftest.sh`; see
  [Reading the selftest report](#reading-the-selftest-report).
- Reinstalling keeps the printer identity (`PRINTER_UUID`), so P-touch keeps
  its saved printer. `sudo ./uninstall.sh --purge` deletes it as well; the next
  install then creates a new identity and clients see a new printer.
- One step at a time: `sudo ./install.sh --only 20` (data plane), `--only 30`
  (SNMP), `--only 40` (Bonjour). `--only` leaves out the selftest; run
  `sudo ./selftest.sh` afterwards.
- `uninstall.sh` keeps a directory that still holds files it did not create,
  and keeps the `includeDir` block in `/etc/snmp/snmpd.conf` while other files
  in `/etc/snmp/snmpd.conf.d` need it. Its output names what it kept.
- If firewalld is not running during the uninstall, the rules the installer
  added are removed from its permanent configuration with
  `firewall-offline-cmd`. What cannot be reversed right now (the CUPS
  scheduler down, firewalld down without `firewall-offline-cmd`, a failing
  `userdel` or `systemctl disable`) stays in
  `/var/lib/ql810wc-proxy/manifest`: the run ends with "partially
  uninstalled" and exit status 1. Fix the cause it names, then run
  `sudo ./uninstall.sh` again; `--purge` keeps `/var/lib/ql810wc-proxy` until
  then.
