"""The documentation agrees with the code it documents.

README.md, docs/*.md and dev/notes/*.md are checked against setup.sh,
install.sh, selftest.sh, uninstall.sh, config.sample.env and the
repository's files:

* selftest.sh is implemented, so no document may still call it a stub, and no
  documented install command may skip it with --no-selftest;
* every option a documented command passes exists in that script's option
  parser (what follows setup.sh's "--" is install.sh's, and the options of
  the piped one-liner, "setup.sh | sudo bash -s -- ...", are setup.sh's),
  and every setup.sh and selftest.sh option is documented in the README;
* the exit codes the README lists for setup.sh, install.sh, selftest.sh and
  uninstall.sh are the ones the scripts declare;
* setup.sh --help lists exactly the options setup.sh accepts and the exit
  codes it declares, every "exit N" in setup.sh is one of them, and the
  README's setup.sh reference shows the help's one-line commands;
* report lines quoted from selftest.sh (PASS/WARN/FAIL/SKIP/INFO lines, the
  verdict's summary lines, DEGRADED, NOTE: and TIER REACHED lines) still
  exist in it, with the same status;
* the README's configuration table lists config.sample.env's keys, in the
  template's order and with its defaults;
* relative links and their #anchors resolve, repository paths quoted as
  inline code (agent/..., dev/..., scripts/..., tests/...) exist, and the
  project's own URLs (the one-liner's raw setup.sh, the repository and its
  pages) are the ones setup.sh uses;
* the user documentation cites no commit hash: a hash tells a reader
  nothing, and it dangles once history is rewritten.

Standard library only. Nothing here runs a script.
"""
from __future__ import annotations

import pathlib
import re
import unittest
from typing import Dict, Iterator, List, Optional, Set, Tuple

from tests._util import REPO

README = REPO / "README.md"
USER_DOCS: Tuple[pathlib.Path, ...] = (README, *sorted((REPO / "docs").glob("*.md")))
DOCS: Tuple[pathlib.Path, ...] = (*USER_DOCS, *sorted((REPO / "dev" / "notes").glob("*.md")))
SCRIPTS: Dict[str, pathlib.Path] = {name: REPO / f"{name}.sh"
                                    for name in ("setup", "install", "selftest", "uninstall")}
SETUP = SCRIPTS["setup"]
SELFTEST = SCRIPTS["selftest"]
# The function that parses a script's own options, where the rest of the
# script has other case arms that look like options.
PARSER_FUNCTION: Dict[str, str] = {"setup": "setup_parse_args"}
CONFIG_TEMPLATE = REPO / "config.sample.env"
# README headings of the sections that document each entry script.
SETUP_HEADING = "## setup.sh reference"
INSTALL_HEADING = "## Manual install with install.sh"
SELFTEST_HEADING = "## Selftest"
UNINSTALL_HEADING = "## Uninstall"
CONFIG_HEADING = "## Configuration"

