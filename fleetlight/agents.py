"""Local Codex, Cursor and Claude remaining-quota checks. Never persist session tokens."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from . import __version__


NAMES = ("codex", "cursor", "claude")
CURSOR_USAGE = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
CURSOR_PLAN = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetPlanInfo"
CLAUDE_USAGE = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_WINDOWS = (("five_hour", "5h"), ("seven_day", "weekly"),
                  ("seven_day_opus", "weekly Opus"), ("seven_day_sonnet", "weekly Sonnet"))


def remaining_percent(used):
    try:
        return max(0, min(100, round(100 - float(used))))
    except (TypeError, ValueError):
        return None


def until(timestamp):
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return ""
    if value > 10_000_000_000:
        value /= 1000
    seconds = max(0, int(value - time.time()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def reset_day(timestamp):
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return ""
    if value > 10_000_000_000:
        value /= 1000
    if value <= 0:
        return ""
    return time.strftime("%a %d %b %H:%M", time.localtime(value))


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
        day = reset_day(window.get("resetsAt"))
        windows.append({"label": label, "remaining_percent": remaining, "reset": reset, "reset_day": day})
    return windows


def format_codex_window(item):
    text = f"{item['remaining_percent']}% {item['label']}"
    if item.get("reset"):
        text += f" · {item['reset']}"
    if item.get("reset_day"):
        text += f" · {item['reset_day']}"
    return text


def rpc(process, ident, method, params=None, timeout=12):
    process.stdin.write((json.dumps({"id": ident, "method": method, "params": params or {}}) + "\n").encode())
    process.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = read_line(process, deadline)
        if line is None:
            break
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") != ident:
            continue
        if message.get("error"):
            raise ConnectionError(method)
        return message
    if process.poll() is not None:
        raise ConnectionError(method)
    raise TimeoutError(method)


def read_line(process, deadline):
    remaining = max(0, deadline - time.time())
    try:
        line = process._lines.get(timeout=remaining)
    except queue.Empty:
        return None
    return line


def read_codex_output(process):
    try:
        for raw in iter(process.stdout.readline, b""):
            process._lines.put(raw.decode(errors="replace").rstrip("\r\n"))
    finally:
        process._lines.put(None)


_codex_lock = threading.Lock()
_codex_process = None
_codex_request = 0


def stop_codex(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError, AttributeError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except Exception:
        try:
            process.kill()
        except OSError:
            pass


def start_codex():
    executable = which("codex")
    if not executable:
        raise FileNotFoundError("codex")
    process = subprocess.Popen(
        [executable, "-s", "read-only", "-a", "on-request", "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=environment(), start_new_session=True)
    process._lines = queue.Queue()
    threading.Thread(target=read_codex_output, args=(process,), daemon=True).start()
    rpc(process, 1, "initialize", {"clientInfo": {"name": "fleetlight", "version": __version__}}, timeout=15)
    process.stdin.write((json.dumps({"method": "initialized", "params": {}}) + "\n").encode())
    process.stdin.flush()
    return process


def codex_session():
    global _codex_process
    if _codex_process is not None and _codex_process.poll() is None:
        return _codex_process
    stop_codex(_codex_process)
    _codex_process = start_codex()
    return _codex_process


def reset_codex():
    global _codex_process
    stop_codex(_codex_process)
    _codex_process = None


def collect_codex():
    if not which("codex"):
        return unavailable("codex", "Codex CLI is not installed")
    with _codex_lock:
        for attempt in (1, 2):
            try:
                process = codex_session()
                global _codex_request
                _codex_request += 1
                account = ((rpc(process, _codex_request, "account/read", timeout=8).get("result") or {}).get("account") or {})
                _codex_request += 1
                limits = ((rpc(process, _codex_request, "account/rateLimits/read", timeout=8).get("result") or {}).get("rateLimits") or {})
                break
            except FileNotFoundError:
                return unavailable("codex", "Codex CLI is not installed")
            except (OSError, ConnectionError, TimeoutError, ValueError, TypeError, KeyError):
                reset_codex()
                if attempt == 2:
                    return unavailable("codex", "Codex disconnected before reporting quota")
        else:
            return unavailable("codex", "Codex disconnected before reporting quota")
    windows = summarize_codex(limits)
    if not windows:
        return unavailable("codex", "Codex did not report a quota window")
    tightest = min(windows, key=lambda item: item["remaining_percent"])
    parts = [format_codex_window(item) for item in windows]
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


def summarize_cursor(payload):
    plan = payload.get("planUsage") if isinstance(payload, dict) else None
    if not isinstance(plan, dict):
        return None
    used = plan.get("totalPercentUsed")
    if used is None:
        used = plan.get("autoPercentUsed")
    remaining = remaining_percent(used)
    if remaining is None:
        return None
    reset = until(payload.get("billingCycleEnd"))
    detail = f"{remaining}% remaining this period" + (f" · {reset}" if reset else "")
    return remaining, detail


def cursor_plan_name(payload):
    info = payload.get("planInfo") if isinstance(payload, dict) else None
    name = info.get("planName") if isinstance(info, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def cursor_request(url, token):
    request = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "Connect-Protocol-Version": "1", "User-Agent": "Fleetlight/" + __version__})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def cursor_plan(token):
    try:
        return cursor_plan_name(cursor_request(CURSOR_PLAN, token))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def collect_cursor():
    token = cursor_token()
    if not token:
        return unavailable("cursor", "Sign in to Cursor to read remaining quota")
    try:
        payload = cursor_request(CURSOR_USAGE, token)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return unavailable("cursor", "Cursor usage could not be checked")
    summarized = summarize_cursor(payload)
    if not summarized:
        return unavailable("cursor", "Cursor did not report plan usage")
    remaining, detail = summarized
    result = {"id": "cursor", "name": "Cursor", "state": "ok", "remaining_percent": remaining, "detail": detail}
    plan = cursor_plan(token)
    if plan:
        result["plan"] = plan
    return result


def claude_credentials_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / ".credentials.json"


def claude_credentials():
    try:
        payload = json.loads(claude_credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str) or not oauth["accessToken"]:
        return None
    return oauth


def iso_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def summarize_claude(payload):
    windows = []
    if not isinstance(payload, dict):
        return windows
    for key, label in CLAUDE_WINDOWS:
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        remaining = remaining_percent(window.get("utilization"))
        if remaining is None:
            continue
        reset = iso_timestamp(window.get("resets_at"))
        windows.append({"label": label, "remaining_percent": remaining,
                        "reset": until(reset) if reset else "", "reset_day": reset_day(reset) if reset else ""})
    return windows


def collect_claude():
    credentials = claude_credentials()
    if not credentials:
        return unavailable("claude", "Sign in to Claude Code to read remaining quota")
    expires = credentials.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time():
        return unavailable("claude", "Open Claude Code to refresh its sign-in")
    request = urllib.request.Request(
        CLAUDE_USAGE, method="GET",
        headers={"Authorization": "Bearer " + credentials["accessToken"], "anthropic-beta": "oauth-2025-04-20",
                 "User-Agent": "Fleetlight/" + __version__})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return unavailable("claude", "Open Claude Code to refresh its sign-in")
        return unavailable("claude", "Claude usage could not be checked")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return unavailable("claude", "Claude usage could not be checked")
    windows = summarize_claude(payload)
    if not windows:
        return unavailable("claude", "Claude did not report a quota window")
    tightest = min(windows, key=lambda item: item["remaining_percent"])
    plan = credentials.get("subscriptionType")
    return {"id": "claude", "name": "Claude", "state": "ok", "plan": plan if isinstance(plan, str) else "",
            "remaining_percent": tightest["remaining_percent"],
            "detail": " · ".join(format_codex_window(item) for item in windows)}


COLLECTORS = {"codex": collect_codex, "cursor": collect_cursor, "claude": collect_claude}


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
                  "remaining_percent": 64, "detail": "64% weekly · 3d 12h · Tue 22 Sep 15:00"},
        "cursor": {"id": "cursor", "name": "Cursor", "state": "ok", "plan": "Pro",
                   "remaining_percent": 41, "detail": "41% remaining this period · 12d 4h"},
        "claude": {"id": "claude", "name": "Claude", "state": "ok", "plan": "Pro",
                   "remaining_percent": 72, "detail": "72% 5h · 2h 10m · Tue 22 Sep 17:00 · 90% weekly · 5d 1h · Sun 27 Sep 09:00"},
    }


if __name__ == "__main__":
    wanted = [name for name in sys.argv[1:] if name in NAMES] or list(NAMES)
    print(json.dumps(collect(wanted), indent=2))
