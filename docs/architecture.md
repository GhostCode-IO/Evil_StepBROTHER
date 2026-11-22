# Architecture (implementation contract)

This document is the contract every component is built against: process
layout, file paths, on-disk formats, unit names and protocol behaviour. If code
and this document disagree, fix one of them in the same commit.

Evidence for the protocol facts below comes from development-time research
(summarised with sources in [`dev/notes/README.md`](../dev/notes/README.md)):
Brother's QL-800/810W/820NWB raster reference, open-source implementations,
static analysis of the macOS P-touch Editor 5.3 / MacPrintSDK client, status
data that others published from real printers, and the tested unit itself.
Facts not yet confirmed on a real QL-810W(c) are marked **(unverified)**.

## 1. What clients actually do

| Client | Discovery | Print data | Status / installed media |
|---|---|---|---|
| P-touch Editor 5.3 (macOS) | Bonjour `_pdl-datastream._tcp`; accepts a service only if TXT `usb_MDL` is exactly `QL-810W` and TXT `UUID` exists. "Connect to printer with an IP address" does one SNMPv1 GET of `1.3.6.1.4.1.2435.2.3.9.1.1.7.0` (IEEE-1284 ID, needs `MDL:QL-810W`, no space after the colon) | raw TCP to `<host>:9100`, never reads the socket | SNMPv1 GET, community **`internal`**, `1.3.6.1.4.1.2435.3.3.9.1.6.1.0` → OCTET STRING, the 32-byte status block. Polled during a job as 3 back-to-back GETs, then a 1 s sleep (up to 180 rounds). Only INTEGER and OCTET STRING replies are accepted. noSuchName is an immediate failure, and snmpd answers with it while the subagent is not registered (§6). |
| Windows driver installer, BRAdmin, P-touch Editor (Windows) | SNMPv1 community **`public`**, broadcast + unicast, reading `sysDescr.0`, `sysObjectID.0`, `sysName.0`, `ifPhysAddress.1`, `hrDeviceDescr.1`, `hrPrinterStatus.1`, Printer-MIB **(unverified per OID)** | Standard TCP/IP port, RAW 9100 | Windows "SNMP status" via Host-Resources/Printer-MIB; Brother status OID as above **(unverified)** |

Real network QLs **never answer anything on 9100**. All feedback is SNMP.
Two-color media shows up **only** in the Brother status block (byte 25 bit 7).

**Observed** (P-touch Editor 5.3.23 for Mac against the tested unit;
[`dev/notes/ptouch-mac-session.md`](../dev/notes/ptouch-mac-session.md)).
P-touch found the box through Bonjour, showed the installed DK-22251 and
printed a correct two-colour label. What it did confirms the Mac row above:

- Discovery: mDNS only (`Brother QL-810W [<host>]._pdl-datastream._tcp`). The
  IEEE-1284 OID was never read; only "Connect to printer with an IP address"
  uses it.
- SNMP: 13 GET requests, all SNMPv1 with community `internal`, all for the
  status block, in bursts of 3 about once a second. No other OID was read.
- TCP 9100: one connection per print. After the job's last `0x1A` the client
  sent a 4-byte trailer (consistent with `1B 69 61 FF`; the `ESC i U t` part
  depends on an SDK flag and was absent). proxyd held it until the printer was
  back at "receiving", then forwarded it. The client half-closed, and proxyd
  closed 0.93 s later, once the printer had finished. P-touch reported
  success.
- The job was compressed (19 000 bytes for a 62 mm label).

## 2. Processes

```
              TCP 9100                        UDP 161                     UDP 5353
                 │                               │                           │
   ┌─────────────▼──────────────┐   ┌────────────▼────────────┐   ┌──────────▼─────────┐
   │ ql810wc-proxyd (python)    │   │ snmpd (net-snmp master) │   │ avahi-daemon       │
   │ user ql810wc, group lp     │   │ v1/v2c communities  ──▶ │   │ static service     │
   │  • sole owner of USB       │   │   context "printer"     │   │ _pdl-datastream    │
   │  • forwards jobs one-way   │   │ v3 authPriv ──▶ host    │   └────────────────────┘
   │  • polls 32-byte status    │   └───────────▲─────────────┘
   │  • writes state.json ──────┼──┐            │ AgentX (unix socket)
   └─────────────▲──────────────┘  │ ┌──────────┴──────────────┐
                 │ /dev/usb/lpN    └▶│ ql810wc-snmp-agent      │
   ┌─────────────┴──────────────┐    │ (python, AgentX sub-    │
   │ Brother QL-810Wc (USB)     │    │ agent, context printer) │
   └────────────────────────────┘    └─────────────────────────┘
   CUPS raw queue  ──socket://127.0.0.1:9100──▶ ql810wc-proxyd   (local/IPP raw jobs)
```

- **`ql810wc-proxyd`** is the only process that touches the printer. Jobs and
  status polls are serialized in one place, so a status request can never land
  in the middle of a job (which wedges the printer).
- **`ql810wc-snmp-agent`** never touches USB. It serves the printer persona from
  `state.json` + `oid-map.yaml` + config.
- **snmpd** stays the single listener on UDP 161 and keeps serving the host's
  own MIBs (default context) to SNMPv3 users.
- **CUPS** gets an optional raw queue whose device is the proxy itself
  (`socket://127.0.0.1:9100`), so CUPS never competes for USB.
- **ipp-usb** is told to ignore the QL (device-specific quirk). It would
  otherwise claim the whole USB device.

## 3. Installed layout