FENCE = re.compile(r"^([ \t]*)```([^\n`]*)\n(.*?)^\1```[ \t]*$", re.M | re.S)
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)\)")
SCRIPT_CALL = re.compile(r"(?:^|(?<=[\s/]))(setup|install|selftest|uninstall)\.sh\b")
# What follows "…/setup.sh" in the one-liner: "| sudo [VAR=value ...] bash -s --".
PIPED_SETUP = re.compile(r"^[ \t]*\|[ \t]*sudo[ \t]+(?:[A-Z_][A-Z0-9_]*=\S*[ \t]+)*bash[ \t]+-s[ \t]+--(?=\s|$)")
OPTION_ARM = re.compile(r"^[ \t]*((?:-{1,2}[A-Za-z][\w-]*)(?:\|-{1,2}[A-Za-z][\w-]*)*)\)", re.M)
COMMAND_END = ("&&", "||", ";", "|", "<")
# The project's own URLs in the documentation.
RAW_SETUP_URL = re.compile(r"https://raw\.githubusercontent\.com/\S*?/setup\.sh")
PROJECT_URL = re.compile(r"https://github\.com/GhostCode-IO/[\w-]+(?:\.git)?")
REPORT_LINE = re.compile(r"^(PASS|WARN|FAIL|SKIP|INFO)[ \t]+(\S.*)$", re.S)
TIER_LINE = re.compile(r"^TIER REACHED: (?:NONE|T[0-3]|…|NONE\|T0\|T1\|T2\|T3)(?: \(\+host SNMP\))?$")
STATUS_CALL = re.compile(r"\b(?:check[ \t]+\S+|emit_line)[ \t]+(pass|warn|fail|skip|info)\b")
STALE_SELFTEST = (
    re.compile(r"selftest_sh[^.]{0,80}\bstub\b"),
    re.compile(r"\bstub\b[^.]{0,80}selftest"),
    re.compile(r"selftest[^.]{0,60}\bnot (?:implemented|built) yet\b"),
    re.compile(r"\bnot (?:implemented|built) yet\b[^.]{0,60}selftest"),
    re.compile(r"selftest[^.]{0,120}\bstatus 2\b"),
)
COMMIT_HASH = re.compile(r"\bcommits?[ \t\n]+`?[0-9a-f]{7,40}\b", re.I)
# setup.sh --help is the here-document of setup_usage(); its "Options:" lines
# start with two blanks and the option ("  --name NAME", "  -h, --help",
# "  -- ARGS..."), its "Exit codes:" lines with two blanks and the code.
SETUP_USAGE = re.compile(r"^setup_usage\(\)[ \t]*\{\n[ \t]*cat <<'EOF'\n(.*?)^EOF\n\}", re.M | re.S)
HELP_OPTION = re.compile(r"^  (--|-{1,2}[A-Za-z][\w-]*)(?:, (-{1,2}[A-Za-z][\w-]*))?(?=[ \"]|$)", re.M)
HELP_EXIT = re.compile(r"^  (\d+)[ \t]{2,}\S", re.M)
# Inline code that names a file or directory of this repository. docs/ is left
# out: the research notes cite docs/... files of other projects.
REPO_PATH = re.compile(r"(?:\.\./)*(?:agent|dev|scripts|tests|\.github)/[\w./-]*")
CONFIG_ROW = re.compile(r"^\|[ \t]*`([A-Z][A-Z0-9_]*)`[ \t]*\|[ \t]*([^|\n]*?)[ \t]*\|", re.M)


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def flat(text: str) -> str:
    """TEXT on one line, whitespace runs collapsed (as Markdown renders it)."""
    return " ".join(text.split())


def rel(path: pathlib.Path) -> str:
    return str(path.relative_to(REPO))


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def fenced_blocks(text: str) -> List[Tuple[str, str]]:
    """(info string, body) of every fenced code block, indentation removed."""
    blocks = []
    for match in FENCE.finditer(text):
        indent, body = match.group(1), match.group(3)
        if indent:
            body = "\n".join(line[len(indent):] if line.startswith(indent) else line
                             for line in body.split("\n"))
        blocks.append((match.group(2).strip(), body))
    return blocks


def prose(text: str) -> str:
    """TEXT without its fenced code blocks."""
    return FENCE.sub("", text)


def code_spans(text: str) -> List[str]:
    """Inline code spans outside fenced blocks. A span may wrap onto the next
    line but never crosses a paragraph; the docs use single backticks only."""
    spans = []
    for paragraph in re.split(r"\n[ \t]*\n", prose(text)):
        spans.extend(re.findall(r"`([^`]+)`", paragraph))
    return spans


def quoted_lines(text: str) -> Iterator[Tuple[str, bool]]:
    """(text, exact) for every code span (whitespace collapsed) and every line
    of a fenced block (verbatim, so exact=True)."""
    for span in code_spans(text):
        yield flat(span), False
    for _info, body in fenced_blocks(text):
        for line in body.split("\n"):
            if line.strip():
                yield line.rstrip(), True


def section(text: str, heading: str) -> str:
    """The body under HEADING (a whole heading line), up to the next heading
    outside a fenced block."""
    match = re.search(r"^%s[ \t]*$" % re.escape(heading), text, re.M)
    if match is None:
        raise AssertionError(f"no heading {heading!r}")
    rest = text[match.end():]
    fences = [m.span() for m in FENCE.finditer(rest)]
    for following in HEADING.finditer(rest):
        if not any(start <= following.start() < end for start, end in fences):
            return rest[:following.start()]
    return rest


