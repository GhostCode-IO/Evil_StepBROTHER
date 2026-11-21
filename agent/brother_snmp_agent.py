#!/usr/bin/env python3
"""ql810wc-snmp-agent: the SNMP printer persona of ql810wc-print-proxy.

Answers SNMP as a networked Brother QL-810W. Its inputs are ql810wc-proxyd's
state.json, the OID map (agent/oid-map.yaml) and the runtime settings. It never
touches USB.

Modes:
  --mode agentx        (default; ql810wc-snmp-agent.service) Runs as an AgentX
                       subagent of snmpd. It registers the persona subtrees in
                       SNMP context "printer" and reconnects for as long as it
                       runs. It reports READY=1 to systemd once snmpd has
                       accepted every registration, and closes its session
                       cleanly on SIGTERM. SIGHUP ("systemctl reload") re-reads
                       the settings, the identity and the OID map and serves
                       them in the same AgentX session (RELOADING=1, then
                       READY=1; the unit is Type=notify-reload, so systemctl
                       waits for both). A restart would close the session,
                       and until the new process registers, snmpd answers the
                       persona's OIDs with noSuchName, which fails a running
                       P-touch print. A reload that fails keeps the previous
                       persona.
                       The master socket, the context and the Python code stay
                       those the agent started with (new code needs a restart).
  --mode pass_persist  (degraded fallback) Started by snmpd from a
                       "pass_persist" line. Serves the Printer-MIB and Brother
                       subtrees in the default context over stdin/stdout. The
                       registration OID is not passed to the script, so one
                       script serves both subtrees. Positional SUBTREE
                       arguments narrow what is served.

Diagnostics:
  --check              Validate the settings and the OID map, print a summary,
                       and exit.
  --dump               Print every OID the persona would answer right now.

Run it as "/usr/bin/python3 -I -B brother_snmp_agent.py ...". The script adds
its own directory to sys.path (docs/architecture.md section 3).

Exit status: 0 ok, 1 unexpected failure, 64 usage error, 78 configuration
error (the systemd unit does not restart on 78).
"""
import os
import sys


def _option(argv, name):
    """The value of option ``name`` (``--name VALUE`` or ``--name=VALUE``), read before parsing."""
    for index, arg in enumerate(argv):
        if arg == name:
            return argv[index + 1] if index + 1 < len(argv) else None
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
    return None


def _wants_pass_persist(argv):
    """True if the command line selects pass_persist mode (checked before anything is imported)."""
    return _option(argv, "--mode") == "pass_persist"


if __name__ == "__main__" and _wants_pass_persist(sys.argv[1:]):
    # snmpd reads our replies from stdout and merges stderr into the same pipe:
    # a single stray byte on fd 2 would desynchronise the protocol.
    _DEVNULL = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_DEVNULL, 2)
    os.close(_DEVNULL)

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import argparse  # noqa: E402
import logging  # noqa: E402
import logging.handlers  # noqa: E402
import re  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from typing import Any, List, Mapping, Optional, Sequence, Tuple  # noqa: E402

from ql810wc import __version__, agentx, config, passpersist, persona  # noqa: E402

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 64
EXIT_CONFIG = 78

SYSLOG_IDENT = "ql810wc-snmp-agent"
LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"
#: How often the systemd STATUS= line is refreshed once the agent is ready.
STATUS_INTERVAL = 5.0
#: How often the main thread looks at the signal flags and the subagent thread.
TICK_SECONDS = 0.2
#: Longest wait, after a reload, for the subagent thread to register and
#: unregister a changed set of subtrees before READY=1 is reported anyway.
RELOAD_SYNC_SECONDS = 10.0
#: Longest wait for the AgentX session to close on stop (the unit's TimeoutStopSec is 10 s).
STOP_GRACE_SECONDS = 8.0
_CONTEXT_RE = re.compile(r"[A-Za-z0-9_.\-]{1,32}")

log = logging.getLogger("ql810wc.agent")


