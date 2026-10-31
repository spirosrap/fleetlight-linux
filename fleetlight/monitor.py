"""Bounded parallel probes and local history. No privileged operations."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
    for item in snapshot.get("memory_filesystems") or []:
        if isinstance(item, dict) and (item.get("percent") or 0) >= 90:
            result.append(str(item.get("mount")) + " is " + str(item["percent"]) + "% full (memory-backed)")
    for item in snapshot.get("memory_runaways") or []:
        result.append(item["name"] + " memory grew from " + gigabytes(item["from"]) + " to " + gigabytes(item["to"])
                      + " in " + str(item["minutes"]) + " min")
    regressions = snapshot.get("service_regressions") or {}
    for name, state in snapshot.get("services", {}).items():
        if name in regressions and state not in ("active", "unsupported"):
            result.append(service_label(name) + ": " + state + " since " + regression_cause(regressions[name]))
        elif state not in ("active", "unsupported") and not (name in snapshot.get("optional_services", []) and state in ("inactive", "not installed")):
            result.append(service_label(name) + ": " + state)
    result.extend(tailscale_issues(snapshot.get("tailscale")))
    result.extend(crash_issues(snapshot.get("crashes"), snapshot.get("oom_events"), seen_until=snapshot.get("crashes_seen_until") or 0))
    result.extend(pressure_issues(snapshot.get("pressure")))
    return result


def clock(timestamp):
    return time.strftime("%H:%M", time.localtime(timestamp))


def duration(seconds):
    """Short idle or streaming duration: 45 s, 12 min, 3 h, 2 d."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


IDLE_AFTER = 600


def presence_summary(presence, now=None):
    """(text, in use) for a computer's presence report: what someone at it or connected to it is doing.

    In use means a stream, a remote desktop viewer, an SSH terminal, or an unlocked desktop with its displays on
    that is not known to be idle for ten minutes. Hyprland reports no idle time, so its unlocked desktop counts.
    """
    if not isinstance(presence, dict):
        return None, False
    now = time.time() if now is None else now
    stream = presence.get("stream") or {}
    if stream.get("streaming"):
        since = stream.get("since")
        return ("Streaming to Moonlight" + (" since " + clock(since) if isinstance(since, (int, float)) else "")), True
    for item in presence.get("remote_desktop") or []:
        if isinstance(item, dict):
            return str(item.get("kind") or "Remote desktop") + " viewer connected" + (
                " from " + str(item["from"]) if item.get("from") else ""), True
    terminals = [item for item in presence.get("terminals") or [] if isinstance(item, dict)]
    desktop = presence.get("desktop")
    idle = presence.get("idle_seconds")
    if desktop and not presence.get("locked") and not presence.get("displays_off") and not (
            isinstance(idle, (int, float)) and idle >= IDLE_AFTER):
        return ("In use" if isinstance(idle, (int, float)) else "Desktop unlocked"), True
    if terminals:
        return "SSH terminal from " + str(terminals[0].get("from")), True
    if not desktop:
        sessions = [item for item in presence.get("remote_sessions") or [] if isinstance(item, dict)]
        if sessions:
            return "Remote desktop session open, no viewer", False
        return "No one signed in", False
    if presence.get("locked"):
        text = "Locked"
    elif presence.get("displays_off"):
        text = "Displays off"
    else:
        text = "Idle"
    if isinstance(idle, (int, float)) and idle >= IDLE_AFTER:
        text += (" · " if text != "Idle" else " ") + ("idle " if text != "Idle" else "") + duration(idle)
    return text, False


CRASH_WINDOW = 86400


def crash_issues(crashes, oom_events=(), now=None, seen_until=0):
    """One line per program that crashed in the last day, newest first, plus out-of-memory kills seen in that day.

    Lines name the latest crash time rather than a count, so a crash ageing out never reads as a new problem.
    Crashes and kills up to `seen_until` were dismissed by the user and no longer count.
    """
    now = time.time() if now is None else now
    result = []
    latest = {}
    for item in (crashes or {}).get("items") or [] if isinstance(crashes, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("at"), (int, float)) and now - item["at"] <= CRASH_WINDOW \
                and item["at"] > seen_until:
            name = str(item.get("name") or "A program")
            latest[name] = max(latest.get(name, 0), item["at"])
    ordered = sorted(latest.items(), key=lambda pair: -pair[1])
    for name, when in ordered[:3]:
        result.append(name + " crashed at " + clock(when))
    if len(ordered) > 3:
        result.append(str(len(ordered) - 3) + " more programs crashed today")
    recent = [moment for moment in oom_events or () if now - moment <= CRASH_WINDOW and moment > seen_until]
    if recent:
        result.append("Out of memory: a program was killed at " + clock(max(recent)))
    return result


