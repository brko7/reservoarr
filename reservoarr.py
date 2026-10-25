#!/usr/bin/env python3
"""Delay-buffer stream profile entrypoint for Dispatcharr.

The IPTorrents CDN delivers live TS in ~10s bursts with occasional 10-20s
gaps; Plex Live TV tolerates only a few seconds of input starvation. This
wrapper drains the CDN socket eagerly into a RAM reservoir and releases it
to ffmpeg at the stream's content rate, so playback runs ~30s behind live
and gaps shorter than the cushion are invisible to Plex.

Pacing is done HERE, not with ffmpeg -re: the provider's streams carry
occasional corrupt packets with garbage DTS values, and -re sleeps on them
(observed 25s output freeze with a full reservoir). Release is executed as
byte-rate sleeps; the *rate reference* is bytes-per-PCR-second measured on
ingest with outlier rejection, so a garbage timestamp is dropped instead of
slept on. v5 paced against the measured *arrival* rate, which squandered
the CDN's per-connection front-load burst (released at 5-7 Mbps chasing the
inflated arrival rate) and could never rebuild a cushion at steady state
(arrival averages exactly realtime; the 0.97 floor was cancelled by a gap
exclusion bias in the rate window). Pacing at the PCR-derived content rate
banks the front-load surplus by construction - on every connect, including
the reconnect after a corrupt-loop flush.

Invoked by Dispatcharr as: reservoarr.py {streamUrl} {userAgent}
stdout -> Dispatcharr relay pipe (must carry ONLY the TS stream)
stderr -> Dispatcharr's transcode logger (INFO only for lines containing
          stream/input/output/video/audio; everything else lands at DEBUG)
stats  -> /data/scripts/logs/delaybuf.log

Dispatcharr stops the channel by signalling THIS pid only: on SIGTERM we
reap ffmpeg; on SIGKILL the broken pipes make ffmpeg exit on its own.

Layout: a Stream owns one tune. Its Upstream (fetcher thread) cuts each
connection into whole TS packets (PacketAligner), passes them through the
optional ReplaySkip gate into the Reservoir and the TsParser; Stream.pump()
takes them out through the Pacer into ffmpeg's stdin. The stall watchdog,
the corrupt-loop watcher on ffmpeg's stderr and the #5 CorruptionDetector
ask the Upstream to reconnect. Settings are module constants read from the
environment at import.
"""
import fcntl
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import NamedTuple

if len(sys.argv) < 2 or not sys.argv[1].strip():
    sys.stderr.write("[delaybuf] usage: reservoarr.py <streamUrl> [userAgent]\n")
    sys.exit(2)
URL = sys.argv[1]
UA = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else "Mozilla/5.0"

CHUNK = 188 * 512                                                     # TS-aligned ~94KB reads
PACE_SLICE = 188 * 64                                                 # ~12KB write slices for smooth pacing
PREFILL_BYTES = int(os.getenv("RESV_PREFILL_BYTES", str(1536 * 1024)))
PREFILL_MAX_S = float(os.getenv("RESV_PREFILL_MAX_S", "3"))           # sniff only: Plex tuner bails ~15s
MAX_BYTES = int(os.getenv("RESV_MAX_BYTES", str(256 * 1024 * 1024)))  # reservoir hard cap
HEADSTART_S = float(os.getenv("RESV_HEADSTART_S", "5"))               # content-seconds released unpaced at start
TARGET_S = float(os.getenv("RESV_TARGET_S", "30"))                    # reservoir level to hold
GRACE_S = float(os.getenv("RESV_GRACE_S", "45"))                     # floor 1.0 this long, then FLOOR (see release_factor)
# Lowest release rate, as a multiple of the content rate, once GRACE_S is over.
# Under 1.0 the reservoir banks content by feeding the player below realtime,
# and every banked second comes out of the player's own buffer: a client with
# a small live buffer (Channels, TiviMate) stutters until the cushion reaches
# TARGET_S. The fixed 0.97 before 6.5.0 did that on every tune whose front-load
# fell short of the target (discussion #46). Worth it only where the client
# buffers deeply but the tuner dies on input silence (Plex), on a CDN whose
# catch-up bursts do not rebuild the cushion. A decimal in (0, 1]; else 1.0.
FLOOR_REQUESTED = os.getenv("RESV_FLOOR", "").strip()
FLOOR_OK = re.fullmatch(r"0?\.\d+|1(\.0*)?", FLOOR_REQUESTED, re.ASCII) is not None and float(FLOOR_REQUESTED) > 0
FLOOR = float(FLOOR_REQUESTED) if FLOOR_OK else 1.0
RATE_WINDOW_S = 120.0                                                 # arrival-rate fallback window
RATE_FLOOR = 125_000                                                  # 1 Mbps floor (bytes/s)
STATS_EVERY_S = 15.0
# Ingest-side TS-corruption detector (#5): a sustained CC/sync error rate while
# data still flows = wedged/corrupt upstream, detectable ~30s before the ffmpeg
# stderr loop trigger. Log-only ("would-fire") until a 2nd real episode confirms
# the threshold; set RESV_TS_RECONNECT=1 to arm the forced reconnect.
TS_RECONNECT = os.getenv("RESV_TS_RECONNECT", "0") == "1"
CC_ERR_PER_WIN = int(os.getenv("RESV_CC_ERR_PER_WIN", "3"))           # CC errors per stats window = flagged
SYNC_ERR_PER_WIN = int(os.getenv("RESV_SYNC_ERR_PER_WIN", "2"))       # sync losses per stats window = flagged
TS_SUSTAIN_WINS = int(os.getenv("RESV_TS_SUSTAIN_WINS", "2"))         # consecutive flagged windows before acting
STALL_S = float(os.getenv("RESV_STALL_S", "25"))                      # no-ingest watchdog (#4); >CDN burst-gap, <urlopen 30s; 0=off
GIVEUP_TRIES = int(os.getenv("RESV_GIVEUP_TRIES", "0"))               # exit after N no-data upstream tries so Dispatcharr fails over; 0=retry forever
REJECT_MP4 = os.getenv("RESV_REJECT_MP4", "0") == "1"
AUDIO_ARGS = {
    "ac3": ["-c:a", "ac3", "-b:a", "192k", "-ac", "2"],
    "aac": ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000"],
}
AUDIO_CODEC_REQUESTED = os.getenv("RESV_AUDIO_CODEC", "ac3").strip().lower()
AUDIO_CODEC = AUDIO_CODEC_REQUESTED if AUDIO_CODEC_REQUESTED in AUDIO_ARGS else "ac3"
AUDIO_FILL_GAPS = os.getenv("RESV_AUDIO_FILL_GAPS", "0") == "1"
REPLAY_SKIP = os.getenv("RESV_REPLAY_SKIP", "0") == "1"
REPLAY_MAX_S = float(os.getenv("RESV_REPLAY_MAX_S", "60"))
REPLAY_HOLD_BYTES = 1024 * 1024
REPLAY_MIN_CUSHION_S = 3.0
REPLAY_STEP_MAX_S = 10.0
TS_WRAP_S = (1 << 33) / 90000.0                                       # PCR base wraps every ~26.5h
VIDEO_STREAM_TYPES = frozenset({0x01, 0x02, 0x10, 0x1B, 0x24, 0x42, 0xEA})
AUDIO_STREAM_TYPES = frozenset({0x03, 0x04, 0x0F, 0x11, 0x81, 0x87})
AUDIO_DESCRIPTOR_TAGS = frozenset({0x6A, 0x7A, 0x7C})
STREAM_HOLE_S = 0.5
TRACK_MISSING_AFTER_S = 5.0
PSI_MAX_BYTES = 1024
PES_HEAD_BYTES = 19

