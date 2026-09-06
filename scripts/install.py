"""User-local installation; preserves private config and provides a rollback directory."""
from pathlib import Path
import shutil
import tempfile

root = Path(__file__).resolve().parent.parent
home = Path.home()
share = home / ".local/share/fleetlight"
share.parent.mkdir(parents=True, exist_ok=True)
stage = Path(tempfile.mkdtemp(prefix=".fleetlight-stage-", dir=share.parent))
previous = share.with_name("fleetlight.previous")
try:
    shutil.copytree(root / "fleetlight", stage / "fleetlight", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(root / "LICENSE", stage / "LICENSE")
    if previous.exists():
        shutil.rmtree(previous)
    if share.exists():
        share.rename(previous)
    stage.rename(share)
except Exception:
    if not share.exists() and previous.exists():
        previous.rename(share)
    raise
finally:
    if stage.exists():
        shutil.rmtree(stage)

binary = home / ".local/bin/fleetlight"
binary.parent.mkdir(parents=True, exist_ok=True)
binary.write_text('#!/usr/bin/python3\nimport sys\nsys.path.insert(0, ' + repr(str(share)) + ')\nfrom fleetlight.__main__ import main\nraise SystemExit(main())\n')
binary.chmod(0o755)
desktop = home / ".local/share/applications/io.github.fleetlight.Linux.desktop"
desktop.parent.mkdir(parents=True, exist_ok=True)
escaped = str(binary).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
desktop.write_text((root / "data/io.github.fleetlight.Linux.desktop").read_text().replace("Exec=fleetlight", 'Exec="' + escaped + '"'))
icons = home / ".local/share/icons/hicolor/scalable/apps"
icons.mkdir(parents=True, exist_ok=True)
shutil.copy(root / "data/io.github.fleetlight.Linux.svg", icons)
print("Installed Fleetlight in " + str(share))
print("Launch it from your desktop application menu or " + str(binary))