def bullet(text: str, lead: str) -> Optional[str]:
    """The text after LEAD of the first list item that starts with LEAD
    (emphasis removed, on one line)."""
    for item in re.split(r"\n(?=- )", text):
        item = flat(item.replace("*", ""))
        if item.startswith("- " + lead):
            return item[len("- " + lead):]
    return None


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: link targets and backticks dropped,
    lower case, punctuation removed, spaces turned into hyphens."""
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).replace("`", "")
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(text: str) -> Set[str]:
    found: Set[str] = set()
    seen: Dict[str, int] = {}
    for match in HEADING.finditer(prose(text)):
        base = slug(match.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


# ---------------------------------------------------------------------------
# Commands in the docs, and the scripts' own declarations
# ---------------------------------------------------------------------------
def options_after_calls(text: str) -> Iterator[Tuple[str, str]]:
    """(script, option) for every option word that follows a call of an entry
    script in TEXT, up to a comment, a redirection or the next command.
    setup.sh hands what follows its "--" to install.sh, and in the one-liner
    ("…/setup.sh | sudo bash -s -- OPTIONS") the options are setup.sh's."""
    calls = list(SCRIPT_CALL.finditer(text))
    for index, call in enumerate(calls):
        end = calls[index + 1].start() if index + 1 < len(calls) else len(text)
        script, rest = call.group(1), text[call.end():end]
        if script == "setup":
            piped = PIPED_SETUP.match(rest)
            if piped:
                rest = rest[piped.end():]
        for word in rest.split():
            if word.startswith(("#", ">", "2>")) or word in COMMAND_END:
                break
            if word == "--" and script == "setup":
                script = "install"
            elif word.startswith("-"):
                yield script, word.rstrip(",.;:)`")


def documented_calls(path: pathlib.Path) -> Iterator[Tuple[str, str, str]]:
    """(script, option, where) for the commands of PATH: fenced shell lines and
    inline code spans."""
    text = read(path)
    for info, body in fenced_blocks(text):
        if info not in ("", "bash", "sh", "shell", "console"):
            continue
        for line in body.split("\n"):
            for script, option in options_after_calls(line):
                yield script, option, f"code block line {line.strip()!r}"
    for span in code_spans(text):
        for script, option in options_after_calls(flat(span)):
            yield script, option, f"inline code `{flat(span)}`"


def option_groups(path: pathlib.Path) -> List[List[str]]:
    """The alternatives of each option arm of a script's argument parser. For
    a script named in PARSER_FUNCTION only that function counts: setup.sh
    also has case arms for the install.sh options it hands on."""
    text = read(path)
    function = PARSER_FUNCTION.get(path.stem)
    if function is not None:
        match = re.search(r"^%s\(\)[ \t]*\{[^\n]*\n(.*?)^\}" % re.escape(function), text, re.M | re.S)
        if match is None:
            raise AssertionError(f"{path.name} has no function {function}()")
        text = match.group(1)
    return [arm.split("|") for arm in OPTION_ARM.findall(text)]


def exit_codes(text: str) -> Set[str]:
    """Codes listed as "0 ..., 1 ...; 64 ..." in TEXT."""
    return set(re.findall(r"(?:^|[;,])[ \t]*(\d+)[ \t]", text))


def setup_help() -> str:
    """What setup.sh --help prints: the here-document of setup_usage()."""
    match = SETUP_USAGE.search(read(SETUP))
    if match is None:
        raise AssertionError("setup.sh has no setup_usage() that prints a quoted here-document")
    return match.group(1)


def help_block(text: str, title: str) -> str:
    """The lines under TITLE (a line of its own, such as "Options:") in a help
    text, up to the next blank line."""
    match = re.search(r"^%s\n(.*?)(?:\n[ \t]*\n|\Z)" % re.escape(title), text, re.M | re.S)
    if match is None:
        raise AssertionError(f"the help has no {title!r} block")
    return match.group(1)


