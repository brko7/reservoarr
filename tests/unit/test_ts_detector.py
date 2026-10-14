"""#5 ingest-side TS-corruption detector: replay the rule against the real
2026-06-14 incident signature, plus baselines and clean reconnect/gap seams.

The rule (CorruptionDetector.window(), once per stats window):
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


def fires(resv, windows):
    """windows: list of per-window (dccerr, dsync, ingest_adv). Feeds the real
    CorruptionDetector the cumulative counters the stats loop hands it and
    returns the indices where the rule trips."""
    det = resv.CorruptionDetector()
    cc = sy = total = 0
    out = []
    for i, (dcc, dsy, adv) in enumerate(windows):
        cc, sy, total = cc + dcc, sy + dsy, total + adv
        if det.window(cc, sy, total) is not None:
            out.append(i)
    return out


# Real 2026-06-14 incident, per-15s (dccerr, dsync) deltas. Bytes were flowing
# throughout. Labels are the actual stats-line timestamps from delaybuf.log.
LABELS = ["08:03:33", "08:03:48", "08:04:03", "08:04:18", "08:04:33", "08:04:48",
          "08:05:03", "08:05:18", "08:05:33", "08:05:48", "08:06:03", "08:06:18"]
INCIDENT = [(5, 2), (10, 2), (5, 1), (0, 1), (5, 2), (10, 2),
            (10, 2), (0, 1), (5, 0), (0, 0), (0, 0), (0, 0)]


def test_fires_30s_before_stderr_on_real_incident(resv):
    windows = [(dcc, dsy, 1) for (dcc, dsy) in INCIDENT]
    indices = fires(resv, windows)
    assert indices, "rule never fired on the real incident"
    first_label = LABELS[indices[0]]
    assert first_label == "08:03:48", (
        f"first fire at {first_label}, expected 08:03:48 — 30s before stderr (08:04:18)"
    )


def test_no_false_positives_on_clean_baseline(resv):
    """20 windows of zero errors with bytes flowing — the production baseline."""
    assert fires(resv, [(0, 0, 1)] * 20) == []


def test_no_false_positives_on_clean_reconnect_seam(resv):
    """A clean reconnect resets PCR but leaves CC/sync flat-zero; calibrated
    on 2026-06-14 evidence. This is the critical false-positive guard."""
    assert fires(resv, [(0, 0, 1)] * 5) == []


def test_no_false_positives_on_clean_cdn_gap(resv):
    """CDN burst-gap: no errors AND no ingest advance. ingest_adv > 0 gate
    prevents these from being counted as bad windows."""
    assert fires(resv, [(0, 0, 0)] * 5) == []


def test_single_bad_window_does_not_fire(resv):
    """sustain=2: a one-shot burst with a clean window before/after must not fire."""
    assert fires(resv, [(0, 0, 1), (5, 0, 1), (0, 0, 1)]) == []


def test_sync_loss_only_can_fire(resv):
    """The OR-gate: 2 sync losses per window for 2 windows fires even with
    zero CC errors."""
    assert fires(resv, [(0, 2, 1), (0, 2, 1)]) == [1]


def test_the_detail_names_the_window(resv):
    det = resv.CorruptionDetector()
    assert det.window(5, 0, 100) is None
    assert det.window(15, 1, 200) == "+10 ccerr/+1 sync this window x2 windows"
