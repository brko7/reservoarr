"""6.4.0: what happens at the edges of an upstream connection.

- Whole packets only: a connection that starts or ends mid-packet used to
  shift every later write to ffmpeg off the 188-byte grid for the rest of the
  tune (align_to_188 counts bytes; it cannot see a seam).
- A forced reconnect really cuts a silent connection: response.close() from
  another thread waits for the buffered reader's lock, i.e. for the 30s socket
  timeout, so the stall watchdog's 25s trip used to change nothing.
- The edge in the log never carries user:password@.
"""
from __future__ import annotations

import http.server
import threading
import time
import urllib.request

import pytest


def pkt(n):
    return bytes([0x47, 0x00, 0x42, 0x10 | (n & 0x0F)]) + bytes([n & 0xFF]) * 184


def packets(*ns):
    return b"".join(pkt(n) for n in ns)


def starts_on_the_grid(data):
    return len(data) % 188 == 0 and all(data[i] == 0x47 for i in range(0, len(data), 188))


# ----- PacketAligner -----

def test_a_lead_before_the_first_packet_is_dropped(resv):
    a = resv.PacketAligner()
    out = a.feed(b"\xab" * 50 + packets(1, 2, 3))
    assert out == packets(1, 2, 3)
    assert a.lead == 50
    assert a.finish() == (b"", 0)


def test_the_unfinished_last_packet_is_dropped_at_the_end(resv):
    a = resv.PacketAligner()
    out = a.feed(packets(1, 2, 3) + pkt(4)[:100])
    assert out == packets(1, 2, 3)
    assert a.finish() == (b"", 100)


@pytest.mark.parametrize("size", [1, 47, 187, 188, 189, 1000, 4096])
def test_odd_reads_come_out_whole_and_in_order(resv, size):
    data = b"\xab" * 7 + packets(*range(40)) + pkt(40)[:33]
    a = resv.PacketAligner()
    out = b"".join(a.feed(data[i:i + size]) for i in range(0, len(data), size))
    rest, dropped = a.finish()
    assert out == packets(*range(40))
    assert (rest, dropped, a.lead) == (b"", 33, 7)


