"""Read-only fleet snapshot for the Android companion, in the Mac observer's schema 1 format.

The document is built from what the app already knows and served on the loopback interface only;
`tailscale serve` (or any HTTPS reverse proxy) is what makes it reachable from the phone.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time

from .monitor import issues, service_issues, service_label, regression_cause

PORT = 8787
PATHS = ("/mobile-feed.json", "/fleetlight/mobile-feed.json")
METRICS_WINDOW_HOURS = 24
METRICS_STEP_SECONDS = 1800
COMPARISON_WINDOWS = (1, 6, 24)
MAX_INCIDENTS = 30
HEALTHY_EVENT = "Connection and services healthy"

REDACTIONS = ((re.compile(r"(?i)https?://\S+"), "[url]"),
              (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[address]"),
              (re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{0,4}\b"), "[address]"),
              (re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b"), "[account]"),
              (re.compile(r"/(Users|home)/[^/\s]+"), r"/\1/[user]"))


def redact(value):
    """Addresses, accounts and home folders never leave this computer, as in the Mac observer's feed."""
    value = str(value or "")
    for pattern, replacement in REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def iso(moment):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)) if isinstance(moment, (int, float)) and moment > 0 else None


def whole(value):
    return int(round(value)) if isinstance(value, (int, float)) else None


def feed_id(host):
    """The phone keys its own order and comparisons on this; `feed_id` keeps it equal to another observer's."""
    return host.get("feed_id") or host["id"]


def pending(status):
    return isinstance(status, dict) and status.get("state") in ("available", "protected")


def services(data):
    result = []
    optional = data.get("optional_services") or []
    regressions = data.get("service_regressions") or {}
    for spec, state in (data.get("services") or {}).items():
        if state == "unsupported":
            continue
        entry = {"kind": "service", "name": service_label(spec)}
        if state == "active":
            entry.update(state="healthy", detail="Active")
        elif spec in optional and state in ("inactive", "not installed"):
            entry.update(state="ok", detail="Not running (optional)")
        else:
            cause = (" since " + regression_cause(regressions[spec])) if spec in regressions else ""
            entry.update(state=str(state), detail=redact(str(state).capitalize() + cause))
        result.append(entry)
    return result


def desktop_app(data, checks):
    """ChatGPT desktop app fields, named after Codex for the older Android clients."""
    status = (checks or {}).get("desktop")
    if not isinstance(status, dict) or status.get("state") in (None, "unknown", "disconnected"):
        return {}
    state = {"current": "current", "available": "update-available", "protected": "update-available",
             "missing": "missing"}.get(status.get("state"), "unavailable")
    result = {"codexDesktopAppPlatform": "macos" if data.get("os") == "Darwin" else "linux",
              "codexDesktopAppProvider": status.get("provider"), "codexDesktopAppVersion": status.get("installed"),
              "codexDesktopAppBuild": status.get("installed_build"), "codexDesktopAppAvailableVersion": status.get("latest"),
              "codexDesktopAppState": state, "codexDesktopAppUpdateAvailable": state == "update-available",
              "codexDesktopAppCheckedAt": iso(status.get("checked_at"))}
    if data.get("os") == "Darwin":
        result.update(codexMacAppVersion=status.get("installed"), codexMacAppBuild=status.get("installed_build"))
    return result


