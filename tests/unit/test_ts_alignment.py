"""TS-packet alignment regression test.

Background: r.read() on the upstream HTTP socket returns arbitrary-length byte
runs (whatever happened to be available); the deque accumulates them whole;
next_slice() peels PACE_SLICE bytes off the front. Without alignment, the
runs of bytes written to ffmpeg's stdin do not start at 188-byte TS-packet
boundaries — and ffmpeg's mpegts demuxer assumes they do. The visible symptom
is a flood of `timestamp discontinuity` + AAC `channel element X.Y is not
allocated` errors, which the viewer experiences as audio/video desync.

Discovered 2026-06-16 evening (channel 500157163 cartoons, kids' viewing).
Confirmed by comparing live ffmpeg session telemetry to ffmpeg run offline on
a captured upstream: identical bytes, file-mode reads (naturally 188-aligned)
produced 0 disc per minute; live-mode reads (paced, arbitrary-length writes)
produced 100+/min. Soaked clean for 5 days on tigar after the fix.

The fix is `align_to_188(tail, chunk) -> (aligned, new_tail)`. These tests
exercise it directly:

- byte-perfect reconstruction: every input byte exits eventually, in order.
- output is ALWAYS a 188-multiple. Never partial packets.
- worst-case pathological inputs (1-byte chunks, prime-length chunks, alternating
  large/tiny) still preserve invariants.
- the empty-input edge cases don't crash.

Pre-fix behaviour: there was no alignment function; ffmpeg was fed `d` as-is.
A plain unit-test that asserts the helper exists is enough to fail on any
revert that drops it.
"""
from __future__ import annotations

import pytest


def test_align_function_exists(resv):
    """Pre-fix code had no align helper. A revert that drops it must fail loudly."""
    assert hasattr(resv, "align_to_188"), (
        "align_to_188 missing — TS alignment fix has been reverted. "
        "Without it, ffmpeg sees mid-packet writes and emits spurious "
        "timestamp-discontinuity + AAC channel-element errors → user-visible "
        "audio/video desync. See CHANGELOG v6.2.1 for incident detail."
    )


def test_aligned_output_is_always_188_multiple(resv):
    """Property #1: every byte string emitted to ffmpeg must be a 188-multiple."""
    tail = b""
    chunks = [b"\xff" * n for n in (47, 188, 189, 1, 376, 5000, 188 * 64)]
    for c in chunks:
        aligned, tail = resv.align_to_188(tail, c)
        assert len(aligned) % 188 == 0, f"unaligned write of {len(aligned)} bytes from input len {len(c)}"


def test_byte_perfect_reconstruction(resv):
    """Property #2: input bytes must equal output bytes plus the carried tail.
    No data may be lost; no data may be duplicated; order must be preserved."""
    inputs = [
        b"A" * 47,         # less than one packet
        b"B" * 141,        # 47+141 = 188, will emit one full packet
        b"C" * 564,        # already 188*3
        b"D" * 1,          # single byte; tail += 1
        b"E" * 187,        # tail+1+187 = 189, emit 188, carry 1
        b"F" * 188 * 64,   # full PACE_SLICE worth
        b"G" * 47,         # tail re-accrues
    ]
    expected = b"".join(inputs)

    tail = b""
    out = []
    for c in inputs:
        aligned, tail = resv.align_to_188(tail, c)
        out.append(aligned)
    out.append(tail)
    actual = b"".join(out)

    assert actual == expected, (
        f"data lost or reordered: expected {len(expected)} bytes, got {len(actual)}"
    )


def test_pathological_one_byte_chunks(resv):
    """Adversarial: a peer that delivers one byte at a time. No emit until 188
    accumulate; tail must hold the remainder."""
    tail = b""
    emitted = b""
    for i in range(187):
        aligned, tail = resv.align_to_188(tail, bytes([i & 0xFF]))
        emitted += aligned
    assert emitted == b"", "emitted bytes before reaching 188-byte threshold"
    assert len(tail) == 187

    aligned, tail = resv.align_to_188(tail, b"\xff")
    assert len(aligned) == 188, f"expected single 188-byte packet, got {len(aligned)}"
    assert tail == b""


def test_empty_chunk_passes_through(resv):
    """Empty input: tail unchanged, no emit. The reservoir's pacing loop can
    legitimately call this with empty d during shutdown drain."""
    tail = b"X" * 50
    aligned, new_tail = resv.align_to_188(tail, b"")
    assert aligned == b""
    assert new_tail == tail


def test_no_tail_no_input(resv):
    """Both empty: degenerate case, must not crash or emit anything."""
    aligned, tail = resv.align_to_188(b"", b"")
    assert aligned == b""
    assert tail == b""


@pytest.mark.parametrize("chunk_size", [1, 47, 187, 188, 189, 1024, 12031, 12032, 65536, 188 * 512])
def test_aligned_lengths_across_chunk_sizes(resv, chunk_size):
    """Across realistic and adversarial r.read() return sizes, alignment holds."""
    tail = b""
    aligned, tail = resv.align_to_188(tail, b"X" * chunk_size)
    assert len(aligned) % 188 == 0
    assert len(aligned) + len(tail) == chunk_size
    assert len(tail) < 188, f"tail of {len(tail)} bytes >= 188 — should have been emitted"


def test_long_stream_bounded_tail(resv):
    """Over a long sequence of arbitrary-sized writes, the tail is always strictly
    less than 188 bytes (i.e., we never accumulate without flushing)."""
    import random
    rng = random.Random(0xDEADBEEF)
    tail = b""
    total_in = 0
    total_out = 0
    for _ in range(2000):
        n = rng.randint(1, 200000)
        c = b"\x47" * n                                       # 0x47 just to look like real TS sync bytes
        total_in += n
        aligned, tail = resv.align_to_188(tail, c)
        total_out += len(aligned)
        assert len(tail) < 188
    assert total_in == total_out + len(tail)
