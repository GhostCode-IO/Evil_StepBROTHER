# P-touch Editor for Mac against the proxy: observed session (2026-11-20)

First end-to-end run: P-touch Editor 5.3.23 (macOS) against the tested unit,
with the QL-810Wc on USB behind `ql810wc-proxyd` and the SNMP persona.
P-touch **found the box via Bonjour, showed the installed tape (62 mm
black/red, DK-22251), and printed a correct two-color label** (checked by
eye). That is T0-T3 for the Mac client.

Below is what P-touch was observed doing during that session. Host names
and addresses are placeholders.

## Discovery

- P-touch resolved `Brother QL-810W [<host>]._pdl-datastream._tcp` over
  mDNS.
- It never asked for the IEEE-1284 OID. That OID is used only by
  "Connect to printer with an IP address".

## SNMP (status / installed media)

- **13 SNMP GET requests, every one with community `internal`, every one
  for `1.3.6.1.4.1.2435.3.3.9.1.6.1.0`** (the 32-byte status block). P-touch
  read no other OID.
- Cadence: one request, then **bursts of 3** about once a second, the three
  requests of a burst within about 15 ms of each other. This matches the
  3-call loop with a 1 s sleep found by static analysis.

## Data (TCP 9100)

- One connection per print. The client half-closed the connection 4.10 s
  after opening it, and the proxy closed its side 0.93 s later, once the
  printer had finished. This delivery acknowledgement matches the design.
- proxyd's job log: `19000 bytes in 5.0 s, two-color, requested 62 mm
  continuous, 4-byte trailer, result ok`. The **4-byte trailer after `0x1A`**
  is consistent with `1B 69 61 FF` (the 6-byte `ESC i U t` part depends on an
  SDK flag and was not sent here). proxyd held it until the printer was back
  to "receiving", then forwarded it. P-touch reported success.
- P-touch's job is compressed (19 000 bytes for a 62 mm label, versus
  112 037 bytes for our uncompressed test job).
- The connection used IPv4. P-touch connects to the Bonjour host name, so on
  a network where the box also has a routable IPv6 address it may pick IPv6.
  Whether it does is not known, so the docs still advise listing IPv6 client
  ranges in `RAW_ALLOWED_CIDRS`.

## Consequences

- The persona's design is right for this client: Bonjour + community
  `internal` + the status block is the whole contract.
- Answering the 3-GET bursts from an in-memory cache is enough: all 13
  requests were answered.

## Not covered by this session

- "Connect to printer with an IP address" (the IEEE-1284 OID path).
- A roll swap while P-touch is open, die-cut DK-11201, multi-copy jobs and
  two clients at once.
- The printer switched off or in an error state while P-touch polls.

Later sessions on the tested unit printed black and two-colour labels from
P-touch for Mac correctly, also after the resync and delivery-bound changes
to `ql810wc-proxyd`.
