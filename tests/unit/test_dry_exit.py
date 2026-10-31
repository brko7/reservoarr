"""6.7.0: with the reservoir empty and nothing arriving, the process ends so
Dispatcharr can reconnect or fail over straight away, instead of noticing
through its health monitor 20-25s later (discussion #46). A process that has
delivered nothing stays: it is waiting for its first byte."""
from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path

PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184


class FakeFfmpeg:
    def __init__(self):
        self.stdin = io.BytesIO()


class Response:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        pass


def pump_in_thread(stream, ff=None):
    t = threading.Thread(target=stream.pump, args=(ff or FakeFfmpeg(), None), daemon=True)
    t.start()
    return t


def outage(stream, silent_s, data=PACKET):
    """State of a process that delivered `data` and has heard nothing since."""
    stream.parser.cum_pcr = 10.0
    stream.reservoir.put(data)
    stream.upstream.last_arrival = time.monotonic() - silent_s


def log_text(resv):
    return Path(resv.LOG_FILE).read_text()


def test_exits_once_the_reservoir_is_dry_and_nothing_arrives(resv, stream, capsys):
    outage(stream, 6.0)
    t = pump_in_thread(stream)
    t.join(2)
    assert not t.is_alive()
    assert "upstream down and reservoir dry (no data " in capsys.readouterr().err


def test_the_exit_line_stays_out_of_dispatcharrs_error_keywords(resv, stream, capsys):
    outage(stream, 6.0)
    pump_in_thread(stream).join(2)
    line = next(ln for ln in capsys.readouterr().err.splitlines() if "reservoir dry" in ln)
    assert not any(word in line.lower() for word in ("error", "failed", "cannot", "invalid", "corrupt"))


def test_everything_left_in_the_reservoir_goes_out_first(resv, stream):
    data = PACKET * 2000
    outage(stream, 60.0, data)
    ff = FakeFfmpeg()
    t = pump_in_thread(stream, ff)
    t.join(10)
    assert not t.is_alive()
    assert ff.stdin.getvalue() == data


def test_stays_while_recently_fed(resv, stream):
    outage(stream, 0.0)
    t = pump_in_thread(stream)
    time.sleep(1.2)
    assert t.is_alive()
    stream.shutdown()
    t.join(2)


def test_stays_when_nothing_has_been_delivered(resv, stream):
    stream.parser.cum_pcr = 10.0
    stream.upstream.last_arrival = time.monotonic() - 60.0        # arrived, but nothing went to ffmpeg
    t = pump_in_thread(stream)
    time.sleep(1.2)
    assert t.is_alive()
    stream.shutdown()
    t.join(2)


def test_a_process_that_never_got_a_byte_stays(resv, stream):
    t = pump_in_thread(stream)
    time.sleep(1.2)
    assert t.is_alive()
    stream.shutdown()
    t.join(2)


def test_junk_without_a_pcr_does_not_count_as_delivered(resv, stream):
    stream.reservoir.put(PACKET)
    stream.upstream.last_arrival = time.monotonic() - 60.0
    t = pump_in_thread(stream)
    time.sleep(1.2)
    assert t.is_alive()
    stream.shutdown()
    t.join(2)


def test_zero_turns_it_off(resv, stream):
    resv.DRY_EXIT_S = 0.0
    outage(stream, 60.0)
    t = pump_in_thread(stream)
    time.sleep(1.2)
    assert t.is_alive()
    stream.shutdown()
    t.join(2)


def test_a_slow_channel_gets_two_of_its_chunk_times(resv, stream):
    """r.read(CHUNK) only returns once it has the whole CHUNK: at 100 kbps a
    healthy read takes 7.5s, longer than the exit threshold."""
    outage(stream, 6.0)
    rate = 12_500
    stream.parser.rate_marks.extend([(0, 0.0), (rate * 10, 10.0)])
    assert abs(stream.parser.chunk_s() - resv.CHUNK / rate) < 1e-6
    assert stream.dry_exit_due() is None
    stream.upstream.last_arrival = time.monotonic() - (2 * resv.CHUNK / rate + 0.1)
    assert stream.dry_exit_due() is not None


def test_bytes_held_or_dropped_by_the_replay_gate_count_as_arriving(resv, stream):
    resv.REPLAY_SKIP = True
    up = stream.upstream
    up.parser.pcr_pid, up.parser.last_pcr = 0x100, 100.0
    up.last_arrival = time.monotonic() - 60.0
    up.replay.arm()
    before = up.reservoir.in_total
    up.drain(Response([PACKET * 10]))
    assert up.reservoir.in_total == before, "the gate should still be holding these"
    assert time.monotonic() - up.last_arrival < 1.0


