"""F1 fix: a stall (no-flush) reconnect must never SUPPRESS or DOWNGRADE a
corrupt-loop flush. Each reconnect class is debounced independently (≤1 per 90s
per class) and the flush request is sticky (set by a flush caller, consumed by
the fetcher).

Ported from tmp/test_reconnect_precedence.py in superlab. Since 6.4.0 these
drive the real Upstream.request_reconnect() / take_request() pair instead of a
copy of the fetcher's flush block."""
from __future__ import annotations

import pytest


@pytest.fixture
def up(stream):
    return stream.upstream


def test_nothing_requested_takes_nothing(up):
    assert up.take_request() is None


def test_corrupt_only_fires_and_flushes(up):
    assert up.request_reconnect("t:corrupt", flush=True) is True
    assert up.take_request() is True


def test_stall_only_fires_without_flush(up):
    assert up.request_reconnect("t:stall", flush=False) is True
    assert up.take_request() is False


def test_corrupt_fires_despite_recent_stall(up):
    """F1: a stall (no-flush) within the 90s debounce window must NOT block
    a subsequent corrupt flush. Per-class debounce is the whole point."""
    up.request_reconnect("t:stall", flush=False)
    up.take_request()
    assert up.request_reconnect("t:corrupt", flush=True) is True
    assert up.take_request() is True


def test_concurrent_stall_does_not_downgrade_flush(up):
    """Race: corrupt fires (sets flush_pending), stall fires before the fetcher
    consumes. flush_pending must stay True; the eventual take flushes."""
    up.request_reconnect("t:corrupt", flush=True)
    up.request_reconnect("t:stall", flush=False)
    assert up.flush_requested is True
    assert up.take_request() is True
    assert up.take_request() is None


def test_two_corrupts_within_90s_second_debounced(up):
    up.request_reconnect("t:c1", flush=True)
    assert up.request_reconnect("t:c2", flush=True) is False


def test_two_stalls_within_90s_second_debounced(up):
    up.request_reconnect("t:s1", flush=False)
    assert up.request_reconnect("t:s2", flush=False) is False


def test_the_debounce_starts_open_right_after_boot(up, resv, monkeypatch):
    """The anchors are -inf: monotonic time can be under 90s after boot, and a
    0.0 anchor would rate-limit the first forced reconnect then."""
    monkeypatch.setattr(resv.time, "monotonic", lambda: 5.0)
    assert up.request_reconnect("t:boot", flush=True) is True


def test_a_third_identical_corrupt_dts_requests_a_flush(up):
    up.register_corrupt(501541200)
    up.register_corrupt(42)
    up.register_corrupt(501541200)
    assert up.take_request() is None
    up.register_corrupt(501541200)
    assert up.take_request() is True