def has_crash_issues(snapshot):
    return bool(crash_issues(snapshot.get("crashes"), snapshot.get("oom_events"), seen_until=snapshot.get("crashes_seen_until") or 0))


PRESSURE_LIMITS = {"memory": ("full", "avg60", 10, "Memory is thrashing: tasks stalled {:.0f}% of the last minute"),
                   "io": ("full", "avg60", 30, "Disk I/O stalled tasks {:.0f}% of the last minute"),
                   "cpu": ("some", "avg300", 80, "CPU is saturated: tasks waited {:.0f}% of the last 5 minutes")}


def pressure_issues(pressure):
    """Sustained stalls from Linux PSI, and macOS critical memory pressure."""
    if not isinstance(pressure, dict):
        return []
    if pressure.get("memory_level") == "critical":
        return ["Memory pressure is critical"]
    result = []
    for resource, (kind, window, limit, message) in PRESSURE_LIMITS.items():
        value = ((pressure.get(resource) or {}).get(kind) or {}).get(window)
        if isinstance(value, (int, float)) and value >= limit:
            result.append(message.format(value))
    return result


class OomWatch:
    """When out-of-memory kills happened, from the probe's per-boot counter (kernel oom_kill or macOS Jetsam reports).

    The counter carries no times, so an increase is stamped when it is first seen; kills that were already
    counted when Fleetlight started are shown on the page but never alerted.
    """
    def __init__(self):
        self.seen = {}

    def observe(self, snapshot, now=None):
        if snapshot.get("status") != "online":
            return list(self.seen.get(snapshot.get("id"), {}).get("events", []))
        now = time.time() if now is None else now
        crashes = snapshot.get("crashes") if isinstance(snapshot.get("crashes"), dict) else {}
        count, boot = crashes.get("oom_kills"), crashes.get("since")
        if not isinstance(count, int):
            return []
        entry = self.seen.get(snapshot.get("id"))
        if entry is None or entry["boot"] != boot or count < entry["count"]:
            entry = {"boot": boot, "count": count, "events": []}
        elif count > entry["count"]:
            entry["events"].append(now)
            entry["count"] = count
        entry["events"] = [moment for moment in entry["events"] if now - moment <= CRASH_WINDOW]
        self.seen[snapshot.get("id")] = entry
        return list(entry["events"])


def gigabytes(size):
    return f"{size / 1024**3:.1f} GB"


class MemoryWatch:
    """Programs whose memory keeps growing over the last hour, like fseventsd filling the Air.

    Samples come from the probe's largest programs (grouped by name). A program counts as running away when,
    over at least 45 minutes, it grew by 1 GiB and by half, mostly without shrinking, and now holds at least
    15% of the computer's memory. A browser opening tabs rarely grows that steadily; a leak does.
    """
    WINDOW, MINIMUM_SPAN = 3600, 2700
    GROWTH, SHARE, STEADY = 1024**3, 0.15, 0.75

    def __init__(self):
        self.samples = {}

    def observe(self, snapshot, now=None):
        if snapshot.get("status") != "online":
            return []
        now = time.time() if now is None else now
        programs = self.samples.setdefault(snapshot.get("id"), {})
        largest = (snapshot.get("processes") or {}).get("memory") or []
        for item in largest:
            if isinstance(item, list) and len(item) >= 2 and isinstance(item[1], (int, float)):
                programs.setdefault(str(item[0]), []).append((now, item[1]))
        total = snapshot.get("memory_total") or 0
        result = []
        for name in list(programs):
            rows = [row for row in programs[name] if now - row[0] <= self.WINDOW]
            if not rows:
                del programs[name]
                continue
            programs[name] = rows
            if now - rows[0][0] < self.MINIMUM_SPAN or rows[-1][0] < now - 300:
                continue
            start, end = min(size for moment, size in rows if moment - rows[0][0] <= 600), rows[-1][1]
            pairs = list(zip(rows, rows[1:]))
            steady = sum(1 for a, b in pairs if b[1] >= a[1] * 0.98) / len(pairs) if pairs else 0
            if (end - start >= self.GROWTH and end >= 1.5 * start and steady >= self.STEADY
                    and total and end >= self.SHARE * total):
                result.append({"name": name, "from": start, "to": end, "minutes": round((now - rows[0][0]) / 60)})
        return result


