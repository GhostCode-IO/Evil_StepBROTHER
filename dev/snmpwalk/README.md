# dev/snmpwalk: read a QL-810W(c) on Wi-Fi

These tools are for contributors who have a Brother QL-810W or QL-810Wc and
can put it on a Wi-Fi network. A printer on the network answers SNMP itself.
Reading it in known states (different rolls, cover open, no roll) shows what
a real printer reports. That confirms or corrects what the box serves in the
printer's place: [`agent/oid-map.yaml`](../../agent/oid-map.yaml). Installing
or using the proxy never needs these tools.

| Tool | What it does |
|---|---|
| `walk.sh` | Reads the printer's Brother private MIB and MIB-2, read-only. |
| `media-swap.sh` | Walks the printer once per roll state and diffs the walks. |
| `diffwalks.py` | Lists the values that differ between two or more walks, byte by byte. |
| `scrub.py` | Replaces serial numbers, MAC addresses and IPv4 addresses in a walk with placeholders. |

All raw output goes to [`dev/walks/`](../walks/README.md), which git ignores:
a walk holds the printer's serial number, its MAC address and addresses of
your network. Share scrubbed values only (step 4).

## What is still unconfirmed

Every entry of `agent/oid-map.yaml` says `verified: true` or
`verified: false`, and its `notes` give the evidence. The status block
(`1.3.6.1.4.1.2435.3.3.9.1.6.1.0`), which carries the loaded roll, is
verified. Most identity values are not. `sysDescr`, `sysObjectID`,
`ifDescr.1`, `ifType.1` and `brpsHardwareType` come from other Brother
devices or from third-party reports, and the IEEE-1284 ID (`brieee1284id`) is
the printer's USB ID: nobody has read its network form yet.
`validated_firmware` is empty: nobody has checked the whole map against one
printer yet. The open questions in
[`dev/notes/README.md`](../notes/README.md) list more, for example whether
the printer answers reads with the community `internal`, which P-touch
Editor for Mac uses.

## 1. Prepare

You need:

- the printer on your Wi-Fi network, set up as Brother's user's guide
  describes, and its IP address, for example from your router's list of
  DHCP leases. Below, `<printer-ip>` stands for it;
- a computer on the same network with a clone of this repository, bash,
  perl, python3 (3.8 or later) and the net-snmp command-line tools
  (`snmpget`, `snmpwalk`, `snmpbulkwalk`). macOS has bash, perl and the
  net-snmp tools built in; git and python3 come with Apple's Command Line
  Tools. On Fedora:
  `sudo dnf install git-core perl-interpreter python3 net-snmp-utils`;
- the rolls you want to compare, for example DK-22251 (62 mm continuous,
  black/red) and a die-cut roll such as DK-11201 (29 x 90 mm).

If the printer normally hangs off a box that runs this project, unplug its
USB cable from the box for the session.

Check that the printer answers. This reads `sysDescr` and `sysObjectID`:

```bash
snmpget -v2c -c public -On <printer-ip> 1.3.6.1.2.1.1.1.0 1.3.6.1.2.1.1.2.0
```

So that no raw walk, private IPv4 address or MAC address reaches a commit,
enable the repository's pre-commit check once per clone:

```bash
git config core.hooksPath dev/hooks
```

## 2. Read the printer

```bash
dev/snmpwalk/walk.sh <printer-ip> <label> [community]
```

`<label>` names the state the printer is in, for example `dk22251`,
`diecut`, `cover-open` or `no-media` (letters, digits, `-` and `_`). The read
community is `public` unless you give another one. The script only reads:
GET, GETNEXT and GETBULK, never SET. It walks the Brother private MIB
(`1.3.6.1.4.1.2435`) and MIB-2 (`1.3.6.1.2.1`) and writes four files to
`dev/walks/`:

```text
<UTC time>_<label>.2435.hex.walk   strings as hex bytes: exact, for diffing
<UTC time>_<label>.2435.txt.walk   readable
<UTC time>_<label>.mib2.hex.walk
<UTC time>_<label>.mib2.txt.walk
```

When bulk requests fail it falls back to plain SNMPv2c walks, then to SNMPv1,
and to `-Cc` when the printer returns OIDs out of order. Each attempt stops
after 10 minutes (bulk) or 15 minutes (plain walks).

## 3. Compare roll states

```bash
dev/snmpwalk/media-swap.sh <printer-ip> [community]
```

The script asks you to set up five states, one after the other, and walks
the printer after each:

1. DK-22251 loaded, cover closed, printer idle;
2. a die-cut roll loaded, cover closed, printer idle;
3. any roll loaded, cover open;
4. no roll loaded, cover closed;
5. DK-22251 loaded again, to check that the values repeat.