def host_entry(host, data, checks, availability, version, platform=None):
    """One computer as the phone shows it. `data` is this session's check, or {} before the first one."""
    status = data.get("status")
    entry = {"id": feed_id(host), "name": host["name"], "isPinned": False, "services": [], "warnings": []}
    if status != "online":
        state = {"offline": "offline", "access": "access", "unsupported": "attention"}.get(status, "unknown")
        entry.update(platform=platform or "Unknown", state=state, status=state,
                     detail=redact(data.get("error") or "Not checked yet"), checkedAt=iso(data.get("checked_at")),
                     issueTypes=[state] if state in ("offline", "access") else [], health=0 if status else None)
        return {key: value for key, value in entry.items() if value is not None}
    trouble = issues(data)
    in_services = set(service_issues(data))
    linux = data.get("os") == "Linux"
    updates_ready, restart = linux and pending((checks or {}).get("system")), linux and pending((checks or {}).get("restart"))
    kinds = (["alerts"] if trouble else []) + (["restart"] if restart else []) + (["updates"] if updates_ready else [])
    state = "attention" if trouble else "online"
    distribution = data.get("distribution")
    battery = data.get("battery") if isinstance(data.get("battery"), dict) else {}
    busiest = ((data.get("processes") or {}).get("cpu") or [None])[0]
    swap_total = data.get("swap_total")
    entry.update(
        platform=data.get("os") or "Unknown", state=state, status=state,
        detail=redact("; ".join(trouble)) if trouble else "Root disk " + str(data.get("disk_percent", "?")) + "% used",
        checkedAt=iso(data.get("checked_at")), issueTypes=kinds,
        health=max(0, min(100, round(100 * (1 if availability is None else availability)) - 10 * len(trouble))),
        fullProbeMs=whole(data.get("check_ms")),
        operatingSystem=("macOS " + str(distribution) if data.get("os") == "Darwin" else distribution) if distribution else data.get("os"),
        bootDescription=time.strftime("%Y-%m-%d %H:%M", time.localtime(data["checked_at"] - data["uptime"]))
        if isinstance(data.get("uptime"), (int, float)) and isinstance(data.get("checked_at"), (int, float)) else None,
        diskPercent=whole(data.get("disk_percent")), memoryPercent=whole(data.get("memory_percent")),
        loadAverage=data.get("load") if isinstance(data.get("load"), (int, float)) else None,
        codexCliVersion=data.get("codex") if isinstance(data.get("codex"), str) else None,
        claudeCliVersion=data.get("claude") if isinstance(data.get("claude"), str) else None,
        cpuCount=whole(data.get("cpus")),
        swapPercent=whole(100 * (data.get("swap_used") or 0) / swap_total) if isinstance(swap_total, (int, float)) and swap_total > 0 else None,
        cpuTemperatureCelsius=whole(data.get("cpu_temperature")),
        batteryPercent=whole(battery.get("percent")), batteryState=battery.get("state"),
        failedUnitCount=len(data["failed_units"]) if isinstance(data.get("failed_units"), list) else None,
        topProcess=redact(f"{busiest[0]} {busiest[1]}%") if isinstance(busiest, (list, tuple)) and len(busiest) >= 2 else None,
        fleetlightVersion=version if host.get("local") else None,
        restartRequired=bool(restart) if linux else None,
        services=services(data),
        warnings=[{"kind": "alert", "title": redact(line)} for line in trouble if line not in in_services],
        **desktop_app(data, checks))
    return {key: value for key, value in entry.items() if value is not None}


def linux_update(host, data, checks):
    """Package state of one Linux computer, from the last update check."""
    system = (checks or {}).get("system") if isinstance((checks or {}).get("system"), dict) else {}
    packages = [name for name in system.get("packages") or [] if isinstance(name, str)]
    ready = pending(system)
    state = "updateAvailable" if ready else "current" if system.get("state") == "current" else "unknown"
    return {"hostId": feed_id(host), "hostName": host["name"], "state": state,
            "detail": redact(system.get("detail") or "Not checked yet"),
            "packageManager": data.get("package_manager"),
            "availableCount": (len(packages) or 1) if ready else 0, "securityCount": 0,
            "snapCount": sum(name.startswith("snap:") for name in packages) if ready else 0,
            "flatpakCount": sum(name.startswith("flatpak:") for name in packages) if ready else 0,
            "restartRequired": pending((checks or {}).get("restart")), "checkedAt": iso(system.get("checked_at"))}


def incidents(events, hosts):
    """Status changes, newest first. Repeats of the same line for a computer are folded into the first."""
    known = {host["id"]: host for host in hosts}
    result, last = [], {}
    for event in events or []:
        if not isinstance(event, dict) or event.get("host") not in known or not isinstance(event.get("time"), (int, float)):
            continue
        message = str(event.get("message") or "")
        if last.get(event["host"]) == message:
            continue
        last[event["host"]] = message
        host = known[event["host"]]
        healthy = message == HEALTHY_EVENT
        first, _, rest = message.partition("; ")
        result.append({"id": feed_id(host) + "-" + str(int(event["time"] * 1000)), "hostId": feed_id(host),
                       "hostName": host["name"], "kind": "recovered" if healthy else "attention",
                       "severity": "info" if healthy else "warning",
                       "title": "Healthy again" if healthy else redact(first), "detail": redact(rest),
                       "startedAt": iso(event["time"])})
    return result[::-1][:MAX_INCIDENTS]


def average(values):
    values = [value for value in values if isinstance(value, (int, float))]
    return sum(values) / len(values) if values else None


