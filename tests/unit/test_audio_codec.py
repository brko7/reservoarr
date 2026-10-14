from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESERVOIR_PATH = REPO_ROOT / "reservoarr.py"

AC3 = ["-c:a", "ac3", "-b:a", "192k", "-ac", "2"]
AAC = ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000"]
FILL = ["-af", "aresample=async=1"]


def load(tmp_log_dir, **env):
    os.environ["RESV_LOG_DIR"] = str(tmp_log_dir)
    os.environ.update(env)
    sys.argv = ["reservoarr", "http://offline/unit-test"]
    spec = importlib.util.spec_from_file_location("resv_audio", str(RESERVOIR_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for key in env:
        os.environ.pop(key, None)
    return mod


def audio_args(cmd):
    start = cmd.index("-c:a")
    return cmd[start:cmd.index("-f", start)]


def journal(resv):
    path = Path(resv.LOG_FILE)
    return path.read_text() if path.exists() else ""


def test_default_audio_is_unchanged_ac3(tmp_path):
    assert audio_args(load(tmp_path).FFMPEG_CMD) == AC3


def test_aac_is_stereo_48k(tmp_path):
    assert audio_args(load(tmp_path, RESV_AUDIO_CODEC="aac").FFMPEG_CMD) == AAC


def test_codec_name_ignores_case_and_spaces(tmp_path):
    assert audio_args(load(tmp_path, RESV_AUDIO_CODEC=" AAC ").FFMPEG_CMD) == AAC


@pytest.mark.parametrize("value", ["copy", "opus", ""])
def test_anything_else_encodes_ac3(tmp_path, value):
    assert audio_args(load(tmp_path, RESV_AUDIO_CODEC=value).FFMPEG_CMD) == AC3


def test_a_fallback_is_logged(tmp_path, capsys):
    resv = load(tmp_path, RESV_AUDIO_CODEC="copy")

    resv.announce_audio_codec()

    assert "RESV_AUDIO_CODEC='copy'" in capsys.readouterr().err
    assert "encoding audio as ac3" in journal(resv)


def test_an_empty_setting_logs_nothing(tmp_path, capsys):
    resv = load(tmp_path, RESV_AUDIO_CODEC="")

    resv.announce_audio_codec()

    assert "RESV_AUDIO_CODEC" not in capsys.readouterr().err
    assert "RESV_AUDIO_CODEC" not in journal(resv)


def test_a_valid_codec_logs_nothing(tmp_path, capsys):
    resv = load(tmp_path, RESV_AUDIO_CODEC="aac")

    resv.announce_audio_codec()

    assert "RESV_AUDIO_CODEC" not in capsys.readouterr().err
    assert "RESV_AUDIO_CODEC" not in journal(resv)


def test_fill_gaps_is_opt_in(tmp_path):
    assert "-af" not in load(tmp_path).FFMPEG_CMD


@pytest.mark.parametrize("codec, args", [("ac3", AC3), ("aac", AAC)])
def test_fill_gaps_follows_either_codec(tmp_path, codec, args):
    resv = load(tmp_path, RESV_AUDIO_CODEC=codec, RESV_AUDIO_FILL_GAPS="1")

    assert audio_args(resv.FFMPEG_CMD) == args + FILL


def test_the_ts_offset_command_keeps_the_audio_args(tmp_path):
    resv = load(tmp_path, RESV_AUDIO_CODEC="aac", RESV_AUDIO_FILL_GAPS="1")

    cmd = resv.ffmpeg_cmd(12.5)

    assert audio_args(cmd) == AAC + FILL
    assert cmd[cmd.index("-output_ts_offset") + 1] == "12.500000"