Press Enter when the printer is ready, or type `skip`. The script waits 3 s
for the status to settle and walks. At the end it writes an N-way diff of
the Brother MIB walks to `dev/walks/<UTC time>_media-diff.txt`; it needs at
least two states for that. To compare other rolls, edit `STATES` in the
script.

To compare any walks by hand, the MIB-2 walks too:

```bash
dev/snmpwalk/diffwalks.py dev/walks/*_dk22251.2435.hex.walk dev/walks/*_diecut.2435.hex.walk
dev/snmpwalk/diffwalks.py --prefix 1.3.6.1.2.1.43 \
  dev/walks/*_dk22251.mib2.hex.walk dev/walks/*_diecut.mib2.hex.walk
```

The first walk is the baseline. Each OID whose value differs is printed with
a leading `*` and one line per walk. For values shown as hex bytes the
differing byte offsets follow, counted from 0, for example
`bytes: [11] 0a->0b`. Values that change on their own (uptimes, interface
and IP counters) are left out unless you pass `--all`. `--ignore REGEX`
leaves out more OIDs, `--show-same` lists the unchanged ones too, and
`--prefix` limits the output to one subtree (`1.3.6.1.2.1.43` is the
Printer-MIB).

What to expect in the status block, from the tested QL-810Wc over USB
([`dev/notes/usb-ground-truth.md`](../notes/usb-ground-truth.md)): byte 10
is the roll width in mm, byte 11 the media type (`0a` continuous, `0b`
die-cut), byte 14 a roll code, byte 17 the label length in mm (0 for a
continuous roll) and byte 25 `81` for a black/red roll, `01` otherwise. A
printer on Wi-Fi that reports something else is worth an issue.

## 4. Scrub before you share

Give `scrub.py` the printer's serial number, MAC address and IP address:

```bash
dev/snmpwalk/scrub.py dev/walks/<walk> --serial <serial> --mac <mac> --ip <printer-ip> \
  -o dev/walks/<name>.scrubbed.walk
```

The readable MIB-2 walk shows the serial number at `prtGeneralSerialNumber`
(`1.3.6.1.2.1.43.5.1.1.17.1`) and the MAC address at `ifPhysAddress.1`
(`1.3.6.1.2.1.2.2.1.6.1`), if the printer has them. `scrub.py` finds the MAC
and IP addresses in a MIB-2 walk by itself, and serial-like strings in a
readable Brother MIB walk, but nothing in a hex walk of the Brother MIB.

It replaces the values as text and as hex bytes, with placeholders of the
same byte length, so byte offsets stay valid:

- serial numbers with `X` characters;
- MAC addresses with `00:00:5E:00:53:NN` (RFC 7042 documentation range),
  also inside Brother node names (`BRW` or `BRN` followed by the MAC);
- IPv4 addresses with `192.0.2.N` (RFC 5737 documentation range), also
  inside OID indexes.

A last pass replaces any other private IPv4 address and any MAC address
written with `:` or `-`. The script exits with status 2 when a private IPv4
address is still left. Scrub every walk you want to quote, then diff the
scrubbed copies: the byte offsets do not move.

`scrub.py` does not change names you gave the printer (node name, location,
contact), and it does not recognise every form a value can take, for example
a MAC address split over two lines of hex. Read the result before you share
it.

## 5. Turn the results into the OID map

For each value you confirmed or corrected, edit its entry in
[`agent/oid-map.yaml`](../../agent/oid-map.yaml). The header of that file
documents the schema.

- Set `type` and `source` to what the printer returned. Values that differ
  from printer to printer (serial number, MAC address, location) stay
  providers or settings: never paste your printer's own values.
- Set `verified: true`.
- Say in `notes` what you read, from which model (QL-810W or QL-810Wc) and
  which firmware version.

Once you have checked every entry against one printer, add its firmware
version to `validated_firmware`. `tests/test_oidmap.py` pins the entries that
are still unconfirmed and expects `validated_firmware` to be empty: update it
in the same change. Then run the tests from the repository root:

```bash
python3 -m unittest discover -s tests -t .
```

Open a pull request with the change. If you would rather not edit the map,
open an [issue](https://github.com/GhostCode-IO/Evil_StepBROTHER/issues)
with the model, the firmware version, the roll and the scrubbed lines that
matter. Never attach raw walks.

## 6. Put the printer back

Turn the printer's Wi-Fi off before you plug it back into the box. Otherwise
clients find it twice: once through the box and once on its own Wi-Fi
interface. Then run `sudo ./selftest.sh` in the checkout on the box.

To compare what the box serves with what you read on Wi-Fi, load the same
roll and read the status block from a computer in the box's
`SNMP_PRINTER_ALLOWED_CIDRS`:

```bash
snmpget -v1 -c internal -On -Ox <box-address> 1.3.6.1.4.1.2435.3.3.9.1.6.1.0
```
