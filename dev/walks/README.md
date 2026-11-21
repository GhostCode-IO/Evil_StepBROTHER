# dev/walks/: raw SNMP walks (never committed)

Everything in this directory except this README is gitignored. The tools in
[`dev/snmpwalk/`](../snmpwalk/README.md) write their raw output here: SNMP
walks of a printer and the diff reports made from them. That output contains
serial numbers, MAC addresses and IP addresses. Only derived results are
committed: `agent/oid-map.yaml` and written analysis in `dev/notes/`, with
every quoted excerpt scrubbed first (`dev/snmpwalk/scrub.py`).
