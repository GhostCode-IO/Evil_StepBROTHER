"""net-snmp ``pass_persist`` protocol for the printer persona (the degraded fallback).

With ``SNMP_AGENT_MODE=pass_persist`` snmpd starts this agent once per
``pass_persist`` line (scripts/30-snmp-agent.sh registers 1.3.6.1.2.1.43 and
1.3.6.1.4.1.2435) and talks to it over a pipe pair:

    snmpd writes                        the agent answers
    PING                                PONG                        (before every varbind)
    get        / <OID>                  <OID> / <type> / <value>    or NONE
    getnext    / <OID>                  the first OID after it      or NONE
    set        / <OID> / <type> <val>   not-writable                (the persona is read-only)

Facts from net-snmp 5.9 (agent/mibgroup/ucd-snmp/pass_persist.c, pass_common.c)
that shape this module:

* snmpd has **no read timeout**. A child that does not answer freezes the whole
  agent, host MIBs included. Every request is answered from memory plus at
  most one stat of state.json, and every request gets exactly one reply.
* The child's stderr is the **same pipe** as its stdout. Anything written to
  fd 2 desynchronises the protocol, so this module never writes there. The
  entry point points fd 2 at /dev/null and logs to syslog.
* snmpd reads each line with ``fgets(4096)``. A reply line can hold at most
  4095 bytes including its newline: 4094 bytes of ``string``, or 1365 bytes as
  hex ``octet``.
* For a GETNEXT that starts before the registered root, snmpd sends the root
  itself. NONE makes it continue in the next registered subtree.
* Each ``pass_persist`` line has its own child, and snmpd does not say which
  line a request arrived through. A request is therefore answered only from the
  served subtree that contains its OID, so a walk never jumps from one
  registration into another.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import select
import time
from typing import Iterable, Optional, Tuple

from .agentx import OidLike, VarBind, format_oid, parse_oid
from .persona import PASS_PERSIST_SUBTREES, Entry, PersonaTree, subtree_end, within

__all__ = ["MAX_LINE", "MAX_STRING_BYTES", "MAX_OCTET_BYTES", "MAX_REQUEST_LINE", "SET_VALUE_TIMEOUT",
           "LineReader", "PassPersistServer", "format_reply"]

log = logging.getLogger("ql810wc.passpersist")

#: Longest line snmpd reads (SNMP_MAXBUF - 1), newline included.
MAX_LINE = 4095
#: Longest ``string`` value: the line minus its newline.
MAX_STRING_BYTES = MAX_LINE - 1
#: Longest ``octet`` value: "xx " per byte, the last one without the space
#: (3 * 1365 - 1 = 4094 characters).
MAX_OCTET_BYTES = (MAX_STRING_BYTES + 1) // 3
#: Input lines longer than this are garbage; they are dropped, not buffered.
MAX_REQUEST_LINE = 65536
#: snmpd writes "set\n<OID>\n<value>\n" with one write(), so the value line is in
#: the pipe by the time the OID has been read. It is never awaited for long:
#: net-snmp 5.9 omits it for value types it cannot format, and 5.6 (macOS) cuts
#: it short without a newline.
SET_VALUE_TIMEOUT = 0.5
_READ_CHUNK = 65536


class _Closed(Exception):
    """snmpd closed our stdin (or asked us to stop)."""


class LineReader:
    """Newline-terminated lines from a raw file descriptor, with optional timeouts.

    Reads with ``os.read`` into its own buffer, so a timeout can be checked
    with ``select`` without losing data held in a userspace buffer.
    """

    def __init__(self, fd: int, *, max_line: int = MAX_REQUEST_LINE) -> None:
        self.fd = fd
        self.eof = False
        self._buf = bytearray()
        self._max_line = max_line

    def readline(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """The next line without its line ending (``\\n`` or ``\\r\\n``).

        Returns None at end of input (:attr:`eof` is then true), or when
        ``timeout`` seconds pass without a complete line. A partial line left
        at end of input is discarded.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            end = self._buf.find(b"\n")
            if end >= 0:
                line = bytes(self._buf[:end])
                del self._buf[:end + 1]
                return line[:-1] if line.endswith(b"\r") else line
            if self.eof:
                if self._buf:
                    log.debug("discarding %d bytes without a newline at end of input", len(self._buf))
                    self._buf.clear()
                return None
            if len(self._buf) > self._max_line:
                log.error("dropping %d bytes of input without a newline", len(self._buf))
                self._buf.clear()
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                readable, _, _ = select.select([self.fd], [], [], remaining)
                if not readable:
                    return None
            try:
                chunk = os.read(self.fd, _READ_CHUNK)
            except OSError as exc:
                log.error("reading from snmpd failed: %s", exc)
                chunk = b""
            if chunk:
                self._buf += chunk
            else:
                self.eof = True

    def discard_partial(self) -> bytes:
        """Drop and return buffered bytes that do not yet form a complete line."""
        if b"\n" in self._buf:
            return b""
        dropped = bytes(self._buf)
        self._buf.clear()
        return dropped