def function_body(text: str, name: str) -> str:
    """The body of the shell function NAME in TEXT, one-line form included."""
    match = re.search(r"^%s\(\)[ \t]*\{(.*?)\}[ \t]*$" % re.escape(name), text, re.M | re.S)
    if match is None:
        raise AssertionError(f"no function {name}()")
    return match.group(1)


def declared_exit_codes(path: pathlib.Path) -> Set[str]:
    """The header's "# Exit codes: ..." sentence (it may go on over further
    "# " lines until it ends with a full stop) plus every "usage N" exit."""
    text = read(path)
    header = re.search(r"^# Exit codes?: (.*(?:\n# (?!shellcheck)\S.*)*)$", text, re.M)
    if header is None:
        raise AssertionError(f"{path.name} declares no exit codes in its header")
    lines = header.group(1).split("\n")
    sentence = lines[0]
    for line in lines[1:]:
        if sentence.rstrip().endswith("."):
            break
        sentence += " " + line[2:]
    return exit_codes(sentence) | set(re.findall(r"\busage (\d+)\b", text))


ASSIGNMENT = re.compile(r"\b([A-Z_][A-Z0-9_]*)=(?:\"([^\"]*)\"|([^\s\"#()]+))")


def shell_constants() -> Dict[str, str]:
    """The fixed values selftest.sh prints in its messages: the QLP_* settings
    of scripts/lib/common.sh (top-level assignments) and selftest.sh's own
    readonly constants, unexpanded. Command substitutions are left out."""
    constants: Dict[str, str] = {}
    common = read(REPO / "scripts" / "lib" / "common.sh")
    for line in re.findall(r"^(QLP_[A-Z0-9_]+=.*)$", common, re.M):
        match = ASSIGNMENT.match(line)
        if match:
            constants[match.group(1)] = match.group(2) or match.group(3) or ""
    for line in re.findall(r"^readonly[ \t]+(.+)$", read(SELFTEST), re.M):
        for name, quoted, bare in ASSIGNMENT.findall(line):
            constants[name] = quoted or bare
    return {name: value for name, value in constants.items() if "$(" not in value}


def selftest_source() -> List[str]:
    """selftest.sh as one logical line per statement: shell arguments continued
    with a backslash and adjacent Python string literals are joined, and the
    constants are expanded, so its messages can be searched as the report
    prints them."""
    text = read(SELFTEST)
    text = re.sub(r'"[ \t]*\\\n[ \t]*"', " ", text)   # check ... "a" \ "b" -> "a b"
    text = re.sub(r'"[ \t]*\n[ \t]*"', "", text)      # Python ("a " "b") -> "a b"
    constants = shell_constants()

    def expand(value: str) -> str:
        return re.sub(r"\$\{?([A-Z_][A-Z0-9_]*)\}?",
                      lambda m: constants.get(m.group(1), m.group(0)), value)

    for _ in range(5):
        constants = {name: expand(value) for name, value in constants.items()}
    return expand(text).split("\n")


def contains_in_order(line: str, fragments: List[str]) -> bool:
    pos = 0
    for fragment in fragments:
        found = line.find(fragment, pos)
        if found < 0:
            return False
        pos = found + len(fragment)
    return True


