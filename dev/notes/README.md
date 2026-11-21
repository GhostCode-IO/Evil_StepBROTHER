# dev/notes: development-time findings

For contributors. These notes record what the design in
[`docs/architecture.md`](../../docs/architecture.md) rests on: facts read from
the tested printer, observed client behaviour, and a summary of the research
done before the code was written. Installing or using the proxy never needs
them.

Rules for everything in this directory: no serial numbers, IP addresses, MAC
addresses or host names (use placeholders and documentation ranges), scrub
any excerpt of an SNMP walk first (`dev/snmpwalk/scrub.py`), and keep raw
walks in `dev/walks/` (gitignored). Facts learned from proprietary binaries
are recorded as short statements only, never as code, disassembly or strings
dumps.

## Index

| File | Contents |
|---|---|
| [usb-ground-truth.md](usb-ground-truth.md) | Hardware facts read over USB from the tested QL-810Wc: USB IDs and interfaces in both modes, the IEEE-1284 ID, status blocks for DK-22251, cover open, no media and DK-11201, bulk-IN zero-length packets, auto power-off, brother_ql_next test prints. |
| [ptouch-mac-session.md](ptouch-mac-session.md) | The first end-to-end session of P-touch Editor 5.3.23 for Mac against the box, as observed: Bonjour discovery, the SNMP requests and their cadence, the TCP 9100 lifecycle and the post-job trailer. |
| README.md (this file) | Index, verification status of the tested unit, and the research summary with sources. |

Related: [`dev/snmpwalk/README.md`](../snmpwalk/README.md) (tools to read a
QL-810W(c) on Wi-Fi with SNMP and diff its roll states),
[`dev/walks/README.md`](../walks/README.md) (raw walks, never committed),
[`agent/oid-map.yaml`](../../agent/oid-map.yaml) (the shipped OID map; each
entry says whether it is verified).

## Verified on the tested unit (2026-11-20 and 2026-11-21)

- **Platform:** a Raspberry Pi 4 with Fedora 44 aarch64, a non-English system
  locale, less than 1 GB free on `/` and SELinux enforcing. Steps 00 to 40 ran
  from the committed scripts and succeeded, with no AVC denial.
- **T0/T1:** labels sent to TCP 9100 went through `ql810wc-proxyd` and usblp
  and printed correctly, black-only and black/red (checked by eye).
- **T2/T3:** P-touch Editor 5.3.23 for Mac found the box through Bonjour,
  showed the installed DK-22251 and printed a correct two-colour label
  ([ptouch-mac-session.md](ptouch-mac-session.md)).
- **SNMP on net-snmp 5.9.5.2:** the communities `public` and `internal` saw
  only the printer persona (no hrStorage, hrSWRun or UCD data); a wrong
  community got no answer; SNMPv3 authPriv saw the host in the default context
  and the persona with `-n printer`.
- **A hardware quirk the test doubles did not model:** the zero-length-packet
  readiness storm on the usblp node, which spun a CPU core until the daemon
  stopped selecting on it ([usb-ground-truth.md](usb-ground-truth.md)).
- **CI:** a Fedora 44 container runs the tests that need Linux (a real snmpd
  with VACM, bash parity checks, a complete `selftest.sh` run on a host where
  nothing is installed). A second, fresh Fedora 44 container without git
  runs setup.sh's one-line bootstrap from standard input with
  `--config-only`, then `install.sh --only 10`.
- **Later on the tested unit (2026-11-20):**
  - `selftest.sh` reached `TIER REACHED: T3 (+host SNMP)` after an in-place
    upgrade and again after `uninstall.sh` plus a fresh `install.sh`. A
    second install run changed nothing.
  - `systemctl reload ql810wc-snmp-agent` (`Type=notify-reload`) kept the
    same process and AgentX session, and all 135 status-block GETs sent
    during two reloads were answered.
  - The 3 s idle status poll kept the printer on for almost four hours
    without a print job (it switched itself off after 60 minutes before).
  - The reinstall cycle found one leftover: snmpd's stale AgentX socket kept
    `/var/agentx` in place. `uninstall.sh` now removes sockets that nothing
    listens on.
  - After the resync and delivery-bound changes to `ql810wc-proxyd`, a
    two-colour `qlctl test-print` label and a P-touch Editor for Mac label
    (19 000 bytes, 4-byte trailer, result ok) both printed correctly.
