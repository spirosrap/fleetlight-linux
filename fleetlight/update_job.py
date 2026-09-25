"""Self-contained durable worker, run locally or through authenticated SSH.

No network listener. The caller already has the user's SSH account access.
An inherited flock prevents two installations on one computer. SSH disconnects
do not kill an installer; status can be recovered using the same job ID.
"""
import fcntl
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time


ERRORS = {
    "installation-invalid": "Protected: local ChatGPT files were modified. Review the repair before updating.",
    "target-changed": "A different release is now available. Check again before updating.",
    "permission-required": "Passwordless permission is required for this installation; no credentials are stored.",
    "source-invalid": "The configured package source did not pass verification.",
    "source-missing": "The required package source or update helper is missing.",
    "signature-invalid": "The downloaded application did not pass OpenAI signature verification.",
    "app-busy": "ChatGPT could not close cleanly. Finish its work and retry.",
    "unsupported-installation": "This Codex installation method needs a manual update.",
    "unsupported-architecture": "This macOS release requires Apple Silicon.",
    "rollback-failed": "Restoring the previous application failed. Manual recovery is required.",
    "post-install-invalid": "The installed application failed verification; inspect the update log.",
    "verification-failed": "The active version did not match the requested release.",
    "install-failed": "The package manager reported a failure. Inspect the update log before retrying.",
    "feed-failed": "The official release feed could not be fetched.",
    "refresh-failed": "Package repository metadata could not be refreshed.",
    "download-failed": "The application download failed.",
    "missing": "The application is no longer installed.",
}


