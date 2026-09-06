"""Fail closed on private infrastructure and credentials in tracked public source."""
import re
import subprocess
import sys
from pathlib import Path

patterns = [r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----", r"gh[pousr]_[A-Za-z0-9_]{20,}",
            r"AKIA[0-9A-Z]{16}", r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b",
            r"\b192\.168\.\d+\.\d+\b", r"\b10\.\d+\.\d+\.\d+\b", r"\w+\.ts\.net",
            r"/(?:Users|home)/(?!user(?:/|\b)|example(?:/|\b))[A-Za-z0-9._-]+"]
paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
bad = []
for raw in paths:
    path = Path(raw)
    if not raw or raw == "scripts/privacy_check.py" or not path.is_file():
        continue
    if path.suffix.lower() in (".png", ".jpg", ".webp"):
        continue
    content = path.read_text(errors="replace")
    if any(re.search(pattern, content) for pattern in patterns):
        bad.append(raw)
    if path.name in ("fleet.json", "history.json", "known_hosts", "id_ed25519"):
        bad.append(raw)
if bad:
    print("Privacy check failed: " + ", ".join(sorted(set(bad))), file=sys.stderr)
    sys.exit(1)
print("Public-source privacy check passed")