- **2026-11-21:** Brother iPrint&Label for iOS found the box and printed a
  label. Its **Check Media** then read the loaded roll (DK-22251) correctly;
  the app starts out with a 1.4" label size and reads the roll only when
  asked. It opened three 402-byte connections to TCP 9100 that closed
  without `0x1A`: two just before its print, and one at about the time
  **Check Media** was tried (tied to it by its timing only; a later tap sent
  no data, see the entry on the colour check). `ql810wc-proxyd` logged each as a
  job that ended early and resynchronised the printer. How the app found the
  box (Bonjour or SNMP), what those 402 bytes hold and where the app reads
  the roll were not checked.
- **2026-11-21:** macOS added the box as a printer from its Bonjour record
  (a `dnssd://…._pdl-datastream._tcp.local./?bidi` queue) with Brother's
  QL-810W CUPS driver for macOS (PPD 4.0.4, filter `rastertobrotherQL800`;
  its **Color** option defaults to Monochrome). A PowerPoint job through it
  (5149 bytes, monochrome, 62 mm continuous) reached the printer with
  DK-22251 loaded: the printer reported "media does not support monochrome
  printing" and then answered no status requests. `ql810wc-proxyd` reopened
  it, which did not help; a power cycle did, and the daemon found the
  printer ready again by itself.
- **2026-11-21, Brother's driver for macOS on its own:** a one-page label
  (black and red text) rendered on a Mac with the queue's PPD, `cupsfilter`
  to CUPS raster and then the driver's `rastertobrotherQL800` filter; nothing
  was sent to a printer. With **Color** Monochrome the job had `ESC i K 0a`
  and `g` rows, with 2-Color(Black/Red) `ESC i K 0b` and `w 01`/`w 02` row
  pairs. Both start with 400 NULs, `ESC @` and `ESC i a 01`, use `M 02`
  compression and end with `0x1A`. The filter takes the label size from the
  job's `media` option: with `media=62mm` the job's `ESC i z` asked for
  62 mm continuous, without it for the PPD's default, 29 mm × 90 mm
  die-cut. Replayed against `ql810wc-proxyd` and the test suite's fake
  printer (DK-22251), the Monochrome job was refused before any of its print
  data went out, and the 2-Color job was forwarded byte for byte.
- **2026-11-21, the colour check on the tested unit:** `setup.sh`, run on the
  box after its checkout was moved to the new commit (from a git bundle),
  wrote `RAW_COLOR_CHECK="yes"` into `runtime.env`, restarted
  `ql810wc-proxyd` and the SNMP agent, and reached `TIER REACHED: T3 (+host
  SNMP)`. With DK-22251 loaded, the 62 mm jobs rendered by Brother's driver
  (previous entry) were sent from the Mac to TCP 9100:
  - Monochrome (12 655 bytes): refused. The Mac's connection was reset; the
    journal read `refused a print job from …: a black-only job, but the
    loaded roll, 62mm continuous black/red (DK-22251), takes two-color jobs
    only. None of its print data was sent to the printer`; the printer kept
    answering status requests, with no error.
  - 2-Color(Black/Red) (17 783 bytes): `two-color, requested 62 mm
    continuous, result ok` after 6.5 s. The label came out right, checked by
    eye: black text, red text and a red line where the PDF had them.
  - `qlctl test-print --mono --force` on the box: refused the same way; qlctl
    printed the reason and exited with 3.
  Not tested yet: printing through a macOS print queue with the check on,
  what iPrint&Label's 402-byte check holds, and a two-colour job on a
  single-colour roll.
  - Later the same day, an iPrint&Label **Check Media** tap left nothing in
    `ql810wc-proxyd`'s journal: no connection with data reached TCP 9100
    (one with only NULs and set-up commands would have been logged as `no
    print job`). The app reads the roll some other way, presumably the
    persona's SNMP status block (not captured), and the 402-byte check has
    been seen only before a print.
- **2026-11-21, setup.sh:** the one-liner was piped into `sudo bash` on the
  tested unit, which had been installed from a checkout of its own and had
  no git. The clone came from a git bundle of a pre-release commit, since the
  repository was not public yet. It installed `git-core`, cloned into
  `/usr/local/src/ql810wc-print-proxy` and stopped with exit status 3,
  naming the `config.env` of the earlier install (the guard read it from the
  newest install log, as that install predates the `config-path` record).
  With that file copied into the checkout, the next run fast-forwarded,
  kept `config.env`, redeployed and reached `TIER REACHED: T3 (+host SNMP)`
  with the same `PRINTER_UUID`. A further run changed and restarted nothing.
  Under the box's non-English locale the bootstrap printed its first
  timestamps in another calendar, and the first selftest failed T2 on a UUID
  reader mix-up; both were fixed before the release.

