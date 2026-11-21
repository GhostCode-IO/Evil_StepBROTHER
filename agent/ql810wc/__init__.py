"""ql810wc: the Python package behind the ql810wc-print-proxy daemons.

Modules (imported explicitly by their users; nothing is loaded eagerly):

* ``rasterproto`` - Brother QL raster protocol: the 32-byte status block, a
  length-driven job tokenizer and a stdlib-only test-label generator.
* ``media``       - the DK media table and the P-touch media-match rule.
* ``statecache``  - atomic writer / never-raising reader for ``state.json``.
* ``config``      - the safe ``KEY=value`` parser shared with the shell
  installer, runtime defaults and typed getters.
* ``usbio``       - printer discovery (sysfs) and the usblp / pyusb transports.
* ``proxyd``      - ql810wc-proxyd, the data plane (TCP 9100 to USB).
* ``persona``     - the SNMP printer persona (oid-map.yaml + state.json).
* ``agentx`` / ``passpersist`` - how the persona is served to snmpd.

Runtime dependencies: the Python standard library (3.9+). Reading
``oid-map.yaml`` needs PyYAML (``python3-pyyaml``; ``persona`` falls back to an
``oid-map.json`` beside it), and ``usbio``'s libusb fallback needs
``python3-pyusb``.
"""

__version__ = "0.1.0"