def save(path, data):
    fd, temp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def package_changes(log):
    """Package upgrades printed by pacman, Omarchy and yay. ANSI and progress lines are ignored."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(log or ""))
    changes = []

    def add(item):
        item = " ".join(item.split())
        if item and item not in changes and len(changes) < 80:
            changes.append(item)

    def versionish(value):
        return bool(re.search(r"\d", value)) and not re.search(r"\b(?:B|KiB|MiB|GiB)\b", value)

    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if "Old Version" in line and "New Version" in line:
            in_table = True
            continue
        if in_table and not line:
            continue
        if in_table and line.startswith(("Total ", "::")):
            in_table = False
            continue
        if in_table:
            parts = re.split(r"\s{2,}", line)
            name = parts[0].split("/")[-1] if parts else ""
            old = parts[1] if len(parts) > 1 else ""
            new = parts[2] if len(parts) > 2 else ""
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._+-]*", name) and versionish(old):
                if versionish(new):
                    add(name + " " + old + " → " + new)
                else:
                    add(name + " → " + old)
                continue
            in_table = False
        if line.startswith("->") or "Excluding packages" in line:
            continue
        match = re.search(r"(?:aur/)?([A-Za-z0-9][A-Za-z0-9@._+-]*)\s+(\S+)\s+->\s+(\S+)", line)
        if match:
            add(match.group(1) + " " + match.group(2) + " → " + match.group(3).rstrip("],"))
            continue
        unpacked = re.search(
            r"Unpacking\s+([A-Za-z0-9][A-Za-z0-9+._-]*)(?::\S+)?\s+\(([^)]+)\)(?:\s+over\s+\(([^)]+)\))?",
            line)
        if unpacked:
            name, new, old = unpacked.group(1), unpacked.group(2), unpacked.group(3)
            add(name + " " + old + " → " + new if old else name + " → " + new)
    return changes


def installation_changes(receipt):
    """Readable install results from explicit markers or the package-manager log."""
    if not isinstance(receipt, dict):
        return []
    log = str(receipt.get("log") or "")
    found = []
    saved = receipt.get("changes")
    if isinstance(saved, list):
        found.extend(item.strip()[:200] for item in saved if isinstance(item, str) and item.strip())
    before = after = ""
    for line in log.splitlines():
        if line.startswith("CHANGED:"):
            item = line.split(":", 1)[1].strip()[:200]
            if item and item not in found and len(found) < 80:
                found.append(item)
        elif line.startswith("BEFORE_VERSION:"):
            before = line.split(":", 1)[1].strip()
        elif line.startswith(("AFTER_VERSION:", "ACTIVE_VERSION:")):
            after = line.split(":", 1)[1].strip()
    packages = package_changes(log)
    if packages:
        found = packages
    kind = {"cli": "Codex CLI", "claude": "Claude CLI", "desktop": "ChatGPT", "system": "Linux packages"}.get(receipt.get("kind"), "Version")
    if before and after and before != after:
        version_line = kind + " " + before + " → " + after
        if version_line not in found:
            found.insert(0, version_line)
    elif after and not found:
        found.append(kind + " " + after)
    return found[:80]


def leading_version(text):
    match = re.search(r"(\d+\.\d+\.\d+)", str(text or ""))
    return match.group(1) if match else ""


def recorded_version(report, kind):
    for item in report:
        if item.get("kind") != kind:
            continue
        for change in item.get("changes") or []:
            if "→" in change:
                found = leading_version(change.split("→")[-1])
                if found:
                    return found
        found = leading_version(item.get("phase"))
        if found:
            return found
    return ""


def file_time(path):
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return max(stat.st_mtime, getattr(stat, "st_birthtime", stat.st_mtime))


def codex_install():
    candidates = [Path.home() / ".local/bin/codex", Path.home() / ".local/share/mise/shims/codex"]
    found = shutil.which("codex")
    if found:
        candidates.append(Path(found))
    for path in candidates:
        try:
            resolved = Path(os.path.realpath(path))
        except OSError:
            continue
        if not resolved.is_file():
            continue
        for parent in [resolved, *resolved.parents]:
            number = leading_version(parent.name)
            if number and parent != resolved:
                return {"kind": "cli", "version": number, "finished_at": file_time(parent)}
    return None


def chatgpt_install():
    if platform.system() != "Darwin":
        return None
    plist = Path("/Applications/ChatGPT.app/Contents/Info.plist")
    if not plist.is_file():
        return None
    try:
        with plist.open("rb") as stream:
            data = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    if data.get("CFBundleIdentifier") != "com.openai.codex":
        return None
    number = leading_version(data.get("CFBundleShortVersionString"))
    if not number:
        return None
    return {"kind": "desktop", "version": number, "finished_at": file_time(plist)}


def merge_current_installs(report, installs):
    """Add the version currently on disk when Fleetlight did not record that install."""
    names = {"cli": "Codex CLI", "desktop": "ChatGPT"}
    combined = list(report)
    for install in installs:
        if not install or not install.get("finished_at"):
            continue
        current = install.get("version")
        if not version(current):
            continue
        previous = recorded_version(combined, install["kind"])
        if previous == current or (version(previous) and version(previous) >= version(current)):
            continue
        change = names[install["kind"]] + " " + (previous + " → " if previous else "") + current
        combined.append({
            "id": "installed-" + install["kind"],
            "kind": install["kind"],
            "state": "succeeded",
            "phase": "Installed " + current,
            "started_at": None,
            "finished_at": install["finished_at"],
            "changes": [change],
        })
    combined.sort(key=lambda item: item.get("finished_at") or item.get("started_at") or 0, reverse=True)
    return combined


def history_report(root=None, limit=12):
    """Finished installs on one computer, with the package and version lines already extracted."""
    directory = Path(root) if root else Path.home() / ".local/state/fleetlight/update-jobs"
    report = []
    for state in saved_installs(directory, limit=limit):
        kind = state.get("kind")
        if kind not in ("cli", "desktop", "claude", "system", "restart"):
            continue
        report.append({
            "id": state.get("id") if isinstance(state.get("id"), str) else "",
            "kind": kind,
            "state": state.get("state"),
            "phase": str(state.get("phase") or "")[:200],
            "started_at": state.get("started_at") if isinstance(state.get("started_at"), (int, float)) else None,
            "finished_at": state.get("finished_at") if isinstance(state.get("finished_at"), (int, float)) else None,
            "changes": installation_changes(state),
        })
    if root is None:
        report = merge_current_installs(report, [item for item in (codex_install(), chatgpt_install()) if item])
    return report[:limit]


def visible_installs(records, os_name):
    """Linux shows package, Codex CLI and ChatGPT installs. macOS shows only Codex CLI and ChatGPT."""
    kinds = ("cli", "claude", "desktop") if os_name == "Darwin" else ("cli", "claude", "desktop", "system")
    return [item for item in records if isinstance(item, dict) and item.get("kind") in kinds]


def saved_installs(root, limit=12):
    """Finished update jobs stored on this computer, newest first."""
    directory = Path(root)
    if not directory.is_dir():
        return []
    records = []
    for state_file in directory.glob("*/state.json"):
        try:
            if state_file.stat().st_size > 100_000:
                continue
            state = json.loads(state_file.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("state") not in ("succeeded", "failed"):
            continue
        log_file = state_file.parent / "output.log"
        log = ""
        try:
            if log_file.is_file():
                with log_file.open("rb") as stream:
                    stream.seek(max(0, log_file.stat().st_size - 80_000))
                    log = stream.read(80_000).decode(errors="replace")
        except OSError:
            log = ""
        state["log"] = log
        records.append(state)
    records.sort(key=lambda item: item.get("finished_at") or item.get("started_at") or 0, reverse=True)
    return records[:limit]


def version(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"\d+(?:\.\d+){2}", raw):
        return ()
    return tuple(int(part) for part in raw.split("."))


def parse_result(kind, target, build, code, lines):
    if kind in ("system", "restart"):
        marker = "FLEETLIGHT_SYSTEM_UPDATE" if kind == "system" else "FLEETLIGHT_RESTART"
        if code == 0 and marker in lines and "VERIFY:ok" in lines:
            detail = "Restart scheduled in one minute; return not yet verified" if kind == "restart" else "System packages verified"
            if "REBOOT:required" in lines:
                detail += " · Restart required"
            return "succeeded", detail, ""
        reason = next((line[7:] for line in reversed(lines) if line.startswith("UPDATE:")), "failed")
        return "failed", ERRORS.get(reason, "System action failed. Check the log before retrying."), ""
    values = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = value
    after = values.get("ACTIVE_VERSION" if kind in ("cli", "claude") else "AFTER_VERSION", "")
    valid = bool(version(after)) and version(after) >= version(target)
    if kind in ("cli", "claude"):
        valid = valid and values.get("VERIFY") == "ok"
    else:
        valid = valid and values.get("VERIFY") in ("updated", "current")
        if build:
            valid = valid and values.get("AFTER_BUILD", "").isdigit() and int(values["AFTER_BUILD"]) >= int(build)
    marker = {"cli": "FLEETLIGHT_CODEX_UPDATE", "claude": "FLEETLIGHT_CLAUDE_UPDATE"}.get(kind, "FLEETLIGHT_CODEX_APP_UPDATE")
    if code == 0 and marker in lines and valid:
        if values.get("RELAUNCH") == "failed":
            return "failed", "Installed " + after + ", but ChatGPT did not reopen. Open it manually.", after
        return "succeeded", "Verified " + after, after
    reason = values.get("UPDATE", "failed")
    return "failed", ERRORS.get(reason, "Update failed; the requested version was not verified. Inspect the log."), after


def worker(directory, lock_fd):
    # Keep the inherited file descriptor alive until the installer finishes.
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.chdir(Path.home())
    request = json.loads((directory / "request.json").read_text())
    state_file = directory / "state.json"
    state = {"id": request["id"], "kind": request["kind"], "state": "running", "pid": os.getpid(),
             "started_at": time.time(), "phase": "Starting update", "target": request["target"]}
    save(state_file, state)
    environment = dict(os.environ)
    environment.update(FLEETLIGHT_EXPECTED_VERSION=request["target"], FLEETLIGHT_EXPECTED_BUILD=request.get("build", ""))
    paths = [str(Path.home() / p) for p in (".local/bin", ".local/share/mise/shims", ".npm-global/bin")]
    environment["PATH"] = os.pathsep.join(paths + ["/usr/share/omarchy/bin", "/opt/homebrew/bin", "/usr/local/bin", environment.get("PATH", "/usr/bin:/bin")])
    lines = []
    changes = []
    try:
        with (directory / "output.log").open("w") as log:
            process = subprocess.Popen(["/bin/sh", str(directory / "update.sh")], env=environment,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, errors="replace", pass_fds=(lock_fd,))
            # Do not kill a package manager halfway through an installation.
            # Detachment and journaling keep it observable if the UI disconnects.
            for raw in process.stdout:
                line = raw.rstrip("\r\n")
                log.write(raw)
                log.flush()
                if line.startswith("CHANGED:"):
                    item = line.split(":", 1)[1].strip()[:200]
                    if item and item not in changes and len(changes) < 80:
                        changes.append(item)
                if line.startswith(("FLEETLIGHT_", "ACTIVE_VERSION:", "AFTER_VERSION:", "AFTER_BUILD:", "BEFORE_VERSION:", "VERIFY:", "UPDATE:", "RELAUNCH:", "REBOOT:", "CHANGED:")):
                    lines.append(line)
                if line.startswith("PHASE:"):
                    state["phase"] = line[6:][:200]
                    save(state_file, state)
            code = process.wait()
        status, detail, after = parse_result(request["kind"], request["target"], request.get("build"), code, lines)
        state.update(state=status, phase=detail, after_version=after, exit_code=code, finished_at=time.time(),
                     changes=installation_changes({"kind": request["kind"], "changes": changes, "log": "\n".join(lines)}))
    except Exception as error:
        state.update(state="failed", phase="Update worker failed: " + type(error).__name__, finished_at=time.time())
    save(state_file, state)


def handle(request):
    ident = request.get("id", "")
    if not re.fullmatch(r"[a-f0-9]{32}", ident):
        raise ValueError("Invalid job ID")
    root = Path.home() / ".local/state/fleetlight/update-jobs"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = root / ident
    state_file = directory / "state.json"
    if request.get("operation") == "status" or state_file.exists():
        if not state_file.exists():
            return {"id": ident, "state": "unknown", "phase": "No receipt exists for this job"}
        state = json.loads(state_file.read_text())
        if state.get("state") == "running":
            try:
                os.kill(state["pid"], 0)
            except (ProcessLookupError, KeyError):
                state.update(state="interrupted", phase="Worker stopped unexpectedly. Check installed versions before retrying.")
                save(state_file, state)
        if state.get("state") == "queued" and time.time() - state.get("started_at", 0) > 30:
            state.update(state="interrupted", phase="The worker did not start. Check the computer before retrying.")
        log = directory / "output.log"
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 12000))
                state["log"] = stream.read(12000).decode(errors="replace")
        return state
    if request.get("operation") != "start" or request.get("kind") not in ("cli", "desktop", "claude", "system", "restart"):
        raise ValueError("Unknown operation")
    if not version(request.get("target")) or (request.get("build") and not str(request["build"]).isdigit()):
        raise ValueError("Invalid target release")
    script = request.get("script", "")
    if not isinstance(script, str) or not 1 < len(script) < 100000:
        raise ValueError("Invalid updater")
    lock = (root / "installation.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {"id": ident, "state": "busy", "phase": "Another Fleetlight update is already running on this computer"}
    directory.mkdir(mode=0o700, exist_ok=True)
    (directory / "update.sh").write_text(script)
    save(directory / "request.json", {k:v for k,v in request.items() if k != "script"})
    # Each job gets its own worker source; an application upgrade cannot change a running job.
    (directory / "worker.py").write_text(request["worker_source"])
    initial = {"id": ident, "kind": request["kind"], "state": "queued", "phase": "Preparing update", "started_at": time.time()}
    save(state_file, initial)
    with (directory / "worker.log").open("ab") as output:
        subprocess.Popen([sys.executable, str(directory / "worker.py"), "run", str(directory), str(lock.fileno())],
                         stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                         start_new_session=True, pass_fds=(lock.fileno(),))
    lock.close()
    return initial


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "history":
        print("FLEETLIGHT_HISTORY=" + json.dumps(history_report()))
    elif len(sys.argv) > 1 and sys.argv[1] == "run":
        worker(Path(sys.argv[2]), int(sys.argv[3]))
    else:
        try:
            request = json.loads(sys.stdin.read(200000))
            print("FLEETLIGHT_JOB=" + json.dumps(handle(request)))
        except Exception as error:
            print("FLEETLIGHT_JOB=" + json.dumps({"state": "failed", "phase": "Job request failed: " + type(error).__name__}))
            sys.exit(1)
