"""Read-only collector, also sent to remote Python 3 interpreters over SSH.

Keep this module self-contained and compatible with Python 3.9 hosts.
"""
import glob
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time


def command(args, timeout=4):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C"})
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def text(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def memory(system):
    if system == "Linux":
        values = dict(re.findall(r"^(\w+):\s+(\d+)", text("/proc/meminfo"), re.M))
        total = int(values.get("MemTotal", 0))
        available = int(values.get("MemAvailable", 0))
        return round(100 * (total - available) / total) if total else None
    raw = command(["vm_stat"])
    page = re.search(r"page size of (\d+) bytes", raw)
    available = sum(int(v) for v in re.findall(r"Pages (?:free|inactive|speculative):\s+(\d+)", raw))
    total = command(["sysctl", "-n", "hw.memsize"])
    if page and total.isdigit():
        return max(0, min(100, round(100 * (1 - available * int(page[1]) / int(total)))))
    return None


def codex_executable():
    shell = os.environ.get("SHELL", "/bin/sh")
    found = command([shell, "-ic", "command -v codex"], timeout=3).splitlines()
    if found and os.path.isfile(found[-1]) and os.access(found[-1], os.X_OK):
        return found[-1]
    return shutil.which("codex")


def codex_version():
    paths = [str(Path.home() / suffix) for suffix in (
        ".local/bin", ".local/share/mise/shims", ".npm-global/bin")]
    paths += sorted(glob.glob(str(Path.home() / ".nvm/versions/node/*/bin")), reverse=True)
    paths += ["/opt/homebrew/bin", "/usr/local/bin"]
    os.environ["PATH"] = os.pathsep.join(paths + [os.environ.get("PATH", "")])
    executable = codex_executable()
    if executable:
        result = command([executable, "--version"], timeout=5)
        match = re.search(r"codex(?:-cli)?\s+(\d+\.\d+\.\d+[^\s]*)", result)
        if match:
            return match[1]
    return None


def claude_executable():
    shell = os.environ.get("SHELL", "/bin/sh")
    found = command([shell, "-lc", "command -v claude"], timeout=3).splitlines()
    if found and os.path.isfile(found[-1]) and os.access(found[-1], os.X_OK):
        return found[-1]
    return shutil.which("claude")


def claude_version():
    executable = claude_executable()
    if not executable:
        return None
    match = re.search(r"(\d+\.\d+\.\d+)", command([executable, "--version"], timeout=5))
    return match[1] if match else None


def claude_installation():
    executable = claude_executable()
    if not executable:
        return {}
    resolved = os.path.realpath(executable)
    paths = executable + ":" + resolved
    method = "unknown"
    if "/.local/share/claude/" in paths or paths.endswith("/.local/bin/claude") or "/.local/bin/claude" in paths:
        method = "native"
    elif "/mise/" in paths:
        method = "mise"
    elif "/node_modules/@anthropic-ai/claude-code/" in paths:
        method = "npm"
    return {"method": method}


def codex_installation():
    executable = codex_executable()
    if not executable:
        return {}
    resolved = os.path.realpath(executable)
    paths = executable + ":" + resolved
    method = "unknown"
    if "/.codex/packages/standalone/" in paths:
        method = "standalone"
    elif "/mise/shims/" in paths or "/mise/installs/codex/" in paths:
        method = "mise"
    elif "/node_modules/@openai/codex/" in paths:
        method = "npm"
    return {"method": method}


def desktop_app(system):
    result = {"version": None, "provider": None, "candidate": None, "status": "not detected"}
    if system == "Darwin":
        path = Path("/Applications/ChatGPT.app/Contents/Info.plist")
        try:
            with path.open("rb") as stream:
                data = plistlib.load(stream)
            if data.get("CFBundleIdentifier") == "com.openai.codex":
                result.update(version=data.get("CFBundleShortVersionString"), build=data.get("CFBundleVersion"),
                              writable=os.access("/Applications", os.W_OK), provider="macOS app", status="installed")
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
        return result
    if shutil.which("pacman"):
        installed = command(["pacman", "-Q", "openai-codex-desktop"]).split()
        if len(installed) == 2:
            version = installed[1].split(":")[-1].rsplit("-", 1)[0]
            try:
                data = json.loads(text("/usr/lib/chatgpt/resources/linux-package-metadata.json"))
            except ValueError:
                data = {}
            if data.get("version") == version and data.get("codexAppBrand") == "chatgpt":
                candidate = command(["pacman", "-Si", "openai-codex-desktop"])
                match = re.search(r"^Version\s*:\s*(\S+)", candidate, re.M)
                target = match[1] if match else None
                comparison = command(["vercmp", installed[1], target]) if target else ""
                status = "update available" if comparison and int(comparison) < 0 else "installed"
                if target and comparison and int(comparison) >= 0:
                    status = "current in cached repository"
                result.update(version=version, provider="pacman", candidate=target, status=status)
    elif shutil.which("dpkg-query"):
        installed = command(["dpkg-query", "-W", "-f=${Status}\t${Version}", "chatgpt"])
        if installed.startswith("install ok installed\t"):
            version = installed.split("\t")[-1]
            match = re.search(r"Candidate:\s*(\S+)", command(["apt-cache", "policy", "chatgpt"]))
            target = match[1] if match and match[1] != "(none)" else None
            newer = False
            if target:
                try:
                    newer = subprocess.run(["dpkg", "--compare-versions", version, "lt", target], timeout=2).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    pass
            result.update(version=version, provider="APT", candidate=target,
                          status="update available" if newer else "installed")
    return result


def cpu_temperature(system, root="/sys"):
    """Hottest readable CPU sensor in Celsius; never substitute GPU/battery heat."""
    if system != "Linux":
        return None
    values = []
    def read_value(path):
        try:
            value = int(text(path).strip()) / 1000
            if -20 <= value <= 150:
                values.append(value)
        except ValueError:
            pass
    for device in Path(root, "class/hwmon").glob("hwmon*"):
        driver = text(device / "name").strip()
        if driver not in ("coretemp", "k10temp", "k8temp", "zenpower"):
            continue
        inputs = list(device.glob("temp*_input"))
        # AMD Tctl may include a control offset; prefer physical die readings.
        physical = [p for p in inputs if text(p.with_name(p.name.replace("_input", "_label"))).strip().startswith(("Tdie", "Tccd"))]
        for path in physical or inputs:
            read_value(path)
    if not values:
        for zone in Path(root, "class/thermal").glob("thermal_zone*"):
            if text(zone / "type").strip().lower() in ("x86_pkg_temp", "cpu-thermal", "cpu_thermal"):
                read_value(zone / "temp")
    return round(max(values), 1) if values else None


def collect_metrics(system=None):
    """Cheap live values; Linux uses kernel files and statvfs, without subprocesses."""
    system = system or platform.system()
    disk = shutil.disk_usage("/")
    uptime = None
    if system == "Linux":
        try:
            uptime = int(float(text("/proc/uptime").split()[0]))
        except (ValueError, IndexError):
            pass
    elif system == "Darwin":
        match = re.search(r"sec = (\d+)", command(["sysctl", "-n", "kern.boottime"]))
        if match:
            uptime = max(0, int(time.time()) - int(match[1]))
    return {"metrics_checked_at": time.time(), "uptime": uptime,
            "disk_percent": round(100 * disk.used / disk.total), "disk_free": disk.free,
            "memory_percent": memory(system), "load": round(os.getloadavg()[0], 2),
            "cpu_temperature": cpu_temperature(system)}


def collect(services=()):
    system = platform.system()
    metrics = collect_metrics(system)
    service_states = {}
    for service in services:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,100}", service):
            continue
        if system == "Linux" and shutil.which("systemctl"):
            raw = command(["systemctl", "show", service, "--property=LoadState,ActiveState", "--no-pager"])
            properties = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
            service_states[service] = ("not installed" if properties.get("LoadState") == "not-found"
                                       else properties.get("ActiveState", "unknown"))
        else:
            service_states[service] = "unsupported"
    distro = platform.mac_ver()[0] if system == "Darwin" else ""
    for line in text("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            distro = line.split("=", 1)[1].strip('"')
    manager = ("omarchy" if shutil.which("omarchy-update") and shutil.which("pacman")
               else next((name for name in ("pacman", "apt", "dnf") if shutil.which(name)), None))
    cli = codex_version()
    claude = claude_version()
    return {"schema": 1, "hostname": platform.node(), "os": system, "distribution": distro,
            "architecture": platform.machine(), "codex_installation": codex_installation(),
            "claude_installation": claude_installation(),
            "package_manager": manager,
            "boot_id": text("/proc/sys/kernel/random/boot_id").strip() if system == "Linux" else None,
            "kernel": platform.release(), "checked_at": time.time(), **metrics,
            "cpus": os.cpu_count() or 1, "services": service_states,
            "codex": cli, "claude": claude, "chatgpt": desktop_app(system)}


if __name__ == "__main__":
    request = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    result = collect(request.get("services", []))
    result["nonce"] = request.get("nonce")
    print("FLEETLIGHT_V1=" + json.dumps(result, separators=(",", ":")))