FFMPEG_BIN = os.getenv("RESV_FFMPEG_BIN", "/usr/local/bin/ffmpeg")    # Dispatcharr AIO container default
# Dispatcharr's buffering watchdog only reads `speed=` off a stderr line that also
# carries `frame=`, and `-loglevel warning` hides ffmpeg's progress line. Off by
# default: turning it on hands Dispatcharr the signal it needs to fail a starving
# feed over to the next stream, at the cost of a progress line every 0.5s.
FFMPEG_STATS = os.getenv("RESV_FFMPEG_STATS", "0") == "1"
# Three lowercase letters (ISO 639-2) or nothing: ffmpeg's mpegts muxer writes
# any other value as no tag at all, and "FRA" is stored as typed.
AUDIO_LANG = os.getenv("RESV_AUDIO_LANG", "").strip().lower()
AUDIO_LANG_OK = re.fullmatch(r"[a-z]{3}", AUDIO_LANG) is not None
# No `-fflags +nobuffer`: it costs the whole first GOP of video. The demuxer
# hands packets on before it has the video's parameter sets, so ffmpeg emits
# audio from the first sample and video only from the next IDR. On a channel
# with an 8.3s GOP the muxed output opened with 7.75s of audio and no picture,
# and every default probe window downstream expired first - ffprobe reported
# width=0, players showed a blank frame with sound. Dropping the flag put the
# same input back to a 0.05s seam. The reservoir already absorbs the latency
# this was meant to save.
FFMPEG_CMD = [
    FFMPEG_BIN, "-hide_banner", "-loglevel", "warning",
    *(["-stats"] if FFMPEG_STATS else []),
    "-analyzeduration", "1000000", "-probesize", "500000",
    "-i", "pipe:0",
    "-map", "0:v", "-map", "0:a:0",
    "-c:v", "copy", "-bsf:v", "dump_extra=freq=keyframe",
    *AUDIO_ARGS[AUDIO_CODEC],
    *(["-af", "aresample=async=1"] if AUDIO_FILL_GAPS else []),
    *(["-metadata:s:a:0", f"language={AUDIO_LANG}"] if AUDIO_LANG_OK else []),
    "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "pipe:1",
]
TS_CHANNEL = re.sub(r"[^A-Za-z0-9_-]", "", os.getenv("RESV_TS_CHANNEL", ""))[:64]
TS_SEAM_S = 1.0
TS_SAVE_EVERY_S = 2.0
TS_MAX_LEAD_S = 30.0
RELAY_DRAIN_S = 2.0

# All interval math (pacing debt, watchdogs, debounces, rate windows) uses
# time.monotonic(): time.time() is the wall clock, and a backward NTP step of
# T seconds turned into a single time.sleep(T) inside the pacing loop - long
# enough and the cushion drains while we sleep, starving the player. Wall
# clock remains only in log-line timestamps and the RESV_TS_CHANNEL timeline.

# Last path segment, for log tagging. Strip any query string and cap length so
# a future provider URL shape can't leak an embedded key into the logs.
STREAM_ID = URL.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1][:48] or "stream"
LOG_DIR = os.getenv("RESV_LOG_DIR", "/data/scripts/logs")              # Dispatcharr AIO container default
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError:
    pass                                                              # unwritable: log() already drops file writes, stderr still works
LOG_FILE = os.path.join(LOG_DIR, "delaybuf.log")
LOG_ROTATE_BYTES = 10 * 1024 * 1024
_log_writes = 0


def _maybe_rotate():
    """Rotate delaybuf.log past LOG_ROTATE_BYTES, keeping one .1. Called at
    startup AND every 512th log() line: rotation used to run only at process
    start, so a single long-lived tune (24/7 channel) grew the file unboundedly
    until the next channel start. Cross-process safe: log() reopens per line,
    and a concurrent rotator losing the os.replace race just raises ENOENT
    here (swallowed)."""
    try:
        if os.path.getsize(LOG_FILE) > LOG_ROTATE_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass


_maybe_rotate()


def log(msg, stderr=True, file=True):
    global _log_writes
    if file:
        _log_writes += 1
        if _log_writes % 512 == 0:                                    # ~2h at stats cadence; ~5min under
            _maybe_rotate()                                           # an ffmpeg-stderr storm
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} [{STREAM_ID}] {msg}"
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
    if stderr:
        sys.stderr.write(f"[delaybuf] {msg}\n")
        sys.stderr.flush()


def ffmpeg_cmd(ts_offset=None):
    if ts_offset is None:
        return FFMPEG_CMD
    return [*FFMPEG_CMD[:-1], "-output_ts_offset", f"{ts_offset:.6f}", FFMPEG_CMD[-1]]


def timeline_path():
    return os.path.join(LOG_DIR, f"pts-{TS_CHANNEL}.json")


