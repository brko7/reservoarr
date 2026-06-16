"""TsParser unit tests. Real synthetic TS fixture for ground-truth comparisons;
hand-crafted byte buffers for spec-edge tests (CC wrap, sync recovery, PCR
garbage rejection)."""
from __future__ import annotations


def feed_file(parser, mod, path, chunk=188 * 512):
    """Replay a TS file through the parser the way the fetcher would, advancing
    the module's in_total global so cushion marks land correctly."""
    data = path.read_bytes()
    for i in range(0, len(data), chunk):
        c = data[i:i + chunk]
        mod.in_total += len(c)
        parser.feed(c)
    return data


# ----- ground truth against the synthetic fixture -----

def test_synth_pcr_matches_ffprobe(resv, synth_ts):
    """cum_pcr from the parser must match the file's true duration. Earned by
    the v6 calibration session — within 0.9% on real captures."""
    p = resv.TsParser()
    feed_file(p, resv, synth_ts)
    assert abs(p.cum_pcr - 180.0) < 0.5, f"cum_pcr={p.cum_pcr} (want ~180.0)"


def test_synth_content_rate_matches_muxrate(resv, synth_ts):
    """content_rate is the pacing reference; it must reflect the true byte rate,
    not the per-chunk arrival rate (the v5 bug). Synthetic fixture is muxed at
    2400kbps; parser should land near it."""
    p = resv.TsParser()
    feed_file(p, resv, synth_ts)
    rate_bps = p.content_rate() * 8
    assert 2.0e6 < rate_bps < 2.6e6, f"content_rate={rate_bps / 1e6:.3f}Mbps (want ~2.4)"


def test_synth_no_anomalies(resv, synth_ts):
    """A clean fixture must produce flat-zero ccerr/sync/pcrrej. Healthy baseline:
    any nonzero rate on a clean stream is the false-positive case v6's calibration
    proved impossible."""
    p = resv.TsParser()
    feed_file(p, resv, synth_ts)
    assert p.cc_errors == 0
    assert p.sync_losses == 0
    assert p.pcr_rejects == 0


def test_pcr_pid_locks_to_first_carrier(resv, synth_ts):
    p = resv.TsParser()
    feed_file(p, resv, synth_ts)
    assert p.pcr_pid is not None
    # Typical libavformat-muxed TS uses PID 256 for the video PES, which carries PCR.
    assert p.pcr_pid == 256, f"pcr_pid={p.pcr_pid}"