def template_settings() -> List[Tuple[str, str]]:
    """(key, default) of every setting in config.sample.env, in file order,
    read the way scripts/lib/common.sh's parser reads it."""
    settings = []
    for line in read(CONFIG_TEMPLATE).split("\n"):
        match = re.match(r"^[ \t]*([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match is None:
            continue
        key, value = match.groups()
        quoted = re.match(r"^\"([^\"]*)\"[ \t]*(?:#.*)?$", value) or re.match(r"^'([^']*)'[ \t]*(?:#.*)?$", value)
        settings.append((key, quoted.group(1) if quoted else value.split("#", 1)[0].rstrip()))
    return settings


def documented_settings() -> List[Tuple[str, Optional[str]]]:
    """(key, default) of every row of the README's configuration table. The
    default column holds "empty" or one code span; anything else is None."""
    rows = []
    for key, default in CONFIG_ROW.findall(section(read(README), CONFIG_HEADING)):
        if default == "empty":
            rows.append((key, ""))
        else:
            match = re.fullmatch(r"`([^`]*)`", default)
            rows.append((key, match.group(1) if match else None))
    return rows


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class DocumentSetTest(unittest.TestCase):
    def test_documents_found(self):
        names = {rel(path) for path in DOCS}
        for expected in ("README.md", "docs/architecture.md", "docs/troubleshooting.md",
                         "docs/client-setup.md", "docs/security.md", "docs/fedora-on-raspberry-pi.md",
                         "dev/notes/README.md"):
            self.assertIn(expected, names)

    def test_parsers_find_the_scripts_options(self):
        """Guards the regular expressions the other tests rely on."""
        groups = {name: [opt for group in option_groups(path) for opt in group] for name, path in SCRIPTS.items()}
        self.assertTrue({"--clients", "--name", "--dir", "--ref", "--snmpv3", "--new-config", "--config-only",
                         "--print-config"} <= set(groups["setup"]))
        self.assertFalse({"--only", "--from", "--skip"} & set(groups["setup"]), "install.sh's options, not setup.sh's")
        self.assertTrue({"--only", "--from", "--skip", "--data-plane-only", "--no-selftest"} <= set(groups["install"]))
        self.assertTrue({"--json", "--quiet", "--print-test", "--mono", "--no-color"} <= set(groups["selftest"]))
        self.assertTrue({"--dry-run", "--purge", "--remove-packages"} <= set(groups["uninstall"]))
        self.assertEqual(list(options_after_calls("sudo ./install.sh --only 20   # data plane --skip")),
                         [("install", "--only")])
        self.assertEqual(list(options_after_calls("git pull --ff-only && sudo ./selftest.sh --json > x.json")),
                         [("selftest", "--json")])
        one_liner = "curl -fsSL https://raw.githubusercontent.com/GhostCode-IO/Evil_StepBROTHER/main/setup.sh"
        self.assertEqual(list(options_after_calls(one_liner + " | sudo bash")), [])
        self.assertEqual(list(options_after_calls(one_liner + " | sudo bash -s -- --clients 192.0.2.0/24 --name x")),
                         [("setup", "--clients"), ("setup", "--name")])
        self.assertEqual(list(options_after_calls("sudo ./setup.sh --name x -- --data-plane-only   # --bogus")),
                         [("setup", "--name"), ("install", "--data-plane-only")])
        self.assertEqual(exit_codes(" 0 ok, 1 error; 3 later (a, b); 64 usage."), {"0", "1", "3", "64"})
        self.assertEqual(declared_exit_codes(SETUP), {"0", "1", "3", "64"})
        self.assertEqual(slug("4. Data plane: `ql810wc-proxyd`"), "4-data-plane-ql810wc-proxyd")
        self.assertEqual(slug("setup.sh reference"), "setupsh-reference")
        self.assertEqual(REPO_PATH.fullmatch("dev/snmpwalk/scrub.py").group(0), "dev/snmpwalk/scrub.py")
        self.assertIsNone(REPO_PATH.fullmatch("/usr/local/libexec/ql810wc-proxy/qlctl"))
        self.assertEqual(template_settings()[0], ("PROXY_IP", ""))
        self.assertIn(("SNMP_PRINTER_COMMUNITIES", "public internal"), template_settings())
        for heading in (SETUP_HEADING, INSTALL_HEADING, SELFTEST_HEADING, UNINSTALL_HEADING, CONFIG_HEADING):
            self.assertTrue(section(read(README), heading).strip(), heading)


class SelftestIsDocumentedTest(unittest.TestCase):
    """selftest.sh is implemented: the docs must say so and use it."""

    def test_selftest_is_implemented(self):
        text = read(SELFTEST)
        self.assertGreater(text.count("\n"), 200)
        self.assertIn("TIER REACHED", text)
        self.assertRegex(text, r"(?m)^verdict\(\) \{")

    def test_no_document_calls_selftest_a_stub(self):
        for path in DOCS:
            text = flat(read(path)).lower().replace("selftest.sh", "selftest_sh")
            for pattern in STALE_SELFTEST:
                match = pattern.search(text)
                self.assertIsNone(match, f"{rel(path)} still describes selftest.sh as unfinished: "
                                         f"{match.group(0) if match else ''!r}")

    def test_install_commands_keep_the_selftest(self):
        for path in DOCS:
            for script, option, where in documented_calls(path):
                if script == "install":
                    self.assertNotEqual(option, "--no-selftest",
                                        f"{rel(path)}: {where} skips the selftest; the standard commands run it")

    def test_readme_documents_every_selftest_option(self):
        text = section(read(README), SELFTEST_HEADING)
        for group in option_groups(SELFTEST):
            if "--help" in group:
                continue
            self.assertTrue(any(re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(opt), text) for opt in group),
                            f"README's Selftest section does not document {' / '.join(group)}")

    def test_readme_documents_every_setup_option(self):
        """setup.sh's case arms group different options that share code
        (--clients|--name|--dir|--ref), not aliases: each one is checked. The
        other way round, every option row of the README's table is one of
        setup.sh's options (or "--", which hands the rest to install.sh)."""
        text = section(read(README), SETUP_HEADING)
        options = dict.fromkeys(opt for group in option_groups(SETUP) for opt in group)
        for opt in options:
            if opt in ("-h", "--help"):
                continue
            self.assertRegex(text, r"(?<![\w-])%s(?![\w-])" % re.escape(opt),
                             f"README's setup.sh reference does not document {opt}")
        rows = re.findall(r"^\|[ \t]*`(-{1,2}[A-Za-z][\w-]*|--)[ =`\s\"]", text, re.M)
        self.assertGreater(len(rows), 5)
        for opt in rows:
            self.assertTrue(opt == "--" or opt in options, f"README's setup.sh reference lists {opt}, "
                                                           f"which setup.sh does not accept")

    def test_readme_exit_codes_match_the_scripts(self):
        readme = read(README)
        for name, heading, lead in (("setup", SETUP_HEADING, "Exit status:"),
                                    ("install", INSTALL_HEADING, "Exit status:"),
                                    ("selftest", SELFTEST_HEADING, "Exit status:"),
                                    ("uninstall", UNINSTALL_HEADING, "Exit status:")):
            text = bullet(section(readme, heading), lead)
            self.assertIsNotNone(text, f"README {heading} has no '- {lead}' item")
            self.assertEqual(exit_codes(text or ""), declared_exit_codes(SCRIPTS[name]),
                             f"README {heading}: exit codes differ from {name}.sh")