def service_label(spec):
    """Short name for a service entry: user:app-dev.lizardbyte.app.Sunshine.service reads "Sunshine"."""
    if spec.startswith("port:"):
        return "port " + spec[5:]
    if spec.startswith(("http://", "https://")):
        return spec.split("://", 1)[1].rstrip("/")
    name = spec[5:] if spec.startswith("user:") else spec
    name = name[:-8] if name.endswith(".service") else name
    parts = name.split(".")
    # Reverse-DNS names (desktop app units, launchd labels) end in the readable part.
    return parts[-1] if len(parts) >= 3 and parts[-1] else name


def regression_cause(regression):
    moment = time.strftime("%a %H:%M", time.localtime(regression.get("at") or 0))
    if regression.get("reason") != "packages":
        return "the restart at " + moment
    changes = regression.get("changes") or []
    detail = ", ".join(changes[:2]) + (" +" + str(regression.get("count", len(changes)) - 2) + " more"
                                        if regression.get("count", len(changes)) > 2 else "")
    return "the package update at " + moment + (" (" + detail + ")" if detail else "")


class ServiceWatch:
    """Remembers which services ran before each package update or restart, so ones that stop afterwards are named.

    A computer's marker is its boot plus its latest package transaction. When the marker changes, every
    service that was running under the previous marker and is not running now is a regression, until it runs again.
    """
    WINDOW = 1800

    def __init__(self, path=None):
        self.path = Path(path or state_path().with_name("service-watch.json"))
        try:
            data = json.loads(self.path.read_text())
            self.hosts = data["hosts"] if isinstance(data.get("hosts"), dict) else {}
        except (OSError, ValueError, KeyError, TypeError):
            self.hosts = {}

    def observe(self, snapshot, now=None):
        """The regressions for this snapshot, as {service: {reason, at, changes, count}}; saves when they change."""
        ident = snapshot.get("id")
        entry = self.hosts.get(ident)
        if snapshot.get("status") != "online":
            return dict((entry or {}).get("regressions", {}))
        now = time.time() if now is None else now
        states = snapshot.get("services") or {}
        active = sorted(name for name, state in states.items() if state == "active")
        activity = snapshot.get("package_activity") or {}
        marker = [snapshot.get("boot_id"), activity.get("at")]
        before = json.dumps(entry, sort_keys=True)
        if not isinstance(entry, dict):
            entry = {"marker": marker, "active": active, "changed_at": 0, "regressions": {}}
        elif entry.get("marker") != marker:
            packages = activity.get("at") and activity.get("at") != (entry.get("marker") or [None, None])[1]
            cause = {"reason": "packages" if packages else "restart",
                     "at": activity.get("at") if packages else now,
                     "changes": self.prioritise(activity.get("changes") or [], entry.get("active", [])) if packages else [],
                     "count": activity.get("count", 0) if packages else 0}
            entry.update(marker=marker, changed_at=now, cause=cause, baseline=entry.get("active", []))
        regressions = {name: value for name, value in entry.get("regressions", {}).items()
                       if name in states and states[name] not in ("active", "unsupported")}
        if now - entry.get("changed_at", 0) <= self.WINDOW:
            for name in entry.get("baseline", []):
                if name in states and states[name] not in ("active", "unsupported") and name not in regressions:
                    regressions[name] = entry["cause"]
        else:
            entry.pop("baseline", None)
        entry["regressions"] = regressions
        # Inside the window keep the pre-update set, so a slow start still counts against the update.
        entry["active"] = sorted(set(active) | set(entry.get("baseline", []))) if "baseline" in entry else active
        self.hosts[ident] = entry
        if json.dumps(entry, sort_keys=True) != before:
            try:
                atomic_json(self.path, {"version": 1, "hosts": self.hosts}, indent=None)
            except OSError:
                pass
        return dict(regressions)

    @staticmethod
    def prioritise(changes, services):
        """Put packages whose name appears in a watched service first: they are the likely cause."""
        words = {service_label(name).lower() for name in services}
        return sorted(changes, key=lambda change: not any(word and word in change.split(" ")[0].lower() for word in words))


TAILSCALE_EXPIRY_WARNING = 7 * 86400


def tailscale_issues(status, now=None):
    """Warn a week ahead when a Tailscale node key expires, like the Fleetlight Mac observer does."""
    if not isinstance(status, dict):
        return []
    if status.get("backend_state") == "NeedsLogin":
        return ["Tailscale is logged out"]
    expiry = status.get("key_expiry")
    if not isinstance(expiry, str):
        return []
    try:
        when = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return []
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    remaining = when.timestamp() - (time.time() if now is None else now)
    if remaining <= 0:
        return ["Tailscale login has expired"]
    if remaining >= TAILSCALE_EXPIRY_WARNING:
        return []
    days = int(remaining // 86400)
    return ["Tailscale login expires " + ("today" if days == 0 else "tomorrow" if days == 1 else "in %d days" % days)]


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