| Path | Owner / mode | Content |
|---|---|---|
| `/usr/local/src/ql810wc-print-proxy/` | root | the checkout that `setup.sh` clones in bootstrap mode (§13). Any other checkout works the same; the scripts run from it and are not installed |
| `<checkout>/config.env` | root 0600 | site settings, written by `setup.sh` when missing (§13); read by `install.sh`, its steps and `selftest.sh`, never deployed |
| `/usr/local/libexec/ql810wc-proxy/` | root 0755 | `ql810wc/` (Python package), `ql810wc-proxyd`, `brother_snmp_agent.py`, `qlctl`, `oid-map.yaml` |
| `/etc/ql810wc-proxy/runtime.env` | root 0644 | non-secret runtime settings, generated from `config.env` |
| `/etc/ql810wc-proxy/identity.env` | root 0644 | `PRINTER_UUID`, generated once and kept across reinstalls |
| `/run/ql810wc-proxy/state.json` | ql810wc 0644 | live state written by proxyd (schema §5) |
| `/etc/systemd/system/ql810wc-proxyd.{socket,service}` | root 0644 | data plane |
| `/etc/systemd/system/ql810wc-snmp-agent.service` | root 0644 | SNMP subagent |
| `/etc/systemd/system/ql810wc-snmp-agent.service.d/ql810wc-proxy-agentx-groups.conf` | root 0644 | only when the AgentX socket or `/var/agentx` belongs to a group of another snmpd setup: `SupplementaryGroups=` with that group |
| `/etc/snmp/snmpd.conf.d/ql810wc-proxy.conf` | root 0600 | VACM + AgentX master config (included from snmpd.conf) |
| `/var/agentx/` | root:ql810wc 0750 | AgentX socket directory. Created like this when absent, so group `ql810wc` can reach `master`. A directory of another non-root group stays as it is, and `ql810wc-snmp-agent` joins that group (the drop-in above). snmpd's own root-group directory gets group `ql810wc` r-x; its old owner and mode go into the manifest as `attrs <path> <uid:gid> <mode>`, which `uninstall.sh` restores |
| `/var/lib/net-snmp/snmpd.conf` | root 0600 | net-snmp's persistent file: the installer appends the SNMPv3 `createUser` line while snmpd is stopped |
| `/etc/avahi/services/ql810wc-proxy.service` | root 0644 | Bonjour record |
| `/etc/ipp-usb/quirks/ql810wc-proxy.conf` | root 0644 | ipp-usb exclusion for the QL |
| `/usr/share/cups/usb/ql810wc-proxy.usb-quirks` | root 0644 | CUPS usb-backend exclusion for the QL (§10) |
| `/etc/udev/rules.d/60-ql810wc-proxy.rules` | root 0644 | group `lp` rw on Brother USB devices (libusb fallback) |
| `/var/lib/ql810wc-proxy/` | root 0750 | install manifest (archived as `manifest.uninstalled-<UTC>` by a completed uninstall), pristine backups, a copy of `identity.env`, the SNMPv3 change marker (salted digest, 0600), and `config-path`: the real path of the `config.env` the last install applied, or `none` for the template defaults |
| `/var/log/ql810wc-proxy/` | root 0750 | one log file per install run |
| `/run/ql810wc-proxy.lock` | root | the lock (`flock`) that `setup.sh`, `install.sh` and `uninstall.sh` hold while they change the box: one run at a time |
| `/opt/ql810wc-proxy/venv` | root 0755 | optional `brother_ql_next` for validation prints |

System user: `ql810wc` (no login, no home), supplementary group `lp`
(`/dev/usb/lp*` is `root:lp 0660`).

Python runs as `/usr/bin/python3 -I -B`. Entry scripts add their own directory
to `sys.path` explicitly. Runtime needs only the stdlib + `python3-pyyaml`
(`python3-pyusb` optional for the libusb fallback).

## 4. Data plane: `ql810wc-proxyd`

- **Listener:** systemd socket activation (`ql810wc-proxyd.socket`,
  `ListenStream=<RAW_PORT>`, `Accept=no`), using the `LISTEN_FDS` protocol;
  binds the port itself when not socket-activated (development). Connections
  are served **one at a time**; others wait in the listen backlog (TCP
  back-pressure, as on the real printer).
- **Forwarding:** bytes from the client reach the printer unmodified and in
  order, or not at all. The stream is tokenized but never modified (job end
  `0x1A`, two-color bit in `ESC i K`, requested media in `ESC i z`, embedded
  `ESC i S`). What the tokenizer sees decides when bytes go out and which
  never do: the bytes held back for a job's checks, when the checks refuse
  the job or the connection ends before the job shows what it asks for, and
  an incomplete command a connection ends in outside a job (see
  **Colour and media checks**, **Trailer handling** and **Set-up commands**
  below). Unknown opcodes switch the tokenizer to "opaque" and forwarding
  continues.
