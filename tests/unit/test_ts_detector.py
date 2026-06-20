"""#5 ingest-side TS-corruption detector: replay the rule against the real
2026-06-14 incident signature, plus baselines and clean reconnect/gap seams.

The rule (per reservoarr.py main()'s stats loop):
  if ingest_adv > 0 and (dccerr >= 3 or dsync >= 2):
      bad_wins += 1
  else:
      bad_wins = 0
  if bad_wins >= 2:
      fire(); bad_wins = 0

Ported from tmp/test_ts_detector.py in superlab. Asserts:
- Fires at 08:03:48 on the real incident (~30s before the ffmpeg-stderr trigger at 08:04:18)
- Zero false positives on clean baseline / clean reconnect seam / clean gap
"""
from __future__ import annotations


def fires(windows, cc_th=3, sync_th=2, sustain=2):
    """windows: list of (dccerr, dsync, ingest_adv). Returns the indices where
    the rule trips. Mirrors reservoarr.py exactly: reset bad_wins after firing."""
    bad = 0
    out = []
    for i, (dcc, dsy, adv) in enumerate(windows):
        if adv > 0 and (dcc >= cc_th or dsy >= sync_th):
            bad += 1
        else:
            bad = 0
        if bad >= sustain:
            out.append(i)
            bad = 0
    return out


# Real 2026-06-14 incident, per-15s (dccerr, dsync) deltas. Bytes were flowing
# throughout. Labels are the actual stats-line timestamps from delaybuf.log.
LABELS = ["08:03:33", "08:03:48", "08:04:03", "08:04:18", "08:04:33", "08:04:48",
          "08:05:03", "08:05:18", "08:05:33", "08:05:48", "08:06:03", "08:06:18"]
INCIDENT = [(5, 2), (10, 2), (5, 1), (0, 1), (5, 2), (10, 2),
            (10, 2), (0, 1), (5, 0), (0, 0), (0, 0), (0, 0)]


def test_fires_30s_before_stderr_on_real_incident():
    windows = [(dcc, dsy, 1) for (dcc, dsy) in INCIDENT]
    indices = fires(windows)
    assert indices, "rule never fired on the real incident"
    first_label = LABELS[indices[0]]
    assert first_label == "08:03:48", (
        f"first fire at {first_label}, expected 08:03:48 — 30s before stderr (08:04:18)"
    )


def test_no_false_positives_on_clean_baseline():
    """20 windows of zero errors with bytes flowing — the production baseline."""
    assert fires([(0, 0, 1)] * 20) == []


def test_no_false_positives_on_clean_reconnect_seam():
    """A clean reconnect resets PCR but leaves CC/sync flat-zero; calibrated
    on 2026-06-14 evidence. This is the critical false-positive guard."""
    assert fires([(0, 0, 1)] * 5) == []


def test_no_false_positives_on_clean_cdn_gap():
    """CDN burst-gap: no errors AND no ingest advance. ingest_adv > 0 gate
    prevents these from being counted as bad windows."""
    assert fires([(0, 0, 0)] * 5) == []


def test_single_bad_window_does_not_fire():
    """sustain=2: a one-shot burst with a clean window before/after must not fire."""
    assert fires([(0, 0, 1), (5, 0, 1), (0, 0, 1)]) == []


def test_sync_loss_only_can_fire():
    """The OR-gate: 2 sync losses per window for 2 windows fires even with
    zero CC errors."""
    assert fires([(0, 2, 1), (0, 2, 1)]) == [1]
