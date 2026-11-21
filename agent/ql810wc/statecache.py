"""``state.json``: the live-state hand-off from ql810wc-proxyd to its readers.

The proxy daemon is the only writer; the SNMP subagent, the pass_persist
fallback and ``selftest.sh`` read it. Writes are atomic (temporary file in
the same directory, fsync, rename), so a reader sees either the previous or
the new document, never a mix. Readers treat a missing, unreadable, corrupt
or wrong-schema file as "printer state unknown": :meth:`StateCache.read`
returns None and never raises.

Schema 1 (docs/architecture.md section 5)::

    {"schema": 1, "updated": <epoch s>,
     "proxy":  {"version", "pid", "started", "raw_port"},
     "device": {"state": "ready|absent|mass-storage|error", "vid", "pid",
                "product", "manufacturer", "serial", "node", "transport",
                "ieee1284_id"},
     "status": {"raw_hex", "polled", "parsed"},
     "job":    {"active", "peer", "started", "bytes", "two_color",
                "last_result": "ok|error|torn|refused|null", "last_finished"}}

Timestamps are wall-clock seconds since the epoch (``time.time()``).
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

from . import __version__
from .rasterproto import Status

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
#: Installed location (``STATE_FILE`` in the runtime config).
DEFAULT_PATH = "/run/ql810wc-proxy/state.json"
#: Device states written by the proxy.
DEVICE_STATES = ("ready", "absent", "mass-storage", "error")
#: Job results written by the proxy ("refused": RAW_COLOR_CHECK kept it from the printer).
JOB_RESULTS = ("ok", "error", "torn", "refused")
#: Files larger than this are rejected as corrupt (the document is ~2 KiB).
MAX_STATE_BYTES = 1 << 20
_FILE_MODE = 0o644


def new_state(*, pid: Optional[int] = None, raw_port: Optional[int] = None,
              started: Optional[float] = None, version: str = __version__) -> Dict[str, Any]:
    """A fresh schema-1 document: printer absent, no status, no job."""
    return {
        "schema": SCHEMA_VERSION,
        "updated": None,
        "proxy": {"version": version, "pid": pid, "started": started, "raw_port": raw_port},
        "device": {
            "state": "absent",
            "vid": None,
            "pid": None,
            "product": None,
            "manufacturer": None,
            "serial": None,
            "node": None,
            "transport": None,
            "ieee1284_id": None,
        },
        "status": {"raw_hex": None, "polled": None, "parsed": None},
        "job": {
            "active": False,
            "peer": None,
            "started": None,
            "bytes": 0,
            "two_color": None,
            "last_result": None,
            "last_finished": None,
        },
    }


def get_path(state: Optional[Mapping[str, Any]], key: str, default: Any = None) -> Any:
    """Look up a dotted key (``"status.polled"``) in a state document."""
    node: Any = state
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def age_seconds(state: Optional[Mapping[str, Any]], key: str = "updated",
                now: Optional[float] = None) -> Optional[float]:
    """Seconds since the timestamp at dotted ``key`` (default ``"updated"``).

    None when the key is missing or not a finite number. A timestamp in the
    future (wall clock stepped back) counts as age 0.
    """
    value = get_path(state, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    current = time.time() if now is None else now
    return max(0.0, current - float(value))


def status_from_state(state: Optional[Mapping[str, Any]]) -> Optional[Status]:
    """The last status block recorded in ``state`` (``status.raw_hex``), or None."""
    raw_hex = get_path(state, "status.raw_hex")
    if not isinstance(raw_hex, str):
        return None
    try:
        return Status.from_hex(raw_hex)
    except ValueError:
        log.debug("state.json has an invalid status.raw_hex: %r", raw_hex)
        return None


class StateCache:
    """Atomic writer / tolerant reader of one ``state.json`` file."""

    def __init__(self, path: Union[str, "os.PathLike[str]"] = DEFAULT_PATH) -> None:
        self.path = Path(path)

    def write(self, state: Mapping[str, Any]) -> None:
        """Atomically replace the file with ``state`` (mode 0644).

        ``updated`` is set to the current time and ``schema`` to 1 in the
        written document (the caller's mapping is not modified). Raises
        ValueError for a different schema, non-JSON values or NaN/infinity,
        and OSError if the directory is not writable; on any error the
        previous file is left intact and no temporary file remains.
        """
        doc = dict(state)
        schema = doc.setdefault("schema", SCHEMA_VERSION)
        if not _is_schema(schema):
            raise ValueError(f"state schema must be {SCHEMA_VERSION}, got {schema!r}")
        doc["updated"] = time.time()
        try:
            payload = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except TypeError as exc:
            raise ValueError(f"state is not JSON-serialisable: {exc}") from exc
        directory = self.path.parent
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload.encode("utf-8") + b"\n")
                fh.flush()
                os.fchmod(fh.fileno(), _FILE_MODE)
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        _fsync_directory(directory)

    def read(self) -> Optional[Dict[str, Any]]:
        """The current document, or None if it is missing, unreadable,
        oversized, not valid JSON, not an object or not schema 1. Never raises."""
        try:
            with open(self.path, "rb") as fh:
                data = fh.read(MAX_STATE_BYTES + 1)
        except (OSError, ValueError) as exc:
            log.debug("state %s not readable: %s", self.path, exc)
            return None
        if len(data) > MAX_STATE_BYTES:
            log.debug("state %s is larger than %d bytes; ignored", self.path, MAX_STATE_BYTES)
            return None
        try:
            doc = json.loads(data.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            log.debug("state %s is not valid JSON: %s", self.path, exc)
            return None
        if not isinstance(doc, dict) or not _is_schema(doc.get("schema")):
            log.debug("state %s has an unexpected shape or schema", self.path)
            return None
        return doc


def _is_schema(value: Any) -> bool:
    return type(value) is int and value == SCHEMA_VERSION


def _fsync_directory(directory: Path) -> None:
    """Persist the rename (best effort: some filesystems refuse directory fsync)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