- **Colour and media checks** (`RAW_COLOR_CHECK` and `RAW_MEDIA_CHECK`, both
  default `yes`): a job's print data (everything from its first command that
  is not NUL padding, a set-up or a post-job command) is held back, with the
  rest of the read it starts in, until the job shows what it asks for. That
  is at its first `g` or `w` raster row (or, without one, its first print
  command). There its colour mode is bit 0 of its last `ESC i K`, as long as
  the row agrees (`w` with the bit set, `g` without); its media is its last
  `ESC i z`; and high resolution is bit 6 (`0x40`) of that `ESC i K`. The
  tokenizer keeps copies of both commands from that point
  (`decided_media`, `decided_expanded_mode`), so the job's first page
  decides even when later pages arrive in the same read; later pages are
  not checked. Brother's QL-810W driver for macOS sends `ESC i K 0a` and
  `g` rows for Monochrome, `ESC i K 0b` and `w` rows for 2-Color (its own
  filter, run on a Mac; see `dev/notes/README.md`). The checks judge the
  roll by a status frame from the last `STATUS_MAX_AGE_SECONDS`, asked for
  at that moment if the last one is older and the printer's input state is
  known. A status read earlier that shows no roll or a fault is read again
  before the job is judged, for the colour rule too: a roll swap passes
  through both (cover open, roll out), and closing the roll cover sends no
  status, so a roll just loaded shows only at the next poll. This read, too,
  needs a known input state.
  The job is refused when:
  - **colour** (`RAW_COLOR_CHECK`): the frame shows a black/red roll (byte
    25 `0x81`) and the job is black only, or a single-colour roll (`0x01`)
    and the job is two-colour. The printer itself rejects such a job ("media
    does not support monochrome printing"; "... two-color printing" per
    Brother's SDK **(unverified)**), and the tested unit, after a black-only
    job on DK-22251, answered no status request until it was switched off
    and on.
  - **media** (`RAW_MEDIA_CHECK`, `media.compare_media`): a field of the
    job's `ESC i z` that its n1 flags ask the printer to check (`0x02` media
    type, `0x04` width, `0x08` length) differs from the roll. The type
    (`0x0A` or `0x0B`; other values are not judged) is compared with byte
    11's low nibble, the width (unless 0) with byte 10, and the length, on a
    die-cut roll only, with byte 17, within ±1 mm (`LENGTH_TOLERANCE_MM`:
    the printer reports a rounded length). Continuous tape takes any length,
    and the raster line count is never compared. Also refused: a two-colour
    job with `ESC i K` bit 6 (high resolution) on a black/red roll. Brother's
    raster reference says that the printer checks only the flagged fields
    and reports a mismatch as error information 2 bit 0, "replace media";
    its user's guide says that high resolution cannot be used with
    two-colour printing, and lists a "Two-color media high resolution
    printing error". QL-800 and QL-810W owners report that after "replace
    media" the printer stops until the roll cover is opened and closed
    **(unverified on the QL-810Wc)**. The media rules are judged only while
    the frame shows no fault (cover open, no or end of media, any error):
    through a fault the printer holds jobs (third-party reports) and
    presumably checks the roll it has when it resumes **(unverified)**. The
    size rules are not judged for a roll that
    `media.describe(..., exact=False)` does not know.

  A refusal judged on a frame read before the verdict (an idle poll) is
  confirmed first: the proxy sends one more `ESC i S`, and the reply
  decides, whatever it shows (another roll, no roll, a fault); without a
  reply the earlier frame stands. None of the job has reached the printer
  then, so it stands between commands. A job is never delayed by two
  status requests in one verdict: when the checks already asked during
  this verdict (answered or not), no confirm read follows. A refused job:
  none of its print data reaches the printer, `state.json` records
  `refused` with the reason, and the client gets a RST once that is
  written. Over raw TCP the RST is all a client learns: macOS's CUPS
  backend shows the job as completed when the RST arrives after its last
  write. Anything uncertain goes through unchecked. Nothing is judged for a
  stream that turns opaque before the job shows its mode, without a recent
  frame, without a roll, or after 256 KiB held before the job shows its
  mode (`_CHECK_HOLD_LIMIT`: no `g` or `w` row and no print command yet;
  `Z` blank lines do not count). The colour rule needs an `ESC i K` before
  that point, a row that agrees with it and a byte 25 of `0x81` or `0x01`;
  the size rules need an `ESC i z` before that point, and the
  high-resolution rule needs the colour rule's two-colour `ESC i K` and a
  row that agrees with it (an `ESC @` there clears both commands). A job
  whose media differs from the roll only in fields it does not flag goes
  through, with an INFO line in the journal (`a job from … for … goes to
  the printer unchecked: …`). A job that ends while it is held back sent no
  print data to the printer, so its end never calls for a resync. The
  checks' own `ESC i S` goes out while the job is held: if the printer
  takes it only in part, the job ends torn without any of its data sent
  (never released behind the partial command), and the next status
  request resynchronises the printer.
- **Connection lifecycle:** close **only** after the client half-closes (EOF)
  or after `RAW_IDLE_TIMEOUT` seconds without data (default 300). **Never close
  on a timer after `0x1A`.** P-touch for Mac sends a trailer on the same
  connection after the job's last `0x1A` (`1B 69 55 74 1C 00`, then
  `1B 69 61 FF`), and a failed send makes it report the print as failed.
  Static analysis of MacPrintSDK shows this.
- **Trailer handling:** bytes that arrive after a `0x1A` at a command boundary
  belong to that job. **Hold them** until the post-job USB poll shows the phase
  back at "receiving", then forward them verbatim, except a command the
  connection ends in before it is complete: like any incomplete command
  outside a job, it is kept back and then dropped (see **Set-up commands**).
  Never resync on them, never count them as a torn job. (P-touch also writes
  a bare `ESC i S` here on its PRN/template paths; it is held and forwarded
  the same way.) Per-connection state: `IDLE → CHECKING (colour and media
  checks) → WRITING → SETTLING (after 0x1A: trailer held, post-job polls) →
  IDLE`.
- **Limits:** `SO_KEEPALIVE` with short timers, `RAW_IDLE_TIMEOUT` (300 s) and
  `MAX_JOB_BYTES` (16 MiB), both settable in `config.env`. One silent client
  must not block later jobs.
  Listen dual-stack: P-touch connects to the Bonjour host name, so jobs may
  arrive over IPv6.
