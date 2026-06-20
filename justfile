# reservoarr — Dispatcharr delay-buffer dev pipeline.
#
# Quick start:
#   just venv       # create .venv with dev deps
#   just fixture    # generate fixtures/synth.ts (~2s)
#   just test       # unit tests (fast, no ffmpeg)
#   just e2e        # synthetic end-to-end (needs ffmpeg + fixture)
#   just all        # lint + unit + e2e

set shell := ["bash", "-cu"]

# Path to Python in the local venv (created by `just venv`).
py := ".venv/bin/python3"
pip := ".venv/bin/pip"
pytest := ".venv/bin/pytest"
ruff := ".venv/bin/ruff"

# Locate ffmpeg/ffprobe (homebrew on macOS, apt on Linux/CI).
ffmpeg_bin := `command -v ffmpeg || true`
ffprobe_bin := `command -v ffprobe || true`

default: all

# Create local dev venv with pytest + ruff.
venv:
    @test -d .venv || python3 -m venv .venv
    @{{pip}} install --quiet --upgrade pip
    @{{pip}} install --quiet --editable '.[dev]'
    @echo "venv ready: $({{py}} --version)"

# Sanity-check the script compiles.
syntax:
    @{{py}} -m py_compile reservoarr.py
    @{{py}} -m py_compile tools/cdn_sim.py
    @{{py}} -m py_compile tools/parsecheck.py
    @{{py}} -m py_compile tools/make_corrupt_ts.py
    @echo "syntax OK"

lint: venv syntax
    @{{ruff}} check .

# Generate the deterministic synthetic TS fixture (180s, ~54MB, H.264 + AC3, with PCR).
# Provider-independent — CI uses this, not a captured stream.
fixture:
    @test -n "{{ffmpeg_bin}}" || (echo "ERROR: ffmpeg not found in PATH"; exit 1)
    @mkdir -p fixtures
    @bash tools/make_synth_ts.sh fixtures/synth.ts

# Unit tests only (no ffmpeg, no subprocesses).
test: venv
    @{{pytest}} tests/unit -v

# End-to-end tests (ffmpeg + cdn_sim subprocess). Generates fixture if missing.
# Runs in parallel (pytest-xdist) — each test takes ~75s wall-clock at the
# simulated CDN rate; parallel keeps total time to one slowest test.
e2e: venv fixture
    @test -n "{{ffmpeg_bin}}" || (echo "ERROR: ffmpeg not found in PATH"; exit 1)
    @RESV_FFMPEG_BIN="{{ffmpeg_bin}}" FFPROBE_BIN="{{ffprobe_bin}}" {{pytest}} tests/e2e -v -m e2e -n auto

all: lint test e2e
    @echo "all green"

# Clean transient artifacts (keeps .venv).
clean:
    @rm -rf fixtures/*.ts fixtures/*.log .pytest_cache .ruff_cache
    @find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
    @echo "cleaned"
