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

Invoked by Dispatcharr as: reservoir.py {streamUrl} {userAgent}
stdout -> Dispatcharr relay pipe (must carry ONLY the TS stream)
stderr -> Dispatcharr's transcode logger (INFO only for lines containing
          stream/input/output/video/audio; everything else lands at DEBUG)
stats  -> /data/scripts/logs/delaybuf.log

Dispatcharr stops the channel by signalling THIS pid only: on SIGTERM we
reap ffmpeg; on SIGKILL the broken pipes make ffmpeg exit on its own.
"""
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

if len(sys.argv) < 2 or not sys.argv[1].strip():
    sys.stderr.write("[delaybuf] usage: reservoir.py <streamUrl> [userAgent]\n")
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
GRACE_S = float(os.getenv("RESV_GRACE_S", "45"))                     # release floor 1.0 this long (see main)
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
TS_WRAP_S = (1 << 33) / 90000.0                                       # PCR base wraps every ~26.5h

FFMPEG_BIN = os.getenv("RESV_FFMPEG_BIN", "/usr/local/bin/ffmpeg")    # Dispatcharr AIO container default
FFMPEG_CMD = [
    FFMPEG_BIN, "-hide_banner", "-loglevel", "warning",
    "-fflags", "+nobuffer",
    "-analyzeduration", "1000000", "-probesize", "500000",
    "-i", "pipe:0",
    "-map", "0:v", "-map", "0:a:0",
    "-c:v", "copy", "-bsf:v", "dump_extra=freq=keyframe",
    "-c:a", "ac3", "-b:a", "192k", "-ac", "2",
    "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "pipe:1",
]

buf = deque()
buf_bytes = 0
in_total = 0
reconnects = 0
upstream_eof = False
stop = threading.Event()
cond = threading.Condition()
in_marks = deque()                                                    # (timestamp, in_total) samples
cur_response = None                                                   # live urllib response (for forced close)
force_reconnect = threading.Event()
corrupt_seen = deque()                                                # (timestamp, dts) from ffmpeg stderr
last_forced_flush = 0.0                                               # corrupt-loop / #5 debounce (flush=True)
last_forced_stall = 0.0                                               # stall watchdog #4 debounce (flush=False)
flush_pending = False                                                 # sticky flush request: set by a flush caller, cleared by the fetcher after it flushes

# Last path segment, for log tagging. Strip any query string and cap length so
# a future provider URL shape can't leak an embedded key into the logs.
STREAM_ID = URL.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1][:48] or "stream"
LOG_DIR = os.getenv("RESV_LOG_DIR", "/data/scripts/logs")              # Dispatcharr AIO container default
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "delaybuf.log")
try:
    if os.path.getsize(LOG_FILE) > 10 * 1024 * 1024:
        os.replace(LOG_FILE, LOG_FILE + ".1")
except OSError:
    pass


def log(msg, stderr=True):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} [{STREAM_ID}] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if stderr:
        sys.stderr.write(f"[delaybuf] {msg}\n")
        sys.stderr.flush()


class TsParser:
    """Observes the ingest byte stream at TS-packet level. Two jobs:

    1. PCR clock: bytes-per-PCR-second is the content's true byte rate -
       the pacing reference. Garbage PCRs (the CDN corruption family that
       freezes -re) are rejected by a plausibility window on the wrap-aware
       delta; a wedged chain re-anchors after a run of rejects. The cum_pcr
       clock plus (in_total, cum_pcr) marks also give the cushion in real
       seconds of media instead of a byte estimate.
    2. Continuity counters per PID: telemetry only in v6 (counted, logged,
       never acted on) - evidence first, actions later.

    Deque append/popleft are thread-safe; the fetcher feeds, the pacer
    reads. Stats counters are plain ints (GIL-atomic reads, drift is fine).
    """

    def __init__(self):
        self.cc_errors = 0
        self.sync_losses = 0
        self.pcr_rejects = 0
        self.pcr_disc = 0
        self.cum_pcr = 0.0
        self.rate_marks = deque()                                     # (in_total, cum_pcr), ~90s content span
        self.cush_marks = deque()                                     # (in_total, cum_pcr), popped as released
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

    def feed(self, data):
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
        n = len(buf_)
        i = buf_.find(0x47, start)
        while 0 <= i:
            if i + 376 >= n:
                return -1                                             # not enough data to confirm; carry
            if buf_[i + 188] == 0x47 and buf_[i + 376] == 0x47:
                self.synced = True
                return i
            i = buf_.find(0x47, i + 1)
        return -1

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
        base = (buf_[i + 6] << 25 | buf_[i + 7] << 17 | buf_[i + 8] << 9
                | buf_[i + 9] << 1 | buf_[i + 10] >> 7)
        ext = (buf_[i + 10] & 0x01) << 8 | buf_[i + 11]
        pcr = (base * 300 + ext) / 27_000_000.0
        if self.last_pcr is None:
            self.last_pcr = pcr
            return
        if pcr == self.last_pcr:                                      # duplicate packet (legal): not garbage
            return
        if disc:
            self.pcr_disc += 1
            self.last_pcr = pcr                                       # legal jump: re-anchor, no cum advance
            return
        delta = (pcr - self.last_pcr) % TS_WRAP_S
        if 0.0 < delta < 10.0:
            self.reject_run = 0
            self.cum_pcr += delta
            self.last_pcr = pcr
            mark = (in_total, self.cum_pcr)
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


parser = TsParser()


def in_rate():
    """Rolling arrival byte rate (fallback when PCR is unavailable). The
    window ends at *now*, not the last arrival - ending at the last arrival
    excluded in-progress gaps and overstated the rate by ~4% on a bursty
    feed, which cancelled the 0.97 release floor (v5 cushion bug)."""
    now = time.time()
    while len(in_marks) > 2 and now - in_marks[0][0] > RATE_WINDOW_S:
        in_marks.popleft()
    if len(in_marks) < 2:
        return RATE_FLOOR
    (t0, b0) = in_marks[0]
    b1 = in_marks[-1][1]
    if now - t0 < 5:
        return RATE_FLOOR
    return max((b1 - b0) / (now - t0), RATE_FLOOR)


def force_upstream_reconnect(reason, flush=True):
    """Forced reconnect shared by the corrupt-loop / #5 detectors (flush=True —
    buffer is poisoned) and the stall watchdog (#4, flush=False — buffer is good).
    Debounced PER CLASS (<=1 per 90s each) so a benign stall can never rate-limit
    away a corrupt flush; the flush request is sticky and is never downgraded by a
    concurrent no-flush. Returns True if it fired, False if rate-limited."""
    global last_forced_flush, last_forced_stall, flush_pending
    now = time.time()
    with cond:
        if flush:
            if now - last_forced_flush <= 90:
                return False
            last_forced_flush = now
            flush_pending = True                                      # sticky; the fetcher clears it after flushing
        else:
            if now - last_forced_stall <= 90:
                return False
            last_forced_stall = now                                   # leave flush_pending alone (never downgrade a flush)
        force_reconnect.set()
    log(reason)
    try:
        cur_response.close()                                          # break the blocking read
    except Exception:
        pass
    return True


def stall_watchdog():
    """No ingest progress for STALL_S while running => silently wedged upstream
    (socket open, no data, not EOF). urlopen's own timeout is 30s, by which point
    the cushion has drained; this trips sooner and reconnects WITHOUT flushing, to
    grab a fresh front-load while the good buffer keeps draining. STALL_S sits
    above the CDN's normal burst-gap so a normal gap never trips it."""
    last_in = -1
    last_adv = time.time()
    while not stop.wait(2):
        cur = in_total
        if cur != last_in:
            last_in = cur
            last_adv = time.time()
        elif time.time() - last_adv > STALL_S and not force_reconnect.is_set():
            if force_upstream_reconnect(
                    f"upstream stalled (no data {time.time() - last_adv:.0f}s) - reconnecting, buffer kept",
                    flush=False):
                last_adv = time.time()


def register_corrupt(dts):
    """CDN edges sometimes wedge a single long-lived connection into serving
    the same corrupt packet in a loop, while fresh connections are clean
    (verified live 2026-06-12). Same dts reported 3x => reconnect + flush."""
    now = time.time()
    corrupt_seen.append((now, dts))
    while corrupt_seen and now - corrupt_seen[0][0] > 120:
        corrupt_seen.popleft()
    same = sum(1 for _, d in corrupt_seen if d == dts)
    if same >= 3:
        force_upstream_reconnect(
            f"corrupt-loop detected in stream (dts={dts} x{same}) - forcing upstream reconnect + buffer flush")


def stderr_watcher(ff):
    """Relay ffmpeg stderr (Dispatcharr's logger reads ours) and detect loops."""
    pat = re.compile(rb"Packet corrupt \(stream = \d+, dts = (\d+)\)")
    for raw in iter(ff.stderr.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            log(f"ffmpeg: {line}")
        m = pat.search(raw)
        if m:
            register_corrupt(int(m.group(1)))


def fetcher():
    global buf_bytes, in_total, reconnects, upstream_eof, cur_response, flush_pending
    backoff = 1
    first = True
    while not stop.is_set():
        try:
            if force_reconnect.is_set():
                with cond:
                    do_flush = flush_pending
                    if do_flush:
                        buf.clear()
                        buf_bytes = 0
                        flush_pending = False                         # consume the sticky flush request
                    force_reconnect.clear()
                if do_flush:
                    log("flushed reservoir after corrupt-loop reconnect")
            req = urllib.request.Request(URL, headers={"User-Agent": UA})
            r = urllib.request.urlopen(req, timeout=30)
            cur_response = r
            parser.on_reconnect()
            edge = r.geturl().split("/")[2]
            log(f"upstream connected edge={edge}")
            if not first:
                reconnects += 1
            first = False
            while not stop.is_set():
                if force_reconnect.is_set():
                    break                                            # corrupt-loop/stall: break clean (flush+reconnect)
                try:
                    d = r.read(CHUNK)
                except Exception:
                    if force_reconnect.is_set():
                        break                                        # our own close() unblocked the read; not an error
                    raise                                            # genuine read error -> backoff path below
                if not d:
                    log("upstream EOF")
                    break
                backoff = 1                                           # reset only once data flows: an empty
                #                                                       connect must keep backing off, not
                #                                                       hammer the provider at 1/s forever
                with cond:
                    while buf_bytes >= MAX_BYTES and not stop.is_set():
                        cond.wait(1)
                    buf.append(d)
                    buf_bytes += len(d)
                    in_total += len(d)
                    in_marks.append((time.time(), in_total))
                    cond.notify_all()
                parser.feed(d)                                        # observe outside the lock
        except Exception as e:
            log(f"upstream error {type(e).__name__}: {e}; retry in {backoff}s")
        if stop.is_set():
            break
        if force_reconnect.is_set():
            continue                                                 # intentional reconnect: flush + reconnect now, no backoff
        time.sleep(backoff)
        backoff = min(backoff * 2, 8)
    upstream_eof = True
    with cond:
        cond.notify_all()


def next_slice():
    """Pop up to PACE_SLICE bytes from the reservoir, returning the released-byte
    total snapshotted under the same lock (in_total and buf_bytes both move under
    cond in the fetcher, so reading them together here avoids a torn read).
    Returns (None, 0) on stop / EOF-drained."""
    global buf_bytes
    with cond:
        while not buf and not upstream_eof and not stop.is_set():
            cond.wait(0.5)
        if stop.is_set() or (not buf and upstream_eof):
            return None, 0
        d = buf.popleft()
        if len(d) > PACE_SLICE:
            buf.appendleft(d[PACE_SLICE:])
            d = d[:PACE_SLICE]
        buf_bytes -= len(d)
        released = in_total - buf_bytes                               # released or flushed bytes
        cond.notify_all()
        return d, released


def main():
    t = threading.Thread(target=fetcher, daemon=True)
    t.start()

    t0 = time.time()
    with cond:
        while buf_bytes < PREFILL_BYTES and time.time() - t0 < PREFILL_MAX_S and not stop.is_set():
            cond.wait(0.5)
    log(f"prefill done: {buf_bytes / 1e6:.1f}MB in {time.time() - t0:.1f}s, releasing stream to ffmpeg")

    ff = subprocess.Popen(FFMPEG_CMD, stdin=subprocess.PIPE, stdout=None, stderr=subprocess.PIPE, bufsize=0)
    threading.Thread(target=stderr_watcher, args=(ff,), daemon=True).start()
    if STALL_S > 0:
        threading.Thread(target=stall_watchdog, daemon=True).start()

    def on_term(signum, frame):
        stop.set()
        with cond:
            cond.notify_all()
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

    out_since_stats = 0
    last_stats = time.time()
    release_start = time.time()                                       # set once; intentionally NOT reset on
    #                                                                   reconnect (no per-seam headstart re-dump)
    headstart_bytes = 0                                               # byte backstop before PCR lock
    pace_debt = 0.0                                                   # seconds owed to pacing
    last_pace = time.time()
    prev_ccerr = prev_sync = prev_in_total = 0                        # #5 detector: per-window deltas
    ts_bad_wins = 0
    try:
        while not stop.is_set():
            d, released_total = next_slice()
            if d is None:
                break
            now = time.time()
            crate = parser.content_rate()
            rate = crate if crate is not None else in_rate()
            cush_pcr = parser.cushion_s(released_total)
            cush = cush_pcr if cush_pcr is not None else buf_bytes / rate
            # Head start: HEADSTART_S seconds of *content* go out unpaced so
            # Plex gets its startup buffer fast; everything else of the CDN's
            # per-connection front-load burst stays banked in the reservoir.
            released_s = parser.cum_pcr - cush_pcr if cush_pcr is not None else None
            in_headstart = (now - release_start < 15
                            and (released_s < HEADSTART_S if released_s is not None
                                 else headstart_bytes < 2560 * 1024))
            if in_headstart:
                headstart_bytes += len(d)
            else:
                # Hold the cushion near TARGET_S around the content rate.
                # Floor 0.97: releasing far below realtime starves the player
                # to feed the reservoir; the cushion comes from front-load and
                # burst surplus instead. For the first GRACE_S the floor is
                # 1.0 - the player owns only the small headstart then, and a
                # sub-realtime feed could drain it before the bank settles.
                floor = 1.0 if now - release_start < GRACE_S else 0.97
                level_err = (cush - TARGET_S) / TARGET_S
                r = rate * min(max(1.0 + 0.3 * level_err, floor), 1.15)
                pace_debt += len(d) / r
                pace_debt -= (now - last_pace)
                last_pace = now
                if pace_debt > 0.005:
                    time.sleep(pace_debt)
                    pace_debt = 0.0
                    last_pace = time.time()
                elif pace_debt < -2.0:
                    pace_debt = 0.0                                   # don't bank idle time
            ff.stdin.write(d)
            out_since_stats += len(d)
            now = time.time()
            if now - last_stats >= STATS_EVERY_S:
                orate = out_since_stats / (now - last_stats)
                src = "pcr" if cush_pcr is not None else "byte"
                log(f"cushion={cush:.0f}s({src}) buf={buf_bytes / 1e6:.1f}MB "
                    f"out={orate * 8 / 1e6:.2f}Mbps in={in_rate() * 8 / 1e6:.2f}Mbps "
                    f"crate={(crate or 0) * 8 / 1e6:.2f}Mbps in_total={in_total / 1e6:.0f}MB "
                    f"reconnects={reconnects} ccerr={parser.cc_errors} "
                    f"pcrrej={parser.pcr_rejects} disc={parser.pcr_disc} sync={parser.sync_losses}",
                    stderr=False)
                # #5 ingest-side corruption detector: sustained CC/sync errors
                # while bytes still flow => wedged/corrupt upstream (fires ~30s
                # before the ffmpeg-stderr loop trigger). NOT pcrrej (recoverable).
                dcc = parser.cc_errors - prev_ccerr
                dsy = parser.sync_losses - prev_sync
                ingest_adv = in_total - prev_in_total
                prev_ccerr, prev_sync, prev_in_total = parser.cc_errors, parser.sync_losses, in_total
                if ingest_adv > 0 and (dcc >= CC_ERR_PER_WIN or dsy >= SYNC_ERR_PER_WIN):
                    ts_bad_wins += 1
                else:
                    ts_bad_wins = 0
                if ts_bad_wins >= TS_SUSTAIN_WINS:
                    detail = f"+{dcc} ccerr/+{dsy} sync this window x{ts_bad_wins} windows"
                    if TS_RECONNECT:
                        force_upstream_reconnect(
                            f"TS corruption detected ({detail}) - forcing upstream reconnect + buffer flush")
                    else:
                        log(f"would-fire: TS corruption detected ({detail}); "
                            f"RESV_TS_RECONNECT=0 (log-only)", stderr=False)
                    ts_bad_wins = 0
                out_since_stats = 0
                last_stats = now
    except (BrokenPipeError, OSError) as e:
        log(f"stream consumer gone ({type(e).__name__}); shutting down")
    finally:
        stop.set()
        with cond:
            cond.notify_all()
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
        log(f"stream wrapper exit (ffmpeg rc={ff.returncode})")


if __name__ == "__main__":
    main()
