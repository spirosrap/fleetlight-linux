"""Local Codex and Cursor remaining-quota checks. Never persist session tokens."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request


NAMES = ("codex", "cursor")
CURSOR_USAGE = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"


def remaining_percent(used):
    try:
        return max(0, min(100, round(100 - float(used))))
    except (TypeError, ValueError):
        return None


def until(timestamp):
    try:
        seconds = max(0, int(float(timestamp) - time.time()))
    except (TypeError, ValueError):
        return ""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def agent_path():
    home = Path.home()
    parts = [str(home / suffix) for suffix in (".local/bin", ".local/share/mise/shims", ".npm-global/bin")]
    parts += ["/usr/share/omarchy/bin", "/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")]
    return os.pathsep.join(parts)


def which(name):
    return shutil.which(name, path=agent_path())


def environment():
    env = dict(os.environ)
    env["PATH"] = agent_path()
    return env


def unavailable(name, detail):
    return {"id": name, "name": name.title(), "state": "unavailable", "remaining_percent": None, "detail": detail}


def window_label(minutes):
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "limit"
    if minutes == 10080:
        return "weekly"
    if minutes and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours}h" if hours != 24 else "daily"
    if minutes:
        return f"{minutes}m"
    return "limit"


def summarize_codex(limits):
    windows = []
    for key in ("primary", "secondary"):
        window = limits.get(key) or {}
        if not isinstance(window, dict) or window.get("usedPercent") is None:
            continue
        remaining = remaining_percent(window.get("usedPercent"))
        if remaining is None:
            continue
        label = window_label(window.get("windowDurationMins"))
        reset = until(window.get("resetsAt"))
        windows.append({"label": label, "remaining_percent": remaining, "reset": reset})
    return windows


def rpc(process, ident, method, params=None, timeout=6):
    process.stdin.write(json.dumps({"id": ident, "method": method, "params": params or {}}) + "\n")
    process.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], 0.2)
        if not ready:
            continue
        line = process.stdout.readline()
        if not line:
            break
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == ident:
            return message
    raise TimeoutError(method)


def collect_codex():
    executable = which("codex")
    if not executable:
        return unavailable("codex", "Codex CLI is not installed")
    try:
        process = subprocess.Popen(
            [executable, "-s", "read-only", "-a", "on-request", "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=environment(), start_new_session=True)
    except OSError:
        return unavailable("codex", "Codex CLI could not be started")
    try:
        rpc(process, 1, "initialize", {"clientInfo": {"name": "fleetlight", "version": "0.3.5"}})
        process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        process.stdin.flush()
        account = ((rpc(process, 2, "account/read", timeout=5).get("result") or {}).get("account") or {})
        limits = ((rpc(process, 3, "account/rateLimits/read", timeout=5).get("result") or {}).get("rateLimits") or {})
    except (OSError, TimeoutError, ValueError, TypeError, KeyError):
        return unavailable("codex", "Sign in with `codex login` to read remaining quota")
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError, AttributeError):
            process.terminate()
        try:
            process.wait(timeout=1)
        except Exception:
            try:
                process.kill()
            except OSError:
                pass
    windows = summarize_codex(limits)
    if not windows:
        return unavailable("codex", "Codex did not report a quota window")
    tightest = min(windows, key=lambda item: item["remaining_percent"])
    parts = [f"{item['remaining_percent']}% {item['label']}" + (f" · {item['reset']}" if item["reset"] else "")
             for item in windows]
    plan = limits.get("planType") or account.get("planType") or ""
    if not isinstance(plan, str):
        plan = ""
    return {"id": "codex", "name": "Codex", "state": "ok", "plan": plan,
            "remaining_percent": tightest["remaining_percent"],
            "detail": " · ".join(parts)}


def cursor_state_db():
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def cursor_token():
    path = cursor_state_db()
    if path.is_file():
        try:
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                row = connection.execute(
                    "SELECT value FROM ItemTable WHERE key = ? LIMIT 1", ("cursorAuth/accessToken",)).fetchone()
            finally:
                connection.close()
            if row and isinstance(row[0], str) and row[0]:
                return row[0]
        except sqlite3.Error:
            pass
    for name in ("cursor-agent", "agent"):
        executable = which(name)
        if not executable:
            continue
        try:
            completed = subprocess.run(
                [executable, "status", "--format", "json"], capture_output=True, text=True,
                timeout=6, env=environment())
        except (OSError, subprocess.TimeoutExpired):
            continue
        try:
            payload = json.loads(completed.stdout)
        except ValueError:
            continue
        auth = payload.get("auth") if isinstance(payload, dict) else None
        token = auth.get("accessToken") if isinstance(auth, dict) else None
        if isinstance(token, str) and token:
            return token
    return None


def money(cents):
    try:
        return f"${float(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return None


def summarize_cursor(payload):
    plan = payload.get("planUsage") if isinstance(payload, dict) else None
    if not isinstance(plan, dict):
        return None
    limit = plan.get("limit")
    included = plan.get("includedSpend")
    if isinstance(limit, (int, float)) and limit > 0 and isinstance(included, (int, float)):
        remaining = max(0, min(100, round(100 * (1 - float(included) / float(limit)))))
        left = money(max(0, limit - included))
        total = money(limit)
        detail = f"{left} of {total} included left" if left and total else f"{remaining}% included remaining"
        return remaining, detail
    used = plan.get("totalPercentUsed")
    remaining = remaining_percent(used)
    if remaining is None:
        return None
    return remaining, f"{remaining}% remaining this period"


def collect_cursor():
    token = cursor_token()
    if not token:
        return unavailable("cursor", "Sign in to Cursor to read remaining quota")
    request = urllib.request.Request(
        CURSOR_USAGE, data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "Connect-Protocol-Version": "1", "User-Agent": "Fleetlight/0.3.5"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return unavailable("cursor", "Cursor usage could not be checked")
    summarized = summarize_cursor(payload)
    if not summarized:
        return unavailable("cursor", "Cursor did not report plan usage")
    remaining, detail = summarized
    return {"id": "cursor", "name": "Cursor", "state": "ok", "remaining_percent": remaining, "detail": detail}


COLLECTORS = {"codex": collect_codex, "cursor": collect_cursor}


def collect(wanted=None):
    names = [name for name in NAMES if name in (wanted or NAMES)]
    result = {}
    if not names:
        return result
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = {name: pool.submit(COLLECTORS[name]) for name in names}
        for name, future in futures.items():
            try:
                result[name] = future.result()
            except Exception:
                result[name] = unavailable(name, "Quota check failed")
    return result


def demo_usage():
    return {
        "codex": {"id": "codex", "name": "Codex", "state": "ok", "plan": "Pro",
                  "remaining_percent": 64, "detail": "64% weekly · 3d 12h"},
        "cursor": {"id": "cursor", "name": "Cursor", "state": "ok",
                   "remaining_percent": 41, "detail": "$8.20 of $20.00 included left"},
    }


if __name__ == "__main__":
    wanted = [name for name in sys.argv[1:] if name in NAMES] or list(NAMES)
    print(json.dumps(collect(wanted), indent=2))