class SetupHelpTest(unittest.TestCase):
    """setup.sh --help, setup.sh's code and the README's setup.sh reference
    agree: the help is what a user of the one-liner reads first."""

    def test_help_lists_exactly_the_options_setup_accepts(self):
        parser = {opt for group in option_groups(SETUP) for opt in group}
        listed: Set[str] = set()
        for match in HELP_OPTION.finditer(help_block(setup_help(), "Options:")):
            listed.update(opt for opt in match.groups() if opt)
        self.assertIn("--", listed, "setup.sh --help does not explain '-- ARGS' (handed to install.sh)")
        self.assertEqual(listed - {"--"}, parser,
                         "setup.sh --help (Options:) must list exactly the options setup_parse_args() accepts")

    def test_help_exit_codes_are_the_declared_ones(self):
        codes = set(HELP_EXIT.findall(help_block(setup_help(), "Exit codes:")))
        self.assertEqual(codes, declared_exit_codes(SETUP), "setup.sh --help and setup.sh's header list other exit codes")
        readme = bullet(section(read(README), SETUP_HEADING), "Exit status:")
        self.assertEqual(codes, exit_codes(readme or ""), "setup.sh --help and the README list other exit codes")

    def test_every_exit_status_setup_uses_is_declared(self):
        """Every literal "exit N" in setup.sh's code (comments left out), and
        those of the scripts/lib/common.sh helpers it calls (die, needs_human),
        is a declared exit code: an exit status setup.sh never documents would
        surprise a script that runs it."""
        text = read(SETUP)
        used: Set[str] = set()
        for line in text.split("\n"):
            if not line.lstrip().startswith("#"):
                used.update(re.findall(r"\bexit (\d+)\b", line))
        common = read(REPO / "scripts" / "lib" / "common.sh")
        for helper in ("die", "needs_human"):
            self.assertRegex(text, r"(?m)^[ \t]+(?:[^#\n]*[;&|][ \t]*)?%s[ \t]" % helper, f"setup.sh calls {helper}")
            used.update(re.findall(r"\bexit (\d+)\b", function_body(common, helper)))
        self.assertGreaterEqual(used, {"0", "1", "3", "64"})
        self.assertLessEqual(used, declared_exit_codes(SETUP), "setup.sh exits with a code it does not declare")

    def test_readme_shows_the_one_line_commands_of_the_help(self):
        text = section(read(README), SETUP_HEADING)
        commands = [line.strip() for line in setup_help().split("\n") if line.strip().startswith("curl ")]
        self.assertGreaterEqual(len(commands), 2)
        for command in commands:
            self.assertIn(command, text, "README's setup.sh reference does not show this command of setup.sh --help")


