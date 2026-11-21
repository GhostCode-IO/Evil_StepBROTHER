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
| Other Mac applications with Brother's QL-810W driver for macOS | partly verified (2026-11-21): macOS adds the box from Bonjour; the driver's black/red output printed correctly (checked by eye) and its monochrome output was refused, both sent from a Mac straight to TCP 9100; printing through a macOS print queue is not tested yet |
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
2. When you print, choose the label size: DK-22251 is the 62 mm continuous
   size, shown as `2.4"`. While DK-22251 is loaded, also set **Color** to
   **2-Color(Black/Red)**, even for black-only labels. The driver's default
   is **Monochrome**, which this roll cannot print: the box refuses such a
   job, so nothing prints, although macOS may show the job as completed
   (`qlctl status` on the box shows it as `refused`;
   [media rules](#media-rules)). A preset in the print dialog keeps these
   settings.

Tested on 2026-11-21, before the box had its colour check: macOS added the
box this way, and a job from PowerPoint reached the printer. It was
monochrome, so the printer, with DK-22251 loaded, rejected it and needed a
power cycle.

Tested on 2026-11-21 with the colour check, DK-22251 loaded: a label
rendered by this driver on a Mac (its own filter, run by hand at the 62 mm
size) was sent from the Mac to TCP 9100. In Monochrome the box refused it:
nothing printed, and the printer kept working. In 2-Color(Black/Red) it
printed in black and red where it should (checked by eye). Printing through
a macOS print queue, from PowerPoint for example, has not been tested with
the colour check yet.

## iOS: Brother iPrint&Label

Verified on the tested unit on 2026-11-21: iPrint&Label for iOS found the
box, read the loaded roll with **Check Media** and printed.

1. Put the iPhone or iPad on the same network as the box, inside the client
   ranges (`SNMP_PRINTER_ALLOWED_CIDRS`, and `MDNS_ALLOWED_CIDRS` when it is
   set).
2. Open iPrint&Label. If iOS asks whether the app may find and connect to
   devices on your local network, allow it. The setting is under Settings →
   Privacy & Security → Local Network.
3. Select the box in the app's printer selection.
4. Use the app's **Check Media**. The app starts out with a 1.4" label size,
   whatever roll is loaded, and reads the roll only when asked; after
   **Check Media** it shows the roll that is loaded. That is how the app
   works, not a limitation of the box.
5. Print. With DK-22251 loaded, follow the [media rules](#media-rules).

Before it prints, the app opens a short connection to TCP 9100 (402 bytes,
no label) and closes it; on 2026-11-21 it opened two before its print. What
it sends was not captured; 400 NULs and `ESC @` fit its size. If it holds
nothing but NULs,
set-up commands and status requests, the box forwards those bytes and does
not count them as a job, and the journal shows
`connection from …: 402 bytes, no print job (…)`. If the journal shows a
`job from …: 402 bytes …` line instead, the check holds more and the box
records it as a job (`qlctl status` and `selftest.sh` then show it as the
last job).

Later on 2026-11-21 another **Check Media** tap sent no data to TCP 9100:
`ql810wc-proxyd`'s journal had no entry after it (with the printer ready, a
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
- `qlctl test-print` needs continuous tape and refuses die-cut labels.
- After you swap rolls, the box reports the new roll within
  `STATUS_POLL_SECONDS`. Whether P-touch picks it up without re-selecting the
  printer has not been tested.

## Acceptance checklist

The box cannot see the printed label, so look at each one. After every print,
`journalctl -u ql810wc-proxyd -n 5` on the box shows a line such as
`job from <client>: 19000 bytes in 5.0 s, two-color, requested 62 mm
continuous, 4-byte trailer, result ok`.

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
| 13 | A Mac application through Brother's QL-810W driver, **Color** 2-Color(Black/Red), DK-22251 loaded | The label prints in black and red | not run through a print queue; the driver's own output, sent to TCP 9100 from a Mac, printed correctly, checked by eye (2026-11-21) |
| 14 | The same with **Color** left at Monochrome | Nothing prints; `qlctl status` shows the job as `refused`; the printer keeps working | not run through a print queue; the driver's own output, sent to TCP 9100 from a Mac, was refused and the printer kept working (2026-11-21) |

If a check fails, note what you saw (blank label, black only, garbled, an
error LED, the client's message), run `sudo ./selftest.sh` and `qlctl status`
on the box, and see [troubleshooting](troubleshooting.md).
