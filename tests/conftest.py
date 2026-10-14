"""Shared fixtures. Imports the production reservoarr.py *as a module* via
importlib so its main() guard keeps it from running; tests then exercise
TsParser / Upstream / Stream / etc. directly on the real code.

This matches how the in-container test toolbox (parsecheck.py,
test_reconnect_precedence.py) already imports the script in production —
no abstraction layer, no fakes, real code under test.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RESERVOIR_PATH = REPO_ROOT / "reservoarr.py"
FIXTURES = REPO_ROOT / "fixtures"


def _load_reservoir(tmp_log_dir):
    """Import reservoarr.py with side-effects (sys.argv parsing, LOG_DIR setup)
    redirected to a clean test environment. Reloaded fresh per test so module-
    level settings a test patches (MAX_BYTES, REPLAY_SKIP, ...) don't leak across tests."""
    os.environ["RESV_LOG_DIR"] = str(tmp_log_dir)
    sys.argv = ["reservoarr", "http://offline/unit-test"]
    spec = importlib.util.spec_from_file_location("resv", str(RESERVOIR_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def resv(tmp_path):
    """Fresh reservoarr module per test."""
    return _load_reservoir(tmp_path)


@pytest.fixture
def stream(resv):
    """A fresh Stream (reservoir, parser, upstream) wired the way main() wires it."""
    return resv.Stream(resv.URL, resv.UA)


@pytest.fixture
def parser(resv):
    return resv.TsParser()


@pytest.fixture(scope="session")
def synth_ts():
    """Path to the synthetic fixture. Skips if not generated yet."""
    p = FIXTURES / "synth.ts"
    if not p.exists():
        pytest.skip(f"{p} not generated; run `just fixture` first")
    return p
