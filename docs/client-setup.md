# Client setup and acceptance test

How to use the print server from P-touch Editor, Brother iPrint&Label and
other Mac applications, which media rules to follow, and the checklist that
shows the setup works.
Below, `<box-ip>` is the print server's IPv4 address (`PROXY_IP`), and
`<host>` is `PRINTER_NAME_SUFFIX` (setup.sh's `--name`) or, when that is
empty, the box's short host name.

| Client | State |
|---|---|
| P-touch Editor 5.3 for Mac | verified with 5.3.23: discovery, installed roll, black and two-colour prints |
| Brother iPrint&Label for iOS | verified (2026-11-21): finds the box, reads the loaded roll with **Check Media**, prints |
| Other Mac applications with Brother's QL-810W driver for macOS | partly verified (2026-11-21): macOS adds the box from Bonjour; the driver's black/red output printed correctly (checked by eye) and its monochrome output was refused, both sent from a Mac straight to TCP 9100; a monochrome job from PowerPoint through a macOS print queue was refused too; a black/red print through a print queue is not tested yet |
| P-touch Editor for Windows with Brother's QL-810W(c) driver | untested |
| Brother iPrint&Label for Android | untested |

## Before you start

- On the server, `sudo ./setup.sh` (or `sudo ./install.sh`) has completed
  with exit status 0, and `sudo ./selftest.sh`, run from the checkout
  (`/usr/local/src/ql810wc-print-proxy` after the one-liner) with DK-22251
  loaded, ends with `TIER REACHED: T3`. T0 is enough to print; T2 means
  clients can find the box, and T3 that P-touch sees the roll.
  `sudo ./selftest.sh --print-test` also prints a black/red test label and
  asks you to check it.
- The QL-810Wc is on USB and powered on, with **Editor Lite off** (green LED
  dark). For the tests below a **DK-22251** roll (62 mm continuous,
  black/red) is loaded.
- The client's address is inside `SNMP_PRINTER_ALLOWED_CIDRS`: P-touch reads
  the printer's status and roll over SNMP (UDP 161). setup.sh sets it to the
  box's own IPv4 network unless you pass `--clients`. Bonjour (UDP 5353) is
  allowed from `MDNS_ALLOWED_CIDRS`, which defaults to the same ranges.
- The client reaches TCP 9100: open to the whole firewalld zone by default,
  or only to `RAW_ALLOWED_CIDRS`. From macOS or Linux, test with
  `nc -vz <box-ip> 9100`; from Windows PowerShell with
  `Test-NetConnection <box-ip> -Port 9100`.

## macOS: P-touch Editor 5.3

**No macOS printer queue and no Brother macOS driver are needed.** P-touch
Editor 5.3 does not print through macOS print queues. Its built-in Brother SDK
finds the printer through Bonjour, reads its status over SNMP and sends the
job straight to TCP 9100.

1. Open P-touch Editor and its printer selection. The box appears as
   **`Brother QL-810W [<host>]`**, without a "c": P-touch knows this model
   only as QL-810W, and accepts the Bonjour record because its `usb_MDL` is
   exactly `QL-810W` and it carries a `UUID`.
2. Select it. P-touch reads the loaded roll: with DK-22251 it shows 62 mm
   black/red and lays out in black and red.
3. Print.

What P-touch was observed doing on the tested unit
([dev/notes/ptouch-mac-session.md](../dev/notes/ptouch-mac-session.md)): it
reads only the status block, over SNMPv1 with the community `internal`, in
bursts of three requests once a second while a job runs. It opens one TCP
connection per print, sends the job plus a 4-byte trailer and closes its side;
the box closes once the label has printed.

If the printer does not appear:

- On macOS 15 and later, P-touch Editor needs **Local Network** access
  (System Settings → Privacy & Security → Local Network).
- Check that the Mac's address is inside `MDNS_ALLOWED_CIDRS` (or
  `SNMP_PRINTER_ALLOWED_CIDRS` while that is empty) and that the record is
  visible: `dns-sd -B _pdl-datastream._tcp local.` in Terminal. More checks:
  [troubleshooting](troubleshooting.md#bonjour-checks).

**Without Bonjour** (`ENABLE_MDNS="no"`, or another network segment), use
P-touch's **"Connect to printer with an IP address"** and enter `<box-ip>`.
P-touch then makes one SNMPv1 request (community `internal`, 10 s timeout) for
the IEEE-1284 device ID and needs `MDL:QL-810W` in it, which the box serves.
The Mac must be inside `SNMP_PRINTER_ALLOWED_CIDRS`. This path has not been
tested yet.

## macOS: other applications (Brother's driver)

Applications other than P-touch Editor (PowerPoint, Word, a browser) print
through a macOS printer. That needs Brother's **QL-810W printer driver for
macOS**, from Brother's support site.

1. System Settings → Printers & Scanners → **Add Printer**. The box is
   listed as **`Brother QL-810W [<host>]`** (its Bonjour record). Add it
   with the driver **Brother QL-810W CUPS**; select the driver yourself if
   macOS does not. Without Bonjour, add the box by IP address instead:
   `<box-ip>`, protocol **HP Jetdirect – Socket**, same driver (not tested).
2. Lay out the page at the size of the label, then print with the settings
   for the loaded roll. The driver's defaults do not suit DK-22251: its
   paper size is `1.1" x 3.5"` (29 × 90 mm die-cut labels) and its colour
   is Monochrome, and the box refuses either while DK-22251 is loaded. The
   job then does not print, although macOS may show it as completed
   (`qlctl status` on the box shows it as `refused`, with the reason;
   [media rules](#media-rules)).

### Printing from PowerPoint on DK-22251

PowerPoint is the example; other applications have the same settings in
their page setup and print dialogs. DK-22251 is 62 mm continuous tape,
black/red.

Brother's driver names its paper sizes, and shows Length and Margin, in
inches or in millimetres, apparently after the Mac's measurement units when
the printer was added. This page uses the inch names. In millimetres,
`2.4"` is `62 mm` (and `2.4" x2` is `62 mm x2`, and so on), `1.1" x 3.5"`
is `29 mm x 90 mm`, `7" Postage Label` is `62 mm x 184 mm Postage Label`,
`8" Postage Label` is `62 mm x 209 mm Postage Label` and
`Binder 2" - Spine` is `Binder 5 cm - Spine`. In inches, enter a Length of
3.94 for 100 mm, 7.25 for 184 mm and 8.27 for 210 mm (rounded up).

1. **The slide:** File → Page Setup, 62 × 100 mm (2.44 × 3.94 in),
   portrait. That is the size of the driver's `2.4"` paper. The driver
   cannot print within about 3 mm of the top and bottom edges and 1.5 mm of
   the sides, so keep the content inside that.
2. **File → Print**, with the box's printer selected:
   - **Paper Size: `2.4"`** (62 × 100 mm). `7" Postage Label`,
     `8" Postage Label` and `Binder 2" - Spine` are longer sizes on the same
     tape. `2.4" x2`, `x3` and `x4` (a wider page split into two to four
     labels) ask for 62 mm continuous tape too (local render; not printed).
     A size for die-cut labels or another width, such as the driver's
     default `1.1" x 3.5"`, asks for other media, and the box refuses it on
     this roll.
   - Brother's **Advanced** panel → **Color: 2-Color(Black/Red)**, even for
     black-only labels.
   - Brother's **Basic** panel → **Length**: at least the paper's length,
     so 100 mm for `2.4"`, 184 mm for `7" Postage Label`, and 210 mm for
     `8" Postage Label` and `Binder 2" - Spine`. On continuous tape the
     driver makes the label as long as Length, whatever the paper size. If
     Length is shorter than the page, the driver leaves out the top 3 mm of
     the page and cuts off the rest of the page at Length. The box cannot
     see this (the job still asks for 62 mm continuous tape), so such a
     label prints, cut short.
   - **Basic → Margin:** its smallest value (3 mm, or 0.11").
   - **Basic → Quality:** **Give priority to print speed** or **Give
     priority to print quality**, never **High resolution**. Brother's guide
     says that high resolution cannot be used with two-colour printing, and
     the box refuses a two-colour job in high resolution on DK-22251.
3. **Presets → Save Current Settings as Preset…**, and choose that preset
   whenever you print on this roll. Otherwise the dialog may start from
   Brother's defaults or from the settings used last.

Optional: make `2.4"` and 2-Color the defaults of the Mac's queue, in
Terminal on the Mac (it asks for an administrator password). `<queue>` is
the Mac's name for the printer: `lpstat -v | grep -i 'QL-810W'` shows it
between `device for` and the colon.

```bash
sudo lpadmin -p <queue> -o PageSize=62mm -o BrMultiColor=BrMultiColorBlackRed
# to undo:
sudo lpadmin -p <queue> -o PageSize=DC03 -o BrMultiColor=BrMultiColorMonochrome
```

A job that names no paper size then asks for 62 mm continuous tape instead
of 29 × 90 mm die-cut labels, but a job that names no colour stays
Monochrome, because the driver takes the colour only from the job's own
options (both seen in local renders of the driver, not yet through the
queue). The colour default can only change what the print dialog starts
with, and that has not been checked yet, so keep choosing 2-Color in the
dialog or the preset. Adding the printer again, or a driver update, undoes
these defaults. Avoid custom paper sizes for now: they are untested through
the queue, and Brother's filter crashes on a paper name it does not know.

When the box refuses a job, nothing prints. macOS shows a small job as
completed, because the reset arrives after its last write. A larger job can
pause the queue instead, as the queue macOS creates for the box stops on an
error (its `printer-error-policy` is `stop-printer`): delete the job in the
queue's window, then resume the queue. To have such jobs dropped instead,
run `sudo lpadmin -p <queue> -o printer-error-policy=abort-job`.

The queue tells macOS that the printer is monochrome (the driver's PPD sets
`ColorDevice` to `False`). Whether a 2-Color print from an application
through the queue comes out red has not been checked yet.

Tested on 2026-11-21, before the box had its colour check: macOS added the
box this way, and a job from PowerPoint reached the printer. It was
monochrome, so the printer, with DK-22251 loaded, rejected it and needed a
power cycle.

Tested on 2026-11-21 with the colour check, DK-22251 loaded: a label
rendered by this driver on a Mac (its own filter, run by hand at the 62 mm
size) was sent from the Mac to TCP 9100. In Monochrome the box refused it:
nothing printed, and the printer kept working. In 2-Color(Black/Red) it
printed in black and red where it should (checked by eye). A second
monochrome job from PowerPoint, through the macOS print queue, was refused
too. Not tested yet: a 2-Color print through the queue, and a refusal by
the media check. The paper size, Length, Margin and Quality settings above
come from local renders of the driver, not from prints.

## iOS: Brother iPrint&Label

Verified on the tested unit on 2026-11-21: iPrint&Label for iOS finds the
box, reads the loaded roll with **Check Media** and prints.

1. Put the iPhone or iPad on the same network as the box, inside the client
   ranges (`SNMP_PRINTER_ALLOWED_CIDRS`, and `MDNS_ALLOWED_CIDRS` when it is
   set).
2. Open iPrint&Label. If iOS asks whether the app may find and connect to
   devices on your local network, allow it. The setting is under Settings →
   Privacy & Security → Local Network.
3. Select the box in the app's printer selection.
4. Use the app's **Check Media** before you print. The app starts out with
   a 1.4" label size (38 mm continuous tape), whatever roll is loaded, and
   reads the roll only when asked; after **Check Media** it shows the roll
   that is loaded. That is how the app works, not a limitation of the box.
   A print at 1.4" (38 mm) does not suit DK-22251. The box refuses it if
   the app's job asks the printer to check the width; whether it does is
   not known yet (the job line shows the flags as `flags 0x…`, and `0x04`
   is the width). So use **Check Media** before you print.
5. Print. With DK-22251 loaded, follow the [media rules](#media-rules).

The app also opens connections to TCP 9100 that print nothing, 402 bytes
each. On 2026-11-21 it opened six: two just before each of its two prints,
and, between those prints, two that no print followed right away (what made
it open those two is not known). The box logged the two before its second
print (the first four came before it counted what such connections hold) as
`connection from …: 402 bytes, no print job (NUL bytes 400, set-up commands 1, status requests 0)`:
400 NULs and an `ESC @`. It forwards such bytes, does not count them as a
job and does not resynchronise the printer after them.

One **Check Media** tap on 2026-11-21 sent no data to TCP 9100:
`ql810wc-proxyd`'s journal had no entry for it (with the printer ready, a
connection that sends no data is logged only with `LOG_LEVEL="DEBUG"`). The
app reads the roll some other way, presumably the box's SNMP status, which
carries the loaded roll (not captured).

Not checked yet: which of the box's answers (Bonjour or SNMP) the app uses
to find the box and to read the roll. If the app does not find the box, the
checks for Macs apply as well:
[Bonjour](troubleshooting.md#bonjour-checks) and
[firewall](troubleshooting.md#firewall-checks).

## Windows: P-touch Editor and Brother's driver (untested)

1. Install Brother's **QL-810W(c) printer driver** for your Windows version
   from Brother's support site. Skip its network search: the installer looks
   for printers with an SNMP broadcast, and the box's identity strings are
   still unverified, so it may not recognise the box.
2. **Settings → Bluetooth & devices → Printers & scanners → Add device →
   "The printer that I want isn't listed" → "Add a printer using an IP address
   or hostname"**:
   - Device type **TCP/IP Device**, hostname or IP address `<box-ip>`.
   - Untick "Query the printer and automatically select the driver".
   - When asked for more port information, choose **Custom → Settings**:
     protocol **Raw**, port **9100**.
   - "SNMP Status Enabled" may stay on with community name **`public`** and
     SNMP device index **1**: the box answers the Host Resources and
     Printer-MIB status of device 1 to `public`. If Windows then shows the
     printer as offline, untick it and report what you saw.
   - Driver: **Brother QL-810W(c)**.
3. In the driver's **Printing Preferences**, choose 62 mm paper and turn on
   **2-Color (Black/Red)** for DK-22251 (see [Media rules](#media-rules)).
4. In P-touch Editor, select this printer and the 62 mm black/red media.

A third-party analysis of Brother's Windows packages found that Brother's
status monitor does not use Standard TCP/IP ports, so P-touch for Windows may
not show the loaded roll even with a real networked QL-810W. Whether it does
with the box is part of the checklist below.

## Media rules

### Every roll: print for the roll that is loaded

- Each job names the media it was made for: continuous tape or die-cut
  labels, the width and, for labels, their length (its `ESC i z` command).
  It also says which of these the printer is to check. Brother documents
  that the printer rejects a job whose checked media does not match the
  loaded roll, with the error "replace media". Owners of the QL-800 and
  QL-810W report that the printer then stops until someone clears it at
  the printer. That has not been tried on the tested unit.
- The box therefore refuses such a job (`RAW_MEDIA_CHECK`, on by default),
  as it refuses a colour mode the roll cannot print: none of the job's print
  data reaches the printer, nothing prints, the client's connection is
  reset, and `qlctl status` shows the job as `refused`, with the reason,
  for example `a job for 29x90 mm die-cut labels, but the loaded roll is
  62mm continuous black/red (DK-22251)`.
- It also refuses a two-colour job in high resolution on DK-22251: Brother's
  guide says that high resolution cannot be used with two-colour printing,
  and lists an error for it.
- So choose the paper size of the loaded roll in the application or the
  driver. P-touch Editor takes it from the roll by itself, and iPrint&Label
  after its **Check Media**.
- The box sees only what the job asks for, not what it prints. On
  continuous tape, a label that the driver cuts too short (its **Length**
  setting), a layout that does not fit the label and a blank label all
  print as sent.
- A job whose media the box cannot judge goes through: when it has no
  recent status from the printer, no roll is loaded, the roll is unknown,
  or the printer reports a fault. A status read earlier that shows no
  roll or a fault is read again before the job is judged: closing the roll
  cover sends no status, so a roll just loaded shows only at the next poll. Third-party reports say
  that the printer holds jobs through a fault; the box assumes that it
  checks the roll it has when it goes on (not checked), so only the colour
  rule is judged then. A job that differs from the roll only in what it
  does not ask the printer to check goes through too, with a note in the
  journal.
- If the printer reports "replace media" anyway (with `RAW_MEDIA_CHECK="no"`,
  or when the box could not judge the job), clear it the way Brother's
  guide says: open and close the roll cover; if the error stays, press the
  Cutter button; if it still stays, switch the printer off and on.

### DK-22251 (62 mm continuous, black/red): always two-colour mode

- Print in **2-colour (black/red) mode, even for labels that are black
  only**. A mono job on DK-22251 makes the printer signal an error that only
  a power cycle clears. On the tested unit a monochrome job from a Mac
  application did exactly that (2026-11-21): the printer answered no status
  requests until it was switched off and on. Third parties report the same
  for the QL-810W, and a Brother FAQ describes P-touch refusing such a job
  because black/red media is installed.
- The box therefore refuses a mono job while this roll is loaded
  (`RAW_COLOR_CHECK`, on by default). None of the job's print data reaches
  the printer: nothing prints, the client's connection is reset, and
  `qlctl status` shows the job as `refused`. The application is not told
  why and may report no error at all: macOS shows such a job as completed
  when the reset arrives after its last write. The box cannot turn the job
  into a two-colour one (it forwards jobs unchanged), so print it again in
  two-colour mode.
- P-touch Editor for Mac selects the black/red mode by itself when it sees
  this roll (status byte 25 = `0x81`). With Brother's driver for macOS, set
  **Color** to **2-Color(Black/Red)**; on Windows, turn on 2-Color in the
  driver.
- Paper sizes for this roll in Brother's driver for macOS: `2.4"`,
  `2.4" x2`, `x3` and `x4`, `7" Postage Label`, `8" Postage Label` and
  `Binder 2" - Spine`, all 62 mm continuous. A size for die-cut labels or
  another width, such as the driver's default `1.1" x 3.5"`, is refused.
  Leave the driver's **Quality** at a "Give priority to …" setting: a
  two-colour job in **High resolution** is refused too. The driver may
  name these sizes in millimetres, such as `62 mm` for `2.4"`
  ([Printing from PowerPoint](#printing-from-powerpoint-on-dk-22251)).
- If the printer shows the error anyway (with `RAW_COLOR_CHECK="no"`, or
  when the box could not tell the job's colour mode or the loaded roll),
  switch it off and on.
  `ql810wc-proxyd` finds it again within a few seconds.
- Server-side test labels, with `qlctl` standing for
  `python3 -I -B /usr/local/libexec/ql810wc-proxy/qlctl`: `qlctl test-print`
  (two-colour bars) or `qlctl test-print --two-color --pattern black` (black
  only, printed as a two-colour job). `qlctl test-print --mono` is refused
  while DK-22251 is loaded.

### DK-11201 (29 mm × 90 mm die-cut): black only

- The printer reports this roll as 29 mm wide, die-cut, 90 mm long, single
  colour (verified over USB on the tested unit). P-touch's matching paper is
  "29mm x 90mm"; the brother_ql label id is `29x90`.
- Print black only. The roll is not black/red: Brother's SDK has an error
  for a two-colour job on such a roll ("paper does not support two-color
  printing"), and third-party drivers report it from other QL models. Not yet
  reproduced on the tested unit. The box refuses a two-colour job while a
  single-colour roll is loaded (`RAW_COLOR_CHECK`), so such a job normally
  never reaches the printer.
- Print at this roll's size: `1.1" x 3.5"` (or `29 mm x 90 mm`) in
  Brother's driver for macOS. The box refuses a job for other media while
  this roll is loaded (`RAW_MEDIA_CHECK`), for example one for 62 mm
  continuous tape.
- `qlctl test-print` needs continuous tape and refuses die-cut labels.
- After you swap rolls, the box reports the new roll within
  `STATUS_POLL_SECONDS`. Whether P-touch picks it up without re-selecting the
  printer has not been tested.

## Acceptance checklist

The box cannot see the printed label, so look at each one. After every print,
`journalctl -u ql810wc-proxyd -n 5` on the box shows a line such as
`job from <client>: 19000 bytes in 5.0 s, two-color, requested 62 mm
continuous, 693 lines (58.7 mm of print), flags 0x86, 4-byte trailer, result
ok`. The media part (`requested …`, the length of the print and the flags)
comes from the job's `ESC i z` command; the flags say which media fields
the printer is to check: `0x02` the type, `0x04` the width and `0x08` the
length. P-touch Editor for Mac sends `0x86`; Brother's driver for macOS
sends `0x8e`, or `0xce` with some **Quality** settings.

| # | Check | Pass when | Tested unit |
|---|---|---|---|
| 1 | P-touch (Mac) lists `Brother QL-810W [<host>]` | It appears without typing an address | passed (2026-11-20) |
| 2 | With DK-22251 loaded, P-touch shows the roll | 62 mm black/red is shown unprompted | passed (2026-11-20) |
| 3 | Black text plus a thin border, two-colour mode | Sharp black print, nothing cut off, clean cut | passed (black labels from P-touch for Mac, and black-only test labels sent to TCP 9100) |
| 4 | Same label with red text and a red shape | Red prints red, black stays black, both line up | passed (two-colour labels from P-touch for Mac, and test labels) |
| 5 | Five copies of #4 in one job | All five print in order, without stalls | not run |
| 6 | Print from two clients at nearly the same time | Both jobs print completely, one after the other | not run |
| 7 | Swap to DK-11201, then print a black label | P-touch shows "29mm x 90mm"; the label prints | not run (roll status verified over USB) |
| 8 | Switch the printer off, then on again | P-touch reports it as switched off; after power-on, `qlctl status` is back to `ready` within seconds | not run |
| 9 | "Connect to printer with an IP address" (Mac) | P-touch finds the printer at `<box-ip>` | not run |
| 10 | Checks 3 to 6 from Windows | As above | not run |
| 11 | iPrint&Label (iOS) finds the box, then prints a label | The app finds the box; the label prints | passed (2026-11-21) |
| 12 | iPrint&Label (iOS) with DK-22251 loaded: **Check Media** | The app shows the loaded roll | passed (2026-11-21) |
| 13 | A Mac application through Brother's QL-810W driver, DK-22251 loaded, with the [PowerPoint settings](#printing-from-powerpoint-on-dk-22251): paper `2.4"`, **Color** 2-Color(Black/Red), Length 100 mm | The whole page prints in black and red, on a label about 100 mm long | not run through a print queue; the driver's own output, sent to TCP 9100 from a Mac, printed correctly, checked by eye (2026-11-21) |
| 14 | The same with **Color** left at Monochrome | Nothing prints; `qlctl status` shows the job as `refused`; the printer keeps working | refused through a print queue, from PowerPoint (2026-11-21); the driver's own output, sent to TCP 9100 from a Mac, was refused and the printer kept working (2026-11-21) |
| 15 | The same as #13 with paper `1.1" x 3.5"`, the driver's default | Nothing prints; `qlctl status` shows the job as `refused` with `a job for 29x90 mm die-cut labels, but the loaded roll is 62mm continuous black/red (DK-22251)`; the printer keeps working | not run yet: the pending live test of the media check |

If a check fails, note what you saw (blank label, black only, garbled, an
error LED, the client's message), run `sudo ./selftest.sh` and `qlctl status`
on the box, and see [troubleshooting](troubleshooting.md).