def test_a_flush_only_ends_the_process_if_the_reconnect_does_not_come(resv, stream):
    """A corrupt-loop flush empties the reservoir on purpose, and its reconnect
    takes a second or two. The clock is the last arrival, not the flush."""
    outage(stream, 3.0)
    stream.reservoir.flush()
    assert stream.dry_exit_due() is None
    stream.upstream.last_arrival = time.monotonic() - 6.0
    assert stream.dry_exit_due() is not None


def test_retries_come_every_second_until_the_dry_exit(resv, stream):
    up = stream.upstream
    up.backoff = 4
    assert up.retry_wait() == 4, "nothing delivered yet: the backoff holds"
    up.last_arrival = time.monotonic() - 2.0
    assert up.retry_wait() == 1
    up.reservoir.put(PACKET)
    assert up.retry_wait() == 4, "cushion left: no hurry"
    up.reservoir.flush()
    up.last_arrival = time.monotonic() - 6.0
    assert up.retry_wait() == 4, "past the exit (a process staying): back off again"


def stays(stream):
    t = pump_in_thread(stream)
    time.sleep(1.2)
    alive = t.is_alive()
    stream.shutdown()
    t.join(2)
    return alive


def test_a_second_outage_on_the_url_within_the_window_rides_it_out(resv, tmp_path, capsys):
    """Three exits end a channel with no other stream, and a short blip clears
    nothing in between. Measured on Dispatcharr 0.33: the third 8s blip."""
    first = resv.Stream(resv.URL, resv.UA)
    outage(first, 6.0)
    pump_in_thread(first).join(2)
    assert "exiting so Dispatcharr" in capsys.readouterr().err

    second = resv.Stream(resv.URL, resv.UA)
    outage(second, 6.0)
    assert stays(second)
    err = capsys.readouterr().err
    assert "exiting" not in err
    assert err.count("staying, at most one exit per stream URL every 31 min (last exit ") == 1


def test_the_exit_is_allowed_again_after_the_window(resv, tmp_path):
    now = time.time()
    assert resv.claim_dry_exit(resv.URL, now) is None
    assert resv.claim_dry_exit(resv.URL, now + resv.DRY_EXIT_EVERY_S - 1) == resv.DRY_EXIT_EVERY_S - 1
    assert resv.claim_dry_exit(resv.URL, now + resv.DRY_EXIT_EVERY_S) is None


def test_the_window_outlasts_dispatcharrs_retry_window(resv):
    assert resv.DRY_EXIT_EVERY_S > 1800


def test_another_url_has_its_own_exit(resv):
    now = time.time()
    assert resv.claim_dry_exit("http://a.test/1.ts", now) is None
    assert resv.claim_dry_exit("http://a.test/2.ts", now) is None
    assert resv.claim_dry_exit("http://a.test/1.ts", now) is not None


def test_a_marker_from_the_future_does_not_block_forever(resv):
    now = time.time()
    assert resv.claim_dry_exit(resv.URL, now + 86400) is None
    assert resv.claim_dry_exit(resv.URL, now) is None


def test_a_marker_that_cannot_be_written_means_staying(resv, stream, tmp_path, capsys):
    resv.LOG_DIR = str(tmp_path / "missing")
    assert resv.claim_dry_exit(resv.URL, time.time()) == float("inf")
    outage(stream, 6.0)
    assert stays(stream)
    assert "marker not writable" in capsys.readouterr().err


def test_the_marker_name_carries_no_part_of_the_url(resv, tmp_path):
    secret = "http://user:hunter2@edge.test/live/hunter2.ts"
    assert resv.claim_dry_exit(secret, time.time()) is None
    names = [n for n in os.listdir(tmp_path) if n.startswith("dryexit-")]
    assert len(names) == 1
    assert "hunter2" not in names[0]


def test_the_staying_line_stays_out_of_dispatcharrs_error_keywords(resv, stream, capsys):
    resv.claim_dry_exit(resv.URL, time.time())
    outage(stream, 6.0)
    assert stays(stream)
    line = next(ln for ln in capsys.readouterr().err.splitlines() if "staying" in ln)
    assert not any(word in line.lower() for word in ("error", "failed", "cannot", "invalid", "corrupt"))


def test_the_exit_is_in_the_log_file(resv, stream):
    outage(stream, 6.0)
    t = pump_in_thread(stream)
    t.join(2)
    assert not t.is_alive()
    assert "reservoir dry" in log_text(resv)
