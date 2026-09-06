#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
assert Adw.get_major_version() >= 1 and Adw.get_minor_version() >= 4, 'libadwaita 1.4 or newer required'
PY
python3 scripts/install.py
