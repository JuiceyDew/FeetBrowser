#!/usr/bin/env bash
# Run the FeetBrowser test suite.
#
# The renderer draws into its own framebuffer, so no display and no toolkit is
# needed. The JavaScript engine is the Rust one: a CPython extension maturin
# builds into the local venv, and the JS suite runs against it like every
# other suite.
#
# Four suites step outside all that: test_cocoa.py, test_x11.py and
# test_win32.py open real windows wherever their platform has one and skip
# everywhere else, and test_nav.py and smoke.py reach the network. The last
# of those is why CI runs both of them against the offline mirror in
# tests/fixtures instead -- see tests/fixture_server.py.
#
# On Windows, run test.cmd instead; it runs the same suites in the same order.
set -euo pipefail
cd "$(dirname "$0")"

# Those window suites open dozens of real windows in a few seconds, and a
# window's default manners -- centre itself, raise above everything, take the
# keyboard -- make the machine unusable for as long as the run lasts. QUIET
# drops exactly those three things and nothing else: the windows are still
# created, mapped, drawn into and sent real events, so the suites still prove
# what they proved before. Export it rather than assign it, so it reaches the
# suites, and honour an existing value so `FEETBROWSER_QUIET=0 ./test.sh`
# still gets you the windows when you want to watch them.
export FEETBROWSER_QUIET="${FEETBROWSER_QUIET:-1}"

if [ ! -x .venv/bin/python ]; then
  # Same venv run.sh builds, and for the same reason it is not sealed. That
  # reason used to be the image decoders and is not any more -- we decode our
  # own -- but curl_cffi is still optional and still lives in the system
  # python, and the impersonating fetch in net.py is the one thing a sealed
  # venv would quietly take away from the tests that exercise it.
  python3 -m venv --system-site-packages .venv
elif grep -qi '^include-system-site-packages *= *false' .venv/pyvenv.cfg 2>/dev/null; then
  # A venv from before that flag was added is sealed, and a venv is only ever
  # created once, so those tests would keep running without curl_cffi on every
  # machine that already has one. Re-running venv over it rewrites pyvenv.cfg
  # and leaves what is installed inside untouched.
  python3 -m venv --system-site-packages .venv
fi

# The Rust engine, in the venv the rest of the suite runs from, rebuilt
# whenever rust/ has moved on since. Importing it successfully is not enough.
# An extension compiled from an older tree runs perfectly well and fails the
# tests that the newer tree added, which reads as "your branch is broken" when
# the truth is "your venv is old" -- and it is the tests of the DOM bridge,
# whose Python and Rust halves have to agree, that go first.
engine=$(.venv/bin/python -c "import feetbrowser_engine as e; print(e.__file__)" 2>/dev/null || true)
if [ -z "$engine" ] || [ -n "$(find rust/src rust/Cargo.toml -newer "$engine" 2>/dev/null | head -1)" ]; then
  .venv/bin/pip install -q maturin
  .venv/bin/maturin develop --release --manifest-path rust/Cargo.toml
fi

# The Rust half has 122 tests of its own -- the tokenizer, the regexp engine,
# the layout reproductions and the html5lib tree construction suite. They ran
# nowhere: CI only ever did `cargo check --all-targets`, which compiles a test
# without running it. They cost 0.07s, so they run here too rather than only
# on a machine that happens to type `cargo test` by hand.
if command -v cargo >/dev/null 2>&1; then
  cargo test -q --manifest-path rust/Cargo.toml
fi

if ! .venv/bin/python -c "import pyflakes" 2>/dev/null; then
  .venv/bin/pip install -q pyflakes
fi

.venv/bin/python -m pyflakes feetbrowser tests

# Every suite below runs behind a deadline. Several of them start HTTP
# servers, open real windows or reach the network, and any of those can stop
# forever rather than fail -- at which point the run says nothing at all. The
# watchdog turns that into every thread's stack and a non-zero exit. See
# tests/watchdog.py; FEETBROWSER_TEST_TIMEOUT overrides the number.
run=".venv/bin/python tests/watchdog.py 900"

$run tests/test_suites.py  # every file below, and nothing missing
$run tests/test_discord.py  # the from-scratch Discord Rich Presence client
$run tests/test_render.py
$run tests/test_cocoa.py   # opens real windows on macOS, skips elsewhere
$run tests/test_x11.py     # opens real windows under X11, skips elsewhere
$run tests/test_win32.py   # opens real windows on Windows, skips elsewhere
$run tests/test_audio.py   # plays real sound where there is a device, skips elsewhere
$run tests/test_units.py
$run tests/test_release_version.py  # the guard release.yml runs first
$run tests/test_js.py
$run tests/test_shoes.py
$run tests/test_settings.py
$run tests/test_e2e.py     # a fixture page in, its pixels back out
$run tests/test_nav.py
$run tests/test_toes.py
$run tests/test_asmx11.py    # raw assembly on Linux/x86-64, Python elsewhere
$run tests/test_asmselect.py # the selection nearest-boundary kernel
$run tests/test_h264.py      # the Fortran H.264 decoder, or the skip where there is no gfortran
$run tests/test_aac.py       # the Fortran AAC decoder, against FFmpeg's samples
$run tests/test_mp3.py       # the Fortran MPEG Layer III decoder, likewise
$run tests/test_pcm.py       # uncompressed sound, against the waveform it was made from
$run tests/smoke.py


