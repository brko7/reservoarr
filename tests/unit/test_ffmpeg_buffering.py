"""`-fflags +nobuffer` costs the whole first GOP of video (invariant #12).

The demuxer hands packets on before it has the video's parameter sets, so
ffmpeg emits audio from the first sample and video only from the next IDR.
Replayed offline through the exact FFMPEG_CMD, one captured channel with an
8.3s GOP produced:

    with    +nobuffer -> first audio 0.63s, first video 8.38s, ffprobe width=0
    without +nobuffer -> first audio 0.63s, first video 0.68s, 1280x720 High

Downstream that seam is longer than every default probe window, which is what
turns it into a blank picture with sound.
"""
from __future__ import annotations


def test_nobuffer_is_not_in_the_ffmpeg_command(resv):
    assert "+nobuffer" not in resv.FFMPEG_CMD


def test_the_probe_window_is_still_small(resv):
    """The flag is gone, the low-latency probe settings stay: those are what
    keep the tune inside Plex's tuner timeout."""
    cmd = resv.FFMPEG_CMD
    assert cmd[cmd.index("-analyzeduration") + 1] == "1000000"
    assert cmd[cmd.index("-probesize") + 1] == "500000"
