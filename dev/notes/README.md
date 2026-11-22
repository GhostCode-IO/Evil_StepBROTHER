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

## Verified on the tested unit (2026-11-20 to 2026-11-22)

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
  label. Its **Check Media** read the loaded roll (DK-22251) correctly
  (whether it was first used before or after that print is not known); the
  app starts out with a 1.4" label size and reads the roll only when asked.
  Up to that evening it opened four 402-byte connections to TCP 9100 that
  ended without `0x1A`: two just before its print, one about 3 minutes
  before the owner reported that **Check Media** worked, and one about 5
  minutes after the owner reported a power cycle of the printer. No print
  from the app followed the last two right away (its next print came that
  evening), and what made it open any of the four is not known (a later
  **Check Media** tap sent no data to TCP 9100, see the entry on the colour
  check). `ql810wc-proxyd`, which then counted set-up commands such as
  `ESC @` as job content, logged each as a job that ended early (`402 bytes
  in 0.2 s, result error`, a time that runs to the end of the box's
  resynchronisation of the printer, not how long the app kept the
  connection open) and resynchronised the printer, so what these four held
  was not counted. How the app found the box (Bonjour or SNMP) and where the
  app reads the roll were not checked; what two later 402-byte connections
  held was counted that evening (see the entry on the colour check).
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
  Not tested yet: a two-colour job on a single-colour roll. A 2-Color print
  through a macOS print queue was not tried that day (a Monochrome one from
  PowerPoint was refused that evening, see the entry on label sizes below);
  one printed in black and red on 2026-11-22 (see the 2026-11-22 entry).
  - Later the same day, an iPrint&Label **Check Media** tap left nothing in
    `ql810wc-proxyd`'s journal: no connection with data reached TCP 9100
    (one with only NULs and set-up commands would have been logged as `no
    print job`). The app reads the roll some other way, presumably the
    persona's SNMP status block (not captured). Of the app's four 402-byte
    connections up to then, two came just before its print, and no print
    from the app had followed the other two (see the iPrint&Label entry
    above).
  - A print from iPrint&Label was asked for after that. The label printed
    in answer (7 056 bytes, `two-color, requested 62 mm continuous, 4-byte
    trailer, result ok`) came from the Mac, which has no iPrint&Label
    (P-touch Editor was running on it). The app had sent nothing to TCP 9100
    since the morning, so what its 402-byte connections held was still not
    known.
  - That evening, with the box on 7879d24, a print from iPrint&Label on the
    iPhone: two connections, each logged as `connection from …: 402 bytes,
    no print job (NUL bytes 400, set-up commands 1, status requests 0)`, and
    right after them the print (12 582 bytes, `two-color, requested 62 mm
    continuous, 4-byte trailer, result ok`). 402 = 400 + 2, and `ESC @` is
    the only 2-byte set-up command (`ESC i a 01` and `ESC i !` take 4), so
    each held 400 NULs and one `ESC @`; the counts do not show their order.
    Neither was recorded as a job, and the printer was not resynchronised
    after them. The first case of `SetupConnectionTest` in
    `tests/test_proxyd.py` sends 400 NULs, then `ESC @`, and expects a line
    with the same counts.
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
- **2026-11-21, label sizes from Brother's driver for macOS, and the media
  check:** more local renders with the driver's own filter (filter 4.0.3,
  PPD 4.0.4), run as in the entry "Brother's driver for macOS on its own"
  above, with the Mac queue's PPD and a `DEVICE_URI` of that queue's form
  (`dnssd://…`). Nothing was sent to a printer.
  - On such a network queue every job's `ESC i z` had n1 `0x8E` (media
    type, width and length valid, printer recovery), or `0xCE` with
    **Quality** "Give priority to print quality" or "High resolution". Read
    from the filter's code, not rendered: on a USB queue it sends `0x00`
    (or `0x40`), with no check flags, and reads the printer's status itself.
  - The media fields come only from the job's `media` option, looked up in
    the filter's paper table. Without that option the filter uses the PPD's
    default paper, `DC03` (`1.1" x 3.5"`, 29 × 90 mm die-cut), whatever size
    the page has; on a paper name it does not know it crashed, with no
    output. Headers seen: `2.4"` `8E 0A 3E 00 57 04 00 00 00 00` (62 mm
    continuous, 1111 lines), `1.1" x 3.5"` `8E 0B 1D 5A DF 03 00 00 00 00`,
    `2.4" x 1.1"` `8E 0B 3E 1D 0F 01 00 00 00 00` and `1.1"`
    `8E 0A 1D 00 57 04 00 00 00 00`. `7" Postage Label`, `8" Postage Label`
    and `Binder 2" - Spine` ask for 62 mm continuous too, with more lines.
    So do `2.4" x2`, `x3` and `x4` (rendered later, during the review of
    the media check): a page two to four labels wide comes out as a job of
    two to four pages, each asking for 62 mm continuous tape, 1111 lines.
    Replayed through `ql810wc-proxyd` and the test suite's fake printer
    with DK-22251, all three went through. A multi-page job repeats the
    page header, with n9 `00` on page 1 and `01` after it. The driver
    refuses no combination of size and colour.
  - **Basic → Length and Margin.** The print dialog sends both (the queue's
    last-used settings hold them, and the second PowerPoint job below
    matches them). On continuous tape the label then has
    trunc(300 × (Length − 2 × Margin) / 25.4 + 3) lines, whatever the paper
    size: Length 69.1 mm and Margin 3 mm give 748 lines
    (`8E 0A 3E 00 EC 02 00 00 00 00`, `ESC i K 09` for 2-Color or `08` for
    Monochrome, `ESC i d 23 00`). With a Length shorter than the page, the
    top 35 rows (3 mm) of the page are dropped and the rest is cut off at
    the Length; with at least the page's length (100 mm for `2.4"`, 184 mm
    for `7" Postage Label`, 210 mm for `8" Postage Label` and
    `Binder 2" - Spine`), the whole page prints. The queue's last-used
    Length, 69.1 mm, cut a 62 × 100 mm page at about 69 mm. The box cannot
    see this: the job still asks for 62 mm continuous tape.
  - **2-Color with Quality "High resolution":** `ESC i K 49` (two-colour,
    cut at end, high resolution), `ESC i z CE 0A 3E 00 AF 08 00 00 00 00`
    and 600-dpi lines. Brother's user's guide says that high resolution
    cannot be used with two-colour printing, and lists a "Two-color media
    high resolution printing error" [B6].
  - The Mac's queue, read from the Mac's own CUPS with `ipptool`: its
    `printer-error-policy` is `stop-printer`, and it tells macOS that it is
    monochrome (`color-supported` false), because the PPD sets `ColorDevice`
    to `False`. The PPD's defaults are `DC03` and Monochrome.
  - **Paper names and units** (read later, during the review of the media
    check). The PPD Brother ships names its sizes in millimetres (`62 mm`,
    `29 mm x 90 mm`, `62 mm x 184 mm Postage Label`, `Binder 5 cm - Spine`
    and so on) and keeps the inch names as an alternative. The PPD of the
    Mac's queue had the inch names and the unit type `inch`, and Brother's
    Basic panel shows Length and Margin in either unit; the queue's
    last-used Length, 69.1 mm, matches the PPD's default of 69 mm after a
    round trip through 2.72 in. So the names and units apparently follow the
    Mac's measurement units when the printer was added (not tried on a Mac
    set to metric units).
  - **The owner's two PowerPoint prints** through that queue, DK-22251
    loaded, both asked for 62 mm continuous tape in Monochrome. The first
    (5149 bytes, the macOS entry above) reached the printer before the box
    had its colour check. The second, sent that evening with the colour
    check on, was refused as black-only, and none of it reached the printer.
    Its bytes were not captured. Its 4179 bytes are exactly the size of a
    blank page rendered locally with the dialog's last-used settings
    (Monochrome, Length 69.1 mm, Margin 3 mm: 748 blank rows), so it most
    likely held nothing in the part of the page the driver prints. So its
    size suited the roll, its colour mode did not, and its Length most
    likely would have cut the page at about 69 mm. Why its page would have
    been blank is not known.
  - **The media check** (`RAW_MEDIA_CHECK`, on by default), built from these
    findings and research item 6 below. It mirrors Brother's rule: it
    refuses a job whose `ESC i z` asks the printer to check a field that
    the loaded roll does not match (type, width, and on a die-cut roll the
    length within ±1 mm), and a two-colour job in high resolution on a
    black/red roll. It decides at the colour check's point, the job's first
    raster row, on the `ESC i z` in force there (the first page's), and
    refuses the same way: nothing of the job reaches the printer, the
    client gets a reset, and the job is recorded as `refused`. It lets
    through whatever it cannot judge: no recent status, no roll or an
    unknown roll, a printer fault, no `ESC i z` before the first row (for
    the size rules), and fields the job does not flag (with an INFO line).
    A status read earlier that shows no roll or a fault is read again before
    the job is judged: closing the roll cover sends no status, so a roll just
    loaded shows only at the next poll. Before either check refuses a job on
    an earlier status frame, it asks the printer once more, and the reply
    decides; when the checks
    already asked during the verdict (answered or not), no such confirm read
    follows, so a job never waits for two status requests. A status request
    of the checks that the printer takes only in part now tears the held
    job without sending it (before, the job was released right behind it).
    The job line now shows the header's line count, the length of the print
    and the n1 flags.
  - **Not verified on the tested unit:** that the printer rejects a flagged
    size mismatch at all, what it then reports and which of Brother's reset
    steps clears it, whether it checks fields a job does not flag, the
    die-cut length tolerance, and the high-resolution error. The live test
    of the box's check followed on 2026-11-22 (next entry): prints that
    should pass, then one refusal (a 2-Color job from PowerPoint at
    `1.1" x 3.5"`), all with the check on. It tested the box, not the
    printer, which never saw the refused job.
- **2026-11-22, prints through the macOS print queue, and the media check
  live:** the box on e6530b9, with both checks on (`RAW_COLOR_CHECK`,
  `RAW_MEDIA_CHECK`) and DK-22251 loaded. Times are the box journal's, and
  `<client>` stands for the client's address. In order:
  - 10:30:35, P-touch Editor for Mac, from the Mac: `job from <client>:
    10523 bytes in 3.6 s, two-color, requested 62 mm continuous, 255 lines
    (21.6 mm of print), flags 0x86, 4-byte trailer, result ok`. The flags
    are the `0x86` of the packet capture (research item 6), now seen in a
    job line.
  - 10:34:56, PowerPoint through the Mac's print queue (Brother's QL-810W
    driver), Paper Size `2.4"`, 2-Color(Black/Red), Length 100 mm (entered
    as 3.94 in, which is 100.08 mm: the dialog shows inches on this Mac):
    `job from <client>: 12549 bytes in 6.6 s, two-color, requested 62 mm
    continuous, 1114 lines (94.3 mm of print), flags 0x8e, result ok`. The
    owner checked the label by eye: red printed red. So a 2-Color print
    through the queue comes out black and red, although the queue tells
    macOS that the printer is monochrome (the PPD's `ColorDevice` is
    `False`).
  - 10:35:38, PowerPoint again, with the owner's saved preset (paper `2.4"`,
    2-Color, Quality "Give priority to print speed", Margin 3 mm), which had
    kept Length 69.1 mm (the queue's last-used Length in the entry above;
    the Mac's preset file holds `BrTapeLength` "69.1"):
    `job from <client>: 9497 bytes in 5.2 s, two-color, requested 62 mm
    continuous, 748 lines (63.3 mm of print), flags 0x8e, result ok`. The
    label came out about 69 mm long, the slide cut short. The box cannot see
    this, and the docs expected it. A preset keeps the Length it had when it
    was saved.
  - 10:37:49, iPrint&Label for iOS, from the iPhone, after **Check Media**:
    two `connection from <client>: 402 bytes, no print job (NUL bytes 400,
    set-up commands 1, status requests 0)` lines, then at 10:37:53 `job
    from <client>: 12778 bytes in 3.6 s, two-color, requested 62 mm
    continuous, 273 lines (23.1 mm of print), flags 0x86, 4-byte trailer,
    result ok`. So the app's job asks the printer to check the media type
    and width, and a print at the app's 1.4" (38 mm) start size would be
    refused on DK-22251 by the width check. Not tried, and the flags of a
    1.4" job were not seen: the `0x86` is from a 62 mm job.
  - 10:38:40, PowerPoint with Paper Size `1.1" x 3.5"` (29 × 90 mm die-cut,
    the driver's default), 2-Color: `refused a print job from <client>: a
    job for 29x90 mm die-cut labels, but the loaded roll is 62mm continuous
    black/red (DK-22251). None of its print data was sent to the printer`,
    and `job from <client>: 10911 bytes in 0.1 s, two-color, requested
    29x90 mm die-cut, 991 lines (83.9 mm of print), flags 0x8e, result
    refused (…)`, with the same reason in the parentheses. The media check
    alone refused it: the job was two-colour, so the colour check passed
    it. The Mac's CUPS listed the job as completed, the queue was not
    paused, and the printer stayed ready (`qlctl status`: no errors) and
    printed the next job.
  - 10:45:06, PowerPoint, Length 100 mm again: `job from <client>: 13157
    bytes in 6.5 s, two-color, requested 62 mm continuous, 1114 lines
    (94.3 mm of print), flags 0x8e, result ok`.
  - On the Mac, read-only: the queue was idle and enabled after the
    refusal; `lpstat -W completed` listed the PowerPoint jobs, the refused
    one included, as completed; and the print dialog's last-used settings
    name the panels used as "PowerPoint, Printer Options": on this macOS,
    Brother's panels sit in the dialog's **Printer Options**.
  - **Verified on the tested unit by these:** the media check refuses a
    real client's mismatched job before any of it reaches the printer, and
    the printer keeps working; a 2-Color print from an application through
    the macOS queue prints black and red; the driver's Length decides the
    label length through the queue too (1114 lines at Length 100 mm, 748 at
    69.1 mm), as the local renders predicted; and the flags of live jobs are
    `0x86` from P-touch Editor for Mac, `0x8e` from Brother's driver for
    macOS and `0x86` from iPrint&Label for iOS.
  - **Still not verified:** what the printer itself does with a mismatched
    job ("replace media", the reported stop, Brother's reset steps), the
    high-resolution rule, iPrint&Label at its 1.4" start size, custom paper
    sizes, the optional `lpadmin` defaults of the Mac's queue, and a queue
    paused by a larger refused job.

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
6. **The printer checks a job's media against the loaded roll, as far as
   the job asks.** A job's `ESC i z` names the media it is for (type `0x0A`
   continuous or `0x0B` die-cut, width and length in mm) and flags in n1
   which of them are valid (`0x02`, `0x04`, `0x08`). When a flagged field
   does not match the loaded roll, the printer reports error information 2
   bit 0, "replace media" [B1]. Brother's user's guide calls it an
   "Incorrect media error", says that high resolution cannot be used with
   two-colour printing ("Two-color media high resolution printing error"),
   and resets an error in this order: open and close the roll cover, then
   press the Cutter button, then switch the printer off and on [B6]. After
   such a mismatch a QL-800 ignored status requests and `ESC @` until its
   roll cover was opened and closed [P4]; a QL-810W on Wi-Fi kept every
   later job until its cover was cycled or it was switched off and on, and
   then printed them [P2]. The flags clients send: P-touch Editor for Mac
   `0x86` (type and width; seen in a packet capture of the session that
   [ptouch-mac-session.md](ptouch-mac-session.md) describes, and in
   P-touch's job line on 2026-11-22; the capture itself stays outside the
   repository), Brother's driver for macOS on a network queue `0x8E` (local
   renders, and live through the Mac's queue on 2026-11-22), iPrint&Label
   for iOS `0x86` (a 62 mm job, 2026-11-22), Brother's Windows driver
   `0x86` for continuous tape and `0x8E` for die-cut labels [P4],
   brother_ql `0xCE` [O1], and `qlctl test-print` `0x86`. The printer's
   side of these rules is not tested on the tested unit; the box's check
   refused a mismatched job there on 2026-11-22.
   → `ql810wc-proxyd` refuses such a job before its print data reaches the
   printer (`RAW_MEDIA_CHECK`, on by default), with the colour check's hold
   and refusal: only the fields the job flags, only on a roll it knows,
   never while the printer reports a fault (it holds jobs then [P2], and
   presumably checks the roll it has when it resumes), the die-cut length
   within ±1 mm (byte 17 is rounded) and no length on continuous tape. It
   also refuses a two-colour job in high resolution on a black/red roll. A
   status read earlier that shows no roll or a fault is read again before
   the job is judged: closing the roll cover sends no status, so a roll just
   loaded shows only at the next poll. Before either check refuses a job on an earlier status frame, it
   asks the printer once more; a job never waits for two status requests.
   What the header does not show (a label cut short by the driver's Length,
   the layout, a blank page) it cannot catch.

### Status block

7. **The SNMP status OID returns the same 32 bytes as the USB `ESC i S`
   reply.** Brother documents `1.3.6.1.4.1.2435.3.3.9.1.6.1.0` for the
   QL-810W [B2]; a real QL-810W (firmware 1.22) answers it [P1]; the tested
   unit's USB frame for DK-22251 equals that published SNMP frame byte for
   byte.
   → Serve the live USB frame verbatim.
8. **Real printers differ from the manual** [B1 vs P1, P3, P4, tested
   unit]: byte 11 is `0x0A`/`0x0B` (the manual says `0x4A`/`0x4B`), byte 14
   is a per-roll code, byte 6 reads `0x04` (meaning unknown; not a cover
   flag), and byte 25 is `0x81` only for the black/red roll, the one place
   where two-colour media shows up.
   → Never normalise status bytes: P-touch requires exactly `0x0A`/`0x0B` and
   `0x81`.
9. **The reply to `ESC i S` is not always status type `0x00`.** With the
   cover open it is `0x02` (error occurred); a removed roll reads width 0 and
   type 0 without the "no media" bit, while byte 25 keeps the removed roll's
   value (tested unit only).
   → The first complete 32-byte frame after a request is its reply, whatever
   its type. "No media" is read from the width and type bytes, and byte 25
   is ignored without media.

### P-touch Editor for Mac

10. **Its whole network contract** [L1], confirmed by observation on the
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
11. **P-touch 5.3 does not print through macOS print queues** [L1]; an older
    Brother FAQ describes adding a macOS CUPS queue instead [B5]. The observed
    session needed none.
    → The P-touch for Mac setup has no queue step.

### Identity

12. **The "c" variant reports the base model.** The tested QL-810Wc
    reports USB ID `04f9:209c`, product "QL-810W" and `MDL:QL-810W` (tested
    unit); Brother describes it as a derivative of the QL-810W with no
    protocol change [B3]; the QL-820NWBc behaves the same way [P3]; the Mac
    SDK has no "QL-810Wc" model at all [L1].
    → Present the box as `QL-810W`, never `QL-810Wc`.
13. **Identity strings of a real networked QL-810W are known only from third
    parties:** `sysDescr` names the NC-36002w network module [P2], and
    Brother's network printers use `sysObjectID` `1.3.6.1.4.1.2435.2.3.9.1`
    (seen on Brother lasers [O8]; unverified on a QL). PEN 2435 is Brother
    Industries [S5].
    → Served as constants marked `verified: false` until someone reads them
    from a real QL-810Wc on Wi-Fi (`dev/snmpwalk/`).

### SNMP agent

14. **Other clients read standard objects at index 1** (system group,
    `ifPhysAddress.1`, `hrDeviceDescr.1`, `hrPrinterStatus.1`, Printer-MIB):
    Brother's Windows driver installer sends one SNMPv1 broadcast request with
    five variables [P5], and in SNMPv1 one unknown variable fails the whole
    request [P1, P2]; CUPS's SNMP code reads Host Resources and Printer-MIB
    objects [O3]; the objects are defined in [S2, S3]. A stock net-snmp would
    answer them as the Linux host.
    → An SNMP context `printer` holding only the persona, served by our own
    AgentX subagent [S1]; the host's MIBs stay in the default context for
    SNMPv3.
15. **net-snmp mechanics** [O5, F3]: `com2sec -Cn` maps a community and
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

16. **USB contention.** ipp-usb, a weak dependency of Fedora's cups package
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
17. **SELinux and firewall.** Programs systemd starts from `bin_t` or `usr_t`
    run as `unconfined_service_t`, while `snmpd_t` children may not open USB
    devices or read generic `/run` files [F1]. Fedora Server's default zone
    allows only ssh, dhcpv6-client and cockpit, so mDNS is closed [F2], and
    `firewall-cmd --reload` drops runtime-only rules [O9].
    → No custom policy module (one file-context rule in `pass_persist` mode);
    rules applied at runtime and permanently without reload; UDP 5353 opened
    per source range, never zone-wide.
18. **Bonjour.** The TXT keys mirror Apple's printing specification and real
    Brother printers [S4, P6]; Avahi publishes static service files [O6], and
    browsing clients need inbound UDP 5353.
19. **Validation tool.** brother_ql_next 0.12.0 (GPL-3.0) supports the
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
- What makes iPrint&Label open a 402-byte TCP 9100 connection that no print
  follows right away, and whether all its 402-byte connections hold the same
  bytes. On 2026-11-21 it opened six. An older `ql810wc-proxyd`, which did
  not count what they held, logged the first four: two just before a print
  and two that no print followed right away. The four the box has counted,
  two just before its second print on 2026-11-21 (see the entry on the
  colour check) and two just before its print on 2026-11-22, each held 400
  NULs and `ESC @`.
- Whether the colour-mismatch error code sits in byte 7 or byte 31 on the
  QL-810W(c) (the Mac SDK reads byte 7; a QL-800 used byte 31) [L1, P4].
- The media rules on the QL-810Wc (research item 6): whether it rejects a
  flagged size mismatch, what it reports then and which of Brother's reset
  steps clears it, whether it checks fields a job does not flag, its
  die-cut length tolerance, and how it reports a two-colour job in high
  resolution. The box's media check keeps such jobs from the printer, so
  testing them needs `RAW_MEDIA_CHECK="no"`. Also the flags of an
  iPrint&Label job at the app's 1.4" start size (its 62 mm job had
  `0x86`).
- Brother's driver for macOS through the Mac's queue: custom paper sizes,
  the optional `lpadmin` defaults, and whether a larger refused job pauses
  the queue (its `printer-error-policy` is `stop-printer`; a refused job of
  about 11 KB did not).
- Answered on 2026-11-22 (see that entry): which flags a 62 mm iPrint&Label
  job carries (`0x86`, the type and width), and whether a 2-Color print
  through a macOS print queue comes out red (it does).

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
- **[B6]** Brother, *User's Guide QL-810W/QL-820NWB*, version 06: two-colour
  printing (p.103), the LED indications with "Incorrect media error" and
  "Two-color media high resolution printing error", and resetting an error
  (p.182).
  <https://download.brother.com/welcome/docp100287/ql810w_ql820nwb_use_ug_06.pdf>

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