def test_a_connection_without_ts_passes_through_then_back_onto_the_grid(resv, capsys):
    """Not a TS at all (an MP4 clip with RESV_REJECT_MP4=0): ffmpeg gets it as
    before and fails on it, instead of the reservoir holding it back silently.
    The filler at the end puts the next connection back on the grid."""
    body = b"\x00\x00\x00 ftypisom" * (resv.CHUNK // 12 + 1)
    a = resv.PacketAligner()
    assert a.feed(body) == body
    assert a.feed(b"more") == b"more"
    rest, dropped = a.finish()
    assert rest == b"\xff" * (-(len(body) + 4) % 188) and dropped == 0
    assert (len(body) + 4 + len(rest)) % 188 == 0
    assert "no TS sync in its first" in capsys.readouterr().err


def test_a_short_answer_without_ts_still_goes_on_at_the_end(resv):
    a = resv.PacketAligner()
    assert a.feed(b"tiny") == b""
    assert a.finish() == (b"tiny" + b"\xff" * 184, 0)


# ----- through the real fetcher -----

class Response:
    def __init__(self, chunks, url="http://edge.test/live/stream.ts"):
        self._chunks = list(chunks)
        self.url = url

    def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def geturl(self):
        return self.url

    def close(self):
        pass


@pytest.fixture
def fetch(resv, stream, monkeypatch):
    """Run the real fetcher over scripted connections, then stop."""
    def go(responses):
        answers = list(responses)

        def urlopen(*_a, **_k):
            if not answers:
                stream.stop.set()
                raise OSError("done")
            return answers.pop(0)

        monkeypatch.setattr(resv.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(resv.time, "sleep", lambda _s: None)
        stream.upstream.run()
        return b"".join(stream.reservoir.chunks)

    return go


def test_a_seam_mid_packet_keeps_the_reservoir_on_the_grid(fetch, tmp_path):
    """The 6.3.x failure: connection 1 ends 100 bytes into a packet, connection
    2 starts 50 bytes before one. Byte counting alone put every packet after
    the seam off the grid."""
    held = fetch([
        Response([packets(1, 2, 3) + pkt(4)[:100]]),
        Response([b"\xab" * 50 + packets(5, 6, 7)]),
    ])
    assert held == packets(1, 2, 3, 5, 6, 7)
    assert starts_on_the_grid(held)
    log = (tmp_path / "delaybuf.log").read_text()
    assert "dropped 0B before its first TS packet and 100B of an unfinished last packet" in log
    assert "dropped 50B before its first TS packet and 0B" in log


@pytest.mark.parametrize("answer", [
    b"Max connections reached\n",                          # error text non_ts_kind does not know
    pkt(1)[50:] + pkt(2)[:20],                             # a fragment too short to confirm a sync
])
def test_a_short_unaligned_answer_mid_tune_leaves_the_grid_intact(fetch, answer):
    """Review finding: a short 200 answer without sync went into the reservoir
    as-is, and everything after it was off the grid for the rest of the tune."""
    held = fetch([
        Response([packets(1, 2, 3)]),
        Response([answer]),
        Response([packets(4, 5, 6)]),
    ])
    assert len(held) % 188 == 0
    assert held.endswith(packets(4, 5, 6))
    assert held[-188 * 6:-188 * 3] != packets(4, 5, 6)          # the answer is in between
    assert starts_on_the_grid(held[-188 * 3:])


def test_a_clean_connection_logs_no_drop(fetch, tmp_path):
    fetch([Response([packets(1, 2, 3, 4)])])
    assert "dropped" not in (tmp_path / "delaybuf.log").read_text()


def test_what_reaches_ffmpeg_stays_on_the_grid_across_a_seam(resv, fetch):
    """align_to_188 over what the reservoir releases, exactly as pump() does."""
    held = fetch([
        Response([packets(1, 2, 3) + pkt(4)[:100]]),
        Response([b"\xab" * 50 + packets(5, 6, 7)]),
    ])
    tail, written = b"", b""
    for i in range(0, len(held), 77):
        out, tail = resv.align_to_188(tail, held[i:i + 77])
        written += out
    assert written == packets(1, 2, 3, 5, 6, 7) and tail == b""


def test_the_edge_in_the_log_carries_no_credentials(fetch, capsys):
    fetch([Response([packets(1, 2, 3)], url="http://user:s3cret@edge.test:8080/live/1.ts")])
    err = capsys.readouterr().err
    assert "upstream connected edge=edge.test:8080" in err
    assert "s3cret" not in err


@pytest.mark.parametrize(("url", "edge"), [
    ("http://edge.test/x", "edge.test"),
    ("https://edge.test:8443/x?token=1", "edge.test:8443"),
    ("http://u:p@[::1]:80/x", "[::1]:80"),
    ("http://u@edge.test/x", "edge.test"),
])
def test_edge_of(resv, url, edge):
    assert resv.edge_of(url) == edge


# ----- forced reconnect on a silent socket -----

class SilentEdge(http.server.ThreadingHTTPServer):
    """Sends a few packets per connection, then holds the socket open without
    a byte - the wedged edge the stall watchdog exists for."""
    daemon_threads = True

    def __init__(self):
        self.connections = 0
        self.release = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(h):
                self.connections += 1
                h.send_response(200)
                h.end_headers()
                h.wfile.write(packets(*range(10)))
                h.wfile.flush()
                self.release.wait(20)

            def log_message(h, *_args):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}/live/1.ts"

    def stop(self):
        self.release.set()
        self.shutdown()
        self.server_close()


@pytest.fixture
def edge():
    srv = SilentEdge()
    yield srv
    srv.stop()


def wait_for(cond, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class Closable:
    def __init__(self, name, aborted):
        self.name, self.aborted = name, aborted

    def close(self):
        self.aborted.append(self.name)


def test_a_reconnect_request_never_cuts_the_connection_it_causes(resv, stream, monkeypatch):
    """Review finding: the abort read self.response after releasing the lock.
    With data flowing the fetcher could take the request, flush and publish the
    NEXT connection in that window, and the abort cut the fresh one."""
    up = stream.upstream
    aborted = []
    up.response = Closable("old", aborted)
    real_log = resv.log

    def fetcher_wins_the_race(msg, *a, **k):
        assert up.take_request() is True                  # the fetcher takes the request...
        up.response = Closable("new", aborted)            # ...and publishes the next connection
        real_log(msg, *a, **k)

    monkeypatch.setattr(resv, "log", fetcher_wins_the_race)
    assert up.request_reconnect("t:corrupt", flush=True) is True
    assert aborted == ["old"]


def test_abort_read_unblocks_a_read_on_a_silent_socket(resv, edge):
    r = urllib.request.urlopen(edge.url, timeout=30)
    done = threading.Event()

    def reader():
        try:
            while r.read(resv.CHUNK):
                pass
        except Exception:
            pass
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.2)                                       # let it block in recv
    t0 = time.monotonic()
    resv.abort_read(r)
    assert done.wait(3), "the read stayed blocked (it would until the 30s socket timeout)"
    assert time.monotonic() - t0 < 3


def test_a_forced_reconnect_cuts_a_silent_connection_at_once(resv, stream, edge, tmp_path):
    up = resv.Upstream(edge.url, "test", stream.reservoir, stream.parser, stream.stop)
    t = threading.Thread(target=up.run, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: edge.connections >= 1, 5)
        time.sleep(0.3)                                   # r.read(CHUNK) now blocks: 10 packets < one CHUNK
        assert up.request_reconnect("t:stall", flush=False) is True
        assert wait_for(lambda: edge.connections >= 2, 3), (
            "no new connection within 3s; urlopen's 30s timeout was doing the work"
        )
    finally:
        stream.stop.set()
        resv.abort_read(up.response)
        t.join(5)
    assert not t.is_alive()
    assert up.reconnects >= 1
    assert "upstream EOF" not in (tmp_path / "delaybuf.log").read_text()
