"""Release checks and durable update orchestration for Linux and macOS."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from .config import validate
from .monitor import python_command, run_process
from .update_job import version

ROOT = Path(__file__).parent
REGISTRY = "https://registry.npmjs.org/@openai/codex/latest"
APPCAST = "https://persistent.oaistatic.com/codex-app-prod/appcast.xml"


def connection(host, command):
    validate({"version": 1, "hosts": [host]})
    if host.get("local"):
        return ["/bin/sh", "-c", command]
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2", "--", host["alias"], command]


def parse_appcast(raw):
    if len(raw) > 2_000_000:
        raise ValueError("Release feed is too large")
    item = ET.fromstring(raw).find("./channel/item")
    if item is None:
        raise ValueError("No release was found")
    fields = {element.tag.rsplit("}", 1)[-1]: element.text for element in item}
    release, build = fields.get("shortVersionString"), fields.get("version", "")
    enclosure = item.find("enclosure")
    url = enclosure.get("url", "") if enclosure is not None else ""
    if not version(release) or not build.isdigit() or not re.fullmatch(r"https://persistent\.oaistatic\.com/codex-app-prod/ChatGPT-darwin-arm64-[0-9.]+\.zip", url):
        raise ValueError("Invalid official application release")
    return {"version": release, "build": build}


def releases():
    result = {}
    try:
        request = urllib.request.Request(REGISTRY, headers={"User-Agent": "Fleetlight/0.2"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read(1_000_000))
        if not version(data.get("version")):
            raise ValueError("Invalid stable version")
        result["cli"] = {"version": data["version"]}
    except Exception:
        result["cli"] = {"error": "Could not check the official npm registry"}
    try:
        # The official appcast rejects Python's default HTTP user agent.
        code, raw, _ = run_process(["curl", "-fsSL", "--connect-timeout", "10", "--max-time", "20", APPCAST], 25)
        if code:
            raise ValueError("Feed unavailable")
        result["desktop"] = parse_appcast(raw)
    except Exception:
        result["desktop"] = {"error": "Could not check the official macOS appcast"}
    return result


def plan(installed, latest, provider, detail="", build=None, installed_build=None, protected=False):
    status = "unknown"
    if version(installed) and version(latest):
        newer = (int(build) > int(installed_build)) if build and installed_build and str(installed_build).isdigit() else version(latest) > version(installed)
        status = "available" if newer else "current"
    elif not installed:
        status = "missing"
    if protected:
        status = "protected"
    return {"state": status, "installed": installed, "latest": latest, "provider": provider,
            "build": build, "installed_build": installed_build, "detail": detail, "checked_at": time.time()}


def parse_desktop_check(code, output, error=""):
    lines = output.splitlines()
    fields = dict(line.split(":", 1) for line in lines if ":" in line)
    installed = fields.get("INSTALLED_VERSION")
    latest = fields.get("AVAILABLE_VERSION")
    provider = fields.get("PROVIDER")
    modified = fields.get("INSTALLATION") == "modified"
    if code == 0 and "FLEETLIGHT_CODEX_APP_RELEASE_CHECK" in lines and fields.get("VERIFY") == "ok":
        result = plan(installed, latest, provider, "Local app repair detected; review it before updating" if modified else "Repository metadata refreshed", protected=modified)
        if not modified and version(installed) and version(latest):
            result["state"] = "available" if fields.get("UPDATE_AVAILABLE") == "1" else "current"
        return result
    from .update_job import ERRORS
    reason = fields.get("CHECK", "failed")
    detail = ERRORS.get(reason, "Update check failed. Check the connection and try again.")
    result = plan(installed, None, provider, detail, protected=modified or reason == "installation-invalid")
    if result["state"] == "missing" and reason != "missing":
        result["state"] = "unknown"
    return result


def check_host(host, snapshot, official):
    info = snapshot.get("codex_installation", {})
    installed = snapshot.get("codex")
    cli = plan(installed, official.get("cli", {}).get("version"), info.get("method"), official.get("cli", {}).get("error", "Official stable release"))
    if cli["state"] == "available" and info.get("method") not in ("standalone", "npm", "mise"):
        cli.update(state="unsupported", detail="This installation method requires a manual update")
    if snapshot.get("status") != "online":
        cli.update(state="offline", detail="Computer is offline")
        return {"cli": cli, "desktop": {**cli, "installed": None, "latest": None}}
    app = snapshot.get("chatgpt", {})
    if snapshot.get("os") == "Darwin":
        release = official.get("desktop", {})
        desktop = plan(app.get("version"), release.get("version"), "macos-appcast", release.get("error", "Signed OpenAI application"), release.get("build"), app.get("build"))
        if snapshot.get("architecture") != "arm64":
            desktop.update(state="unsupported", detail="The current macOS app requires Apple Silicon")
        elif app.get("version") and not app.get("writable", False):
            desktop.update(state="protected", detail="This account cannot replace /Applications/ChatGPT.app")
    elif snapshot.get("os") == "Linux":
        try:
            script = (ROOT / "updaters/desktop_check.sh").read_text()
            code, output, error = run_process(connection(host, "/bin/sh -c " + shlex.quote(script)), timeout=180)
            desktop = parse_desktop_check(code, output, error)
        except (OSError, TimeoutError):
            desktop = plan(app.get("version"), None, app.get("provider"), "Repository check timed out or the SSH connection failed")
    else:
        desktop = plan(app.get("version"), None, None, "Unsupported operating system")
    result = {"cli": cli, "desktop": desktop}
    if snapshot.get("os") == "Linux":
        result.update(check_system(host))
    return result


def check_all(hosts, snapshots, callback):
    official = releases()
    with ThreadPoolExecutor(max_workers=min(4, len(hosts) or 1)) as pool:
        pending = {pool.submit(check_host, host, snapshots.get(host["id"], {}), official): host["id"] for host in hosts}
        for future in as_completed(pending):
            ident = pending[future]
            try:
                value = future.result()
            except Exception:
                value = {kind: plan(None, None, None, "Update check failed") for kind in ("cli", "desktop")}
            callback(ident, value)


def job_request(host, request):
    source = (ROOT / "update_job.py").read_text()
    payload = dict(request)
    if request["operation"] == "start":
        payload["worker_source"] = source
    command = python_command(source)
    # The payload travels through stdin; no shell interpolation and no secrets in argv.
    try:
        result = subprocess.run(connection(host, command), input=json.dumps(payload), text=True,
                                capture_output=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        return {"id": request["id"], "state": "disconnected", "phase": "Connection lost; checking the existing job again"}
    marker = next((line[len("FLEETLIGHT_JOB="):] for line in result.stdout.splitlines() if line.startswith("FLEETLIGHT_JOB=")), None)
    try:
        parsed = json.loads(marker)
        if not isinstance(parsed, dict) or parsed.get("id") != request["id"]:
            raise ValueError("Bad receipt")
        return parsed
    except (TypeError, ValueError):
        return {"id": request["id"], "state": "disconnected", "phase": "No verified job receipt; checking the same job again"}


def start_job(host, kind, checked, ident=None):
    if kind not in ("cli", "desktop", "system", "restart") or not version(checked.get("latest")):
        raise ValueError("A verified release check is required")
    if checked.get("state") not in ("available", "current"):
        raise ValueError("This installation is protected or unavailable")
    ident = ident or uuid.uuid4().hex
    if kind in ("system", "restart"):
        script = python_command((ROOT / "system_ops.py").read_text(), "update" if kind == "system" else "restart")
    else:
        script = (ROOT / ("updaters/cli.sh" if kind == "cli" else "updaters/desktop.sh")).read_text()
    return job_request(host, {"operation": "start", "id": ident, "kind": kind,
                              "target": checked["latest"], "build": checked.get("build") or "",
                              "script": script})


def job_status(host, ident):
    return job_request(host, {"operation": "status", "id": ident})


AUTO_UPDATE_KINDS = ("cli", "desktop", "system")


def auto_target_key(host, kind, checked):
    ident = host["id"] if isinstance(host, dict) else host
    checked = checked if isinstance(checked, dict) else {}
    if kind == "system":
        packages = checked.get("packages") or []
        token = ",".join(packages) if isinstance(packages, list) else str(packages)
        return (ident, kind, token or str(checked.get("detail") or "system"))
    return (ident, kind, str(checked.get("latest") or ""), str(checked.get("build") or ""))


def next_auto_batch(hosts, snapshots, checks, attempted=(), now=None):
    """Next unattended Codex, ChatGPT or Linux-package batch; never automatic restarts."""
    skipped = set(tuple(item) for item in attempted)
    for kind in AUTO_UPDATE_KINDS:
        pending, _ = batch_candidates(hosts, snapshots, checks, kind, now=now)
        pending = [item for item in pending if auto_target_key(item["host"], kind, item["checked"]) not in skipped]
        if pending:
            return kind, pending
    return None, []


def batch_candidates(hosts, snapshots, checks, kind, now=None):
    """Freeze only online, supported, fresh, available releases for review."""
    if kind not in ("cli", "desktop", "system", "restart"):
        raise ValueError("Unknown application")
    now = time.time() if now is None else now
    eligible, skipped = [], []
    for host in hosts:
        checked = checks.get(host["id"], {}).get(kind, {})
        reason = checked.get("state", "not checked")
        if snapshots.get(host["id"], {}).get("status") != "online":
            reason = "offline"
        elif kind == "restart" and not snapshots.get(host["id"], {}).get("boot_id"):
            reason = "boot identity unavailable"
        elif now - checked.get("checked_at", 0) > 1800:
            reason = "check again"
        elif reason == "available" and version(checked.get("latest")):
            eligible.append({"host": dict(host), "checked": dict(checked)})
            continue
        skipped.append({"name": host["name"], "reason": reason})
    return eligible, skipped


def relevant_job(last, checks):
    """Keep a failed job visible only while that update is still outstanding."""
    if not isinstance(last, dict):
        return None
    if last.get("state") == "succeeded":
        return last
    checked = checks.get(last.get("kind"), {}) if isinstance(checks, dict) else {}
    if checked.get("state") == "current":
        return None
    if last.get("kind") in ("cli", "desktop") and version(checked.get("installed")) and version(last.get("target")):
        if version(checked["installed"]) >= version(last["target"]):
            return None
    return last


def check_system(host):
    try:
        source = (ROOT / "system_ops.py").read_text()
        code, output, _ = run_process(connection(host, python_command(source, "check")), timeout=180)
        raw = next(line.split("=", 1)[1] for line in output.splitlines() if line.startswith("FLEETLIGHT_SYSTEM_CHECK="))
        value = json.loads(raw)
        if code or value.get("state") not in ("available", "current", "protected", "unknown", "unsupported"):
            raise ValueError("Invalid system check")
        system = {"state": value["state"], "latest": "0.0.0", "checked_at": time.time(),
                  "detail": value.get("detail", ""), "provider": value.get("manager"), "packages": value.get("packages", [])}
        restart = value.get("restart", {})
        reboot = {"state": "available" if restart.get("required") is True else "unknown" if restart.get("required") is None else "current",
                  "latest": "0.0.0", "checked_at": time.time(), "detail": restart.get("reason", "Restart requirement unknown")}
        return {"system": system, "restart": reboot}
    except TimeoutError:
        return {kind: {"state": "unknown", "checked_at": time.time(),
                       "detail": "System package check timed out; check package-manager or network activity and retry"}
                for kind in ("system", "restart")}
    except (OSError, ValueError, StopIteration, TypeError):
        return {kind: {"state": "unknown", "checked_at": time.time(), "detail": "System check failed"} for kind in ("system", "restart")}