def test_cushion_s_shrinks_as_released(resv, synth_ts):
    """cushion_s(released_total) drains as bytes are released. Returns None
    if the released cursor is before the first PCR mark (pre-lock seam)."""
    p = resv.TsParser()
    feed_file(p, resv, synth_ts)
    # The first cushion mark sits at the in_total of the first valid PCR delta —
    # not zero (a few packets of PMT/PAT precede the first PCR). Use that as
    # the "released nothing past first mark" baseline.
    first_mark_in_total = p.cush_marks[0][0]
    full = p.cushion_s(first_mark_in_total)
    half = p.cushion_s(resv.in_total // 2)
    near_end = p.cushion_s(resv.in_total)
    assert full is not None and full > 170, f"full cushion={full}"
    # Allow some slack: cushion is keyed to (in_total, cum_pcr) marks, not exact bytes.
    assert half is not None and 50 < half < full, f"half={half} full={full}"
    assert near_end is None or near_end < 5, f"near_end={near_end}"


# ----- spec-edge tests with hand-crafted packets -----

def _ts_packet(pid=0x100, cc=0, payload_unit=False, adaptation=False,
               discontinuity=False, pcr_base=None, pcr_ext=0):
    """Build a single 188-byte TS packet. payload_unit toggles AFC bit 0 (payload
    present), adaptation toggles bit 1. PCR is optional via the adaptation field."""
    afc = (0x2 if adaptation else 0) | (0x1 if payload_unit else 0)
    if not afc:
        afc = 0x1   # default payload-only
    pkt = bytearray(188)
    pkt[0] = 0x47
    pkt[1] = (pid >> 8) & 0x1F
    pkt[2] = pid & 0xFF
    pkt[3] = (afc << 4) | (cc & 0x0F)
    body_start = 4
    if afc & 0x2:                                             # adaptation field
        # length byte, then flags byte, then optionally PCR (6 bytes), then stuffing
        af_payload = bytearray([0])                           # flags
        if discontinuity:
            af_payload[0] |= 0x80
        if pcr_base is not None:
            af_payload[0] |= 0x10
            b = pcr_base & ((1 << 33) - 1)
            e = pcr_ext & 0x1FF
            af_payload += bytes([
                (b >> 25) & 0xFF,
                (b >> 17) & 0xFF,
                (b >> 9) & 0xFF,
                (b >> 1) & 0xFF,
                ((b & 0x1) << 7) | 0x7E | ((e >> 8) & 0x01),
                e & 0xFF,
            ])
        af_len = len(af_payload)
        pkt[4] = af_len
        pkt[5:5 + af_len] = af_payload
        body_start = 5 + af_len
    # Fill the rest with payload bytes (anything non-zero — TsParser only inspects header).
    for i in range(body_start, 188):
        pkt[i] = 0xFF
    return bytes(pkt)


def test_cc_continuity_increments_normally(resv):
    p = resv.TsParser()
    p.synced = True                                           # skip sync hunt
    for cc in (3, 4, 5, 6, 7):
        p.feed(_ts_packet(cc=cc))
    assert p.cc_errors == 0


def test_cc_wraps_at_f_to_0(resv):
    p = resv.TsParser()
    p.synced = True
    for cc in (0xE, 0xF, 0x0, 0x1):
        p.feed(_ts_packet(cc=cc))
    assert p.cc_errors == 0


def test_cc_jump_counts_as_error(resv):
    p = resv.TsParser()
    p.synced = True
    p.feed(_ts_packet(cc=3))
    p.feed(_ts_packet(cc=7))                                  # jumped 4 — error
    assert p.cc_errors == 1


def test_cc_duplicate_allowed_once(resv):
    """ISO 13818-1: one packet duplicate is legal; a second is an error."""
    p = resv.TsParser()
    p.synced = True
    p.feed(_ts_packet(cc=3))
    p.feed(_ts_packet(cc=3))                                  # one dup — fine
    assert p.cc_errors == 0
    p.feed(_ts_packet(cc=3))                                  # second dup — error
    assert p.cc_errors == 1


def test_cc_jump_with_discontinuity_flag_is_legal(resv):
    """discontinuity_indicator in the adaptation field means the CC jump is
    intentional (e.g. SCTE-35 splice) — must NOT count as an error."""
    p = resv.TsParser()
    p.synced = True
    p.feed(_ts_packet(cc=3))
    p.feed(_ts_packet(cc=9, adaptation=True, discontinuity=True))
    assert p.cc_errors == 0


def test_null_pid_skipped(resv):
    """PID 0x1FFF (null/stuffing) has undefined CC per spec; never count."""
    p = resv.TsParser()
    p.synced = True
    p.feed(_ts_packet(pid=0x1FFF, cc=3))
    p.feed(_ts_packet(pid=0x1FFF, cc=15))                     # jump — must NOT register
    assert p.cc_errors == 0


def test_sync_byte_recovery(resv):
    """Garbage prefix then 3 consecutive 0x47 at 188 stride should re-sync."""
    p = resv.TsParser()
    # 50 garbage bytes, then a real packet, then two more
    garbage = bytes(range(50))
    pkt = _ts_packet(cc=3)
    p.feed(garbage + pkt + pkt + pkt)
    assert p.synced


def test_pcr_garbage_delta_rejected(resv):
    """A PCR delta > 10s (or negative) is the corruption family that froze
    ffmpeg -re. Must be rejected (counted as pcrrej) and not advance cum_pcr."""
    p = resv.TsParser()
    p.synced = True
    p.feed(_ts_packet(pid=0x100, cc=0, adaptation=True, pcr_base=27_000_000))     # ~1s into stream
    before = p.cum_pcr
    # Jump 100s into the future — outside the 0<Δ<10s plausibility window.
    p.feed(_ts_packet(pid=0x100, cc=1, adaptation=True, pcr_base=27_000_000 * 101))
    assert p.pcr_rejects == 1
    assert p.cum_pcr == before


def test_pcr_normal_delta_accepted(resv):
    """A plausible PCR delta (~0.04s — one frame at 25fps) is normal."""
    p = resv.TsParser()
    p.synced = True
    # 90kHz units: 0.04s = 3600 ticks; pcr_base is in 90kHz, pcr resolution is base*300+ext at 27MHz.
    # We pass pcr_base directly; cum_pcr is computed from base*300+ext / 27e6.
    p.feed(_ts_packet(pid=0x100, cc=0, adaptation=True, pcr_base=90000))          # t=1.0s
    p.feed(_ts_packet(pid=0x100, cc=1, adaptation=True, pcr_base=90000 + 3600))   # +0.04s
    assert p.pcr_rejects == 0
    assert 0.03 < p.cum_pcr < 0.05, f"cum_pcr={p.cum_pcr}"


def test_pcr_reject_run_re_anchors(resv):
    """After > 25 consecutive rejects, the parser must re-anchor on the live
    PCR sample (a wedged chain on accepted garbage). Prevents permanent dropout."""
    p = resv.TsParser()
    p.synced = True
    p.feed(_ts_packet(pid=0x100, cc=0, adaptation=True, pcr_base=90000))
    # 26 garbage rejects in a row (delta way above 10s each).
    for i in range(26):
        p.feed(_ts_packet(pid=0x100, cc=(i + 1) & 0xF, adaptation=True,
                          pcr_base=90000 + (i + 1) * 27_000_000 * 100))
    # After the run, a small-delta sample should now ANCHOR (last_pcr updated to
    # the most recent reject), so a follow-up delta-3600 sample is accepted from
    # that new anchor.
    last_anchor = 90000 + 26 * 27_000_000 * 100
    p.feed(_ts_packet(pid=0x100, cc=11, adaptation=True, pcr_base=last_anchor + 3600))
    assert p.pcr_rejects == 26                                # the reanchoring sample itself wasn't counted again
    assert p.reject_run == 0                                  # cleared on the accepted small-delta sample


def test_only_first_pcr_pid_used(resv):
    """If the stream advertises PCR on multiple PIDs, only the first one we
    saw counts (`one clock only`)."""
    p = resv.TsParser()
    p.synced = True
    p.feed(_ts_packet(pid=0x100, cc=0, adaptation=True, pcr_base=90000))
    p.feed(_ts_packet(pid=0x100, cc=1, adaptation=True, pcr_base=90000 + 3600))
    cum_after = p.cum_pcr
    # Different PID with adaptation+PCR — must be ignored, cum unchanged.
    p.feed(_ts_packet(pid=0x200, cc=0, adaptation=True, pcr_base=90000 + 90000))
    assert p.cum_pcr == cum_after
    assert p.pcr_pid == 0x100


def test_content_rate_floor(resv):
    """content_rate must never report below RATE_FLOOR (1Mbps). v5 had a path
    where an arrival-rate-overestimate cancelled the floor; v6 enforces it on
    every return."""
    p = resv.TsParser()
    p.synced = True
    # Build a parser with two PCR marks 1s apart and only 1 byte advance — would
    # naively report 8 bits/sec. RATE_FLOOR must dominate.
    p.feed(_ts_packet(pid=0x100, cc=0, adaptation=True, pcr_base=90000))          # t=1s
    # Snapshot the rate_marks tuples and force the in_total values low by hand.
    p.rate_marks.clear()
    p.rate_marks.append((0, 0.0))
    p.rate_marks.append((1, 2.0))                              # span 2s > 1.5s startup guard
    p.cum_pcr = 2.0
    rate = p.content_rate()
    assert rate == resv.RATE_FLOOR