class DocumentedCommandsTest(unittest.TestCase):
    def test_every_documented_option_exists(self):
        known = {name: {opt for group in option_groups(path) for opt in group} for name, path in SCRIPTS.items()}
        checked = 0
        for path in DOCS:
            for script, option, where in documented_calls(path):
                checked += 1
                self.assertIn(option, known[script], f"{rel(path)}: {where}: {script}.sh has no option {option}")
        self.assertGreater(checked, 10)


class QuotedReportTest(unittest.TestCase):
    """Lines the docs quote from selftest.sh's report still exist in it."""

    @classmethod
    def setUpClass(cls):
        cls.source = selftest_source()
        text = read(SELFTEST)
        cls.labels = [label for label in re.findall(r'\("([^"]+)", (?:t[0-3]|host)\)', text) if " " in label]
        cls.sections = set(re.findall(r'\bsection "([^"$]+)"', text))

    def lines_with(self, fragments: List[str]) -> List[str]:
        return [line for line in self.source if contains_in_order(line, fragments)]

    def test_summary_labels_found(self):
        self.assertEqual(self.labels, ["T0 data plane", "T1 two-colour", "T2 discovery", "T3 live media",
                                       "host SNMP (bonus)"])

    def test_quoted_lines_exist_in_selftest(self):
        quoted = 0
        for path in DOCS:
            for text, exact in quoted_lines(read(path)):
                where = f"{rel(path)}: {text!r}"
                report = REPORT_LINE.match(text)
                if report:
                    quoted += 1
                    self.check_report_line(report.group(1).lower(), flat(report.group(2)), where)
                elif text.startswith(("DEGRADED", "NOTE: ")):
                    quoted += 1
                    fragments = [f.strip() for f in flat(text).split("…") if f.strip()]
                    self.assertTrue(self.lines_with(fragments), f"{where} is not in selftest.sh")
                elif text.startswith("TIER REACHED"):
                    quoted += 1
                    self.assertRegex(flat(text), TIER_LINE, where)
                elif text.startswith("== ") and text.endswith(" =="):
                    self.assertIn(text[3:-3], self.sections, f"{where}: selftest.sh has no such section")
                else:
                    self.check_summary_line(text, exact, where)
        self.assertGreater(quoted, 15)

    def check_report_line(self, status: str, text: str, where: str) -> None:
        fragments = [f.strip() for f in text.split("…") if f.strip()]
        lines = self.lines_with(fragments)
        self.assertTrue(lines, f"{where} is not a message of selftest.sh")
        statuses = {m.group(1) for line in lines for m in STATUS_CALL.finditer(line)}
        if statuses:
            self.assertIn(status, statuses, f"{where}: selftest.sh reports it as {', '.join(sorted(statuses))}")

    def check_summary_line(self, text: str, exact: bool, where: str) -> None:
        """A verdict summary line ("T1 two-colour      capable (not printed)").
        Inline code collapses the padding, so only code blocks keep the layout."""
        for label in self.labels:
            if not text.startswith(label + " "):
                continue
            status = flat(text[len(label):])
            self.assertTrue(any(status in line for line in self.source),
                            f"{where}: selftest.sh never reports {label} as {status!r}")
            if exact:
                self.assertEqual(text, "%-18s %s" % (label, status), f"{where}: not the report's layout")


