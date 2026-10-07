"""RESV_GIVEUP_TRIES: a stream that never delivers a byte ends the process so
Dispatcharr can fail over to the next stream in the channel. The fetcher runs
synchronously here: time.sleep is replaced by a recorder that can also stop
the loop, and urlopen by a scripted sequence of responses."""
from __future__ import annotations

import email.message
import io
import urllib.error

import pytest

NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184


class Response:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def geturl(self):
        return "http://edge.test/live/stream.ts"

    def close(self):
        self.closed = True


def forbidden(*_args, **_kwargs):
    raise urllib.error.HTTPError(
        "http://offline/unit-test", 403, "Forbidden", {}, io.BytesIO(b"")
    )


@pytest.fixture
def run(resv, monkeypatch):
    """Run the real fetcher against scripted upstream answers. Returns the
    number of upstream attempts and the backoff sleeps it took; `stop_after`
    ends the loop the way Dispatcharr's SIGTERM would."""
    def go(answers, giveup, stop_after=None):
        resv.GIVEUP_TRIES = giveup
        attempts = []
        sleeps = []

        def urlopen(*args, **kwargs):
            answer = answers[min(len(attempts), len(answers) - 1)]
            attempts.append(answer)
            return answer(*args, **kwargs) if callable(answer) else answer

        def sleep(seconds):
            sleeps.append(seconds)
            if stop_after is not None and len(attempts) >= stop_after:
                resv.stop.set()

        monkeypatch.setattr(resv.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(resv.time, "sleep", sleep)
        resv.fetcher()
        return len(attempts), sleeps

    return go


def test_gives_up_after_the_configured_tries_without_data(resv, run, capsys):
    tries, sleeps = run([forbidden], giveup=3)
    assert tries == 3
    assert sleeps == [1, 2]
    assert resv.upstream_eof is True
    assert "giving up: no data after 3 upstream attempts" in capsys.readouterr().err


def test_default_keeps_retrying_as_before(resv, run, capsys):
    tries, sleeps = run([forbidden], giveup=0, stop_after=10)
    assert tries == 10
    assert sleeps[-1] == 8
    assert "giving up" not in capsys.readouterr().err


def test_a_connect_that_closes_before_any_byte_counts_as_a_try(resv, run):
    tries, _ = run([Response([])], giveup=2)
    assert tries == 2
    assert resv.in_total == 0


MP4_HEAD = b"\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2avc1mp41"


class Served(Response):
    def __init__(self, chunks, content_type):
        super().__init__(chunks)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type


def test_an_mp4_courtesy_file_counts_as_an_attempt_without_data(resv, run, capsys):
    resv.REJECT_MP4 = True
    courtesy = Served([MP4_HEAD * 64], "video/mp4")
    tries, sleeps = run([courtesy], giveup=1)
    assert tries == 1
    assert sleeps == []
    assert resv.in_total == 0
    assert courtesy.closed is True
    err = capsys.readouterr().err
    assert "upstream sent an MP4 file, not a live TS" in err
    assert "giving up: no data after 1 upstream attempts" in err


def test_an_mp4_is_recognised_by_its_ftyp_box_without_a_content_type(resv, run):
    resv.REJECT_MP4 = True
    tries, _ = run([Response([MP4_HEAD * 64])], giveup=1)
    assert tries == 1
    assert resv.in_total == 0


def test_a_live_ts_labelled_video_mp4_is_taken_as_before(resv, run, capsys):
    resv.REJECT_MP4 = True
    answers = [Served([NULL_PACKET * 4], "video/mp4"), forbidden]
    tries, _ = run(answers, giveup=1, stop_after=4)
    assert tries == 4
    assert resv.in_total == len(NULL_PACKET) * 4
    assert "MP4" not in capsys.readouterr().err


def test_an_mp4_is_ingested_as_before_when_the_check_is_off(resv, run):
    resv.REJECT_MP4 = False
    tries, _ = run([Served([MP4_HEAD * 64], "video/mp4"), forbidden], giveup=1, stop_after=3)
    assert tries == 3
    assert resv.in_total == len(MP4_HEAD) * 64


def test_never_gives_up_once_data_has_flowed(resv, run, capsys):
    answers = [Response([NULL_PACKET * 4]), forbidden]
    tries, _ = run(answers, giveup=2, stop_after=8)
    assert tries == 8
    assert resv.in_total == len(NULL_PACKET) * 4
    assert "giving up" not in capsys.readouterr().err


class FfmpegStarted(Exception):
    """Raised by the Popen stand-in: main() got past the prefill."""


@pytest.fixture
def prefill(resv, monkeypatch):
    """Run the real main() up to the prefill with a scripted fetcher in place of
    the real one. Returns the seconds main() waited; `FfmpegStarted` means it
    went on to start ffmpeg."""
    def go(fetcher):
        resv.PREFILL_MAX_S = 5.0

        def popen(*_args, **_kwargs):
            raise FfmpegStarted

        monkeypatch.setattr(resv, "fetcher", fetcher)
        monkeypatch.setattr(resv.subprocess, "Popen", popen)
        t0 = resv.time.monotonic()
        resv.main()
        return resv.time.monotonic() - t0

    return go


def test_a_fetcher_that_gives_up_ends_the_prefill_at_once(resv, prefill, capsys):
    def gives_up():
        resv.upstream_eof = True
        with resv.cond:
            resv.cond.notify_all()

    waited = prefill(gives_up)
    assert waited < 1.0
    err = capsys.readouterr().err
    assert "no data before the fetcher gave up" in err
    assert "prefill done" not in err


def test_data_still_releases_the_prefill_to_ffmpeg(resv, prefill, capsys):
    resv.PREFILL_BYTES = len(NULL_PACKET)

    with pytest.raises(FfmpegStarted):
        prefill(lambda: resv.ingest(NULL_PACKET))
    assert "prefill done" in capsys.readouterr().err