class UsageError(Exception):
    """The command line is invalid."""


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that raises instead of printing (stderr may be the snmpd pipe)."""

    def error(self, message: str):  # type: ignore[override]
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser (also used by the tests to check the systemd unit)."""
    here = os.path.dirname(os.path.realpath(__file__))
    parser = _Parser(prog="brother_snmp_agent.py", allow_abbrev=False,
                     description="SNMP printer persona (Brother QL-810W) for ql810wc-print-proxy.")
    parser.add_argument("--mode", choices=("agentx", "pass_persist"), default="agentx",
                        help="agentx subagent (default) or snmpd pass_persist child")
    parser.add_argument("--socket", metavar="PATH",
                        help="AgentX master socket (default: AGENTX_SOCKET, /var/agentx/master)")
    parser.add_argument("--context", default=persona.PERSONA_CONTEXT,
                        help="SNMP context of the persona in agentx mode (default: %(default)s)")
    parser.add_argument("--config", metavar="FILE", default=config.RUNTIME_ENV,
                        help="runtime settings (default: %(default)s)")
    parser.add_argument("--identity", metavar="FILE",
                        help="identity settings (default: identity.env next to --config)")
    parser.add_argument("--state", metavar="FILE",
                        help="state.json of ql810wc-proxyd (default: STATE_FILE)")
    parser.add_argument("--oid-map", metavar="FILE", default=os.path.join(here, "oid-map.yaml"),
                        help="OID map (default: %(default)s)")
    parser.add_argument("--log-level", type=str.upper, choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help="log level (default: LOG_LEVEL from the settings)")
    parser.add_argument("--log-file", metavar="FILE",
                        help="also log to FILE; in pass_persist mode instead of syslog (development: "
                             "under SELinux, snmpd's children may write only snmpd's own files)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="validate settings and OID map, then exit")
    action.add_argument("--dump", action="store_true", help="print the current value of every OID, then exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("subtrees", nargs="*", metavar="SUBTREE",
                        help="pass_persist: serve only these subtrees, each inside .1.3.6.1.2.1.43 or "
                             ".1.3.6.1.4.1.2435 (default: both)")
    return parser


def parse_args(argv: Optional[Sequence[str]]) -> Tuple[argparse.Namespace, Tuple[agentx.Oid, ...]]:
    """Parse and check the command line. Raises :class:`UsageError`."""
    args = build_parser().parse_args(argv)
    if args.subtrees and args.mode != "pass_persist":
        raise UsageError("SUBTREE arguments are only accepted with --mode pass_persist")
    if not _CONTEXT_RE.fullmatch(args.context):
        raise UsageError(f"--context {args.context!r}: use 1-32 letters, digits, '.', '_' or '-'")
    subtrees: List[agentx.Oid] = []
    for text in args.subtrees:
        try:
            oid = agentx.parse_oid(text)
        except ValueError:
            raise UsageError(f"invalid SUBTREE {text!r}") from None
        if not any(persona.within(oid, root) for root in persona.PASS_PERSIST_SUBTREES):
            raise UsageError(f"SUBTREE {text} is outside the pass_persist subtrees "
                             f"{', '.join('.' + agentx.format_oid(s) for s in persona.PASS_PERSIST_SUBTREES)}")
        subtrees.append(oid)
    return args, tuple(subtrees) or persona.PASS_PERSIST_SUBTREES


# ---------------------------------------------------------------------------
# Logging and systemd
# ---------------------------------------------------------------------------
def _syslog_handler() -> Optional[logging.Handler]:
    for address in ("/dev/log", "/var/run/syslog"):
        if not os.path.exists(address):
            continue
        try:
            handler = logging.handlers.SysLogHandler(address=address,
                                                     facility=logging.handlers.SysLogHandler.LOG_DAEMON)
        except OSError:
            continue
        handler.ident = f"{SYSLOG_IDENT}[{os.getpid()}]: "
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        return handler
    return None


def setup_logging(mode: str, log_file: Optional[str]) -> None:
    """agentx: stderr (the journal). pass_persist: syslog, or only ``log_file`` when given."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.INFO)
    handlers: List[logging.Handler] = []
    if mode == "pass_persist":
        logging.raiseExceptions = False  # a logging failure must never reach the (closed) stderr
        if not log_file:
            syslog = _syslog_handler()
            if syslog is not None:
                handlers.append(syslog)
    else:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(LOG_FORMAT))
        handlers.append(stream)
    if log_file:
        try:
            handler = logging.FileHandler(log_file, encoding="utf-8")
        except OSError as exc:
            log.warning("cannot open --log-file %s: %s", log_file, exc)
        else:
            handler.setFormatter(logging.Formatter("%(asctime)s %(process)d " + LOG_FORMAT))
            handlers.append(handler)
    for handler in handlers or [logging.NullHandler()]:
        root.addHandler(handler)


class SystemdNotifier:
    """sd_notify(3) over $NOTIFY_SOCKET, standard library only. A no-op outside systemd."""

    def __init__(self, address: Optional[str] = None) -> None:
        if address is None:
            address = os.environ.get("NOTIFY_SOCKET", "")
        if address.startswith("@"):
            address = "\0" + address[1:]       # abstract namespace
        elif address and not address.startswith("/"):
            log.debug("NOTIFY_SOCKET %r is not a unix socket path; not notifying systemd", address)
            address = ""
        self._address = address or None
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """True when running under systemd with a notification socket."""
        return self._address is not None

    def notify(self, *fields: str) -> bool:
        """Send ``KEY=value`` fields (e.g. ``READY=1``). Returns False if not sent."""
        if self._address is None:
            return False
        message = "\n".join(fields).encode("utf-8", "replace")
        with self._lock:
            try:
                if self._sock is None:
                    self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self._sock.sendto(message, self._address)
            except OSError as exc:
                log.debug("sd_notify %r failed: %s", fields, exc)
                return False
        return True

    def close(self) -> None:
        """Release the socket."""
        with self._lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None


class SignalFlags:
    """What SIGTERM, SIGINT and SIGHUP ask for, acted on by :meth:`AgentXService.run`.

    The handlers only assign attributes. They take no lock and do no I/O, so
    they can never deadlock the code they interrupt (a handler that sets a
    threading.Event can, when the main thread is inside that Event's lock).
    """

    def __init__(self) -> None:
        #: The first stop signal received (SIGTERM or SIGINT), or None.
        self.stop: Optional[int] = None
        #: How many SIGHUPs were received. Those that arrive before a reload
        #: starts share it; one that arrives during a reload causes another.
        self.reloads = 0

    def install(self) -> None:
        """Install the handlers (main thread only)."""
        signal.signal(signal.SIGTERM, self._on_stop)
        signal.signal(signal.SIGINT, self._on_stop)
        signal.signal(signal.SIGHUP, self._on_reload)

    def _on_stop(self, signum: int, _frame: object) -> None:
        if self.stop is None:
            self.stop = signum

    def _on_reload(self, _signum: int, _frame: object) -> None:
        self.reloads += 1


class NothingToServe(Exception):
    """The OID map has no entry inside the subtrees the subagent registers."""


def _state_path(args: argparse.Namespace, cfg: Mapping[str, str]) -> str:
    return args.state or cfg.get("STATE_FILE") or config.DEFAULTS["STATE_FILE"]


def _master_address(args: argparse.Namespace, cfg: Mapping[str, str]) -> str:
    return args.socket or cfg.get("AGENTX_SOCKET") or agentx.DEFAULT_SOCKET


def _descr(the_persona: persona.Persona) -> str:
    return f"ql810wc-print-proxy {__version__}: Brother {the_persona.oid_map.model} printer persona"


def _serving(args: argparse.Namespace, the_persona: persona.Persona) -> Tuple[persona.PersonaTree,
                                                                            List[agentx.Registration]]:
    """The tree and registrations that serve ``the_persona``. Raises :class:`NothingToServe`."""
    tree = persona.PersonaTree(the_persona, context=args.context)
    subtrees = tree.registrations()
    if not subtrees:
        raise NothingToServe("the OID map has no entries inside the persona subtrees; nothing to register")
    return tree, [agentx.Registration(args.context, s) for s in subtrees]


def _subtree_list(registrations: Sequence[agentx.Registration]) -> str:
    return ", ".join("." + agentx.format_oid(reg.subtree) for reg in registrations)


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return (text.splitlines()[0] if text else type(exc).__name__)[:120]


class AgentXService:
    """The AgentX subagent at run time: the persona it serves, reloads and systemd notifications.

    :meth:`run` keeps the main thread for signals, reloads and sd_notify; the
    AgentX session runs on a worker thread (:meth:`agentx.Subagent.run_forever`).
    ``subagent_options`` go to :class:`agentx.Subagent` (tests pass a
    ``connector`` and short timeouts). Raises :class:`NothingToServe`, or
    ValueError for an unusable master address.
    """

    def __init__(self, args: argparse.Namespace, the_persona: persona.Persona, cfg: Mapping[str, str], *,
                 notifier: Optional[SystemdNotifier] = None, **subagent_options: Any) -> None:
        self.args = args
        self.persona = the_persona
        self.tree, self.registrations = _serving(args, the_persona)
        self.master = _master_address(args, cfg)
        self.notifier = notifier if notifier is not None else SystemdNotifier()
        self.subagent = agentx.Subagent(self.tree, self.master, self.registrations, name=SYSLOG_IDENT,
                                        descr=_descr(the_persona), **subagent_options)
        #: Generation of the subagent's registration set after the last reload.
        self.generation = 0
        #: Why the last reload failed (first line), or None.
        self.reload_error: Optional[str] = None
        self._status_failed = False

    def describe(self) -> str:
        """One log line: what is served where."""
        return (f"printer persona {self.persona.oid_map.model}: {len(self.tree.entries)} OIDs in context "
                f"{self.args.context!r} ({_subtree_list(self.registrations)}), master {self.master}")

    def status_line(self) -> str:
        """The systemd STATUS= text: session state, a failed reload, printer state.

        A failed reload comes before the printer state, which can be long, so
        the 240-character cut never drops it: the installer looks for "last
        reload failed" after "systemctl reload" (qlp_try_reload)."""
        try:
            printer = self.persona.summary()
            self._status_failed = False
        except Exception:  # a status line must never take the agent down
            if not self._status_failed:
                self._status_failed = True
                log.exception("cannot summarise the printer state for systemd")
            printer = "state unknown (internal error, see the journal)"
        text = self.subagent.status
        if self.reload_error:
            text += f"; last reload failed: {self.reload_error}"
        return f"{text}; printer {printer}"[:240]

    def reload(self) -> bool:
        """Re-read the settings, the identity and the OID map, and serve them in the running session.

        Builds a new persona that carries on from the current one (sysUpTime,
        remembered USB identity, job progress) and hands it to the subagent
        without closing the AgentX session. On any error the current persona
        stays in place. Returns True on success.
        """
        args = self.args
        log.info("reloading %s, %s and %s", args.config,
                 args.identity or os.path.join(os.path.dirname(args.config), config.IDENTITY_ENV_NAME),
                 args.oid_map)
        try:
            cfg = config.load_runtime_config(args.config, identity_path=args.identity)
            oid_map = persona.load_oid_map(args.oid_map)
            new_persona = persona.Persona(oid_map, cfg, _state_path(args, cfg), previous=self.persona)
            tree, registrations = _serving(args, new_persona)
            generation = self.subagent.update(tree, registrations, descr=_descr(new_persona))
        except (config.ConfigError, persona.OidMapError, NothingToServe) as exc:
            self.reload_error = _first_line(exc)
            log.error("reload failed; still serving the previous settings and OID map: %s", exc)
            return False
        except Exception as exc:  # an internal bug must not kill a 24/7 agent
            self.reload_error = _first_line(exc)
            log.exception("reload failed; still serving the previous settings and OID map")
            return False
        if not args.log_level:
            logging.getLogger().setLevel(cfg.get("LOG_LEVEL") or "INFO")
        master = _master_address(args, cfg)
        if master != self.master:
            log.warning("the AgentX master is now %s, but the subagent stays connected to %s until "
                        "ql810wc-snmp-agent is restarted", master, self.master)
        changed = set(registrations) != set(self.registrations)
        self.persona, self.tree, self.registrations = new_persona, tree, registrations
        self.generation = generation
        self.reload_error = None
        log.info("reloaded: %s%s", self.describe(), "" if changed else " (registrations unchanged)")
        return True

    def run(self, signals: SignalFlags) -> int:
        """Serve until a stop signal. Returns the exit status.

        The main thread acts on ``signals``: a stop closes the AgentX session
        (STOPPING=1), a reload runs :meth:`reload` between RELOADING=1 and
        READY=1. It reports READY=1 once every registration is accepted and
        refreshes STATUS= while the status changes. If the session thread ever
        dies, the agent exits with status 1 so that systemd restarts it.
        """
        stop = threading.Event()
        worker = threading.Thread(target=self.subagent.run_forever, args=(stop,), name="agentx", daemon=True)
        worker.start()
        try:
            return self._supervise(signals, worker)
        finally:
            stop.set()
            worker.join(STOP_GRACE_SECONDS)
            if worker.is_alive():
                log.warning("the AgentX session did not close within %.0f s; exiting anyway", STOP_GRACE_SECONDS)
            self.notifier.close()

    def _supervise(self, signals: SignalFlags, worker: threading.Thread) -> int:
        reloads_seen = 0
        announced = False   # READY=1 sent
        last: Optional[str] = None
        next_refresh = 0.0
        while True:
            if signals.stop is not None:
                log.info("%s received; closing the AgentX session", _signal_name(signals.stop))
                self.notifier.notify("STOPPING=1")
                return EXIT_OK
            if not worker.is_alive():
                log.error("the AgentX session thread stopped unexpectedly; exiting so that systemd "
                          "restarts the agent")
                return EXIT_FAILURE
            if signals.reloads != reloads_seen:
                reloads_seen = signals.reloads
                status = self._reload_and_notify(announced, signals, worker)
                if status is not None:
                    last = status
                next_refresh = time.monotonic() + STATUS_INTERVAL
                continue
            now = time.monotonic()
            if not announced:
                if self.subagent.ready.is_set():
                    last = self.status_line()
                    self.notifier.notify("READY=1", f"STATUS={last}")
                    log.info("serving the printer persona: %s", last)
                    announced = True
                    next_refresh = now + STATUS_INTERVAL
            elif now >= next_refresh:
                status = self.status_line()
                if status != last:
                    self.notifier.notify(f"STATUS={status}")
                    last = status
                next_refresh = now + STATUS_INTERVAL
            time.sleep(TICK_SECONDS)

    def _reload_and_notify(self, announced: bool, signals: SignalFlags,
                           worker: threading.Thread) -> Optional[str]:
        """Reload; once READY=1 was sent, bracket it with RELOADING=1 and READY=1 (sd_notify(3)).

        Before the first READY=1 the reload is silent: systemd still waits for
        that one, which follows once every registration is accepted. Returns
        the STATUS= text sent, if any.
        """
        if announced:
            self.notifier.notify("RELOADING=1", f"MONOTONIC_USEC={time.monotonic_ns() // 1000}",
                                 "STATUS=reloading the settings and the OID map")
        if self.reload():
            deadline = time.monotonic() + RELOAD_SYNC_SECONDS
            while not self.subagent.wait_applied(self.generation, TICK_SECONDS):
                if signals.stop is not None or not worker.is_alive() or time.monotonic() >= deadline:
                    break
        if not announced:
            return None
        status = self.status_line()
        self.notifier.notify("READY=1", f"STATUS={status}")
        return status


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
def run_agentx(args: argparse.Namespace, the_persona: persona.Persona, cfg: config.RuntimeConfig,
               signals: Optional[SignalFlags] = None) -> int:
    """Serve the persona as an AgentX subagent until SIGTERM/SIGINT; reload on SIGHUP."""
    if signals is None:
        signals = SignalFlags()
        signals.install()
    try:
        service = AgentXService(args, the_persona, cfg)
    except NothingToServe as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    except ValueError as exc:
        log.error("cannot start the AgentX subagent: %s", exc)
        return EXIT_CONFIG
    log.info("%s", service.describe())
    return service.run(signals)


def run_pass_persist(the_persona: Optional[persona.Persona], subtrees: Tuple[agentx.Oid, ...]) -> int:
    """Serve snmpd's pass_persist requests on stdin/stdout until snmpd closes the pipe."""
    tree = None
    if the_persona is not None:
        tree = persona.PersonaTree(the_persona, context="", subtrees=subtrees)
    return passpersist.PassPersistServer(tree, 0, 1, subtrees=subtrees).serve()


def _print_check(oid_map: persona.OidMap, args: argparse.Namespace) -> None:
    agentx_entries = oid_map.within(persona.AGENTX_SUBTREES)
    pp_entries = oid_map.within(persona.PASS_PERSIST_SUBTREES)
    firmware = ", ".join(oid_map.validated_firmware) or "none yet"
    print(f"OID map {oid_map.path}: {len(oid_map.entries)} entries, model {oid_map.model}, "
          f"validated firmware: {firmware}, verified entries: {sum(e.verified for e in oid_map.entries)}")
    print(f"agentx: {len(agentx_entries)} OIDs in context {args.context!r}")
    print(f"pass_persist: {len(pp_entries)} OIDs under "
          f"{', '.join('.' + agentx.format_oid(s) for s in persona.PASS_PERSIST_SUBTREES)}")


def _dump(the_persona: persona.Persona, args: argparse.Namespace, subtrees: Tuple[agentx.Oid, ...]) -> None:
    roots = subtrees if args.mode == "pass_persist" else persona.AGENTX_SUBTREES
    tree = persona.PersonaTree(the_persona, context=args.context, subtrees=roots)
    print(f"# printer {the_persona.summary()}")
    for entry, vb in tree.walk():
        print(persona.describe_varbind(entry, vb))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns the exit status."""
    raw = list(sys.argv[1:] if argv is None else argv)
    setup_logging("pass_persist" if _wants_pass_persist(raw) else "agentx", _option(raw, "--log-file"))
    try:
        args, subtrees = parse_args(raw)
    except UsageError as exc:
        log.error("usage: %s (see --help)", exc)
        return EXIT_USAGE
    setup_logging(args.mode, args.log_file)
    if args.log_level:
        logging.getLogger().setLevel(args.log_level)
    interactive = args.check or args.dump
    signals: Optional[SignalFlags] = None
    if args.mode == "agentx" and not interactive:
        # From here on a SIGHUP (reload) or SIGTERM is recorded, not fatal:
        # the agent acts on it once it serves.
        signals = SignalFlags()
        signals.install()

    try:
        cfg = config.load_runtime_config(args.config, identity_path=args.identity)
    except config.ConfigError as exc:
        if args.mode != "pass_persist" or interactive:
            log.error("%s", exc)
            return EXIT_CONFIG
        log.error("%s; serving with built-in defaults", exc)
        cfg = config.load_runtime_config(None)
    if not args.log_level:
        logging.getLogger().setLevel(cfg.get("LOG_LEVEL") or "INFO")

    try:
        oid_map = persona.load_oid_map(args.oid_map)
    except persona.OidMapError as exc:
        log.error("%s", exc)
        if args.mode == "pass_persist" and not interactive:
            log.error("answering NONE to every request until the OID map is fixed")
            return run_pass_persist(None, subtrees)
        return EXIT_CONFIG
    if args.check:
        _print_check(oid_map, args)
        return EXIT_OK

    state_path = _state_path(args, cfg)
    try:
        the_persona = persona.Persona(oid_map, cfg, state_path)
    except config.ConfigError as exc:
        if args.mode != "pass_persist" or interactive:
            log.error("%s", exc)
            return EXIT_CONFIG
        log.error("%s; serving with built-in defaults", exc)
        the_persona = persona.Persona(oid_map, config.load_runtime_config(None), state_path)
    if args.dump:
        _dump(the_persona, args, subtrees)
        return EXIT_OK
    if args.mode == "pass_persist":
        return run_pass_persist(the_persona, subtrees)
    return run_agentx(args, the_persona, cfg, signals)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_OK)
    except Exception:  # last resort: log it where this mode's logs go
        log.exception("ql810wc-snmp-agent failed")
        sys.exit(EXIT_FAILURE)