def _plain_text(data: bytes) -> bool:
    """True if ``data`` can travel as a pass_persist ``string``: UTF-8 without control characters."""
    if any(b < 0x20 or b == 0x7F for b in data):
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _cut_utf8(data: bytes, limit: int) -> bytes:
    return data[:limit].decode("utf-8", "ignore").encode("utf-8")


def format_reply(entry: Entry, vb: VarBind) -> str:
    """The three reply lines (``OID``, ``type``, ``value``) for one value.

    ``string`` entries travel as text when they are plain UTF-8, otherwise (and
    for ``octets``) as space-separated hex bytes. An empty value always goes
    out as an empty ``octet`` line: net-snmp's ``string`` parser strips a
    trailing CR with ``buf[strlen(buf) - 1]``, which on an empty value reads
    and may write the byte before its buffer. Values that would exceed snmpd's
    4095-byte line are cut, with a warning, so the pipe never loses sync.
    """
    oid = "." + format_oid(vb.oid)
    kind = entry.type
    value = vb.value
    if kind in ("octets", "string"):
        data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if kind == "string" and data and _plain_text(data):
            if len(data) > MAX_STRING_BYTES:
                log.warning("%s: %d-byte string cut to %d bytes (pass_persist line limit)",
                            entry.name, len(data), MAX_STRING_BYTES)
                data = _cut_utf8(data, MAX_STRING_BYTES)
            keyword, text = "string", data.decode("utf-8")
        else:
            if len(data) > MAX_OCTET_BYTES:
                log.warning("%s: %d bytes cut to %d (pass_persist line limit)",
                            entry.name, len(data), MAX_OCTET_BYTES)
                data = data[:MAX_OCTET_BYTES]
            keyword, text = "octet", " ".join(f"{b:02x}" for b in data)
    elif kind == "integer":
        keyword, text = "integer", str(int(value))
    elif kind == "oid":
        keyword, text = "objectid", "." + format_oid(parse_oid(value))
    elif kind == "timeticks":
        keyword, text = "timeticks", str(int(value) & 0xFFFFFFFF)
    elif kind == "counter32":
        keyword, text = "counter", str(int(value) & 0xFFFFFFFF)
    elif kind == "gauge32":
        keyword, text = "gauge", str(min(int(value), 0xFFFFFFFF))
    elif kind == "ipaddress":
        keyword, text = "ipaddress", str(ipaddress.IPv4Address(value))
    else:
        raise ValueError(f"{entry.name}: type {kind!r} cannot be sent through pass_persist")
    return f"{oid}\n{keyword}\n{text}\n"


