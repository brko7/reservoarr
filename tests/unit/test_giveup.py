"""RESV_GIVEUP_TRIES: a stream that never delivers a byte ends the process so
Dispatcharr can fail over to the next stream in the channel. The fetcher runs
synchronously here: time.sleep is replaced by a recorder that can also stop
the loop, and urlopen by a scripted sequence of responses."""
from __future__ import annotations

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


def test_never_gives_up_once_data_has_flowed(resv, run, capsys):
    answers = [Response([NULL_PACKET * 4]), forbidden]
    tries, _ = run(answers, giveup=2, stop_after=8)
    assert tries == 8
    assert resv.in_total == len(NULL_PACKET) * 4
    assert "giving up" not in capsys.readouterr().err
