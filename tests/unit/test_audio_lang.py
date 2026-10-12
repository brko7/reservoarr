"""RESV_AUDIO_LANG: tag the audio track's language, nothing else.

The default command is the one every earlier release ran. The invariant tokens
(#3 ac3, #4 dump_extra, #8 -loglevel warning, #1 no -re/-readrate) must hold with
and without the tunable, and the tag must sit between the input and the output.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

RESERVOIR_PATH = Path(__file__).resolve().parent.parent.parent / "reservoarr.py"

DEFAULT_TAIL = [
    "-map", "0:v", "-map", "0:a:0",
    "-c:v", "copy", "-bsf:v", "dump_extra=freq=keyframe",
    "-c:a", "ac3", "-b:a", "192k", "-ac", "2",
    "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "pipe:1",
]


def load(tmp_path, **env):
    os.environ["RESV_LOG_DIR"] = str(tmp_path)
    os.environ.update(env)
    sys.argv = ["reservoarr", "http://offline/unit-test"]
    try:
        spec = importlib.util.spec_from_file_location("resv_audio_lang", str(RESERVOIR_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for key in env:
            os.environ.pop(key, None)
    return mod


def check_invariants(cmd):
    assert cmd[cmd.index("-c:a") + 1] == "ac3"
    assert cmd[cmd.index("-bsf:v") + 1] == "dump_extra=freq=keyframe"
    assert cmd.count("-bsf:v") == 1
    assert cmd[cmd.index("-loglevel") + 1] == "warning"
    assert cmd.index("-i") < cmd.index("-map") < cmd.index("-f")
    assert cmd[-1] == "pipe:1"
    for banned in ("-re", "-readrate", "-copyts"):
        assert banned not in cmd


def test_default_command_is_unchanged(tmp_path):
    cmd = load(tmp_path).FFMPEG_CMD
    assert cmd[cmd.index("-map"):] == DEFAULT_TAIL
    assert not any("language" in t for t in cmd)
    check_invariants(cmd)


@pytest.mark.parametrize("raw", ["fra", " FRA ", "Fra"])
def test_valid_code_adds_the_tag_after_the_codec_options(tmp_path, raw):
    cmd = load(tmp_path, RESV_AUDIO_LANG=raw).FFMPEG_CMD
    i = cmd.index("-metadata:s:a:0")
    assert cmd[i + 1] == "language=fra"
    assert cmd[i - 2:i] == ["-ac", "2"]
    assert cmd[i + 2] == "-f"
    assert cmd.index("-i") < i < cmd.index("pipe:1")
    check_invariants(cmd)


@pytest.mark.parametrize("raw", ["", "fr", "francais", "fra;x", "fr a", "f1a", "é"])
def test_anything_else_leaves_the_command_alone(tmp_path, raw):
    cmd = load(tmp_path, RESV_AUDIO_LANG=raw).FFMPEG_CMD
    assert cmd[cmd.index("-map"):] == DEFAULT_TAIL
    check_invariants(cmd)


def test_invalid_value_is_logged_once(tmp_path, capsys):
    resv = load(tmp_path, RESV_AUDIO_LANG="francais")
    resv.fetcher = lambda: setattr(resv, "upstream_eof", True)
    resv.main()
    err = capsys.readouterr().err
    assert err.count("RESV_AUDIO_LANG='francais' is not a 3-letter code") == 1


def test_timeline_offset_still_lands_before_the_output(tmp_path):
    resv = load(tmp_path, RESV_AUDIO_LANG="fra")
    cmd = resv.ffmpeg_cmd(1234.5)
    assert cmd[-3:] == ["-output_ts_offset", "1234.500000", "pipe:1"]
    assert cmd[:-3] == resv.FFMPEG_CMD[:-1]
    check_invariants(cmd)
