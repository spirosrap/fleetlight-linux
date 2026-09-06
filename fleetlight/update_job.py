"""Self-contained durable worker, run locally or through authenticated SSH.

No network listener. The caller already has the user's SSH account access.
An inherited flock prevents two installations on one computer. SSH disconnects
do not kill an installer; status can be recovered using the same job ID.
"""
import fcntl
import json
import os
from pathlib import Path
import re
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


def version(raw):
    if not isinstance(raw, str) or not re.fullmatch(r"\d+(?:\.\d+){2}", raw):
        return ()
    return tuple(int(part) for part in raw.split("."))


def parse_result(kind, target, build, code, lines):
    values = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = value
    after = values.get("ACTIVE_VERSION" if kind == "cli" else "AFTER_VERSION", "")
    valid = bool(version(after)) and version(after) >= version(target)
    if kind == "cli":
        valid = valid and values.get("VERIFY") == "ok"
    else:
        valid = valid and values.get("VERIFY") in ("updated", "current")
        if build:
            valid = valid and values.get("AFTER_BUILD", "").isdigit() and int(values["AFTER_BUILD"]) >= int(build)
    marker = "FLEETLIGHT_CODEX_UPDATE" if kind == "cli" else "FLEETLIGHT_CODEX_APP_UPDATE"
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
    environment["PATH"] = os.pathsep.join(paths + ["/opt/homebrew/bin", "/usr/local/bin", environment.get("PATH", "/usr/bin:/bin")])
    lines = []
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
                if line.startswith(("FLEETLIGHT_", "ACTIVE_VERSION:", "AFTER_VERSION:", "AFTER_BUILD:", "VERIFY:", "UPDATE:", "RELAUNCH:")):
                    lines.append(line)
                if line.startswith("PHASE:"):
                    state["phase"] = line[6:][:200]
                    save(state_file, state)
            code = process.wait()
        status, detail, after = parse_result(request["kind"], request["target"], request.get("build"), code, lines)
        state.update(state=status, phase=detail, after_version=after, exit_code=code, finished_at=time.time())
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
    if request.get("operation") != "start" or request.get("kind") not in ("cli", "desktop"):
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
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        worker(Path(sys.argv[2]), int(sys.argv[3]))
    else:
        try:
            request = json.loads(sys.stdin.read(200000))
            print("FLEETLIGHT_JOB=" + json.dumps(handle(request)))
        except Exception as error:
            print("FLEETLIGHT_JOB=" + json.dumps({"state": "failed", "phase": "Job request failed: " + type(error).__name__}))
            sys.exit(1)
