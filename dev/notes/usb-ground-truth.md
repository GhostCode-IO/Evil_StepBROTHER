# USB ground truth from the tested QL-810Wc (2026-11-20)

Observed directly on the tested unit over USB, on the Fedora 44 box. These
are the first hardware-verified facts in this repo. Identifying values (serial)
are omitted.

## USB identity and descriptors

| Mode | VID:PID | Interfaces |
|---|---|---|
| Printer | `04f9:209c` ("Brother" / "QL-810W") | if0 alt0 = 7/1/2 bidirectional printer (EP `0x81` IN, `0x02` OUT); **if0 alt1 = 7/1/4 IPP-over-USB** (EP `0x01` OUT, `0x82` IN); if1 alt0 = 255/1/0 vendor (EP `0x83` IN); if1 alt1 = 7/1/4 IPP-over-USB (EP `0x03` OUT, `0x84` IN) |
| Editor Lite ON | `04f9:209f` ("QL-810W") | if0 = 8/6/80 USB mass storage (2 MB disk), no printer interface |

- The QL-810Wc reports the **same PID as the plain QL-810W** (`0x209c`).
- IPP-over-USB is an *alternate setting of interface 0*. When `ipp-usb`
  selects it, the kernel `usblp` driver is unbound, so raw Brother raster
  and IPP-over-USB cannot be used at the same time. The proxy therefore
  excludes the printer from ipp-usb (`blacklist = true` quirk).
- The USB serial string differs between the two modes. Treat it as opaque.

## IEEE-1284 device ID (usblp sysfs `ieee1284_id`)

```
MFG:Brother;CMD:PT-CBP,URF;MDL:QL-810W;CLS:PRINTER;CID:Brother LabelPrinter TypeE1;URF:V1.5,W8,CP1,IS1,MT1-5,OB10,PQ4-5,RS300,FN3-11-60-63;
```

`MDL:QL-810W` has no space after the colon and no "c". This is exactly what
P-touch Editor for Mac requires from the SNMP 1284 OID.

## 32-byte status block (reply to `ESC i S`, DK-22251 loaded, idle)

```
80 20 42 34 39 30 04 00 00 00 3e 0a 00 00 23 00 00 00 00 00 00 00 00 00 00 81 00 00 00 00 00 00
```

| Byte | Value | Meaning |
|---|---|---|
| 3 / 4 | `34` / `39` | series '4' / model '9' = QL-810W |
| 6 | `04` | undocumented. Same value as in the published QL-810W (fw 1.22) SNMP status fixtures and the QL-820NWBc status frames |
| 10 | `3e` | media width 62 mm |
| 11 | `0a` | continuous. Hardware reports `0x0A`, not the manual's `0x4A` |
| 14 | `23` | roll code of DK-22251 |
| 17 | `00` | length 0 (continuous) |
| 18 / 19 | `00` / `00` | reply to status request; phase "waiting to receive" |
| 25 | `81` | **two-color (black/red) media** (bit 7) |

This matches the public QL-810W SNMP fixture for DK-22251 byte for byte. It is
the value the proxy serves at `1.3.6.1.4.1.2435.3.3.9.1.6.1.0`.

## Media-swap readings (USB, same unit, same session)

| State | Status block |
|---|---|
| DK-22251, cover closed, idle | `80 20 42 34 39 30 04 00 00 00 3e 0a 00 00 23 00 00 00 00 00 00 00 00 00 00 81 00 00 00 00 00 00` |
| DK-22251, **cover open** | `80 20 42 34 39 30 04 00 00 10 3e 0a 00 00 23 00 00 00 02 00 00 00 00 00 00 81 00 00 00 00 00 00` |
| **DK-11201 29x90 die-cut**, closed | `80 20 42 34 39 30 04 00 00 00 1d 0b 00 00 01 00 00 5a 00 00 00 00 00 00 00 01 00 00 00 00 00 00` |

What this shows:
- **Cover open:** err2 bit `0x10` is set, and the reply to `ESC i S` has
  **status type `0x02` ("error occurred"), not `0x00`**. A status reader must
  accept error frames as replies. The media bytes keep describing the loaded
  roll while the cover is open.
- **Die-cut:** width 29 (`0x1d`), type `0x0b`, length 90 (`0x5a`), roll code
  `0x01`, byte 25 `0x01` (mono). This matches the published QL-820NWBc status
  frame for DK-11201 and the P-touch for Mac media table entry "29mm x 90mm".
  The tested die-cut roll is therefore **DK-11201 (29x90)**, brother_ql label
  `29x90`.
- No unsolicited frames were queued by opening or closing the cover. The
  printer only answered the request.

A second swap was polled every 3 s, so each transition was seen as it happened:

| Step | type | width | media type | byte 25 | err1/err2 |
|---|---|---|---|---|---|
| die-cut in, cover closed | `00` | 29 | `0b` | `01` | `00`/`00` |
| cover opened | `02` | 29 | `0b` | `01` | `00`/`10` |
| roll removed (cover open) | `02` | **0** | **`00`** | `01` | `00`/`10` |
| DK-22251 inserted (cover open) | `02` | 62 | `0a` | `81` | `00`/`10` |
| cover closed | `00` | 62 | `0a` | `81` | `00`/`00` |

- **No media** reads as width 0 and media type `00`. P-touch for Mac treats
  that as "No media is in the printer". **err1 bit `0x01` ("no media") was
  not set** while the cover was open. Byte 25 kept its previous value, so
  ignore byte 25 when the media type is `00`.
- The printer re-reads the roll while the cover is still open, so media
  bytes change before the cover closes.

## Bulk-IN zero-length packets (usblp readiness)

The printer answers every bulk-IN request on EP `0x81` with a zero-length
packet when it has nothing to send. The kernel's usblp driver therefore marks
`/dev/usb/lpN` readable again immediately, and every read returns 0 bytes.
`select`/`poll` readiness on the node carries no information.

Found on the box after the first deployment: `ql810wc-proxyd`, which waited on
the node with `select`, spun at about 100 % of one core on the Raspberry Pi 4
(about one CPU-second per second). The daemon now never selects on the usblp
node and reads it on a timer (every 0.25 s while idle, every 0.05 s during a
job), as it already did for the libusb transport. A regression test drives an
always-readable fake node: 632 878 reads in 1.5 s before the fix, a handful
after. The original fake printer had no such quirk, which is why only the
real hardware showed it.

## Auto power-off

The unit powered itself off (USB disconnect) 60 min 9 s after the last
status request. Its auto-power-off is therefore about **60 minutes**, and an
`ESC i S` restarts the timer.

Soak test: `ql810wc-proxyd` sent `ESC i S` every 3 s and no print job arrived.
The unit stayed on with no USB disconnect from its last job until the check
almost four hours later. The idle poll keeps it on.

## Printing

`brother_ql_next` 0.12.0 (Python 3.14, linux_kernel backend, `-m QL-810W`)
printed a black-only label and a black+red label on DK-22251 with
`-l 62red --red`. The printer reported "printing completed" for both. Its
status decoder cannot tell `62` from `62red`; byte 25 is the only signal.
