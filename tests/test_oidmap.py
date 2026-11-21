"""Tests for the shipped OID map (agent/oid-map.yaml) and the oid-map schema (ql810wc.persona).

* ShippedMapTest: the shipped asset loads and gives every client what it needs.
  That covers P-touch for Mac's OIDs and types, and Brother's discovery bundles,
  which are never absent. It also checks that the map is honest about what is
  verified, matches the VACM views of scripts/30-snmp-agent.sh, documents its
  vocabulary, holds no site data, and that its JSON fallback serves the same
  persona. Needs PyYAML in this interpreter.
* SchemaTest: each rule of the schema (docs/architecture.md section 7) rejects
  a broken document with a message that names the entry, and every problem is
  reported at once.
* LoadTest: .yaml/.yml/.json files, the oid-map.json fallback without PyYAML,
  and unreadable, oversized, invalid or unsafe files.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import pathlib
import re
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
AGENT_DIR = REPO / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from ql810wc import config, persona  # noqa: E402
from ql810wc.agentx import parse_oid  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the interpreter
    yaml = None

SHIPPED_MAP = AGENT_DIR / "oid-map.yaml"
SNMP_SCRIPT = REPO / "scripts" / "30-snmp-agent.sh"

STATUS = "1.3.6.1.4.1.2435.3.3.9.1.6.1.0"
IEEE1284 = "1.3.6.1.4.1.2435.2.3.9.1.1.7.0"
HR_PRINTER_STATUS = "1.3.6.1.2.1.25.3.5.1.1.1"
#: Brother Windows driver installer: one SNMPv1 broadcast GET of these 5 varbinds.
WIN_INSTALLER_BUNDLE = ("1.3.6.1.4.1.2435.2.3.9.1.1.7.0", "1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.2.2.1.6.1",
                        "1.3.6.1.4.1.2435.2.4.3.1240.1.3.0", "1.3.6.1.2.1.1.1.0")
#: Brother mobile SDK / iPrint&Label discovery: one GET of these 6 varbinds.
MOBILE_BUNDLE = ("1.3.6.1.2.1.1.6.0", "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.0", "1.3.6.1.2.1.1.5.0",
                 "1.3.6.1.2.1.2.2.1.6.1", "1.3.6.1.2.1.25.3.2.1.3.1", "1.3.6.1.4.1.2435.2.3.9.1.1.7.0")
#: Providers that can have nothing to say: no USB identity seen yet, no MAC found.
MAY_BE_EMPTY = frozenset({"ieee1284_id", "serial", "mac"})
#: Entries whose value nobody has read from a QL-810W(c) yet: they must not claim to be verified.
UNCONFIRMED = ("sysDescr", "sysObjectID", "sysServices", "ifDescr.1", "ifType.1", "brieee1284id",
               "brpsHardwareType", "brPhoenixSupport", "brUnknown46", "prtInputDimUnit.1.1")
#: The same patterns as dev/hooks/check-hygiene.sh: private IPv4 and MAC addresses.
PRIVATE_IP = re.compile(r"(^|[^0-9.])(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|"
                        r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})([^0-9.]|$)")
MAC_ADDRESS = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
DOC_MAC = re.compile(r"00[:-]00[:-]5[Ee][:-]00[:-]53[:-][0-9A-Fa-f]{2}")


def ptouch_model(device_id: str) -> str:
    """P-touch for Mac's model parse: fields split on ';' then ':', exactly two parts, key MDL, untrimmed."""
    for part in device_id.split(";"):
        pieces = part.split(":")
        if len(pieces) == 2 and pieces[0] == "MDL":
            return pieces[1]
    return ""


def load_shipped() -> persona.OidMap:
    if yaml is None:
        raise unittest.SkipTest("PyYAML is not installed in this interpreter")
    return persona.load_oid_map(SHIPPED_MAP)


def view_arrays() -> Dict[str, set]:
    """VIEW_AGENTX / VIEW_PASS_PERSIST of scripts/30-snmp-agent.sh as sets of OID tuples."""
    text = SNMP_SCRIPT.read_text(encoding="utf-8")
    arrays = {}
    for name in ("VIEW_AGENTX", "VIEW_PASS_PERSIST"):
        match = re.search(rf"(?m)^readonly {name}=\(([^)]*)\)", text)
        if match is None:
            raise AssertionError(f"{SNMP_SCRIPT.name} no longer defines {name}: keep it in sync with "
                                 f"persona.AGENTX_SUBTREES / PASS_PERSIST_SUBTREES")
        arrays[name] = {parse_oid(oid) for oid in match.group(1).split()}
    return arrays


class ShippedMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.map = load_shipped()
        cls.by_name = {e.name: e for e in cls.map.entries}
        cls.text = SHIPPED_MAP.read_text(encoding="utf-8")

    def entry(self, oid: str) -> persona.Entry:
        found = self.map.get(oid)
        self.assertIsNotNone(found, f"{oid} is missing from the shipped map")
        return found

    def test_header(self):
        self.assertEqual((self.map.schema_version, self.map.model), (1, "QL-810W"))
        self.assertEqual(self.map.validated_firmware, (), "nothing has been confirmed on a real QL-810W(c) yet")
        self.assertGreaterEqual(len(self.map.entries), 30)

    def test_what_ptouch_for_mac_reads(self):
        """The status block, the 1284 ID and hrPrinterStatus: INTEGER or OCTET STRING only."""
        for oid in (STATUS, IEEE1284, HR_PRINTER_STATUS):
            with self.subTest(oid=oid):
                entry = self.entry(oid)
                self.assertIn("ptouch-mac", entry.clients)
                self.assertIn(entry.type, persona.PTOUCH_MAC_TYPES)
        status = self.entry(STATUS)
        self.assertEqual((status.type, status.source.kind, status.source.arg), ("octets", "provider", "status_block"))
        self.assertTrue(status.verified, "the status block is hardware-verified on the tested unit")
        ieee = self.entry(IEEE1284)
        self.assertEqual((ieee.source.kind, ieee.source.arg), ("provider", "ieee1284_id"))
        self.assertEqual(ptouch_model(ieee.source.default), "QL-810W")
        for entry in self.map.entries:
            if "ptouch-mac" in entry.clients:
                self.assertIn(entry.type, persona.PTOUCH_MAC_TYPES, entry.name)

    def test_discovery_bundles_are_never_absent(self):
        """One missing OID fails a whole SNMPv1 multi-varbind GET."""
        for bundle in (WIN_INSTALLER_BUNDLE, MOBILE_BUNDLE):
            for oid in bundle:
                with self.subTest(oid=oid):
                    source = self.entry(oid).source
                    if source.kind == "provider" and source.arg in MAY_BE_EMPTY:
                        self.assertIsNotNone(source.default, f"{oid} needs a default")

    def test_providers_that_may_be_empty(self):
        """MAY_BE_EMPTY (above) is exactly the set of providers that can return nothing."""
        tmp = tempfile.mkdtemp(prefix="qlmap-")
        self.addCleanup(shutil.rmtree, tmp, True)

        class NoNet:
            @staticmethod
            def mac():
                return None

        bare = persona.Persona(self.map, config.load_runtime_config(None), os.path.join(tmp, "state.json"),
                               netinfo=NoNet())
        logging.getLogger("ql810wc.persona").disabled = True   # the missing-MAC warning is expected
        self.addCleanup(setattr, logging.getLogger("ql810wc.persona"), "disabled", False)
        view = bare.view()   # no state.json: printer unknown
        for name, provider in bare._providers.items():
            with self.subTest(provider=name):
                self.assertEqual(provider(view) is None, name in MAY_BE_EMPTY)

    def test_status_bytes_of_the_persona(self):
        """Only bytes 18-21 are ever synthesised; the map documents it on the status entry."""
        notes = self.entry(STATUS).notes
        self.assertIn("bytes 18-21", notes)
        self.assertIn("never the media bytes", notes)
        self.assertEqual(persona.SYNTH_BYTES, (18, 19, 20, 21))

    def test_honest_verified_flags(self):
        for name in UNCONFIRMED:
            with self.subTest(name=name):
                self.assertIn(name, self.by_name)
                self.assertFalse(self.by_name[name].verified, f"{name}: nobody has read it from a QL-810W(c) yet")
        for name in ("brStatusBlock", "sysName", "hrDeviceDescr.1", "hrPrinterStatus.1",
                     "hrPrinterDetectedErrorState.1", "prtInputMediaName.1.1"):
            with self.subTest(name=name):
                self.assertTrue(self.by_name[name].verified, f"{name} was read from a real QL-810W")
        for entry in self.map.entries:
            self.assertGreater(len(entry.notes), 20, f"{entry.name}: notes must give the evidence")

    def test_subtrees_match_the_snmpd_views(self):
        if not SNMP_SCRIPT.exists():
            self.skipTest(f"{SNMP_SCRIPT} is not in this checkout")
        views = view_arrays()
        self.assertEqual(views["VIEW_AGENTX"], set(persona.AGENTX_SUBTREES))
        self.assertEqual(views["VIEW_PASS_PERSIST"], set(persona.PASS_PERSIST_SUBTREES))
        served = self.map.within(persona.PASS_PERSIST_SUBTREES)
        self.assertIn(STATUS, {e.dotted for e in served})
        self.assertIn(IEEE1284, {e.dotted for e in served})
        self.assertTrue(all(any(persona.within(e.oid, s) for s in persona.AGENTX_SUBTREES)
                            for e in self.map.entries))

    def test_vocabulary_is_documented_in_the_header(self):
        header = self.text.split("\nschema_version:", 1)[0]
        for name in list(persona.PROVIDERS) + list(persona.TYPES) + list(persona.CLIENTS):
            with self.subTest(name=name):
                self.assertTrue(re.search(rf"(?m)^#\s+{re.escape(name)}\s", header),
                                f"{name!r} is not documented in the oid-map.yaml header")

    def test_no_site_data(self):
        self.assertIsNone(PRIVATE_IP.search(self.text))
        for match in MAC_ADDRESS.finditer(self.text):
            self.assertRegex(match.group(0), DOC_MAC, "only RFC 7042 documentation MACs")

    def test_json_fallback_serves_the_same_persona(self):
        tmp = tempfile.mkdtemp(prefix="qlmap-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "oid-map.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(yaml.safe_load(self.text), fh)
        from_json = persona.load_oid_map(path)
        self.assertEqual((from_json.schema_version, from_json.model, from_json.validated_firmware, from_json.entries),
                         (self.map.schema_version, self.map.model, self.map.validated_firmware, self.map.entries))


# ---------------------------------------------------------------------------
# Schema rules
# ---------------------------------------------------------------------------
def entry(**overrides: Any) -> Dict[str, Any]:
    base = {"oid": "1.3.6.1.4.1.2435.99.1.0", "name": "testEntry", "type": "integer", "source": {"const": 1},
            "context": "printer", "verified": False, "clients": ["nms"], "notes": "test entry"}
    base.update(overrides)
    return base


def document(*entries: Any, **top: Any) -> Dict[str, Any]:
    doc = {"schema_version": 1, "model": "QL-810W", "validated_firmware": [],
           "entries": list(entries) if entries else [entry()]}
    doc.update(top)
    return doc


class SchemaTest(unittest.TestCase):
    def assertRejected(self, doc: Any, *fragments: str) -> str:
        with self.assertRaises(persona.OidMapError) as caught:
            persona.OidMap.from_dict(doc, path="test-map")
        message = str(caught.exception)
        self.assertTrue(message.startswith("test-map"), message)
        for fragment in fragments:
            self.assertIn(fragment, message)
        return message

    def test_a_valid_map(self):
        doc = document(
            entry(oid="1.3.6.1.4.1.2435.99.2.0", name="second", type="octets", source={"const": "80 20:42"}),
            entry(oid="1.3.6.1.4.1.2435.99.10.0", name="tenth", type="oid", source={"const": ".1.3.6.1.4.1.2435"}),
            entry(oid="1.3.6.1.2.1.1.6.0", name="location", type="string", source={"config": "PERSONA_LOCATION"},
                  clients=["mobile-sdk"], verified=True, notes="  padded  "),
            entry(oid="1.3.6.1.4.1.2435.99.3.0", name="serial", type="string",
                  source={"provider": "serial", "default": ""}),
            entry(oid="1.3.6.1.4.1.2435.99.4.0", name="label", type="string", source={"template": "$$ ${MODEL}"}),
        )
        oid_map = persona.OidMap.from_dict(doc, path="test-map")
        self.assertEqual([e.name for e in oid_map.entries], ["location", "second", "serial", "label", "tenth"])
        self.assertEqual(oid_map.get("1.3.6.1.4.1.2435.99.2.0").source.arg, b"\x80\x20\x42")
        self.assertEqual(oid_map.get(".1.3.6.1.4.1.2435.99.10.0").source.arg, (1, 3, 6, 1, 4, 1, 2435))
        self.assertIsNone(oid_map.get("1.3.6.1.4.1.2435.99.5.0"))
        location = oid_map.get("1.3.6.1.2.1.1.6.0")
        self.assertEqual((location.verified, location.clients, location.notes), (True, ("mobile-sdk",), "padded"))
        self.assertEqual(oid_map.get("1.3.6.1.4.1.2435.99.3.0").source, persona.Source("provider", "serial", ""))
        self.assertEqual([e.name for e in oid_map.within(["1.3.6.1.2.1"])], ["location"])
        self.assertEqual(oid_map.path, "test-map")

    def test_document_level_rules(self):
        self.assertRejected([], "must be a mapping")
        self.assertRejected(document(extra=1), "unknown top-level key 'extra'")
        for bad in (2, "1", True, None):
            with self.subTest(schema_version=bad):
                self.assertRejected(document(schema_version=bad), "schema_version must be 1")
        self.assertRejected(document(model="QL-810Wc"), "model must be one of")
        for bad in ("2.19", [""], [2.19]):
            with self.subTest(validated_firmware=bad):
                self.assertRejected(document(validated_firmware=bad), "validated_firmware must be a list")
        for bad in ([], "entries", None):
            with self.subTest(entries=bad):
                self.assertRejected(dict(document(), entries=bad), "entries must be a non-empty list")

    def test_entry_rules(self):
        cases = [
            ("x", "entry 1: must be a mapping"),
            (entry(colour="red"), "entry 1 (testEntry): unknown key 'colour'"),
            (entry(oid=1.3), "oid must be a quoted dotted string"),
            (entry(oid="1.3.x"), "invalid OID"),
            (entry(oid="1"), "is too short"),
            (entry(name="bad name"), "name must be"),
            (entry(type="counter64"), "type must be one of"),
            (entry(context=""), "context must be 'printer'"),
            (entry(verified="yes"), "verified must be true or false"),
            (entry(clients="nms"), "clients must be a list"),
            (entry(clients=["bradmin", "snmpwalk"]), "unknown client 'snmpwalk'"),
            (entry(clients=["nms", "nms"]), "lists a client twice"),
            (entry(type="timeticks", source={"const": 5}, clients=["ptouch-mac"]), "accepts only"),
            (entry(notes=" "), "notes must say"),
            (dict((k, v) for k, v in entry().items() if k != "notes"), "notes must say"),
        ]
        for raw, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertRejected(document(raw), fragment)

    def test_source_rules(self):
        cases = [
            ({"type": "integer", "source": "const"}, "source must be a mapping"),
            ({"type": "integer", "source": {}}, "exactly one of"),
            ({"type": "string", "source": {"const": "x", "config": "PERSONA_LOCATION"}}, "exactly one of"),
            ({"type": "integer", "source": {"const": 1, "fallback": 2}}, "unknown source key(s) 'fallback'"),
            ({"type": "integer", "source": {"const": 1, "default": 2}}, "only meaningful with a provider"),
            ({"type": "integer", "source": {"const": 2 ** 31}}, "32-bit signed"),
            ({"type": "integer", "source": {"const": True}}, "32-bit signed"),
            ({"type": "octets", "source": {"const": "zz"}}, "not hex"),
            ({"type": "octets", "source": {"const": 5}}, "hex text"),
            ({"type": "string", "source": {"const": "x" * 256}}, "longer than 255 bytes"),
            ({"type": "oid", "source": {"const": "1"}}, "at least two sub-identifiers"),
            ({"type": "oid", "source": {"const": "1.3.x"}}, "invalid OID"),
            ({"type": "ipaddress", "source": {"const": "192.0.2.300"}}, "invalid IpAddress"),
            ({"type": "timeticks", "source": {"const": -1}}, "must be a number 0.."),
            ({"type": "string", "source": {"config": "NOT_A_SETTING"}}, "must name a runtime setting"),
            ({"type": "oid", "source": {"config": "PERSONA_LOCATION"}}, "cannot produce type 'oid'"),
            ({"type": "string", "source": {"template": "${NOT_A_SETTING}"}}, "neither a runtime setting"),
            ({"type": "string", "source": {"template": "costs $5"}}, "write '$$'"),
            ({"type": "integer", "source": {"template": "1"}}, "a template produces text"),
            ({"type": "integer", "source": {"provider": "magic"}}, "unknown provider 'magic'"),
            ({"type": "integer", "source": {"provider": "status_block"}}, "produces octets, not 'integer'"),
            ({"type": "string", "source": {"provider": "serial", "default": 5}}, "string constant must be text"),
        ]
        for overrides, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertRejected(document(entry(**overrides)), fragment)

    def test_rules_across_entries(self):
        twin = entry(name="other")
        self.assertRejected(document(entry(), twin), "is listed twice")
        self.assertRejected(document(entry(), entry(oid="1.3.6.1.4.1.2435.99.2.0")), "name 'testEntry' is used by")
        self.assertRejected(document(entry(oid="1.3.6.1.4.1.2435.99.1"), entry(name="child")), "is a prefix of")
        self.assertRejected(document(entry(oid="1.3.6.1.4.1.9999.1.0")), "outside the persona subtrees")

    def test_every_problem_is_reported_at_once(self):
        message = self.assertRejected(document(entry(type="nope"), entry(oid="1.3.x", name="b"),
                                               entry(oid="1.3.6.1.4.1.2435.99.3.0", name="c", notes=""),
                                               schema_version=3))
        self.assertIn("4 problem(s)", message)
        self.assertIn("entry 1 (testEntry)", message)
        self.assertIn("entry 2 (b)", message)
        self.assertIn("entry 3 (c)", message)

    def test_the_input_document_is_not_modified(self):
        doc = document(entry(type="octets", source={"const": "80 20 42"}))
        before = copy.deepcopy(doc)
        persona.OidMap.from_dict(doc)
        self.assertEqual(doc, before)


# ---------------------------------------------------------------------------
# Loading files
# ---------------------------------------------------------------------------
class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qlmap-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.doc = document(entry(), entry(oid="1.3.6.1.4.1.2435.99.2.0", name="second", type="string",
                                           source={"config": "PERSONA_LOCATION"}))

    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)

    def write(self, name: str, data: Any) -> str:
        path = self.path(name)
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(path, mode, **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as fh:
            fh.write(data)
        return path

    def assertLoadError(self, path: str, fragment: str) -> None:
        with self.assertRaises(persona.OidMapError) as caught:
            persona.load_oid_map(path)
        self.assertIn(fragment, str(caught.exception))

    def test_json(self):
        oid_map = persona.load_oid_map(pathlib.Path(self.write("map.json", json.dumps(self.doc))))
        self.assertEqual([e.name for e in oid_map.entries], ["testEntry", "second"])
        self.assertTrue(oid_map.path.endswith("map.json"))

    @unittest.skipIf(yaml is None, "PyYAML is not installed in this interpreter")
    def test_yaml_and_yml(self):
        for name in ("map.yaml", "map.YML"):
            with self.subTest(name=name):
                oid_map = persona.load_oid_map(self.write(name, yaml.safe_dump(self.doc)))
                self.assertEqual(len(oid_map.entries), 2)

    @unittest.skipIf(yaml is None, "PyYAML is not installed in this interpreter")
    def test_invalid_and_unsafe_yaml(self):
        self.assertLoadError(self.write("bad.yaml", "entries: [\n"), "not valid YAML")
        self.assertLoadError(self.write("list.yaml", "- a\n- b\n"), "must be a mapping")
        marker = self.path("marker")
        unsafe = f"!!python/object/apply:os.mkdir [{json.dumps(marker)}]\n"
        self.assertLoadError(self.write("unsafe.yaml", unsafe), "not valid YAML")
        self.assertFalse(os.path.exists(marker), "the safe loader must not run code")

    def test_without_pyyaml_the_json_sibling_is_read(self):
        yaml_path = self.write("oid-map.yaml", "this would need PyYAML")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            self.assertLoadError(yaml_path, "python3-pyyaml")
            self.write("oid-map.json", json.dumps(self.doc))
            with self.assertLogs("ql810wc.persona", logging.INFO) as logs:
                oid_map = persona.load_oid_map(yaml_path)
        self.assertTrue(oid_map.path.endswith("oid-map.json"))
        self.assertEqual(len(oid_map.entries), 2)
        self.assertIn("PyYAML is not installed", "\n".join(logs.output))

    def test_unreadable_and_invalid_files(self):
        self.assertLoadError(self.path("missing.json"), "cannot read")
        self.assertLoadError(self.write("map.txt", "{}"), "expected a .yaml, .yml or .json file")
        self.assertLoadError(self.write("binary.json", b"\xff\xfe{}"), "not UTF-8")
        self.assertLoadError(self.write("broken.json", "{not json"), "not valid JSON")
        self.assertLoadError(self.write("huge.json", " " * ((1 << 20) + 1)), "larger than")
        self.assertLoadError(self.write("schema.json", json.dumps(dict(self.doc, schema_version=2))),
                             "schema_version must be 1")


if __name__ == "__main__":
    unittest.main()
