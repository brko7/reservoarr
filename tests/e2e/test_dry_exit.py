"""6.7.0: an outage that has used up the cushion ends the process, so Dispatcharr
can act straight away; anything shorter or earlier does not. Times on the sim's
edge clock: wall time = edge time - front (see run_pipeline)."""
from __future__ import annotations

import pytest

from .harness import run_pipeline

EXIT_LINE = "upstream down and reservoir dry"


@pytest.mark.e2e
def test_outage_with_no_cushion_exits_within_seconds(tmp_path, synth_ts):
    """The shape of discussion #46: a realtime edge, then EOF and 500s."""
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=50, front_s=0.0,
                       outages=[(20.0, 100.0)])
    log = run.log_text()
    assert run.exited_by_itself, f"still running after the outage:\n{log}"
    assert run.returncode == 0
    assert EXIT_LINE in log
    assert "upstream error HTTPError" in log, "should have kept retrying until then"
    assert 24.0 < run.duration_s < 31.0, f"exit at {run.duration_s:.1f}s, outage at 20s:\n{log}"
    assert "stream wrapper exit" in log
    assert "Traceback" not in run.resv_stderr


@pytest.mark.e2e
def test_outage_is_ridden_out_while_cushion_remains(tmp_path, synth_ts):
    """About 20s banked: the exit waits for it, then follows at once."""
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=60, front_s=25.0,
                       outages=[(30.0, 100.0)])
    log = run.log_text()
    assert run.exited_by_itself, f"still running after the cushion ran out:\n{log}"
    assert EXIT_LINE in log
    assert 20.0 < run.duration_s < 35.0, f"exit at {run.duration_s:.1f}s, outage at 5s:\n{log}"


@pytest.mark.e2e
def test_a_short_outage_with_no_cushion_is_retried_through(tmp_path, synth_ts):
    """3.5s: with the plain backoff the next retry after it comes at 7s, past the exit."""
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=40, front_s=0.0,
                       outages=[(20.0, 3.5)])
    log = run.log_text()
    assert not run.exited_by_itself, f"exited on a short outage:\n{log}"
    assert EXIT_LINE not in log
    assert run.stats_lines()[-1]["reconnects"] >= 1


@pytest.mark.e2e
def test_a_quick_reconnect_with_no_cushion_survives(tmp_path, synth_ts):
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=35, front_s=0.0, eof_at=20.0)
    assert not run.exited_by_itself, run.log_text()
    assert run.stats_lines()[-1]["reconnects"] >= 1


@pytest.mark.e2e
def test_no_exit_before_the_first_byte(tmp_path, synth_ts):
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=15, front_s=0.0,
                       outages=[(0.0, 100.0)])
    log = run.log_text()
    assert not run.exited_by_itself, f"gave up waiting for the first byte:\n{log}"
    assert EXIT_LINE not in log
    assert "upstream error HTTPError" in log


@pytest.mark.e2e
@pytest.mark.parametrize("stall_s", ["25", "0"])
def test_silent_connection_with_no_cushion_exits(tmp_path, synth_ts, stall_s):
    """No EOF and no error, only silence; the watchdog being off changes nothing."""
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=45, front_s=0.0,
                       stalls=[(20.0, 100.0)], extra_env={"RESV_STALL_S": stall_s})
    log = run.log_text()
    assert run.exited_by_itself, log
    assert EXIT_LINE in log
    assert 24.0 < run.duration_s < 31.0, f"exit at {run.duration_s:.1f}s:\n{log}"


@pytest.mark.e2e
def test_the_second_process_on_a_url_rides_out_its_outage(tmp_path, synth_ts):
    """The marker is written as an earlier process of this URL would have."""
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=40, front_s=0.0,
                       outages=[(20.0, 100.0)],
                       claim_exit_first=True)
    log = run.log_text()
    assert not run.exited_by_itself, log
    assert "staying, at most one exit per stream URL" in log
    assert "exiting so Dispatcharr" not in log


@pytest.mark.e2e
def test_zero_keeps_the_process_retrying(tmp_path, synth_ts):
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=35, front_s=0.0,
                       outages=[(20.0, 100.0)], extra_env={"RESV_DRY_EXIT_S": "0"})
    assert not run.exited_by_itself, run.log_text()
    assert EXIT_LINE not in run.log_text()


@pytest.mark.e2e
def test_dry_and_maxgap_show_an_outage_and_stay_flat_without_one(tmp_path, synth_ts):
    run = run_pipeline(tmp_path, synth_ts, rate_bps=300_010, duration_s=62, front_s=0.0,
                       outages=[(35.0, 3.0)])
    stats = run.stats_lines()
    assert not run.exited_by_itself, run.log_text()
    assert all("dry" in s and "maxgap" in s for s in stats), stats[0]["raw"]
    assert stats[0]["maxgap"] < 2.0, stats[0]["raw"]
    assert max(s["maxgap"] for s in stats) >= 2.5, [s["raw"] for s in stats]
    assert max(s["dry"] for s in stats) >= 1.0, [s["raw"] for s in stats]
    # 94KB lumps at 2.4 Mbps: released within a lump's time, never a spell worth
    # counting. min(), since a scheduling hiccup under load can make one.
    assert min(s["dry"] for s in stats) == 0.0, [s["raw"] for s in stats]
