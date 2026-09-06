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


def collect(services=()):
    system = platform.system()
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
    manager = next((name for name in ("pacman", "apt", "dnf") if shutil.which(name)), None)
    cli = codex_version()
    return {"schema": 1, "hostname": platform.node(), "os": system, "distribution": distro,
            "architecture": platform.machine(), "codex_installation": codex_installation(),
            "package_manager": manager,
            "boot_id": text("/proc/sys/kernel/random/boot_id").strip() if system == "Linux" else None,
            "kernel": platform.release(), "checked_at": time.time(), "uptime": uptime,
            "disk_percent": round(100 * disk.used / disk.total), "disk_free": disk.free,
            "memory_percent": memory(system), "load": round(os.getloadavg()[0], 2),
            "cpus": os.cpu_count() or 1, "services": service_states,
            "codex": cli, "chatgpt": desktop_app(system)}


if __name__ == "__main__":
    request = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    result = collect(request.get("services", []))
    result["nonce"] = request.get("nonce")
    print("FLEETLIGHT_V1=" + json.dumps(result, separators=(",", ":")))
