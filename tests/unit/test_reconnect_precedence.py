"""F1 fix: a stall (no-flush) reconnect must never SUPPRESS or DOWNGRADE a
corrupt-loop flush. Each reconnect class is debounced independently (≤1 per 90s
per class) and the flush request is sticky (set by a flush caller, consumed by
the fetcher).

Ported from tmp/test_reconnect_precedence.py in superlab — the original ran in
the dispatcharr container; here it runs against the standalone repo's
reservoarr.py via the conftest fixture."""
from __future__ import annotations


def _fetcher_consume(mod):
    """Mimic the fetcher's flush block: check force_reconnect, drain
    flush_pending if set, clear the event. Returns True if it flushed, False
    if it just reconnected, None if nothing to do."""
    if not mod.force_reconnect.is_set():
        return None
    do_flush = mod.flush_pending
    if do_flush:
        mod.flush_pending = False
    mod.force_reconnect.clear()
    return do_flush


def _reset(mod):
    mod.last_forced_flush = 0.0
    mod.last_forced_stall = 0.0
    mod.flush_pending = False
    mod.force_reconnect.clear()


def test_corrupt_only_fires_and_flushes(resv):
    _reset(resv)
    assert resv.force_upstream_reconnect("t:corrupt", flush=True) is True
    assert _fetcher_consume(resv) is True


def test_stall_only_fires_without_flush(resv):
    _reset(resv)
    assert resv.force_upstream_reconnect("t:stall", flush=False) is True
    assert _fetcher_consume(resv) is False


def test_corrupt_fires_despite_recent_stall(resv):
    """F1: a stall (no-flush) within the 90s debounce window must NOT block
    a subsequent corrupt flush. Per-class debounce is the whole point."""
    _reset(resv)
    resv.force_upstream_reconnect("t:stall", flush=False)
    _fetcher_consume(resv)
    assert resv.force_upstream_reconnect("t:corrupt", flush=True) is True
    assert _fetcher_consume(resv) is True


def test_concurrent_stall_does_not_downgrade_flush(resv):
    """Race: corrupt fires (sets flush_pending), stall fires before the fetcher
    consumes. flush_pending must stay True; the eventual consume flushes."""
    _reset(resv)
    resv.force_upstream_reconnect("t:corrupt", flush=True)
    resv.force_upstream_reconnect("t:stall", flush=False)
    assert resv.flush_pending is True
    assert _fetcher_consume(resv) is True


def test_two_corrupts_within_90s_second_debounced(resv):
    _reset(resv)
    resv.force_upstream_reconnect("t:c1", flush=True)
    assert resv.force_upstream_reconnect("t:c2", flush=True) is False


def test_two_stalls_within_90s_second_debounced(resv):
    _reset(resv)
    resv.force_upstream_reconnect("t:s1", flush=False)
    assert resv.force_upstream_reconnect("t:s2", flush=False) is False
