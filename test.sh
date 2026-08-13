#!/usr/bin/env bash
# Run the FeetBrowser test suite. Uses nix-shell for Tk on NixOS.
set -euo pipefail
cd "$(dirname "$0")"

run() {
  python3 -m pyflakes feetbrowser tests
  python3 tests/test_units.py
  python3 tests/test_nav.py
  python3 tests/test_toes.py
  python3 tests/test_sock.py
  python3 tests/smoke.py
}

if python3 -c "import tkinter" 2>/dev/null && python3 -c "import pyflakes" 2>/dev/null; then
  run
else
  nix-shell -p "python3.withPackages(ps: with ps; [ tkinter pyflakes ])" \
    --run "$(declare -f run); run"
fi
