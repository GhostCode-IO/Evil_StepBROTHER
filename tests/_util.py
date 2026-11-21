"""Shared test helpers."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import sys
from typing import Dict, List

REPO = pathlib.Path(__file__).resolve().parent.parent


def load_module(relpath: str, name: str):
    """Import a script that is not inside a package (e.g. dev/snmpwalk/*.py)."""
    path = REPO / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, path
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def bashes() -> List[str]:
    """Every distinct bash at hand: $QLP_TEST_BASH, the one on PATH and
    /bin/bash (3.2 on macOS), for scripts that must run with all of them."""
    found: Dict[str, str] = {}
    for bash in (os.environ.get("QLP_TEST_BASH"), shutil.which("bash"), "/bin/bash"):
        if bash and os.access(bash, os.X_OK):
            found.setdefault(os.path.realpath(bash), bash)
    return list(found.values())