## Research summary

What the design rests on, by topic. Each item names its sources (keys in
[Sources](#sources)) and, after the arrow, what the design does about it.
"Tested unit" means facts verified on this project's own hardware.

### Data plane

1. **Two-colour printing is part of the raster stream.** In a two-colour job
   each raster line is sent as a black plane and a red plane, `ESC i K`
   carries the two-colour flag, and a job ends with `0x1A` [B1, O1, O2].
   Printed both ways on the tested unit, directly over USB and through the
   proxy.
   → Forward the TCP 9100 stream byte for byte; never re-render it or
   synthesize red.
2. **A networked QL never answers on TCP 9100.** It closes its side about
   0.2 to 1 s after the client half-closes, once it has read the whole job; a
   connection torn mid-job leaves it busy until a power cycle [P1, P2].
   Brother's own network flow chart has no status reads on the data channel
   [B1].
   → Write-only by default (`RAW_BIDIRECTIONAL="no"`), close only after the
   client's half-close (delivery acknowledgement) or an idle timeout, and
   resynchronise the printer (400 NUL bytes, then `ESC @`) after a torn job.
3. **Never inject bytes inside a command.** Status requests that landed at
   arbitrary points of a job made a QL-800 report an error and refuse further
   data; a client's own `ESC i S` at a command boundary is tolerated
   (brother_ql_next sends one in every page header) [O2, P4].
   → One process owns the printer, and it never polls while a job is being
   written.
4. **P-touch for Mac keeps the connection open after the job.** After the last
   `0x1A` it sends a short trailer on the same connection (an undocumented
   `ESC i U t` command when an SDK flag is set, then `ESC i a FF`), and a
   failed send counts as a failed print [L1]. Observed as a 4-byte trailer
   on the tested unit.
   → Never close on a timer after `0x1A`; hold trailing bytes until the
   printer is back at "receiving", then forward them.
5. **A mono job on DK-22251 leaves the printer in an error that only a power
   cycle clears** [P1]; P-touch itself refuses such a job when it knows the
   roll [B4].
   → `ql810wc-proxyd` refuses such a job, and a two-colour job on a
   single-colour roll, before its print data reaches the printer
   (`RAW_COLOR_CHECK`, on by default: the job's `ESC i K` and first raster
   row against status byte 25 of a recent frame); a job whose colour mode or
   roll it cannot tell goes through unchecked. The documentation insists on
   two-colour mode for DK-22251, and `qlctl test-print` refuses the
   mismatch. The proxy itself never alters jobs.

### Status block

6. **The SNMP status OID returns the same 32 bytes as the USB `ESC i S`
   reply.** Brother documents `1.3.6.1.4.1.2435.3.3.9.1.6.1.0` for the
   QL-810W [B2]; a real QL-810W (firmware 1.22) answers it [P1]; the tested
   unit's USB frame for DK-22251 equals that published SNMP frame byte for
   byte.
   → Serve the live USB frame verbatim.
7. **Real printers differ from the manual** [B1 vs P1, P3, P4, tested
   unit]: byte 11 is `0x0A`/`0x0B` (the manual says `0x4A`/`0x4B`), byte 14
   is a per-roll code, byte 6 reads `0x04` (meaning unknown; not a cover
   flag), and byte 25 is `0x81` only for the black/red roll, the one place
   where two-colour media shows up.
   → Never normalise status bytes: P-touch requires exactly `0x0A`/`0x0B` and
   `0x81`.
8. **The reply to `ESC i S` is not always status type `0x00`.** With the
   cover open it is `0x02` (error occurred); a removed roll reads width 0 and
   type 0 without the "no media" bit, while byte 25 keeps the removed roll's
   value (tested unit only).
   → The first complete 32-byte frame after a request is its reply, whatever
   its type. "No media" is read from the width and type bytes, and byte 25
   is ignored without media.

### P-touch Editor for Mac

9. **Its whole network contract** [L1], confirmed by observation on the
   tested unit where marked:
   - discovery through Bonjour `_pdl-datastream._tcp` only, accepting a
     service only if TXT `usb_MDL` is exactly `QL-810W` and TXT `UUID` exists;
     the saved printer's model is derived again from the instance name
     `Brother QL-810W [...]` (observed: P-touch resolved
     `Brother QL-810W [<host>]` and sent no SNMP discovery);
   - printing to TCP port 9100 of the resolved host name, hard-coded
     (observed: yes);
   - SNMPv1 with the community `internal`; the string `public` does not exist
     in its SDK (observed: all 13 requests used `internal`);
   - one variable per request, 2 s per try, retries for up to 60 s; an
     error-status such as noSuchName fails at once; only INTEGER and OCTET
     STRING replies are accepted;
   - status and media only from the status OID; during a job, 3 requests then
     a 1 s pause, at most 180 rounds (observed: bursts of 3 each second);
   - roll matching on bytes 10 (width), 17 (length) and 11 (type), plus byte
     25 = `0x81` for the black/red paper;
   - "Connect to printer with an IP address" reads only the IEEE-1284 OID
     `1.3.6.1.4.1.2435.2.3.9.1.1.7.0` and needs `MDL:QL-810W` with no space.

   → An Avahi record with exactly those TXT keys; the communities `public` and
   `internal`; a status OID that the persona answers in every situation (a
   "turned off" block when the printer is unavailable, a synthesised printing
   phase while a job is in flight); the persona says `QL-810W` everywhere.
   One gap is snmpd's, not the persona's: while snmpd runs and the subagent is
   not registered (an agent restart, about 1 s, or a crash), snmpd answers
   noSuchName, so new settings reach the agent by reload in its running
   AgentX session, and a restart is kept for new code. After an snmpd restart
   requests are dropped until the agent registers again, and P-touch retries.
10. **P-touch 5.3 does not print through macOS print queues** [L1]; an older
    Brother FAQ describes adding a macOS CUPS queue instead [B5]. The observed
    session needed none.
    → The P-touch for Mac setup has no queue step.

### Identity

11. **The "c" variant reports the base model.** The tested QL-810Wc
    reports USB ID `04f9:209c`, product "QL-810W" and `MDL:QL-810W` (tested
    unit); Brother describes it as a derivative of the QL-810W with no
    protocol change [B3]; the QL-820NWBc behaves the same way [P3]; the Mac
    SDK has no "QL-810Wc" model at all [L1].
    → Present the box as `QL-810W`, never `QL-810Wc`.
12. **Identity strings of a real networked QL-810W are known only from third
    parties:** `sysDescr` names the NC-36002w network module [P2], and
    Brother's network printers use `sysObjectID` `1.3.6.1.4.1.2435.2.3.9.1`
    (seen on Brother lasers [O8]; unverified on a QL). PEN 2435 is Brother
    Industries [S5].
    → Served as constants marked `verified: false` until someone reads them
    from a real QL-810Wc on Wi-Fi (`dev/snmpwalk/`).

### SNMP agent

13. **Other clients read standard objects at index 1** (system group,
    `ifPhysAddress.1`, `hrDeviceDescr.1`, `hrPrinterStatus.1`, Printer-MIB):
    Brother's Windows driver installer sends one SNMPv1 broadcast request with
    five variables [P5], and in SNMPv1 one unknown variable fails the whole
    request [P1, P2]; CUPS's SNMP code reads Host Resources and Printer-MIB
    objects [O3]; the objects are defined in [S2, S3]. A stock net-snmp would
    answer them as the Linux host.
    → An SNMP context `printer` holding only the persona, served by our own
    AgentX subagent [S1]; the host's MIBs stay in the default context for
    SNMPv3.
14. **net-snmp mechanics** [O5, F3]: `com2sec -Cn` maps a community and
    source to a context, and the first matching line wins; USM users cannot be
    limited by source; a context is silently dropped until a subagent has
    registered in it; `pass_persist` children have no read timeout and serve
    only the default context; reloading (SIGHUP) mishandles `pass_persist` in
    5.9.5.2; `agentaddress` lines append and a second bind of UDP 161 makes
    snmpd exit; `agentXSocket` is last-wins; AgentX over TCP has no
    authentication; Fedora's stock configuration answers nothing to v1/v2c.
    → AgentX over the unix socket, pinned to its default path; restart snmpd,
    never reload it; `agentaddress` only for IPv6; source scoping by `com2sec`
    and the firewall; the installer refuses conflicting community mappings.

### Fedora 44 platform

15. **USB contention.** ipp-usb, a weak dependency of Fedora's cups package
    [F4], claims printer-class protocol-4 (IPP-over-USB) interfaces [O4]; on
    the QL-810Wc that is alternate setting 1 of the raster interface, so an
    ipp-usb claim unbinds usblp (tested unit). CUPS 2.4's libusb backend
    detaches usblp whenever it lists devices, and a usb-quirks `blacklist`
    entry makes it skip a device for listing and printing alike [O3]. usblp
    itself only refuses a second open of its node; it cannot stop another
    program from detaching it [O7].
    → `ql810wc-proxyd` is the only USB owner; an ipp-usb quirk and a CUPS
    usb-quirks entry keep the others away; the local CUPS queue prints to the
    proxy over `socket://127.0.0.1:9100`.
16. **SELinux and firewall.** Programs systemd starts from `bin_t` or `usr_t`
    run as `unconfined_service_t`, while `snmpd_t` children may not open USB
    devices or read generic `/run` files [F1]. Fedora Server's default zone
    allows only ssh, dhcpv6-client and cockpit, so mDNS is closed [F2], and
    `firewall-cmd --reload` drops runtime-only rules [O9].
    → No custom policy module (one file-context rule in `pass_persist` mode);
    rules applied at runtime and permanently without reload; UDP 5353 opened
    per source range, never zone-wide.
17. **Bonjour.** The TXT keys mirror Apple's printing specification and real
    Brother printers [S4, P6]; Avahi publishes static service files [O6], and
    browsing clients need inbound UDP 5353.
18. **Validation tool.** brother_ql_next 0.12.0 (GPL-3.0) supports the
    QL-810W and two-colour labels [O2] and printed on the tested unit; the
    original brother_ql 0.9.4 on PyPI fails with current Pillow [O1].
    → Optional, installed from PyPI into a separate venv, never vendored and
    never used by the service.

### Open questions

- The persona's identity strings and their types on a real QL-810Wc
  (`sysDescr`, `sysObjectID`, `ifDescr`, `brpsHardwareType`, serial-number
  objects), and whether its firmware answers `internal` for reads.
- The real printer's status block during a job, its TCP 9100 idle timeout and
  its own Bonjour TXT values (`pdl`, `usb_CMD`, `priority`).
- Windows clients: the driver installer's search, the port monitor's SNMP
  status, whether P-touch for Windows shows the roll on a TCP/IP port [P4].
- How iPrint&Label for iOS finds the box and reads the roll (both work), and
  which Bonjour service types or SNMP objects the iOS and Android apps use.
  A **Check Media** tap on 2026-11-21 sent no data to TCP 9100 (see the
  entry on the colour check), so the app presumably reads the persona's SNMP
  status block (not captured).
- What iPrint&Label's 402-byte TCP 9100 check, sent before it prints, holds.
  400 NULs and `ESC @` fit its size, but it was not captured. A print from
  the app shows it: `ql810wc-proxyd`'s journal tells:
  `connection from …: 402 bytes, no print job (NUL bytes …, set-up commands
  …, status requests …)` if it holds no print data (NULs, set-up commands,
  status requests, `ESC i U t`), with `; the last N bytes, an incomplete
  command, were not sent` if it ends inside a command; a `job from …: 402
  bytes …` line if it holds print data or a command the tokenizer does not
  know. Print data that shows no colour mode (no `g`/`w` row, `0x0C` or
  `0x1A`) in a stream the tokenizer can follow is then held back by the
  colour check (`RAW_COLOR_CHECK`) and never sent (`none of its print data
  was sent to the printer`); on 2026-11-21, before the check, all 402 bytes
  were forwarded.
- Whether the colour-mismatch error code sits in byte 7 or byte 31 on the
  QL-810W(c) (the Mac SDK reads byte 7; a QL-800 used byte 31) [L1, P4].

## Sources

Brother:

- **[B1]** Brother, *Software Developer's Manual: Raster Command Reference
  QL-800/810W/820NWB*, version 1.01.
  <https://download.brother.com/welcome/docp100278/cv_ql800_eng_raster_101.pdf>
- **[B2]** Brother Developer Center, Command Reference FAQ (status OID and
  models). <https://support.brother.com/g/s/es/dev/en/command/faq/index.html>
- **[B3]** Brother, insert D02JDX001A on the QL-810Wc/820NWBc/1110NWBc.
  <https://support.brother.com/g/b/files/dlf/docp100714/d02jdx001a_balder_insertion_ql_eu1.pdf>
- **[B4]** Brother FAQ faqp00100383_000 (QL-810W, black/red media installed).
  <https://support.brother.com/g/b/faqend.aspx?c=us&lang=en&prod=lpql810weus&faqid=faqp00100383_000>
- **[B5]** Brother FAQ faqp00001674_001 (QL-810W, printing from macOS).
  <https://support.brother.com/g/b/faqend.aspx?c=us&lang=en&prod=lpql810weus&faqid=faqp00001674_001>

Published hardware observations and prior art (read, not copied):

- **[P1]** mattj23/BrotherRaster (MIT): `docs/hardware.md` and SNMP status
  fixtures from a QL-810W, firmware 1.22. <https://github.com/mattj23/BrotherRaster>
- **[P2]** chiva/labelito: `docs/known-limitations.md`, `docs/snmp-status.md`.
  <https://github.com/chiva/labelito>
- **[P3]** thermal-label/brother-ql (MIT): USB status frames from a
  QL-820NWBc. <https://github.com/thermal-label/brother-ql>
- **[P4]** joffs/ql-unidrv (MIT): print-to-file output of Brother's Windows
  driver, status decoding, notes on Brother's Windows packages.
  <https://github.com/joffs/ql-unidrv>
- **[P5]** codyps/brother2, `PROTO`: the Brother driver installer's SNMP
  broadcast search. <https://github.com/codyps/brother2/blob/HEAD/PROTO>
- **[P6]** okms/brother-ql700-raw-bridge (MIT): an earlier raw-port bridge
  with a Bonjour record and an SNMP responder.
  <https://github.com/okms/brother-ql700-raw-bridge>

Software (source code read):

- **[O1]** pklaus/brother_ql, including issues with raw status frames.
  <https://github.com/pklaus/brother_ql>
- **[O2]** LunarEclipse363/brother_ql_next 0.12.0 (GPL-3.0).
  <https://github.com/LunarEclipse363/brother_ql_next>
- **[O3]** OpenPrinting CUPS 2.4.19: `backend/usb-libusb.c`,
  `backend/snmp.c`. <https://github.com/OpenPrinting/cups>
- **[O4]** OpenPrinting ipp-usb: udev rules and quirks.
  <https://github.com/OpenPrinting/ipp-usb>
- **[O5]** net-snmp 5.9.5.2: `snmpd.conf(5)` and the agent sources.
  <https://github.com/net-snmp/net-snmp>
- **[O6]** Avahi 0.9-rc2: `avahi.service(5)`. <https://github.com/avahi/avahi>
- **[O7]** Linux kernel, `drivers/usb/class/usblp.c`.
  <https://github.com/torvalds/linux/blob/master/drivers/usb/class/usblp.c>
- **[O8]** LibreNMS, Brother OS detection.
  <https://github.com/librenms/librenms/blob/HEAD/resources/definitions/os_detection/brother.yaml>
- **[O9]** firewalld 2.4.4. <https://github.com/firewalld/firewalld/tree/v2.4.4>

Fedora 44:

- **[F1]** fedora-selinux/selinux-policy, branch f44.
  <https://github.com/fedora-selinux/selinux-policy/tree/f44>
- **[F2]** Fedora firewalld package, `FedoraServer.xml` zone.
  <https://src.fedoraproject.org/rpms/firewalld/raw/f44/f/FedoraServer.xml>
- **[F3]** Fedora net-snmp package (default configuration, build options).
  <https://src.fedoraproject.org/rpms/net-snmp>
- **[F4]** Fedora cups package (dependencies). <https://src.fedoraproject.org/rpms/cups>

Standards:

- **[S1]** RFC 2741, Agent Extensibility (AgentX) Protocol.
  <https://www.rfc-editor.org/rfc/rfc2741.txt>
- **[S2]** RFC 3805, Printer MIB v2. <https://www.rfc-editor.org/rfc/rfc3805.txt>
- **[S3]** RFC 2790, Host Resources MIB. <https://www.rfc-editor.org/rfc/rfc2790.txt>
- **[S4]** Apple, Bonjour Printing Specification.
  <https://developer.apple.com/bonjour/printing-specification>
- **[S5]** IANA, Private Enterprise Numbers.
  <https://www.iana.org/assignments/enterprise-numbers.txt>

Local:

- **[L1]** Static analysis of P-touch Editor for Mac 5.3.23 (local,
  read-only): the Mac App Store build, its bundled MacPrintSDK framework and
  Brother's QL-810W macOS CUPS driver, inspected on a development Mac with
  standard binary tools. The analysis did not run the application. Nothing
  from the binaries is kept in this repository except the short facts above.
