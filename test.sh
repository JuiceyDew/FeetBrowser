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
# everywhere else, and test_nav.py and smoke.py reach the network. test_x11.py
# and test_wayland.py are the two that run against a *headless* server on this
# machine, and neither needs a display to do it -- the suite checks its own
# "is there a server/compositor" question. test_wayland.py needs a compositor,
# so the section below starts weston's headless backend when there is one
# installed and otherwise runs just the offline half. The last of those is
# why CI runs both of them against the offline mirror in tests/fixtures
# instead -- see tests/fixture_server.py.
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

# feetplayer, in the same venv: the H.264, AAC and MP3 decoders, the container
# readers and the three audio backends, which used to live in this tree and
# are their own repository now. requirements.txt pins it to a commit; pip
# compiles its Fortran during the install, which takes a minute, so the pin
# already installed is compared against the pin asked for and nothing is done
# when they agree. `pip freeze` prints a VCS install as the requirement line
# that produced it, which is why the comparison is a plain string match.
want=$(grep -v '^[[:space:]]*#' requirements.txt | grep -v '^[[:space:]]*$')
if ! .venv/bin/python -m pip freeze 2>/dev/null | grep -qxF "$want"; then
  .venv/bin/python -m pip install -q -r requirements.txt
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

# The Rust half has 60 tests of its own -- the regexp engine, the CSS
# matcher, the layout reproductions. They ran nowhere: CI only ever did
# `cargo check --all-targets`, which compiles a test without running it. They
# cost hundredths of a second, so they run here too rather than only on a
# machine that happens to type `cargo test` by hand. The tokenizer and tree
# builder are not among them any more; they are `footnote`'s, and it runs
# its own suite against three platforms.
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
# Wayland has no Xvfb: the live half of test_wayland.py needs a compositor,
# and the only one we can start on a machine with no display is weston's
# headless backend. Start it when it exists, point the suite at it, tear it
# down; where there is no weston the suite's offline half still runs.
WL_RUNTIME="$(mktemp -d)"
chmod 0700 "$WL_RUNTIME"
if command -v weston >/dev/null 2>&1; then
  XDG_RUNTIME_DIR="$WL_RUNTIME" weston --backend=headless-backend.so \
    --socket=feetbrowser-tests --renderer=pixman >"$WL_RUNTIME/weston.log" 2>&1 &
  WL_PID=$!
  sleep 1
  XDG_RUNTIME_DIR="$WL_RUNTIME" WAYLAND_DISPLAY=feetbrowser-tests \
    $run tests/test_wayland.py
  kill "$WL_PID" 2>/dev/null || true
  wait "$WL_PID" 2>/dev/null || true
else
  $run tests/test_wayland.py   # offline half only
fi
rm -rf "$WL_RUNTIME"
$run tests/test_audio.py   # a <video> element's soundtrack, and the pictures that follow it
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
$run tests/smoke.py


