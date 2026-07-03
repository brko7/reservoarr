"""Log-rotation regression tests. v6.3.1 moved the 10 MB rotation check from
import-time-only into log() (every 512th line): rotation used to run once per
process start, so a single long-lived tune (24/7 channel) grew delaybuf.log
unboundedly until the next channel start. These pin both rotation paths and
the below-threshold no-op."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESERVOIR_PATH = REPO_ROOT / "reservoarr.py"


def _load(tmp_log_dir):
    """Load reservoarr.py fresh with RESV_LOG_DIR pointed at tmp_log_dir.
    Mirrors conftest._load_reservoir, but callable AFTER the test has staged
    a pre-existing log file (the conftest fixture imports too early for the
    import-time-rotation case)."""
    os.environ["RESV_LOG_DIR"] = str(tmp_log_dir)
    sys.argv = ["reservoarr", "http://offline/unit-test"]
    spec = importlib.util.spec_from_file_location("resv_rotation", str(RESERVOIR_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rotation_at_import(tmp_path):
    """A pre-existing >10MB log rotates to .1 when the module loads (the
    per-tune spawn path that existed before v6.3.1)."""
    log_file = tmp_path / "delaybuf.log"
    log_file.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    _load(tmp_path)
    assert (tmp_path / "delaybuf.log.1").exists()
    assert not log_file.exists()


def test_rotation_mid_run(resv):
    """The 512th log() call rotates an oversized file — a long-lived stream
    must not grow the log unboundedly. The rotation check runs BEFORE the
    write, so the fresh file holds only the new line."""
    log_file = Path(resv.LOG_FILE)
    log_file.write_bytes(b"x" * (resv.LOG_ROTATE_BYTES + 1))
    resv._log_writes = 511                      # next log() lands on the check boundary
    resv.log("rotate now", stderr=False)
    assert Path(resv.LOG_FILE + ".1").exists()
    assert log_file.stat().st_size < 1024, "new log should hold only the fresh line"
    assert "rotate now" in log_file.read_text()


def test_no_rotation_below_threshold(resv):
    """The periodic check must not rotate a small file."""
    resv.log("small file", stderr=False)
    resv._log_writes = 511
    resv.log("check fires, no rotate", stderr=False)
    assert not Path(resv.LOG_FILE + ".1").exists()
    assert "small file" in Path(resv.LOG_FILE).read_text()