def timeline_projection(path, now):
    try:
        with open(path) as f:
            state = json.load(f)
        end, wall = float(state["end"]), float(state["wall"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None
    if not (math.isfinite(end) and math.isfinite(wall)):
        return None
    projected = end + max(now - wall, 0.0)
    return projected if projected - now < TS_WRAP_S / 2 else None


def pes_pts(buf_, i):
    if not buf_[i + 1] & 0x40 or not buf_[i + 3] & 0x10:
        return None
    p = i + 4
    if buf_[i + 3] & 0x20:
        p += 1 + buf_[i + 4]
    if p + 14 > i + 188 or buf_[p:p + 3] != b"\x00\x00\x01" or not buf_[p + 7] & 0x80:
        return None
    b = buf_[p + 9:p + 14]
    return ((b[0] >> 1) & 0x07) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


class Timeline:
    def __init__(self, path, now):
        self.path = path
        projected = timeline_projection(path, now)
        self.origin = now if projected is None else max(now, projected + TS_SEAM_S)
        self.offset = self.origin % TS_WRAP_S
        self.started = time.monotonic()
        self.high = None
        self.rejected = 0
        self.saved_at = float("-inf")

    def observe(self, pts, now=None):
        expected = self.origin + ((time.monotonic() if now is None else now) - self.started)
        base = self.origin - self.offset + pts / 90000.0
        seconds = base + round((expected - base) / TS_WRAP_S) * TS_WRAP_S
        if seconds > expected + TS_MAX_LEAD_S:
            self.rejected += 1
            return
        if self.high is None or seconds > self.high:
            self.high = seconds

    def end(self):
        return self.high

    def save(self, now):
        self.saved_at = time.monotonic()
        end = self.end()
        if end is None:
            return
        try:
            with open(f"{self.path}.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                projected = timeline_projection(self.path, now)
                if projected is not None and projected > end:
                    return
                tmp = f"{self.path}.{os.getpid()}"
                with open(tmp, "w") as f:
                    json.dump({"end": end, "wall": now}, f)
                os.replace(tmp, self.path)
        except OSError:
            pass


def stdout_relay(ff, timeline, out=None):
    out = out or sys.stdout.buffer
    carry = b""
    try:
        while True:
            chunk = os.read(ff.stdout.fileno(), CHUNK)
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            data = carry + chunk
            i = data.find(0x47)
            while 0 <= i and i + 188 <= len(data):
                if data[i] != 0x47:
                    i = data.find(0x47, i + 1)
                    continue
                pts = pes_pts(data, i)
                if pts is not None:
                    timeline.observe(pts)
                i += 188
            carry = data[i:] if 0 <= i else b""
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            ff.stdout.close()
        except Exception:
            pass


def ts_sync(buf_, start):
    n = len(buf_)
    i = buf_.find(0x47, start)
    while 0 <= i:
        if i + 376 >= n:
            return -1                                                 # not enough data to confirm; carry
        if buf_[i + 188] == 0x47 and buf_[i + 376] == 0x47:
            return i
        i = buf_.find(0x47, i + 1)
    return -1


def pcr_seconds(buf_, i):
    base = (buf_[i + 6] << 25 | buf_[i + 7] << 17 | buf_[i + 8] << 9
            | buf_[i + 9] << 1 | buf_[i + 10] >> 7)
    ext = (buf_[i + 10] & 0x01) << 8 | buf_[i + 11]
    return (base * 300 + ext) / 27_000_000.0


def pes_time(buf_, i):
    return ((buf_[i] >> 1 & 0x7) << 30 | buf_[i + 1] << 22 | (buf_[i + 2] >> 1) << 15
            | buf_[i + 3] << 7 | buf_[i + 4] >> 1)


def carries_audio(stream_type, buf_, start, end):
    if stream_type in AUDIO_STREAM_TYPES:
        return True
    if stream_type != 0x06:
        return False
    while start + 2 <= end:
        if buf_[start] in AUDIO_DESCRIPTOR_TAGS:
            return True
        start += 2 + buf_[start + 1]
    return False


class TrackHoles:
    def __init__(self):
        self.pid = None
        self.last = None
        self.span = 0.0
        self.holes = 0.0
        self.seen = False
        self.pending_disc = False
        self.head = None

    def adopt(self, pid):
        if pid != self.pid:
            self.pid = pid
            self.last = None
            self.head = None

    def observe(self, t):
        skip, self.pending_disc = self.pending_disc, False
        self.seen = True
        last, self.last = self.last, t
        if last is None or skip:
            return
        delta = ((t - last) % (1 << 33)) / 90000.0
        if delta >= 10.0:
            return
        self.span += delta
        if delta > STREAM_HOLE_S:
            self.holes += delta

    def take(self):
        span, holes, seen = self.span, self.holes, self.seen
        self.span = self.holes = 0.0
        self.seen = False
        return (round(100 * holes / span) if span > 0 else 0), seen, span


def packet_pcr(buf_, i):
    if not buf_[i + 3] & 0x20:
        return None
    aflen = buf_[i + 4]
    if aflen < 7 or not buf_[i + 5] & 0x10:
        return None
    pid = (buf_[i + 1] & 0x1F) << 8 | buf_[i + 2]
    return pid, pcr_seconds(buf_, i), bool(buf_[i + 5] & 0x80)


class TsParser:
    """Observes the ingest byte stream at TS-packet level. Two jobs:

    1. PCR clock: bytes-per-PCR-second is the content's true byte rate -
       the pacing reference. Garbage PCRs (the CDN corruption family that
       freezes -re) are rejected by a plausibility window on the wrap-aware
       delta; a wedged chain re-anchors after a run of rejects. The cum_pcr
       clock plus (fed, cum_pcr) marks also give the cushion in real
       seconds of media instead of a byte estimate.
    2. Continuity counters per PID: telemetry only in v6 (counted, logged,
       never acted on) - evidence first, actions later.

    Deque append/popleft are thread-safe; the fetcher feeds, the pacer
    reads. Stats counters are plain ints (GIL-atomic reads, drift is fine).
    Marks are keyed on `fed`, the bytes fed so far: every piece the fetcher
    puts in the reservoir is fed here once, so `fed` tracks the reservoir's
    in_total and the pacer's released-byte count lines up with the marks.
    """

    def __init__(self):
        self.fed = 0
        self.cc_errors = 0
        self.sync_losses = 0
        self.pcr_rejects = 0
        self.pcr_disc = 0
        self.pcr_back = 0                                             # backward PCR jumps (CDN overlap-replay signal)
        self.cum_pcr = 0.0
        self.rate_marks = deque()                                     # (fed, cum_pcr), ~90s content span
        self.cush_marks = deque()                                     # (fed, cum_pcr), popped as released
        self.pmt_pid = None
        self.video = TrackHoles()
        self.audio = TrackHoles()
        self.holes_lock = threading.Lock()
        self.on_reconnect()

    def on_reconnect(self):
        """New HTTP connection: new CC phase, PCR chain re-anchors without
        advancing cum_pcr (the seam counts as 0s of content - slightly
        underestimates cushion, never overestimates). Marks are kept so the
        cushion stays measurable across plain EOF reconnects."""
        self.carry = b""
        self.synced = False
        self.cc = {}                                                  # pid -> (last_cc, dup_seen)
        self.pcr_pid = None
        self.last_pcr = None
        self.reject_run = 0
        self.video.last = self.audio.last = None
        self.video.head = self.audio.head = None
        self.psi = {}

    def feed(self, data):
        self.fed += len(data)
        buf_ = self.carry + data if self.carry else data
        n = len(buf_)
        i = 0 if self.synced else self._sync(buf_, 0)
        while 0 <= i and i + 188 <= n:
            if buf_[i] != 0x47:
                self.sync_losses += 1
                self.synced = False
                i = self._sync(buf_, i + 1)
                continue
            self._packet(buf_, i)
            i += 188
        self.carry = buf_[i:] if i >= 0 else buf_[-376:]

    def _sync(self, buf_, start):
        """Find a 0x47 confirmed by two more at 188-byte stride."""
        i = ts_sync(buf_, start)
        if i >= 0:
            self.synced = True
        return i

    def _packet(self, buf_, i):
        b3 = buf_[i + 3]
        pid = (buf_[i + 1] & 0x1F) << 8 | buf_[i + 2]
        if pid == 0x1FFF:                                             # null packets: CC undefined by spec
            return
        afc = b3 >> 4 & 0x3
        disc = False
        if afc & 0x2:                                                 # adaptation field present
            aflen = buf_[i + 4]
            if aflen >= 1 and buf_[i + 5] & 0x80:
                disc = True                                           # discontinuity_indicator: CC jump legal
            if aflen >= 7 and buf_[i + 5] & 0x10:                     # PCR_flag
                self._pcr(buf_, i, pid, disc)
        if disc and pid in (self.pcr_pid, self.video.pid, self.audio.pid):
            self.video.pending_disc = self.audio.pending_disc = True
        if afc & 0x1:
            start = i + 4 + (1 + buf_[i + 4] if afc & 0x2 else 0)
            end = i + 188
            pusi = buf_[i + 1] & 0x40
            if start < end:
                if pid == 0 or pid == self.pmt_pid:
                    self._psi(buf_, start, end, pid, pusi)
                elif pid == self.video.pid and (pusi or self.video.head is not None):
                    self._pes(buf_, start, end, self.video, pusi)
                elif pid == self.audio.pid and (pusi or self.audio.head is not None):
                    self._pes(buf_, start, end, self.audio, pusi)
        if afc & 0x1:                                                 # CC increments only on payload packets
            cc = b3 & 0x0F
            prev = self.cc.get(pid)
            if prev is None or disc:
                self.cc[pid] = (cc, False)
            else:
                last, dup = prev
                if cc == (last + 1) & 0xF:
                    self.cc[pid] = (cc, False)
                elif cc == last and not dup:
                    self.cc[pid] = (cc, True)                         # one duplicate packet is legal
                else:
                    self.cc_errors += 1
                    self.cc[pid] = (cc, False)

    def _pcr(self, buf_, i, pid, disc):
        if self.pcr_pid is None:
            self.pcr_pid = pid                                        # lock onto the first PCR carrier
        elif pid != self.pcr_pid:
            return                                                    # one clock only
        pcr = pcr_seconds(buf_, i)
        if self.last_pcr is None:
            self.last_pcr = pcr
            return
        if pcr == self.last_pcr:                                      # duplicate packet (legal): not garbage
            return
        if disc:
            self.pcr_disc += 1
            self.last_pcr = pcr                                       # legal jump: re-anchor, no cum advance
            return
        raw = pcr - self.last_pcr                                     # signed: small negatives = real rewind,
        if -TS_WRAP_S / 2 < raw < -0.5:                               # large negatives = wrap aliasing (ignore)
            self.pcr_back += 1
            log(f"pcr backward jump: {raw:.1f}s "
                f"(last={self.last_pcr:.1f} cur={pcr:.1f})", stderr=False)
        delta = (pcr - self.last_pcr) % TS_WRAP_S
        if 0.0 < delta < 10.0:
            self.reject_run = 0
            self.cum_pcr += delta
            self.last_pcr = pcr
            mark = (self.fed, self.cum_pcr)
            self.rate_marks.append(mark)
            self.cush_marks.append(mark)
            rm = self.rate_marks
            while len(rm) > 2 and self.cum_pcr - rm[0][1] > 90.0:
                rm.popleft()
        else:
            self.pcr_rejects += 1
            self.reject_run += 1
            if self.reject_run > 25:                                  # chain wedged on an accepted garbage
                self.last_pcr = pcr                                   # sample: re-anchor on the live values
                self.reject_run = 0

    def _psi(self, buf_, start, end, pid, pusi):
        if pusi:
            begin = start + 1 + buf_[start]
            if begin >= end:
                self.psi.pop(pid, None)
                return
            section = bytearray(buf_[begin:end])
        elif pid in self.psi:
            section = self.psi[pid] + buf_[start:end]
        else:
            return
        if len(section) >= 3:
            total = 3 + ((section[1] & 0x0F) << 8 | section[2])
            if total > PSI_MAX_BYTES:
                self.psi.pop(pid, None)
                return
            if len(section) >= total:
                self.psi.pop(pid, None)
                if total < 8 or not section[5] & 0x01:
                    return
                if pid == 0:
                    self._pat(bytes(section[:total]))
                else:
                    self._pmt(bytes(section[:total]))
                return
        self.psi[pid] = section

    def _pat(self, section):
        if section[0] != 0x00:
            return
        for j in range(8, len(section) - 7, 4):
            if section[j] << 8 | section[j + 1]:
                pmt_pid = (section[j + 2] & 0x1F) << 8 | section[j + 3]
                if pmt_pid != self.pmt_pid:
                    self.psi.pop(self.pmt_pid, None)
                    self.pmt_pid = pmt_pid
                return

    def _pmt(self, section):
        stop = len(section) - 4
        if section[0] != 0x02 or stop < 12:
            return
        j = 12 + ((section[10] & 0x0F) << 8 | section[11])
        video = audio = None
        while j + 5 <= stop:
            pid = (section[j + 1] & 0x1F) << 8 | section[j + 2]
            info = (section[j + 3] & 0x0F) << 8 | section[j + 4]
            if video is None and section[j] in VIDEO_STREAM_TYPES:
                video = pid
            elif audio is None and carries_audio(section[j], section, j + 5, min(j + 5 + info, stop)):
                audio = pid
            j += 5 + info
        with self.holes_lock:
            self.video.adopt(video)
            self.audio.adopt(audio)

    def _pes(self, buf_, start, end, track, pusi):
        if pusi:
            head = bytes(buf_[start:min(end, start + PES_HEAD_BYTES)])
        else:
            head = track.head + bytes(buf_[start:min(end, start + PES_HEAD_BYTES - len(track.head))])
        track.head = None
        if len(head) < 9:
            track.head = head
            return
        if head[:3] != b"\x00\x00\x01":
            return
        flags = head[7] & 0xC0
        need = 19 if flags == 0xC0 else 14 if flags & 0x80 else 0
        if not need:
            return
        if len(head) < need:
            track.head = head
            return
        t = pes_time(head, 14 if flags == 0xC0 else 9)
        with self.holes_lock:
            track.observe(t)

    def take_holes_pct(self):
        with self.holes_lock:
            video, video_seen, video_span = self.video.take()
            audio, audio_seen, audio_span = self.audio.take()
            video_pid, audio_pid = self.video.pid, self.audio.pid
        if video_span >= TRACK_MISSING_AFTER_S and audio_pid is not None and not audio_seen:
            audio = 100
        if audio_span >= TRACK_MISSING_AFTER_S and video_pid is not None and not video_seen:
            video = 100
        return video, audio

    def content_rate(self):
        """Mux bytes per second of content time, or None before PCR lock."""
        rm = self.rate_marks
        if len(rm) < 2:
            return None
        (o0, p0), (o1, p1) = rm[0], rm[-1]
        if p1 - p0 < 1.5:                                             # startup: even a short span beats the
            return None                                               # burst-inflated arrival rate
        return max((o1 - o0) / (p1 - p0), RATE_FLOOR)

    def cushion_s(self, released_total):
        """Seconds of media in the reservoir, or None if unmeasurable
        (pre-PCR-lock, or right after a reconnect seam)."""
        cm = self.cush_marks
        while len(cm) >= 2 and cm[1][0] <= released_total:
            cm.popleft()
        if not cm or cm[0][0] > released_total:
            return None
        return self.cum_pcr - cm[0][1]


class Reservoir:
    """The RAM buffer between the fetcher (put) and the pacer (take). Every
    field moves under `cond`; `in_total - bytes` is the released-or-flushed
    byte count the parser's cushion marks are compared against."""

    def __init__(self, stop):
        self.stop = stop
        self.cond = threading.Condition()
        self.chunks = deque()
        self.bytes = 0
        self.in_total = 0
        self.eof = False                                              # the fetcher has ended; nothing more will arrive
        self.in_marks = deque()                                       # (monotonic ts, in_total) samples

    @property
    def full(self):
        return self.bytes >= MAX_BYTES

    def put(self, piece):
        with self.cond:
            while self.bytes >= MAX_BYTES and not self.stop.is_set():
                self.cond.wait(1)
            self.chunks.append(piece)
            self.bytes += len(piece)
            self.in_total += len(piece)
            self.in_marks.append((time.monotonic(), self.in_total))
            self.cond.notify_all()

    def take(self, timeout):
        """Pop up to PACE_SLICE bytes, returning the released-byte total
        snapshotted under the same lock (in_total and bytes both move under
        cond, so reading them together avoids a torn read). Returns (None, 0)
        on stop or once the fetcher has ended and the reservoir is drained,
        and (b"", released) when nothing arrived within `timeout`: the caller
        keeps its telemetry going while the reservoir is dry."""
        with self.cond:
            if not self.chunks and not self.eof and not self.stop.is_set():
                self.cond.wait(timeout)
            if self.stop.is_set() or (not self.chunks and self.eof):
                return None, 0
            if not self.chunks:
                return b"", self.in_total - self.bytes
            d = self.chunks.popleft()
            if len(d) > PACE_SLICE:
                self.chunks.appendleft(d[PACE_SLICE:])
                d = d[:PACE_SLICE]
            self.bytes -= len(d)
            self.cond.notify_all()
            return d, self.in_total - self.bytes

    def flush(self):
        with self.cond:
            self.chunks.clear()
            self.bytes = 0
            self.cond.notify_all()

    def close(self):
        """The fetcher has ended: wake the prefill and the pacer."""
        with self.cond:
            self.eof = True
            self.cond.notify_all()

    def wake(self):
        with self.cond:
            self.cond.notify_all()

    def wait_prefill(self, min_bytes, max_s):
        t0 = time.monotonic()
        with self.cond:
            while (self.bytes < min_bytes and not self.eof
                   and time.monotonic() - t0 < max_s and not self.stop.is_set()):
                self.cond.wait(0.5)

    def in_rate(self):
        """Rolling arrival byte rate (fallback when PCR is unavailable). The
        window ends at *now*, not the last arrival - ending at the last arrival
        excluded in-progress gaps and overstated the rate by ~4% on a bursty
        feed, which cancelled the 0.97 release floor (v5 cushion bug)."""
        marks = self.in_marks
        now = time.monotonic()
        while len(marks) > 2 and now - marks[0][0] > RATE_WINDOW_S:
            marks.popleft()
        if len(marks) < 2:
            return RATE_FLOOR
        (t0, b0) = marks[0]
        b1 = marks[-1][1]
        if now - t0 < 5:
            return RATE_FLOOR
        return max((b1 - b0) / (now - t0), RATE_FLOOR)


class ReplaySkip:
    """RESV_REPLAY_SKIP: after a plain reconnect the edge often resends seconds
    that were already ingested. Holds the new connection's bytes until its PCR
    passes the last PCR taken before the seam, drops what came before that
    point, and passes everything through untouched whenever the seam can't be
    placed."""

    def __init__(self, parser, reservoir):
        self.parser = parser
        self.reservoir = reservoir
        self.seam = None
        self.held = bytearray()
        self.scan = self.first = self.prev = None
        self.advanced_at = 0

    def arm(self, allowed=True):
        self.held = bytearray()
        self.scan = self.first = self.prev = None
        self.advanced_at = 0
        p = self.parser
        ready = allowed and REPLAY_SKIP and p.pcr_pid is not None and p.last_pcr is not None
        self.seam = (p.pcr_pid, p.last_pcr) if ready else None

    def _release(self, reason):
        out, self.held = self.held, bytearray()
        self.seam = None
        if reason:
            log(f"replay after reconnect: {reason}; passing through", stderr=False)
        return out

    def release_held(self):
        if self.seam is None or not self.held:
            return b""
        return self._release("connection ended before the seam")

    def running_low(self):
        crate = self.parser.content_rate()
        return crate is not None and self.reservoir.bytes < crate * REPLAY_MIN_CUSHION_S

    def feed(self, data):
        if self.seam is None:
            return data
        held = self.held
        held.extend(data)
        if self.reservoir.bytes + len(held) > MAX_BYTES:
            return self._release("replay does not fit in the reservoir")
        if self.running_low():
            return self._release("reservoir running low")
        if self.scan is None:
            start = ts_sync(held, 0)
            if start < 0:
                return self._release("no TS sync within the hold limit") if len(held) > REPLAY_HOLD_BYTES else b""
            self.scan = self.advanced_at = start
        pid_seam, seam = self.seam
        i = self.scan
        while i + 188 <= len(held):
            if i - self.advanced_at > REPLAY_HOLD_BYTES:
                return self._release("no PCR within the hold limit" if self.first is None
                                     else "PCR not advancing through the replay")
            if held[i] != 0x47:
                return self._release("lost TS sync")
            found = packet_pcr(held, i)
            if found and found[0] == pid_seam:
                _, pcr, disc = found
                if disc:
                    return self._release("discontinuity flag")
                ahead = (pcr - seam) % TS_WRAP_S
                past_seam = 0 < ahead < TS_WRAP_S / 2
                if self.first is None:
                    if past_seam:
                        return self._release("")
                    behind = (seam - pcr) % TS_WRAP_S
                    if behind > REPLAY_MAX_S:
                        return self._release(f"{behind:.0f}s back is beyond RESV_REPLAY_MAX_S")
                    self.first = self.prev = pcr
                    self.advanced_at = i
                else:
                    step = (pcr - self.prev) % TS_WRAP_S
                    if step > REPLAY_STEP_MAX_S:
                        return self._release("PCR not advancing through the replay")
                    if step > 0:
                        self.prev = pcr
                        self.advanced_at = i
                    if past_seam:
                        skipped = (seam - self.first) % TS_WRAP_S
                        log(f"replay after reconnect: skipped {skipped:.1f}s "
                            f"({i / 1e6:.1f}MB) already delivered", stderr=False)
                        del held[:i]
                        self.held = bytearray()
                        self.seam = None
                        return held
            i += 188
        self.scan = i
        return b""


class PacketAligner:
    """Cuts one upstream connection into whole TS packets, so everything in
    the reservoir starts on a packet boundary. align_to_188() on the write side
    counts bytes; that keeps writes aligned only while every byte before them
    was. A connection that started mid-packet, or ended mid-packet (an EOF
    lands wherever the edge stopped), shifted every later write by the
    leftover, for the rest of the tune. Within a connection the bytes are
    contiguous, so after the first sync only the unfinished last packet is
    carried; garbage inside the stream is passed on for the parser to count,
    as before. A connection with no TS sync in its first CHUNK is not a TS at
    all: it passes through, so ffmpeg fails on it as it always did instead of
    the stream going quiet. So does a short answer that never showed a sync
    (an error text, a fragment of under three packets). Either is padded with
    0xFF to the next packet boundary when its connection ends, so whatever
    follows starts back on the grid; ffmpeg skips filler as it skips the junk
    before it."""

    def __init__(self):
        self.carry = b""
        self.state = "seeking"                                        # -> "aligned", or "raw" when there is no TS to align
        self.lead = 0                                                 # bytes dropped before the first packet
        self.passed = 0                                               # bytes passed on unaligned (raw mode)

    def feed(self, data):
        buf_ = self.carry + data if self.carry else data
        self.carry = b""
        if self.state == "raw":
            self.passed += len(buf_)
            return buf_
        start = 0
        if self.state == "seeking":
            start = ts_sync(buf_, 0)
            if start < 0:
                if len(buf_) < CHUNK:
                    self.carry = buf_
                    return b""
                self.state = "raw"
                self.passed = len(buf_)
                log(f"upstream connection: no TS sync in its first {len(buf_) // 1024}KB; passing it on unaligned")
                return buf_
            self.state = "aligned"
            self.lead = start
        end = start + (len(buf_) - start) // 188 * 188
        self.carry = buf_[end:]
        return buf_[start:end]

    def finish(self):
        """The connection is over. Returns (rest, dropped): bytes still to pass
        on (what an unaligned connection left, padded to the packet grid), and
        the unfinished last packet of an aligned stream, which can never
        complete and is dropped."""
        carry, self.carry = self.carry, b""
        if self.state == "aligned":
            return b"", len(carry)
        pad = -(self.passed + len(carry)) % 188
        return carry + b"\xff" * pad, 0


def abort_read(response):
    """Unblock an r.read() stuck in the fetcher thread on a silent socket.
    response.close() cannot: the buffered reader holds its lock for the whole
    blocked read, and close() waits for that lock, i.e. for the socket timeout.
    shutdown() acts on the socket itself and the read returns at once. Called
    through socket.socket so an SSL socket's TLS state is not touched from this
    thread. The read ends at once: an EOF over HTTP, an OSError over TLS; the
    fetcher takes either as the reconnect it asked for."""
    if response is None:
        return
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
            return
        except OSError:
            pass
    try:
        response.close()
    except Exception:
        pass


def edge_of(url):
    """host[:port] of the URL the edge answered from, without any
    user:password@ that the provider URL may carry."""
    return urllib.parse.urlsplit(url).netloc.rpartition("@")[2]


BACKOFF_RESET_BYTES = 1024 * 1024                                      # a connection resets the reconnect backoff after this
BACKOFF_RESET_S = 10.0                                                 # many bytes or this long, not on its first chunk


def is_mp4(head):
    return head[:1] != b"\x47" and head[4:8] == b"ftyp"


NON_TS_SIGNATURES = (
    (b"#EXTM3U", "an HLS playlist"),
    (b"<!doctype", "an HTML page"),
    (b"<html", "an HTML page"),
    (b"<?xml", "an XML document"),
)


def non_ts_kind(head):
    """What a connection's first chunk is when it can never be a live TS, or
    None. Matched on content, not Content-Type (CDNs label live TS as
    video/mp4) and not on 'first byte != 0x47' (a TS may start mid-packet;
    ts_sync resyncs). MP4 is opt-in via RESV_REJECT_MP4; the rest are always
    rejected: ffmpeg's mpegts demuxer cannot play them, and the small body used
    to look like data, which reset the backoff and kept RESV_GIVEUP_TRIES from
    firing."""
    if REJECT_MP4 and is_mp4(head):
        return "an MP4 file"
    if head[:1] == b"\x47":
        return None
    start = head[:16].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    for sig, kind in NON_TS_SIGNATURES:
        if start.startswith(sig.lower()):
            return kind
    return None


class Upstream:
    """The fetcher thread, which drains the CDN socket eagerly into the
    reservoir, and everything that asks it to reconnect: the corrupt-loop
    watcher on ffmpeg's stderr, the #5 detector and the stall watchdog (#4)."""

    def __init__(self, url, ua, reservoir, parser, stop):
        self.url = url
        self.ua = ua
        self.reservoir = reservoir
        self.parser = parser
        self.stop = stop
        self.replay = ReplaySkip(parser, reservoir)
        self.lock = threading.Lock()                                  # guards the request fields below
        self.force = threading.Event()                                # a reconnect has been requested
        self.flush_pending = False                                    # sticky flush request: set by a flush caller, cleared by the fetcher after it flushes
        # Debounce anchors are -inf, not 0.0: monotonic time can be under 90s
        # shortly after host boot, and 0.0 would rate-limit the first forced
        # reconnect then.
        self.last_forced_flush = float("-inf")                        # corrupt-loop / #5 debounce (flush=True)
        self.last_forced_stall = float("-inf")                        # stall watchdog #4 debounce (flush=False)
        self.response = None                                          # live urllib response (for a forced abort)
        self.corrupt_seen = deque()                                   # (monotonic ts, dts) from ffmpeg stderr
        self.arrived_total = 0                                        # bytes off the wire, held replay and dropped partials included
        self.reconnects = 0
        self.backoff = 1

    def request_reconnect(self, reason, flush=True):
        """Forced reconnect shared by the corrupt-loop / #5 detectors (flush=True —
        buffer is poisoned) and the stall watchdog (#4, flush=False — buffer is good).
        Debounced PER CLASS (<=1 per 90s each) so a benign stall can never rate-limit
        away a corrupt flush; the flush request is sticky and is never downgraded by a
        concurrent no-flush. Returns True if it fired, False if rate-limited."""
        now = time.monotonic()
        with self.lock:
            if flush:
                if now - self.last_forced_flush <= 90:
                    return False
                self.last_forced_flush = now
                self.flush_pending = True                             # sticky; the fetcher clears it after flushing
            else:
                if now - self.last_forced_stall <= 90:
                    return False
                self.last_forced_stall = now                          # leave flush_pending alone (never downgrade a flush)
            self.force.set()
            # Taken under the lock with the request: once force is set, the next
            # connection the fetcher publishes is the answer to this request, and
            # aborting it would cut the fresh connection instead of the stale one.
            live = self.response
        log(reason)
        abort_read(live)
        return True

    def take_request(self):
        """Fetcher side: consume a pending reconnect request. Returns None when
        there is none, True when the reservoir must be flushed, else False."""
        with self.lock:
            if not self.force.is_set():
                return None
            flush, self.flush_pending = self.flush_pending, False
            self.force.clear()
        return flush

    @property
    def flush_requested(self):
        with self.lock:
            return self.flush_pending

    def register_corrupt(self, dts):
        """CDN edges sometimes wedge a single long-lived connection into serving
        the same corrupt packet in a loop, while fresh connections are clean
        (verified live 2026-06-12). Same dts reported 3x => reconnect + flush."""
        now = time.monotonic()
        seen = self.corrupt_seen
        seen.append((now, dts))
        while seen and now - seen[0][0] > 120:
            seen.popleft()
        same = sum(1 for _, d in seen if d == dts)
        if same >= 3:
            self.request_reconnect(
                f"corrupt-loop detected in stream (dts={dts} x{same}) - forcing upstream reconnect + buffer flush")

    def stall_watchdog(self):
        """No ingest progress for STALL_S while running => silently wedged upstream
        (socket open, no data, not EOF). urlopen's own timeout is 30s, by which point
        the cushion has drained; this trips sooner and reconnects WITHOUT flushing, to
        grab a fresh front-load while the good buffer keeps draining. STALL_S sits
        above the CDN's normal burst-gap so a normal gap never trips it. A full
        reservoir is not a stall: put() stops reading on purpose then, so the clock
        restarts while it is full."""
        last_in = -1
        last_adv = time.monotonic()
        while not self.stop.wait(2):
            cur = self.arrived_total
            if cur != last_in or self.reservoir.full:
                last_in = cur
                last_adv = time.monotonic()
            elif time.monotonic() - last_adv > STALL_S and not self.force.is_set():
                if self.request_reconnect(
                        f"upstream stalled (no data {time.monotonic() - last_adv:.0f}s) - reconnecting, buffer kept",
                        flush=False):
                    last_adv = time.monotonic()

    def ingest(self, d):
        for start in range(0, len(d), CHUNK):
            piece = bytes(d[start:start + CHUNK])
            self.reservoir.put(piece)
            self.parser.feed(piece)                                   # observe outside the reservoir lock

    def run(self):
        first = True
        tries_without_data = 0
        while not self.stop.is_set():
            flushed = False
            try:
                if self.take_request():
                    self.reservoir.flush()
                    flushed = True
                    log("flushed reservoir after corrupt-loop reconnect")
                req = urllib.request.Request(self.url, headers={"User-Agent": self.ua})
                r = urllib.request.urlopen(req, timeout=30)
                with self.lock:
                    self.response = r
                self.replay.arm(allowed=not flushed)
                self.parser.on_reconnect()
                log(f"upstream connected edge={edge_of(r.geturl())}")
                if not first:
                    self.reconnects += 1
                first = False
                self.drain(r)
            except Exception as e:
                log(f"upstream error {type(e).__name__}: {e}; retry in {self.backoff}s")
            if self.stop.is_set():
                break
            held = b"" if self.flush_requested else self.replay.release_held()
            if held:
                self.ingest(held)
            if self.arrived_total == 0:
                tries_without_data += 1
                if GIVEUP_TRIES and tries_without_data >= GIVEUP_TRIES:
                    log(f"giving up: no data after {tries_without_data} upstream attempts "
                        f"(RESV_GIVEUP_TRIES={GIVEUP_TRIES}); exiting so Dispatcharr can try the next stream")
                    break
            if self.force.is_set():
                continue                                             # intentional reconnect: flush + reconnect now, no backoff
            time.sleep(self.backoff)
            self.backoff = min(self.backoff * 2, 8)
        self.reservoir.close()

    def drain(self, r):
        """Read one connection until EOF, an error, stop or a reconnect request.
        Only whole TS packets go on to the reservoir (see PacketAligner)."""
        aligner = PacketAligner()
        checked = False
        conn_bytes = 0
        conn_t0 = time.monotonic()
        try:
            while not self.stop.is_set() and not self.force.is_set():
                try:
                    d = r.read(CHUNK)
                except Exception:
                    if self.force.is_set():
                        break                                        # our own abort unblocked the read; not an error
                    raise                                            # genuine read error -> backoff path in run()
                if not d:
                    if not self.force.is_set():                      # an aborted read ends as an EOF too
                        log("upstream EOF")
                    break
                if not checked:
                    checked = True
                    kind = non_ts_kind(d)
                    if kind:
                        hint = (" (reservoarr needs a raw MPEG-TS URL, see README)"
                                if kind == "an HLS playlist" else "")
                        log(f"upstream sent {kind}, not a live TS; counted as an attempt without data{hint}")
                        return
                conn_bytes += len(d)
                if len(d) >= CHUNK or conn_bytes >= BACKOFF_RESET_BYTES or time.monotonic() - conn_t0 >= BACKOFF_RESET_S:
                    self.backoff = 1                                  # reset only once a connection has carried real
                    #                                                   data: an empty or tiny answer must keep
                    #                                                   backing off, not hammer the provider at 1/s
                self.arrived_total += len(d)
                self.pass_on(aligner.feed(d))
        finally:
            with self.lock:
                self.response = None
            rest, tail = aligner.finish()
            if not self.stop.is_set():
                self.pass_on(rest)
                if aligner.lead or tail:
                    log(f"upstream connection ended: dropped {aligner.lead}B before its first TS packet "
                        f"and {tail}B of an unfinished last packet", stderr=False)
            try:
                r.close()
            except Exception:
                pass

    def pass_on(self, d):
        if d:
            d = self.replay.feed(d)
        if d:
            self.ingest(d)


def stderr_watcher(ff, on_corrupt):
    """Relay ffmpeg stderr (Dispatcharr's logger reads ours) and hand every
    `Packet corrupt` dts to on_corrupt (the corrupt-loop detector)."""
    pat = re.compile(rb"Packet corrupt \(stream = \d+, dts = (\d+)\)")

    def relay(raw):
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            # The progress line is for Dispatcharr's watchdog, not for the
            # journal: at two lines a second it would rotate delaybuf.log
            # away from under the cushion telemetry it exists to keep.
            log(f"ffmpeg: {line}", file=not line.startswith("frame="))
        m = pat.search(raw)
        if m:
            on_corrupt(int(m.group(1)))

    pending = b""
    while True:
        # os.read on the fd returns whatever is already in the pipe, raw or
        # buffered. Not read1(): ff is spawned bufsize=0, so stderr is a FileIO,
        # which has no read1. Dispatcharr's stats watchdog needs this real-time.
        chunk = os.read(ff.stderr.fileno(), 4096)
        if not chunk:
            break
        pending += chunk
        # ffmpeg terminates its progress line with \r, so readline() used to hold
        # every one of them until an unrelated \n arrived. Splitting on both is a
        # no-op while RESV_FFMPEG_STATS is off: nothing else ends in \r.
        while True:
            breaks = [i for i in (pending.find(b"\n"), pending.find(b"\r")) if i != -1]
            if not breaks:
                break
            idx = min(breaks)
            raw, pending = pending[:idx], pending[idx + 1:]
            relay(raw)
    # readline() handed back the last line even without a terminator; a crash
    # message on the way out arrives exactly that way.
    if pending:
        relay(pending)


def align_to_188(tail, chunk):
    """TS-packet-aligned write helper. r.read() and the deque produce arbitrary-
    length byte runs, but ffmpeg's mpegts demuxer assumes each write to its stdin
    starts at a 188-byte packet boundary. Mid-packet writes cause spurious
    `timestamp discontinuity` detections plus AAC `channel element X.Y is not
    allocated` errors as half-frames hit the decoder, which present to the viewer
    as audio/video desync drifting worse over a session.

    Discovered 2026-06-16 evening: a live session produced 100+ disc lines per
    minute that disappeared when ffmpeg consumed the same captured upstream as a
    regular file (offline reads naturally land 188-aligned). Fix: only ever pass
    188-multiples downstream; carry the unaligned tail to the next call.

    Counting bytes keeps writes aligned only if every byte before them was on a
    packet boundary; PacketAligner makes that hold at each connection seam
    (6.4.0). This stays as the write-side guard.

    Returns (aligned_bytes, new_tail). Both bytes objects, possibly empty.
    """
    buf_ = tail + chunk if tail else chunk
    n = (len(buf_) // 188) * 188
    return buf_[:n], buf_[n:]


def release_factor(cushion_s, since_release_s):
    """Release rate as a multiple of the content rate: hold the cushion near
    TARGET_S. Floor FLOOR, 1.0 by default: the player is never fed below
    realtime, and the cushion comes from front-load and burst surplus. A
    FLOOR under 1.0 still holds 1.0 for the first GRACE_S - the player owns
    only the small headstart then, and a sub-realtime feed could drain it
    before the bank settles. Ceiling 1.15: bleeding a big cushion faster IS
    the v5 bug."""
    floor = 1.0 if since_release_s < GRACE_S else FLOOR
    level_err = (cushion_s - TARGET_S) / TARGET_S
    return min(max(1.0 + 0.3 * level_err, floor), 1.15)


class Level(NamedTuple):
    crate: float | None                                               # PCR content rate, None before lock
    rate: float                                                       # pacing reference: crate, else arrival rate
    cush_pcr: float | None                                            # cushion off the PCR clock, None if unmeasurable
    cush: float                                                       # cushion in seconds (PCR, else a byte estimate)


class Pacer:
    """Byte-rate pacing of the release (never ffmpeg -re: invariant #1)."""

    def __init__(self, parser, reservoir):
        self.parser = parser
        self.reservoir = reservoir
        self.release_start = time.monotonic()                         # set once; intentionally NOT reset on
        #                                                               reconnect (no per-seam headstart re-dump)
        self.headstart_bytes = 0                                      # byte backstop before PCR lock
        self.debt = 0.0                                               # seconds owed to pacing
        self.last = time.monotonic()

    def measure(self, released_total):
        crate = self.parser.content_rate()
        rate = crate if crate is not None else self.reservoir.in_rate()
        cush_pcr = self.parser.cushion_s(released_total)
        cush = cush_pcr if cush_pcr is not None else self.reservoir.bytes / rate
        return Level(crate, rate, cush_pcr, cush)

    def pace(self, n, level):
        """Sleep off what releasing n more bytes owes at the target rate."""
        now = time.monotonic()
        # Head start: HEADSTART_S seconds of *content* go out unpaced so
        # Plex gets its startup buffer fast; everything else of the CDN's
        # per-connection front-load burst stays banked in the reservoir.
        released_s = self.parser.cum_pcr - level.cush_pcr if level.cush_pcr is not None else None
        if (now - self.release_start < 15
                and (released_s < HEADSTART_S if released_s is not None
                     else self.headstart_bytes < 2560 * 1024)):
            self.headstart_bytes += n
            return
        r = level.rate * release_factor(level.cush, now - self.release_start)
        self.debt += n / r
        self.debt -= now - self.last
        self.last = now
        if self.debt > 0.005:
            # The debt stays: the next call takes the whole sleep off it, so an
            # oversleep is paid back. Zeroing it here lost every oversleep -
            # ~0.23ms per 12ms slice on Linux, 2% under the target rate on an
            # 8 Mbps channel, which a small player buffer runs dry on.
            time.sleep(self.debt)
        elif self.debt < -2.0:
            self.debt = 0.0                                           # don't bank idle time


class CorruptionDetector:
    """#5 ingest-side corruption detector: sustained CC/sync errors while bytes
    still flow => wedged/corrupt upstream (fires ~30s before the ffmpeg-stderr
    loop trigger). NOT pcrrej (recoverable). Fed once per stats window with the
    parser's cumulative counters."""

    def __init__(self):
        self.prev = (0, 0, 0)                                         # (cc_errors, sync_losses, in_total) last window
        self.bad_wins = 0

    def window(self, cc_errors, sync_losses, in_total):
        """Returns a detail string when the rule trips this window, else None."""
        dcc = cc_errors - self.prev[0]
        dsy = sync_losses - self.prev[1]
        ingest_adv = in_total - self.prev[2]
        self.prev = (cc_errors, sync_losses, in_total)
        if ingest_adv > 0 and (dcc >= CC_ERR_PER_WIN or dsy >= SYNC_ERR_PER_WIN):
            self.bad_wins += 1
        else:
            self.bad_wins = 0
        if self.bad_wins < TS_SUSTAIN_WINS:
            return None
        detail = f"+{dcc} ccerr/+{dsy} sync this window x{self.bad_wins} windows"
        self.bad_wins = 0
        return detail


def announce_audio_codec():
    if AUDIO_CODEC_REQUESTED and AUDIO_CODEC != AUDIO_CODEC_REQUESTED:
        log(f"RESV_AUDIO_CODEC={AUDIO_CODEC_REQUESTED!r} is not one of {', '.join(AUDIO_ARGS)}; "
            f"encoding audio as {AUDIO_CODEC}")


class Stream:
    """One tune: Upstream fills the Reservoir, pump() releases it to ffmpeg
    through the Pacer, and the watchers on both ends run beside it."""

    def __init__(self, url, ua):
        self.stop = threading.Event()
        self.parser = TsParser()
        self.reservoir = Reservoir(self.stop)
        self.upstream = Upstream(url, ua, self.reservoir, self.parser, self.stop)
        self.detector = CorruptionDetector()

    def shutdown(self):
        self.stop.set()
        self.reservoir.wake()

    def watch_ffmpeg(self, ff):
        """Relay ffmpeg's stderr until it closes, i.e. until ffmpeg is gone. If
        nothing asked it to go (OOM kill, crash), end the stream: with the
        reservoir empty nothing writes to its stdin, so the dead pipe went
        unnoticed and the channel sat on dead air instead of ending, which is
        what lets Dispatcharr retry or fail over."""
        stderr_watcher(ff, self.upstream.register_corrupt)
        if self.stop.is_set():
            return
        try:
            rc = ff.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return
        if self.stop.is_set():
            return
        log(f"ffmpeg exited on its own (rc={rc}); shutting down")
        self.shutdown()

    def run(self):
        threading.Thread(target=self.upstream.run, daemon=True).start()

        t0 = time.monotonic()
        self.reservoir.wait_prefill(PREFILL_BYTES, PREFILL_MAX_S)
        if self.reservoir.eof and not self.reservoir.bytes:
            log(f"no data before the fetcher gave up ({time.monotonic() - t0:.1f}s); exiting without ffmpeg")
            return
        log(f"prefill done: {self.reservoir.bytes / 1e6:.1f}MB in {time.monotonic() - t0:.1f}s, "
            f"releasing stream to ffmpeg")

        timeline = Timeline(timeline_path(), time.time()) if TS_CHANNEL else None
        ff = subprocess.Popen(ffmpeg_cmd(timeline.offset if timeline else None), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE if timeline else None, stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self.watch_ffmpeg, args=(ff,), daemon=True).start()
        relay = None
        if timeline:
            log(f"ts timeline: channel {TS_CHANNEL} starts at {timeline.origin:.3f}s "
                f"(output offset {timeline.offset:.3f}s)")
            relay = threading.Thread(target=stdout_relay, args=(ff, timeline), daemon=True)
            relay.start()
        if STALL_S > 0:
            threading.Thread(target=self.upstream.stall_watchdog, daemon=True).start()

        def on_term(signum, frame):
            self.shutdown()
            try:
                ff.stdin.close()
            except Exception:
                pass
            try:
                ff.terminate()
            except Exception:
                pass
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)

        try:
            self.pump(ff, timeline)
        except (BrokenPipeError, OSError) as e:
            log(f"stream consumer gone ({type(e).__name__}); shutting down")
        except ValueError:
            # on_term (signal handler, runs in this thread) closes ff.stdin - the
            # close is deliberate: it unblocks a write wedged on a full pipe. But
            # if the signal lands between take() and the write, the write
            # hits a *closed* file object and raises ValueError, not OSError.
            # Expected only during shutdown; anything else is a real bug.
            if not self.stop.is_set():
                raise
            log("shutdown during write (ff.stdin closed by signal handler)")
        finally:
            self.shutdown()
            try:
                ff.stdin.close()
            except Exception:
                pass
            try:
                ff.terminate()
                ff.wait(timeout=5)
            except Exception:
                try:
                    ff.kill()
                except Exception:
                    pass
            if relay:
                relay.join(timeout=RELAY_DRAIN_S)
            if timeline:
                timeline.save(time.time())
                if timeline.rejected:
                    log(f"ts timeline: ignored {timeline.rejected} output PTS more than "
                        f"{TS_MAX_LEAD_S:.0f}s ahead of the wall clock")
            log(f"stream wrapper exit (ffmpeg rc={ff.returncode})")

    def pump(self, ff, timeline):
        """Release the reservoir to ffmpeg's stdin at the content rate, and log
        the telemetry line every STATS_EVERY_S - also while the reservoir is
        dry, when the cushion it reports is the one thing worth knowing."""
        pacer = Pacer(self.parser, self.reservoir)
        out_since_stats = 0
        last_stats = time.monotonic()
        tail = b""                                                    # see align_to_188(): ffmpeg expects 188-aligned writes
        while not self.stop.is_set():
            d, released_total = self.reservoir.take(timeout=0.5)
            if d is None:
                break
            level = pacer.measure(released_total)
            if d:
                pacer.pace(len(d), level)
                aligned, tail = align_to_188(tail, d)
                if aligned:
                    ff.stdin.write(aligned)
                    out_since_stats += len(aligned)
            now = time.monotonic()
            if timeline and now - timeline.saved_at >= TS_SAVE_EVERY_S:
                timeline.save(time.time())
            if now - last_stats >= STATS_EVERY_S:
                self.report(level, out_since_stats / (now - last_stats))
                out_since_stats = 0
                last_stats = now

    def report(self, level, orate):
        p, res = self.parser, self.reservoir
        src = "pcr" if level.cush_pcr is not None else "byte"
        vholes, aholes = p.take_holes_pct()
        log(f"cushion={level.cush:.0f}s({src}) buf={res.bytes / 1e6:.1f}MB "
            f"out={orate * 8 / 1e6:.2f}Mbps in={res.in_rate() * 8 / 1e6:.2f}Mbps "
            f"crate={(level.crate or 0) * 8 / 1e6:.2f}Mbps in_total={res.in_total / 1e6:.0f}MB "
            f"reconnects={self.upstream.reconnects} ccerr={p.cc_errors} "
            f"pcrrej={p.pcr_rejects} disc={p.pcr_disc} sync={p.sync_losses} "
            f"pcr_back={p.pcr_back} vholes={vholes}% aholes={aholes}%",
            stderr=False)
        detail = self.detector.window(p.cc_errors, p.sync_losses, res.in_total)
        if detail:
            if TS_RECONNECT:
                self.upstream.request_reconnect(
                    f"TS corruption detected ({detail}) - forcing upstream reconnect + buffer flush")
            else:
                log(f"would-fire: TS corruption detected ({detail}); "
                    f"RESV_TS_RECONNECT=0 (log-only)", stderr=False)


def main():
    if AUDIO_LANG and not AUDIO_LANG_OK:
        log(f"RESV_AUDIO_LANG={AUDIO_LANG!r} is not a 3-letter code (e.g. fra); ignored")
    if FLOOR_REQUESTED and not FLOOR_OK:
        log(f"RESV_FLOOR={FLOOR_REQUESTED!r} is not a decimal above 0 and at most 1 (e.g. 0.97); "
            f"releasing the stream at no less than realtime (1.0)")
    announce_audio_codec()
    Stream(URL, UA).run()


if __name__ == "__main__":
    main()