def metrics(host, rows, now):
    """A day of samples in half-hour steps. Rows follow History.FIELDS: time, up, disk, memory, cpu, load, temperature, ms."""
    buckets = {}
    for row in rows:
        if now - METRICS_WINDOW_HOURS * 3600 <= row[0] <= now:
            buckets.setdefault(int(row[0] // METRICS_STEP_SECONDS), []).append(row)
    result = []
    for key in sorted(buckets):
        columns = list(zip(*buckets[key]))
        up = average(columns[1])
        sample = {"hostId": feed_id(host), "capturedAt": iso(average(columns[0])),
                  "state": "online" if up is None or up >= 0.5 else "offline",
                  "fullProbeMs": whole(average(columns[7])), "diskPercent": whole(average(columns[2])),
                  "memoryPercent": whole(average(columns[3])), "loadAverage": average(columns[5])}
        if sample["loadAverage"] is not None:
            sample["loadAverage"] = round(sample["loadAverage"], 2)
        result.append({key: value for key, value in sample.items() if value is not None})
    return result


def timing_comparisons(host, rows, now):
    """How long a full check took in the last 1, 6 and 24 hours against the period before each."""
    def period(start, end):
        inside = [row for row in rows if start < row[0] <= end and isinstance(row[7], (int, float)) and row[1]]
        mean = average([row[7] for row in inside])
        return whole(mean), len(inside), (inside[-1][0] - inside[0][0]) if len(inside) > 1 else None
    result = []
    for hours in COMPARISON_WINDOWS:
        span = hours * 3600
        current, previous = period(now - span, now), period(now - 2 * span, now - span)
        result.append({"hostId": feed_id(host), "metric": "fullProbe", "windowHours": hours,
                       "currentAverageMs": current[0], "currentSampleCount": current[1], "currentCoverageSeconds": current[2],
                       "previousAverageMs": previous[0], "previousSampleCount": previous[1], "previousCoverageSeconds": previous[2]})
    return result


def document(configuration, snapshots, app_updates, history, version, now=None, refresh_ms=None, last_seen=None):
    """The whole feed. `snapshots` holds this session's checks only, so a computer not checked yet reads as unknown;
    `last_seen` (each computer's last online check) only says what kind of computer an unreachable one is."""
    now = time.time() if now is None else now
    hosts = configuration["hosts"]
    label = "v" + version + " (Linux)"
    local = next((host for host in hosts if host.get("local")), hosts[0])
    entries, updates = [], []
    for host in hosts:
        data = snapshots.get(host["id"]) or {}
        seen = data if data.get("status") == "online" else (last_seen or {}).get(host["id"]) or {}
        checks = app_updates.get(host["id"])
        available = history.availability(host["id"], now - 86400, now, 1)[0]
        entries.append(host_entry(host, data, checks, available, label, platform=seen.get("os")))
        if seen.get("os") == "Linux":
            updates.append(linux_update(host, seen, checks))
    states = [entry["state"] for entry in entries]
    return {
        "schemaVersion": 1, "generatedAt": iso(now), "metricsWindowHours": METRICS_WINDOW_HOURS,
        "metricsSampleIntervalSeconds": METRICS_STEP_SECONDS,
        "observer": {"id": feed_id(local), "name": local["name"], "appVersion": label, "status": "reporting",
                     **({"lastRefreshDurationMilliseconds": int(refresh_ms)} if isinstance(refresh_ms, (int, float)) else {})},
        "summary": {"total": len(entries), "online": sum(state in ("online", "attention") for state in states),
                    "offline": states.count("offline"), "accessIssues": states.count("access"), "slowConnections": 0,
                    "alerts": states.count("attention"),
                    "updatesAvailable": sum(item["availableCount"] > 0 for item in updates),
                    "restartRequired": sum(item["restartRequired"] for item in updates)},
        "hosts": entries, "linuxUpdates": updates, "incidents": incidents(history.events, hosts),
        "metrics": [sample for host in hosts for sample in metrics(host, history.series.get(host["id"], []), now)],
        "timingComparisons": [item for host in hosts if not host.get("local")
                              for item in timing_comparisons(host, history.series.get(host["id"], []), now)],
    }


def encode(feed):
    return json.dumps(feed, separators=(",", ":"), sort_keys=True).encode()


class FeedServer:
    """Serves the latest published feed on 127.0.0.1. Nothing is served until the first publish."""

    def __init__(self):
        self.payload = None
        self.server = None

    @property
    def running(self):
        return self.server is not None

    def publish(self, payload):
        self.payload = payload

    def start(self, port=PORT):
        if self.server is not None:
            return True
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, body):
                payload = owner.payload
                found = self.path.split("?", 1)[0] in PATHS
                code = 404 if not found else 503 if payload is None else 200
                content = payload if code == 200 else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    self.wfile.write(content)

            def do_GET(self):
                self.reply(True)

            def do_HEAD(self):
                self.reply(False)

            def log_message(self, *_):
                pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            return False
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return True

    def stop(self):
        server, self.server, self.payload = self.server, None, None
        if server is not None:
            threading.Thread(target=lambda: (server.shutdown(), server.server_close()), daemon=True).start()