class PassPersistServer:
    """Answer snmpd's pass_persist requests from a :class:`PersonaTree`.

    ``rfd``/``wfd`` are the pipe ends (stdin/stdout). ``tree`` may be None when
    the OID map could not be loaded: every request is then answered NONE, so
    snmpd keeps working. ``subtrees`` (default: the tree's) bounds what is
    served; each request is answered only within the subtree containing it.
    """

    def __init__(self, tree: Optional[PersonaTree], rfd: int = 0, wfd: int = 1, *,
                 subtrees: Optional[Iterable[OidLike]] = None,
                 set_value_timeout: float = SET_VALUE_TIMEOUT) -> None:
        if subtrees is None:
            subtrees = tree.subtrees if tree is not None else PASS_PERSIST_SUBTREES
        self.tree = tree
        self.subtrees = tuple(sorted(parse_oid(s) for s in subtrees))
        self.requests = 0
        self._reader = LineReader(rfd)
        self._wfd = wfd
        self._set_timeout = float(set_value_timeout)
        self._outside_logged = False

    def serve(self) -> int:
        """Serve until snmpd closes the pipe (or sends a blank line). Returns the exit status."""
        log.info("pass_persist: serving %s", ", ".join("." + format_oid(s) for s in self.subtrees))
        try:
            while True:
                line = self._reader.readline()
                if line is None:
                    raise _Closed("end of input")
                command = line.decode("ascii", "replace").strip().lower()
                if not command:
                    raise _Closed("blank line")
                self._dispatch(command)
        except _Closed as why:
            log.info("pass_persist: snmpd closed the session (%s) after %d request(s); exiting",
                     why, self.requests)
            return 0
        except BrokenPipeError:
            log.info("pass_persist: snmpd went away after %d request(s); exiting", self.requests)
            return 0
        except OSError as exc:
            log.error("pass_persist: cannot answer snmpd: %s", exc)
            return 1

    def _readline(self, timeout: Optional[float] = None) -> Optional[bytes]:
        line = self._reader.readline(timeout)
        if line is None and self._reader.eof:
            raise _Closed("end of input in the middle of a request")
        return line

    def _dispatch(self, command: str) -> None:
        if command == "ping":
            self._write("PONG\n")
            return
        if command in ("get", "getnext"):
            raw = self._readline()
            self.requests += 1
            self._write(self.answer(command, raw or b""))
            return
        if command == "set":
            raw = self._readline()
            value = self._readline(self._set_timeout)
            if value is None:
                # snmpd sent the whole command in one write and now waits for our
                # answer, so whatever is still buffered belongs to this SET: net-snmp
                # 5.6 cuts the value line short ("string " without a newline), and 5.9
                # omits it for types it cannot format. Dropping it keeps the next
                # PING from being glued onto it.
                dropped = self._reader.discard_partial()
                if dropped:
                    log.debug("pass_persist: dropped an incomplete SET value line %r", dropped[:80])
            self.requests += 1
            log.info("pass_persist: refusing SET of %s: the persona is read-only",
                     (raw or b"").decode("ascii", "replace").strip())
            self._write("not-writable\n")
            return
        log.warning("pass_persist: unknown command %r; answering NONE", command[:40])
        self._write("NONE\n")

    def answer(self, command: str, raw_oid: bytes) -> str:
        """The reply to ``get`` or ``getnext`` of ``raw_oid`` (one or three lines)."""
        try:
            oid = parse_oid(raw_oid.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError):
            log.warning("pass_persist: %s of an invalid OID %r; answering NONE", command, raw_oid[:80])
            return "NONE\n"
        try:
            found = self._get(oid) if command == "get" else self._getnext(oid)
            reply = "NONE\n" if found is None else format_reply(*found)
        except Exception:  # never leave snmpd waiting for an answer
            log.exception("pass_persist: %s %s failed; answering NONE", command, format_oid(oid))
            return "NONE\n"
        if log.isEnabledFor(logging.DEBUG):
            log.debug("pass_persist: %s .%s -> %s", command, format_oid(oid), reply.replace("\n", " | "))
        return reply

    def _get(self, oid: Tuple[int, ...]) -> Optional[Tuple[Entry, VarBind]]:
        if self.tree is None or not any(within(oid, root) for root in self.subtrees):
            return None
        return self.tree.lookup(oid)

    def _getnext(self, oid: Tuple[int, ...]) -> Optional[Tuple[Entry, VarBind]]:
        if self.tree is None:
            return None
        for root in self.subtrees:
            if within(oid, root):
                return self.tree.lookup_next(oid, subtree_end(root))
        # Registered above our subtrees (e.g. ".1.3.6.1.4.1"): start at the first one below it.
        for root in self.subtrees:
            if len(root) > len(oid) and within(root, oid):
                found = self.tree.lookup_next(root, subtree_end(root), include=True)
                if found is not None:
                    return found
        if not self._outside_logged:
            self._outside_logged = True
            log.warning("pass_persist: request for .%s, outside the served subtrees %s: is the "
                        "pass_persist line registered for another OID?", format_oid(oid),
                        ", ".join("." + format_oid(s) for s in self.subtrees))
        return None

    def _write(self, text: str) -> None:
        view = memoryview(text.encode("utf-8"))
        while view:
            written = os.write(self._wfd, view)
            view = view[written:]
