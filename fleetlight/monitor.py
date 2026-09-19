"""Bounded parallel probes and local history. No privileged operations."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import time
import uuid

from .config import atomic_json, state_path, validate


def run_process(argv, timeout=25):
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        output, error = process.communicate(timeout=timeout)
        return process.returncode, output, error
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise TimeoutError("Check timed out")


def classify_error(error):
    if "Host key verification failed" in error or "REMOTE HOST IDENTIFICATION" in error:
        return "SSH host key needs verification in a terminal", "access"
    if "Permission denied" in error:
        return "SSH authentication failed", "access"
    if "xcodebuild -license" in error or "Xcode license" in error:
        return "Apple Python is blocked by the Xcode license; Homebrew Python is preferred", "unsupported"
    if "python3" in error and ("not found" in error or "No such" in error):
        return "Python 3 is required on this computer", "unsupported"
    if "resolve hostname" in error:
        return "SSH alias or hostname could not be resolved", "offline"
    return "SSH connection unavailable", "offline"


def python_command(source, extra=""):
    """Run remote Python with Homebrew/CLT first. Apple /usr/bin/python3 can be an Xcode stub."""
    command = ("PATH=/opt/homebrew/bin:/usr/local/bin:/Library/Developer/CommandLineTools/usr/bin:$PATH "
               "python3 -c " + shlex.quote(source))
    return command + ((" " + extra) if extra else "")


def probe_host(host):
    # Validate even callers outside the GTK app; aliases never become options.
    validate({"version": 1, "hosts": [host]})
    nonce = uuid.uuid4().hex
    request = json.dumps({"services": host.get("services", []), "nonce": nonce})
    source = Path(__file__).with_name("probe.py").read_text()
    if host.get("local"):
        argv = [sys.executable, "-c", source, request]
    else:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=6", "-o", "ConnectionAttempts=1",
                "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
                "--", host["alias"], python_command(source, shlex.quote(request))]
    start = time.monotonic()
    base = {"id": host["id"], "checked_at": time.time(), "status": "offline"}
    try:
        code, output, error = run_process(argv)
        if code:
            detail, status = classify_error(error)
            return {**base, "status": status, "error": detail}
        marker = next((line[14:] for line in output.splitlines() if line.startswith("FLEETLIGHT_V1=")), None)
        # Marker is 14 characters including the equals sign.
        if marker is None:
            raise ValueError("No probe receipt")
        data = json.loads(marker)
        if data.get("nonce") != nonce or data.get("schema") != 1:
            raise ValueError("Invalid probe receipt")
        data.pop("nonce", None)
        data.update(optional_services=host.get("optional_services", []), id=host["id"], status="online", check_ms=round(1000 * (time.monotonic() - start)))
        return data
    except (OSError, TimeoutError, ValueError, TypeError):
        return {**base, "error": "Check timed out or returned an invalid receipt"}


def refresh(hosts, callback):
    with ThreadPoolExecutor(max_workers=min(8, len(hosts) or 1)) as pool:
        futures = {pool.submit(probe_host, host): host["id"] for host in hosts}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:
                result = {"id": futures[future], "status": "offline", "checked_at": time.time(),
                          "error": "Unable to check this computer"}
            callback(result)


def issues(snapshot):
    if snapshot.get("status") != "online":
        return [snapshot.get("error", "Not checked yet")]
    result = []
    if snapshot.get("disk_percent", 0) >= 90:
        result.append("Root disk is nearly full")
    if (snapshot.get("memory_percent") or 0) >= 95:
        result.append("Memory usage is high")
    for name, state in snapshot.get("services", {}).items():
        if state not in ("active", "unsupported") and not (name in snapshot.get("optional_services", []) and state in ("inactive", "not installed")):
            result.append(name + ": " + state)
    return result


def linux_update_issues(checks):
    """Linux package and restart findings used by the companion as well as this app."""
    result = []
    if not isinstance(checks, dict):
        return result
    for kind, fallback in (("system", "Linux package updates available"), ("restart", "Restart required")):
        status = checks.get(kind, {})
        if status.get("state") in ("available", "protected"):
            result.append(status.get("detail") or fallback)
    return result


class History:
    def __init__(self, path=None):
        self.path = Path(path or state_path())
        try:
            data = json.loads(self.path.read_text()) if self.path.stat().st_size <= 4_000_000 else {}
            self.samples = data.get("samples", [])[-2048:]
            self.events = data.get("events", [])[-100:]
            if not isinstance(self.samples, list) or not isinstance(self.events, list):
                raise ValueError("Invalid history")
        except (OSError, ValueError, TypeError, AttributeError):
            self.samples, self.events = [], []

    def record(self, snapshots, previous):
        now = time.time()
        for ident, current in snapshots.items():
            old = previous.get(ident)
            current_issues = issues(current)
            if old and (old.get("status") != current.get("status") or issues(old) != current_issues):
                self.events.append({"time": now, "host": ident,
                                    "message": "; ".join(current_issues) or "Connection and services healthy"})
            self.samples.append({"time": now, "host": ident, "status": current.get("status"),
                                 "disk": current.get("disk_percent"), "memory": current.get("memory_percent")})
        self.samples = [s for s in self.samples if isinstance(s, dict) and s.get("time", 0) > now - 86400][-2048:]
        self.events = self.events[-100:]
        atomic_json(self.path, {"samples": self.samples, "events": self.events})