class ConfigurationTableTest(unittest.TestCase):
    """README's configuration table is config.sample.env, key by key."""

    def test_table_lists_the_template_keys_in_order(self):
        template = [key for key, _ in template_settings()]
        self.assertGreater(len(template), 30)
        self.assertEqual([key for key, _ in documented_settings()], template,
                         "README's Configuration table must list config.sample.env's keys, in the same order")

    def test_table_shows_the_template_defaults(self):
        defaults = dict(template_settings())
        for key, documented in documented_settings():
            self.assertEqual(documented, defaults.get(key),
                             f"README's Configuration table: {key} default is not config.sample.env's "
                             f"{defaults.get(key)!r} (write `value`, or empty for \"\")")


class LinksTest(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        cache: Dict[pathlib.Path, Set[str]] = {}
        checked = 0
        for path in DOCS:
            for target in LINK.findall(prose(read(path))):
                if re.match(r"[a-z][a-z0-9+.-]*:", target):
                    continue                                    # https:, mailto:
                checked += 1
                name, _, fragment = target.partition("#")
                dest = (path.parent / name).resolve() if name else path
                self.assertTrue(dest.exists(), f"{rel(path)}: link target {target} does not exist")
                if fragment and dest.suffix == ".md":
                    if dest not in cache:
                        cache[dest] = anchors(read(dest))
                    self.assertIn(fragment, cache[dest], f"{rel(path)}: {target}: no such heading")
        self.assertGreater(checked, 20)

    def test_repository_paths_in_inline_code_exist(self):
        checked = 0
        for path in DOCS:
            for span in code_spans(read(path)):
                match = REPO_PATH.fullmatch(span.strip())
                if match is None:
                    continue
                checked += 1
                name = match.group(0)
                dest = (path.parent / name).resolve() if name.startswith("../") else REPO / name
                self.assertTrue(dest.exists(), f"{rel(path)}: `{name}` does not exist in the repository")
        self.assertGreater(checked, 20)


class ProjectUrlTest(unittest.TestCase):
    """The one-liner and the repository links in the docs are setup.sh's."""

    @classmethod
    def setUpClass(cls):
        text = read(SETUP)
        raw = re.search(r'^[ \t]*S_RAW_URL="([^"]+)"', text, re.M)
        clone = re.search(r'^[ \t]*S_REPO_URL_DEFAULT="([^"]+)"', text, re.M)
        if raw is None or clone is None:
            raise AssertionError("setup.sh sets no S_RAW_URL or S_REPO_URL_DEFAULT")
        cls.raw, cls.clone = raw.group(1), clone.group(1)

    def test_documented_urls_are_setup_sh_s(self):
        repo = self.clone[:-len(".git")] if self.clone.endswith(".git") else self.clone
        found = {"raw": 0, "repo": 0}
        for path in DOCS:
            text = read(path)
            for url in RAW_SETUP_URL.findall(text):
                found["raw"] += 1
                self.assertEqual(url, self.raw, f"{rel(path)}: the one-liner's URL is not setup.sh's")
            for url in PROJECT_URL.findall(text):
                found["repo"] += 1
                self.assertIn(url, (repo, self.clone), f"{rel(path)}: {url} is not this repository ({repo})")
        self.assertGreater(found["raw"], 3)
        self.assertGreater(found["repo"], 2)


class PublicHistoryTest(unittest.TestCase):
    def test_no_commit_hashes_in_user_docs(self):
        for path in USER_DOCS:
            match = COMMIT_HASH.search(read(path))
            self.assertIsNone(match, f"{rel(path)} cites {match.group(0) if match else ''!r}: a commit hash tells "
                                     f"a reader nothing and dangles once history is rewritten; describe the change "
                                     f"instead")


if __name__ == "__main__":
    unittest.main()
