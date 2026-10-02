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
    """Status events and per-computer samples saved on this computer.

    Every check is kept for two hours; older samples are averaged into 5-minute steps for a day
    and 15-minute steps for a week, so a large fleet still fits in a small file.
    """
    FIELDS = ("time", "up", "disk", "memory", "cpu", "load", "temperature", "ms")
    RAW, DAY, WEEK = 2 * 3600, 86400, 7 * 86400
    SAVE_INTERVAL = 240

    def __init__(self, path=None):
        self.path = Path(path or state_path())
        self.series, self.events = {}, []
        self.saved_at = 0
        try:
            data = json.loads(self.path.read_text()) if self.path.stat().st_size <= 4_000_000 else {}
            events = data.get("events", [])[-100:]
            if not isinstance(events, list):
                raise ValueError("Invalid history")
            self.events = events
            width = len(self.FIELDS)
            if isinstance(data.get("series"), dict):
                for ident, rows in data["series"].items():
                    if isinstance(ident, str) and isinstance(rows, list):
                        self.series[ident] = [(row + [None] * width)[:width] for row in rows
                                              if isinstance(row, list) and row and isinstance(row[0], (int, float))]
            else:
                # Version 1 kept one object per check with disk and memory only.
                for sample in data.get("samples", []):
                    if isinstance(sample, dict) and isinstance(sample.get("host"), str) and isinstance(sample.get("time"), (int, float)):
                        self.series.setdefault(sample["host"], []).append(
                            [sample["time"], 1 if sample.get("status") == "online" else 0,
                             sample.get("disk"), sample.get("memory"), None, None, None, None])
        except (OSError, ValueError, TypeError, AttributeError):
            self.series, self.events = {}, []

    @classmethod
    def compact(cls, rows, now):
        """Average samples older than two hours into coarser steps; drop anything older than a week."""
        kept, key, bucket = [], None, []

        def close():
            if len(bucket) == 1:
                kept.append(bucket[0])
            elif bucket:
                merged = []
                for column in zip(*bucket):
                    values = [value for value in column if isinstance(value, (int, float))]
                    merged.append(round(sum(values) / len(values), 2) if values else None)
                kept.append(merged)
            bucket.clear()

        for row in sorted(rows, key=lambda item: item[0]):
            age = now - row[0]
            if age > cls.WEEK:
                continue
            step = 0 if age <= cls.RAW else 300 if age <= cls.DAY else 900
            current = (step, int(row[0] // step)) if step else None
            if current != key or current is None:
                close()
                key = current
            bucket.append(row)
        close()
        return kept

    def sample(self, ident, current, now):
        online = current.get("status") == "online"
        cpu = current.get("cpu_percent")
        if cpu is None and isinstance(current.get("load"), (int, float)) and current.get("cpus"):
            cpu = min(100, round(100 * current["load"] / current["cpus"]))
        self.series.setdefault(ident, []).append(
            [now, 1 if online else 0, current.get("disk_percent"), current.get("memory_percent"),
             cpu if online else None, current.get("load"), current.get("cpu_temperature"), current.get("check_ms")])

    def record(self, snapshots, previous):
        now = time.time()
        changed = False
        for ident, current in snapshots.items():
            old = previous.get(ident)
            current_issues = issues(current)
            if old and (old.get("status") != current.get("status") or issues(old) != current_issues):
                self.events.append({"time": now, "host": ident,
                                    "message": "; ".join(current_issues) or "Connection and services healthy"})
                changed = True
            self.sample(ident, current, now)
        self.series = {ident: self.compact(rows, now) for ident, rows in self.series.items()}
        self.series = {ident: rows for ident, rows in self.series.items() if rows}
        self.events = self.events[-100:]
        if changed or now - self.saved_at >= self.SAVE_INTERVAL:
            self.save()

    def save(self):
        atomic_json(self.path, {"version": 2, "fields": list(self.FIELDS), "series": self.series,
                                "events": self.events, "samples": []}, indent=None)
        self.saved_at = time.time()

    def points(self, ident, field, since=0):
        """(time, value) pairs for one computer; value is None where nothing was measured."""
        column = self.FIELDS.index(field)
        return [(row[0], row[column]) for row in self.series.get(ident, []) if row[0] >= since]

    def availability(self, ident, start, end, segments):
        """Share of checks that reached the computer in each equal slice of time; None without checks."""
        totals = [[0, 0] for _ in range(segments)]
        width = (end - start) / segments
        for moment, up in self.points(ident, "up", start):
            if moment < end and isinstance(up, (int, float)):
                slot = totals[min(segments - 1, int((moment - start) / width))]
                slot[0] += up
                slot[1] += 1
        return [total / count if count else None for total, count in totals]