- **Delivery acknowledgement:** after the client half-closes, the proxy
  finishes writing to USB and only then closes its side (the real printer
  closes about 0.2-1 s after the client's half-close). Every such wait is
  bounded. After a half-close without `0x1A`, the proxy waits up to
  `RAW_IDLE_TIMEOUT` for the printer to take the last USB transfer. When
  settling after `0x1A`, the printer must have taken the end of the job by
  `post_job_timeout` (60 s), or within `post_job_error_grace` (5 s) once it
  reports an error. Otherwise the job is an error (never `ok`), the transport
  is reopened (which drops the stuck USB transfer; the reopened printer is
  resynchronised), and the client gets a RST once the result is in
  `state.json`. The held trailer of a failed job is forwarded with a
  `post_job_error_grace` stall bound, so the connection never hangs on a
  printer that takes no data.
- **Back-channel:** `RAW_BIDIRECTIONAL=no` (default) never writes to the
  client. `yes` relays printer→host bytes back on the socket (helps CUPS
  back-channel users; real QLs don't).
- **Torn jobs:** a job is torn **only** on abnormal termination before its
  `0x1A`: RST/error, `RAW_IDLE_TIMEOUT` or `MAX_JOB_BYTES`. Parser state alone
  (for example an "opaque" stream) never makes a job torn. Recovery is
  400×`0x00` + `ESC @` to resynchronise the printer (not needed for a job
  still held back for the checks, which sent no print data). The real
  printer would stay BUSY until power-cycled. A torn job may print a partial
  label first.
- **Tokenizer extras:** `ESC i U t` + 2 bytes (from P-touch's trailer;
  provisional length), alongside `ESC i U J` + 14 and `ESC i U w` + 128.
- **Post-job commands:** `ESC i S`, `ESC i U t` + 2 and `ESC i a FF` are not
  job content. After a `0x1A` they never start a new job and never cause a
  resync, and the job stays ended. `ESC i a FF` (back to the printer's own
  command mode) still ends parsing: the stream turns opaque there and is only
  counted from then on. As long as nothing has followed it, the stream still
  stands at a command boundary, so the idle polls go on while the client
  keeps the connection open. A second job sent on the same connection after
  P-touch's trailer is forwarded unchanged but neither tracked nor checked:
  its bytes count as the first job's trailer, and proxyd does not poll the
  printer again until that connection closes. A connection held open like
  that for longer than `STATUS_MAX_AGE_SECONDS` makes the persona report the
  printer as switched off until it closes (P-touch closes about 1 s after
  its trailer).
- **Set-up commands:** `ESC @`, `ESC i a 01` and `ESC i !` are not job
  content either. A connection with nothing but NULs, set-up commands and
  `ESC i S` is no job. iPrint&Label for iOS opens 402-byte connections of
  its own: six on 2026-11-21, two just before each of its two prints and
  two between them that no print followed right away (a **Check Media** tap
  that day sent no data to TCP 9100). The two before its second print, the
  only ones whose content was counted (an older `ql810wc-proxyd` logged the
  first four as jobs that ended early), held 400 NULs and an `ESC @`
  (`NUL bytes 400, set-up commands 1, status requests 0`). Such bytes are
  forwarded, the
  printer is left at a command boundary and is not resynchronised, and the
  journal line `connection from …: <n> bytes, no print job (NUL bytes …,
  set-up commands …, status requests …)` counts what the connection held.
  While no job
  is open on a connection (before its first job, and in a settled job's
  trailer until the next job starts), a read that ends inside a command
  keeps that command back until it is complete (and drops it, with a journal
  line, if the connection ends first), so where a read ends never leaves the
  printer waiting inside a command. After a settled job the next job starts
  at its first print-data command, which need not be its `ESC i z` (an
  `ESC i U J` may come first), so the checks hold it from that command,
  with the rest of the read it starts in. NULs and set-up commands
  that arrived in an earlier read were forwarded already and count as the
  previous job's trailer. While
  set-up commands wait on an open connection for a job, the idle polls
  pause, as during a job; held open for longer than
  `STATUS_MAX_AGE_SECONDS`, such a connection makes the persona report the
  printer as switched off until its status is read again.
- **Status polling:** never during a job. Idle: every `STATUS_POLL_SECONDS`
  send `ESC i S` and read the 32-byte reply. The request is preceded by
  400×`0x00` + `ESC @` only while the printer's input state is unknown: at
  start-up and after a reopen, after an I/O error or a torn or aborted
  write, after a status request written only in part, and after an opaque
  job that ended in an error. Never after a fault the printer reports at the
  end of a complete job: `ESC @` cancels a job the printer holds until the
  fault is cleared. A request that was written but not answered does not
  make the state unknown. While the last frame shows an error, no roll,
  printing or busy, the `ESC @` is deferred: only 400×`0x00` + `ESC i S` is
  sent until two polls in a row find the printer idle, and the full resync
  then goes out right after the second probe. Start-up and reopen resync at
  once. A printer that leaves three requests in a row unanswered is closed
  and reopened; while its last frame showed printing, that reopen waits, at
  most `post_job_timeout` after that frame (the reopened printer is
  resynchronised, and `ESC @` would cut the label short). After a job: read
  volunteered frames, then poll until the phase is back to "receiving" (cap
  ~60 s), so SNMP clients see completion.
- **Device lifecycle:** sysfs rescan every few seconds; Editor Lite (USB class 8)
  and absence are reported in `state.json`. All USB I/O has timeouts.
- **usblp readiness: never select on the usblp node.** The QL answers every
  bulk-IN request with a zero-length packet, so the kernel reports
  `/dev/usb/lpN` readable again at once and each read returns 0 bytes.
  Waiting on it with `select` spun a CPU core on the tested unit (about one
  CPU-second per second). proxyd therefore treats usblp like the libusb
  transport, which has no pollable descriptor (`PrinterLink.fileno()` returns
  None for it), and reads it on a timer: every 0.25 s while idle, every
  0.05 s during a job. `tests/test_proxyd.py` keeps a regression test with an
  always-readable fake node. Test doubles do not model every hardware quirk;
  this one was found only on the real printer.
- **Hardware-verified on the tested unit** (`dev/notes/usb-ground-truth.md`):
  - Printer mode is `04f9:209c`; Editor Lite is `04f9:209f` (mass storage only).
  - **The reply to `ESC i S` is not always status type `0x00`.** With the cover
    open it arrives as type **`0x02` (error occurred)** with err2 `0x10`. Treat
    the first complete 32-byte frame after a request as its reply, whatever its
    status type. Opening or closing the cover queued no unsolicited frame.
  - DK-22251 idle reads `... 3e 0a 00 00 23 ... 81 ...`. DK-11201 (29x90
    die-cut) reads width `0x1d`, type `0x0b`, length `0x5a`, roll code `0x01`,
    byte 25 `0x01`.
  - **No media** (roll removed) reads width `0` and media type `0x00` while
    err1 bit `0x01` ("no media") stays clear, and byte 25 keeps the removed
    roll's value. `Status.has_media` therefore trusts only the width and type
    bytes (or that err1 bit), and `Status.two_color` is False without media.
  - Auto power-off: the unit switched itself off (a USB disconnect) about 60
    minutes after the last status request. The idle `ESC i S` poll restarts
    that timer: with it, the unit stayed on for almost four hours without a
    job.
- **systemd:** `Type=notify`, `READY=1` once listening and after the first
  device scan (so the first `state.json` never says `absent` for an attached
  printer), `STATUS=` updates, watchdog pings.

## 5. `state.json` (schema 1)

Written atomically (temp file + rename) by proxyd only. Readers treat missing
or unparseable files as "printer unknown".

```json
{
  "schema": 1,
  "updated": 1763650000.12,
  "proxy": {"version": "0.1.0", "pid": 1234, "started": 1763640000.0, "raw_port": 9100},
  "device": {
    "state": "ready | absent | mass-storage | error",
    "vid": "04f9", "pid": "209c", "product": "QL-810W", "manufacturer": "Brother",
    "serial": "<usb serial>", "node": "/dev/usb/lp0", "transport": "usblp | pyusb",
    "ieee1284_id": "MFG:Brother;CMD:...;MDL:QL-810W;CLS:PRINTER;"
  },
  "status": {
    "raw_hex": "802042343930...",
    "polled": 1763650000.10,
    "parsed": {"model_code": 57, "media_width_mm": 62, "media_type": 10,
               "media_length_mm": 0, "two_color": true, "errors": [], "...": "..."}
  },
  "job": {"active": false, "peer": null, "started": null, "bytes": 0,
          "two_color": null, "last_result": "ok | error | torn | refused | null",
          "last_finished": null}
}
```

proxyd also writes `device.detail` (why the printer is not ready),
`status.media` (the roll as P-touch would recognise it), `status.source`
(`reply` to a status request, or `unsolicited`), `job.requested_media` (the
`ESC i z` of the job's first page: `flags`, `type`, `width`, `length`,
`lines`, `page`) and `job.last_detail` (for `refused`, the checks' reason).
Readers ignore fields they do not know.
`device.firmware` is reserved for the printer's firmware version, which
`selftest.sh` matches against the OID map's `validated_firmware` (§7); proxyd
does not write it today.

`device.serial` stays on the box (it is what the real printer reports over SNMP).
It never goes into git.

## 6. Control plane: SNMP persona

- **Context split:** v1/v2c communities (`SNMP_PRINTER_COMMUNITIES`, default
  `public internal`) are mapped with `com2sec -Cn printer` to the SNMP
  context **`printer`**, read-only, view `printerView`, sources limited to
  `SNMP_PRINTER_ALLOWED_CIDRS` (+127.0.0.1 for selftest, and ::1 when an IPv6
  range is configured). Verified on net-snmp 5.9.5.2 on the tested unit:
  the communities see only the persona (no hrStorage, hrSWRun or UCD data),
  and a wrong community gets no answer.
- **Subagent:** `ql810wc-snmp-agent` (AgentX over `/var/agentx/master`)
  registers the persona subtrees **in context `printer`**: Brother
  (`.1.3.6.1.4.1.2435`, which holds the status block) first, then system
  (`.1.3.6.1.2.1.1`), `ifPhysAddress.1`/ifTable row 1, Host-Resources
  device/printer row 1 and Printer-MIB (`.1.3.6.1.2.1.43`). It reconnects and
  re-registers when snmpd restarts. Settings, identity and OID-map changes go
  through `systemctl reload ql810wc-snmp-agent`: the unit is
  `Type=notify-reload`, so systemd sends SIGHUP and waits for the agent's
  `RELOADING=1` and `READY=1`, and the new values are served in the same
  AgentX session. An invalid file is logged as an ERROR, the running persona
  stays, and the unit's status says `last reload failed: …` (the install step
  that reloaded it then fails). Only new code or a new unit file restarts the
  agent.
- **AgentX access:** the drop-in sets `agentXPerms 0660 0750 root ql810wc`
  (socket group `ql810wc`) unless another snmpd configuration sets
  `agentXPerms`. net-snmp keeps one value per field for the whole agent (the
  last line wins), so the drop-in then sets none: the subagent uses what that
  line grants (write access for others, group `ql810wc`, or another non-root
  group, which it joins through the unit drop-in of §3), or step 30 stops and
  says what to change.
- **Host persona:** the default context keeps net-snmp's real host MIBs. Only
  SNMPv3 `authPriv` users get it (`SNMP_V3_*`); they can also read the persona
  with `-n printer` (both verified on the tested unit).
- **Fallback:** `SNMP_AGENT_MODE=pass_persist` serves only `.1.3.6.1.4.1.2435`
  and `.1.3.6.1.2.1.43` in the default context (degraded: no system/ifPhys/hr
  persona).
- **Values** come from `agent/oid-map.yaml` (§7): constants, config values, or a
  fixed set of named providers computed from `state.json`. There is no `eval`.

Key OIDs (full list in `oid-map.yaml`):

| OID | Meaning | Source |
|---|---|---|
| `1.3.6.1.4.1.2435.3.3.9.1.6.1.0` | 32-byte Brother status block (OCTET STRING) | live USB status, served verbatim. While a job is in flight, **only bytes 18-21** are synthesised (type `0x00`, phase `0x01` printing, phase number `00 00`), so P-touch keeps polling. Media bytes are never touched, and the synthesis is capped at 60 s after the job's last observed byte (the post-job poll limit). A job still in flight past that cap (a stalled write, a half-close drain, a client gone silent mid-job) gets the turned-off block below, never the pre-job idle frame. When the printer or proxyd is unavailable (absent, Editor Lite, USB error, status older than `STATUS_MAX_AGE_SECONDS` while idle, `state.json` missing or not rewritten for 3 × `STATUS_POLL_SECONDS`), a **"turned off" block**: QL-810W header, status type `0x02`, err1 `0x20`, no media. P-touch reports that at once as switched off, whereas a silent agent would make it retry for 60 s. The persona answers the OID in every situation. But while snmpd runs and `ql810wc-snmp-agent` is not registered (a restart, about 1 s; a crash, `RestartSec=1` plus start-up), net-snmp keeps context `printer` alive through its own null registrations and answers SNMPv1 GETs with noSuchName, which P-touch treats as an immediate failure. Settings, identity and OID-map changes therefore go through `systemctl reload ql810wc-snmp-agent` (Subagent, above). After an snmpd restart the context does not exist until the agent has registered again, so requests are dropped and P-touch retries for up to 60 s; the agent registers `.1.3.6.1.4.1.2435` first |
| `1.3.6.1.4.1.2435.2.3.9.1.1.7.0` | IEEE-1284 device ID (`MDL:QL-810W`) | USB device ID, normalised |
| `1.3.6.1.2.1.25.3.5.1.1.1` | hrPrinterStatus.1 (3 idle, 4 printing, 1 other) | state |
| `1.3.6.1.2.1.1.2.0` | sysObjectID `1.3.6.1.4.1.2435.2.3.9.1` | constant **(unverified for QL)** |

## 7. `oid-map.yaml` (the shipped, firmware-tagged asset)

```yaml
schema_version: 1
model: QL-810W
validated_firmware: []        # firmware versions the whole map was checked against (none yet)
entries:
  - oid: 1.3.6.1.4.1.2435.3.3.9.1.6.1.0
    name: brStatusBlock
    type: octets              # octets | string | integer | oid | timeticks | counter32 | gauge32 | ipaddress
    source: {provider: status_block}   # or {const: ...} / {config: KEY} / {template: "..."}
    context: printer
    verified: false           # true once confirmed on a real QL-810W(c)
    notes: "..."
```

`selftest.sh` (§12) validates the map against the live printer, through the
installed agent and snmpd rather than from the file alone:

- `brother_snmp_agent.py --check` loads the map and the settings without
  error;
- the map serves the status block (`1.3.6.1.4.1.2435.3.3.9.1.6.1.0`, type
  `octets`, provider `status_block`);
- an SNMPv1 GET of that OID (community `internal`, from 127.0.0.1) returns
  the 32 bytes proxyd last read over USB, byte for byte. Only bytes 18-21 may
  differ, and only while a job prints (the synthesised printing phase, §6);
- that block's model code (bytes 3/4) is the map's `model` (`34 39` for
  `QL-810W`), its media type (byte 11) is `0x0a` or `0x0b` whenever a roll is
  loaded, and its header is `80 20 42`.

When the map lacks the status block or the block does not fit it, the
printer or its firmware encodes status differently from what the map and
P-touch expect. The self-test then reports **`DEGRADED: T0-T2 with manual
media`**: T3 is not reached, printing and discovery keep working, and users
select the roll by hand in P-touch. A degraded map never fails the install,
because the exit status depends on T0 alone.

`validated_firmware` is matched against `device.firmware` in `state.json`
(§5). proxyd does not write that field today, because the firmware version
cannot be read over USB, and the list is still empty: nobody has checked the
whole map against one printer yet (`dev/snmpwalk/` has the tools). Until
proxyd reports a firmware version that the list contains, every T3 pass
carries this information line, which lowers no tier:

```
NOTE: identity strings in oid-map.yaml (sysDescr, sysObjectID, ...) are not yet verified on a real QL-810Wc; P-touch Editor for Mac and iPrint&Label for iOS work with them as they are
```

## 8. Discovery (T2)

Avahi static service `_pdl-datastream._tcp`, port `RAW_PORT`, instance
`Brother QL-810W [<PRINTER_NAME_SUFFIX or hostname>]`. TXT records:
`txtvers=1 qtotal=1 pdl=application/octet-stream ty=Brother QL-810W
product=(Brother QL-810W) usb_MFG=Brother usb_MDL=QL-810W priority=75
UUID=<PRINTER_UUID> note=<location>`. The Mac client reads only `usb_MDL` and
`UUID` (exact match, case-sensitive); the rest mirrors real Brother printers.

## 9. Firewall (firewalld, additive only)

| Port | Scope | Why |
|---|---|---|
| TCP `RAW_PORT` (9100) | zone-wide, or `RAW_ALLOWED_CIDRS` | print data |
| UDP 161 | rich rules per `SNMP_PRINTER_ALLOWED_CIDRS` + `SNMP_V3_ALLOWED_CIDRS` | printer persona + host monitoring |
| UDP 5353 | rich rules per `MDNS_ALLOWED_CIDRS` (default = SNMP client ranges) | Bonjour discovery (never the zone-wide `mdns` service) |

Nothing else is opened or changed. Cockpit (9090) and CUPS (631) are never touched.

## 10. Keeping other USB users off the printer

`ql810wc-proxyd` must be the only process that opens the QL. Two Fedora
components would otherwise interfere:

- **ipp-usb** selects the printer's IPP-over-USB alternate setting, which
  unbinds `usblp`. `/etc/ipp-usb/quirks/ql810wc-proxy.conf` blacklists the QL
  (`blacklist = true`) in a section named after the printer's
  "<USB manufacturer> <USB product>" string, detected at install time
  (`[Brother QL-810W]` on the tested unit), which is how ipp-usb matches
  devices. ipp-usb reads quirks only at start-up: when it is running and the
  quirk changed, or the printer has no usblp node, the installer stops
  ipp-usb so it releases the printer, and starts it again at once if other
  IPP-over-USB devices are attached, so those keep working. The installer
  then binds `usblp` to the printer interface if nothing holds it.
- **CUPS's usb backend** detaches `usblp` from every printer-class device
  whenever it enumerates: `lpinfo -v`, cups-deviced, the web UI's "Add
  Printer" page, and the 5-second rescans of any `usb://` queue whose printer
  is absent. Install `/usr/share/cups/usb/ql810wc-proxy.usb-quirks` (the only
  quirks directory CUPS reads) with `0x04f9 0x<runtime pid> blacklist`. That
  makes CUPS skip the device entirely. Our own CUPS queue prints through
  `socket://127.0.0.1:<RAW_PORT>`, so it does not need CUPS's usb backend.

snmpd drop-in:

- **`agentXSocket /var/agentx/master`, pinned to net-snmp's default path.**
  `ql810wc-snmp-agent` connects to exactly that path (`--socket
  /var/agentx/master` in its unit), and `agentXSocket` is last-wins. The
  installer warns when another snmpd configuration file sets a different
  socket. AgentX over TCP is never used: it has no authentication.
- **`agentaddress` only when IPv6 is needed.** snmpd's default, when no
  `agentaddress` is configured, is already the UDP/161 IPv4 wildcard that SNMP
  broadcast discovery needs. `agentaddress` lines append to each other, and a
  second bind of UDP 161 makes snmpd exit. So the drop-in emits no
  `agentaddress` line unless `SNMP_PRINTER_ALLOWED_CIDRS` contains an IPv6
  range, or `SNMP_V3_ALLOWED_CIDRS` does while SNMPv3 is on
  (`SNMP_V3_ENABLE` resolves to on; otherwise that setting is ignored). Then
  it emits `agentaddress udp:161,udp6:161`, because any explicit line
  replaces the implicit IPv4 default. If another snmpd configuration already
  sets listening addresses (an `agentaddress` line, or addresses on the
  snmpd command line in `OPTIONS` of `/etc/sysconfig/snmpd`), the drop-in
  emits none and the installer warns. `OPTIONS` is read with snmpd's own
  option rules: the values of options such as `-Lf FILE`, `-LS 0-6 d` or
  `-p FILE` are never taken for listening addresses.
- Once snmpd runs, step 30 checks with `ss` that it listens on the UDP 161
  IPv4 wildcard (and warns when IPv6 sources are allowed but nothing listens
  on IPv6). The step fails if the IPv4 wildcard is missing or held by another
  program.

## 11. SELinux

Stays enforcing. Our daemons are started by systemd from `bin_t` executables
and run as `unconfined_service_t`, so they can use USB and the AgentX socket.
The `pass_persist` fallback runs inside `snmpd_t`, which cannot read USB. That
is why it reads `state.json` instead (labelled `snmpd_var_run_t` by
`semanage fcontext` only in that mode). Any policy module we need ships in the
repo and is removed by `uninstall.sh`; none is needed so far. On the tested
unit, steps 00 to 40 ran under enforcing mode with no AVC denial.

## 12. Self-test (`selftest.sh`)

A read-only, end-to-end check that runs from the checkout (it is not
installed). `install.sh` runs it after its steps, except with `--only N` or
`--no-selftest`. It looks at the system the way clients do: the units, the
TCP and UDP listeners, `state.json` through `qlctl status --json`, the
persona over SNMP from 127.0.0.1 (with a private client configuration, so no
`snmp.conf` of the host or the user interferes), the Bonjour record through
`avahi-browse`, and the firewall rules, runtime and permanent. Only
`--print-test` has an effect: one test label through TCP `RAW_PORT`.

- Every check yields one record: section (`setup`, `t0`, `t1`, `t2`, `t3` or
  `host`), status (`pass`, `warn`, `fail`, `skip` or `info`), an id and a
  message.
- A tier is reached when no check of its section failed and every tier below
  it is reached. T0 also needs no `setup` failure. T1 also needs a passing
  roll check (a black/red roll, DK-22251, is loaded); without one T1 is "not
  verified" and the ladder stops at T0. T3 is "degraded" when the status
  block does not fit the OID map (§7). A `warn` never counts against a tier.
- The self-test's own SNMP and Bonjour queries come from this host, which no
  allowed range filters. So T2 also judges who on the network can reach
  them: `t2.reach.snmp` and `t2.reach.mdns` fail while
  `SNMP_PRINTER_ALLOWED_CIDRS`, or the ranges UDP 5353 is opened for
  (`MDNS_ALLOWED_CIDRS`, else the SNMP ranges), are empty or hold only
  loopback or documentation ranges (192.0.2.0/24, 198.51.100.0/24,
  203.0.113.0/24, 2001:db8::/32). `t2.reach.mdns.local` warns when no mDNS
  range is a subnet of this host (mDNS queries never cross a router). They
  are judged only when `config.env` is readable; a non-root run with a
  root-only `config.env` skips them. Warnings that never lower a tier:
  `t2.listen.ipv6` (IPv6 client ranges, but no UDP 161 listener on IPv6),
  `host.reach` (`SNMP_V3_ALLOWED_CIDRS` holds only documentation ranges),
  `t2.agent.canreload` (the agent's unit file cannot reload, so new settings
  would need a restart), `t2.agent.reload` (the agent rejected its last
  reload), and `t0.mediacheck` and `t1.colorcheck` (`runtime.env` turns
  `RAW_MEDIA_CHECK` or `RAW_COLOR_CHECK` off; `t1.colorcheck` is only
  `info` unless a black/red roll is loaded).
- The SNMPv3 bonus is reached when the SNMPv3 user reads the host's own
  `sysDescr` in the default context and no community leaked host data. It is
  skipped while SNMPv3 is off, and it never moves the ladder.
- Verdict line: `TIER REACHED: NONE|T0|T1|T2|T3`, plus ` (+host SNMP)` for the
  bonus. Before it come one summary line per tier, a `NOTE` line when tiers
  passed above one that did not, and the `DEGRADED` line or the firmware
  `NOTE:` line of §7.
- Exit status: 0 when T0 is reached, 1 when it is not, 64 for a usage error.
  If the script breaks off before its verdict, it prints `TIER REACHED: NONE`
  and exits non-zero. `install.sh` turns any non-zero result into its own exit
  status 1 ("installed, but selftest reported problems"). A missing higher
  tier never fails an install.
- `--json` prints a summary document (`"schema": 1`) on stdout and the report
  on stderr. When `install.sh` runs it, the report also goes into the install
  log.
- Timing: a just-started proxyd may not have read the printer yet, so the
  self-test waits up to 15 s for a ready printer with a valid status block
  before judging T0. proxyd's CPU use is sampled for 5 s and must stay at or
  below 20 % while the printer is idle (the usblp readiness storm, §4).
- Output: the printer's serial number is masked, and so are community names
  other than `public` and `internal`. The SNMPv3 passphrases go into a
  private client configuration file (mode 0600), never onto a command line.

## 13. One-step setup (`setup.sh`)

`setup.sh` at the repository root is the single entry point for users. It
runs in one of two modes and asks no questions.

- **Bootstrap mode:** the script is read from standard input
  (`curl … | sudo bash`), or no `install.sh` and `scripts/lib/common.sh` sit
  beside it. It needs root and Fedora (other systems: error; a Fedora other
  than 44: warning, the preflight decides). It is self-contained, because
  nothing else of the project exists yet. After the usage checks it takes the
  lock (below), installs `git-core` when git is missing, clones
  `QLP_REPO_URL` (default
  `https://github.com/GhostCode-IO/Evil_StepBROTHER.git`) at `QLP_REPO_REF`
  (`--ref`, default `main`) into `QLP_SRC_DIR` (`--dir`, default
  `/usr/local/src/ql810wc-print-proxy`), or fast-forwards the checkout there
  (fetch and merge, fast-forward only). A directory that is not owned by
  root, is neither empty nor a git checkout, is a checkout of another
  repository or cannot be fast-forwarded is refused, and the files and the
  branch of the checkout are left as they were (a refused fast-forward has
  fetched, so only git's own records of the fetch are new). The first two
  are refused before anything on the system
  changes; the last two need git, so `git-core` may have been installed by
  then. Then it executes the checkout's `setup.sh` with the same arguments.
  With `--print-config` it takes no lock and installs and fetches nothing:
  it hands over to the checkout already in place, as it is, and exits 1
  when there is none.
- **Checkout mode:** it installs `python3` and `iproute` when they are
  missing (except with `--print-config`), and records them, like a
  `git-core` the bootstrap installed, as `pkg` entries of the install
  manifest. It uses the `config.env` the installer reads (`QL_PROXY_CONFIG`,
  else the checkout's, else `/etc/ql810wc-proxy/config.env`), or else a new
  `config.env` next to `install.sh`. A file that does not exist yet it
  writes (root, 0600, never readable by others while being written: a
  temporary `.config.*.tmp.env` file in the same directory, renamed over
  it) as a copy of `config.sample.env` whose first paragraph is replaced by
  a header of its own, with `SNMP_PRINTER_ALLOWED_CIDRS`,
  `PRINTER_NAME_SUFFIX` (`--name`) and `SNMP_V3_ENABLE="no"` filled in.
  `SNMP_PRINTER_ALLOWED_CIDRS` is `--clients`, or every global IPv4 network
  of the interface that holds the IPv4 default route with the lowest metric
  (from `ip -4 route show default` and
  `ip -4 -o addr show dev IFACE scope global`, computed with Python's
  `ipaddress`; a /32 network gets a warning). The
  host's other IPv4 networks that this leaves out are reported: a warning
  for those on an interface with a device in `/sys/class/net` (Ethernet,
  Wi-Fi), with the `--clients` value that admits them, a note for bridges,
  container networks and tunnels. `--snmpv3` sets `SNMP_V3_ENABLE="yes"`,
  random passphrases (32 letters and digits, so the installer's
  `KEY="value"` parser accepts them; usable passphrases already in
  `config.env` are kept), and `SNMP_V3_ALLOWED_CIDRS` as the client
  networks while it is empty, the template's example or the same as the
  client networks (a range of its own is kept). An existing `config.env` is
  kept; only the keys of the options given are rewritten in place, each
  rewrite is logged with its old and new value (a passphrase only by name),
  and nothing is detected. Then it runs `./install.sh` with the arguments
  after `--`.
- **Settings of an installed box:** `install.sh` records the real path of
  the `config.env` it applied in `/var/lib/ql810wc-proxy/config-path`
  (`none` for the template defaults; an install made before that record
  existed is read from the `config:` line of its install log). When
  `runtime.env` exists, no `config.env` is found and the record is not
  `none`, a new `config.env` from the template would replace the settings
  the box was installed with. setup.sh then stops with exit status 3 before
  it writes one, and its `HUMAN` lines name the recorded file, the
  `install -m 600 -o root -g root` command that copies it into this
  checkout, and the setup.sh of the checkout it came from (or, when that
  checkout has none, the command that names the file in
  `QL_PROXY_CONFIG`). `--new-config` writes the new `config.env` anyway;
  `--print-config` only warns. `install.sh` makes the same check and stops
  with exit status 3 as well.
- **One run at a time:** `setup.sh`, `install.sh` and `uninstall.sh` take an
  exclusive `flock` on `/run/ql810wc-proxy.lock` through file descriptor 9
  before they change anything (setup.sh: except with `--print-config`).
  The checkout's `setup.sh` and the `install.sh` it runs inherit the
  descriptor, and with it the lock (`QLP_LOCK_HELD=1`). A run that finds
  the lock taken stops with exit status 1.
- **Robust when piped:** the whole body is functions, and the last line is
  `main "$@"`, so a truncated download cannot run half a script. When
  standard input is not a terminal, child commands read `/dev/null`, so they
  cannot consume the rest of the piped script.
- **Exit status:** 0 installed (`install.sh` finished, and its selftest
  reached at least T0; `-- --only N` and `-- --no-selftest` leave the
  selftest out), 1 an error, 3 a person must act first (the `HUMAN` lines
  say what; setup.sh adds the command to run again), 64 usage error. With
  `--config-only`, 0 means that `config.env` was written or found
  unchanged.
- **`--print-config`** changes nothing. It exits 0 once it has printed the
  configuration: the `config.env` setup.sh would write, or the existing one,
  with the SNMPv3 passphrases, the serial numbers (`USB_SERIAL`,
  `PERSONA_SERIAL`) and community names other than `public` and `internal`
  masked, so that the output can be shown in public. In a checkout it needs
  no root, unless `config.env` exists and only root may read it. It
  installs nothing, so it exits 1 when `python3` or `iproute` is missing,
  and also when it has to detect the client network and finds no IPv4
  default route or address (pass `--clients`). The one-liner needs root,
  uses the checkout in `--dir` as it is, without updating it, and exits 1
  when there is none yet (use `--config-only` there). A usage error gives
  64.
