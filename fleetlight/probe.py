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


def memory_info(system):
    """Physical memory and swap in bytes, plus the share of memory in use."""
    result = {"memory_percent": None, "memory_total": None, "memory_used": None,
              "swap_total": None, "swap_used": None}
    if system == "Linux":
        values = dict(re.findall(r"^(\w+):\s+(\d+)", text("/proc/meminfo"), re.M))
        total = int(values.get("MemTotal", 0)) * 1024
        available = int(values.get("MemAvailable", 0)) * 1024
        if total:
            result.update(memory_percent=round(100 * (total - available) / total),
                          memory_total=total, memory_used=total - available)
        swap = int(values.get("SwapTotal", 0)) * 1024
        result.update(swap_total=swap, swap_used=max(0, swap - int(values.get("SwapFree", 0)) * 1024))
        return result
    raw = command(["vm_stat"])
    page = re.search(r"page size of (\d+) bytes", raw)
    available = sum(int(v) for v in re.findall(r"Pages (?:free|inactive|speculative):\s+(\d+)", raw))
    total = command(["sysctl", "-n", "hw.memsize"])
    if page and total.isdigit():
        percent = max(0, min(100, round(100 * (1 - available * int(page[1]) / int(total)))))
        result.update(memory_percent=percent, memory_total=int(total),
                      memory_used=max(0, int(total) - available * int(page[1])))
    swap = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", command(["sysctl", "-n", "vm.swapusage"]))
    if swap:
        result.update(swap_total=int(float(swap[1]) * 1024**2), swap_used=int(float(swap[2]) * 1024**2))
    return result


def memory(system):
    return memory_info(system)["memory_percent"]


_cpu_last = None


def cpu_times():
    """Total and idle jiffies from the first line of /proc/stat."""
    try:
        values = [int(v) for v in text("/proc/stat").split("\n", 1)[0].split()[1:9]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def cpu_usage(before, after):
    if not before or not after or after[0] <= before[0]:
        return None
    total = after[0] - before[0]
    return max(0, min(100, round(100 * (total - (after[1] - before[1])) / total)))


def cpu_percent(system, cpus=None):
    """CPU in use since the previous call on Linux; the first call has nothing to compare with."""
    global _cpu_last
    if system == "Linux":
        current = cpu_times()
        previous, _cpu_last = _cpu_last, current
        return cpu_usage(previous, current)
    if system == "Darwin":
        try:
            used = sum(float(v) for v in command(["ps", "-A", "-o", "%cpu="]).split())
        except ValueError:
            return None
        return max(0, min(100, round(used / (cpus or os.cpu_count() or 1))))
    return None


_battery_last = {"at": 0.0, "value": None}


def battery_cached(system):
    """Battery charge at most every 30 seconds: reading it asks the embedded controller, which is slow."""
    now = time.time()
    if now - _battery_last["at"] >= 30:
        _battery_last.update(at=now, value=battery(system))
    return _battery_last["value"]


def battery(system, root="/sys"):
    """Charge of the first battery, or None for computers without one."""
    if system == "Linux":
        for supply in sorted(Path(root, "class/power_supply").glob("BAT*")):
            try:
                percent = int(text(supply / "capacity").strip())
            except ValueError:
                continue
            return {"percent": max(0, min(100, percent)),
                    "state": text(supply / "status").strip().lower() or "unknown"}
        return None
    if system == "Darwin":
        match = re.search(r"(\d+)%;\s*([A-Za-z ]+);", command(["pmset", "-g", "batt"]))
        if match:
            return {"percent": max(0, min(100, int(match[1]))), "state": match[2].strip().lower()}
    return None


REAL_FILESYSTEMS = ("ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "zfs", "bcachefs", "jfs",
                    "ntfs", "ntfs3", "fuseblk", "exfat", "vfat")
SKIPPED_MOUNTS = ("/boot", "/efi", "/snap", "/var/snap", "/var/lib/docker", "/var/lib/containers", "/nix")


def disks(system, mounts=None):
    """Mounted local filesystems of at least 1 GiB, one per device, root first."""
    candidates, seen = [], set()
    if system == "Linux":
        for line in (text("/proc/mounts") if mounts is None else mounts).splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[2] not in REAL_FILESYSTEMS or parts[0] in seen:
                continue
            mount = parts[1].replace("\\040", " ")
            if mount != "/" and (mount + "/").startswith(tuple(prefix + "/" for prefix in SKIPPED_MOUNTS)):
                continue
            seen.add(parts[0])
            candidates.append(mount)
    elif system == "Darwin":
        candidates.append("/")
        for line in command(["df", "-kP"]).splitlines()[1:]:
            parts = line.split(None, 5)
            if (len(parts) == 6 and parts[0].startswith("/dev/") and parts[5].startswith("/Volumes/")
                    and parts[5][9:] not in ("Recovery", "Preboot", "VM", "Update")):
                candidates.append(parts[5])
    result = []
    for mount in sorted(candidates, key=lambda item: item != "/")[:8]:
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        if usage.total >= 1024**3:
            result.append({"mount": mount, "total": usage.total, "free": usage.free,
                           "percent": round(100 * usage.used / usage.total)})
    return result


def process_times():
    """Name, CPU ticks and resident pages of every process, read from /proc without subprocesses."""
    result = {}
    for path in glob.glob("/proc/[0-9]*/stat"):
        try:
            with open(path) as stream:
                raw = stream.read()
            end = raw.rindex(")")
            fields = raw[end + 2:].split()
            result[path.split("/")[2]] = (raw[raw.index("(") + 1:end], int(fields[11]) + int(fields[12]), int(fields[21]))
        except (OSError, ValueError, IndexError):
            continue
    return result


def top_processes(system, before=None, seconds=0, limit=5):
    """Busiest and largest programs, grouped by name. CPU is a share of one core."""
    cpu, resident = {}, {}
    if system == "Linux":
        ticks = os.sysconf("SC_CLK_TCK")
        page = os.sysconf("SC_PAGE_SIZE")
        own = str(os.getpid())
        for pid, (name, used, pages) in process_times().items():
            if pid == own:
                continue
            entry = resident.setdefault(name, [0, 0])
            entry[0] += pages * page
            entry[1] += 1
            old = (before or {}).get(pid)
            if old and old[0] == name and used > old[1] and seconds > 0:
                cpu[name] = cpu.get(name, 0) + 100 * (used - old[1]) / ticks / seconds
    elif system == "Darwin":
        own = str(os.getpid())
        for line in command(["ps", "-A", "-o", "pid=", "-o", "pcpu=", "-o", "rss=", "-o", "comm="]).splitlines():
            parts = line.split(None, 3)
            try:
                share, size = float(parts[1]), int(parts[2]) * 1024
            except (ValueError, IndexError):
                continue
            if parts[0] == own:
                continue
            # Some programs rewrite their title to include a user or host name; keep the program only.
            name = os.path.basename(parts[3].strip()).split(":")[0][:40] if len(parts) > 3 else "?"
            entry = resident.setdefault(name, [0, 0])
            entry[0] += size
            entry[1] += 1
            cpu[name] = cpu.get(name, 0) + share
    else:
        return None
    busiest = sorted(((name, round(value)) for name, value in cpu.items() if value >= 4), key=lambda item: -item[1])
    largest = sorted(((name, size, count) for name, (size, count) in resident.items() if size), key=lambda item: -item[1])
    return {"cpu": [list(item) for item in busiest[:limit]], "memory": [list(item) for item in largest[:limit]]}


def failed_units(system):
    """Names of failed system units; None where systemd is not available."""
    if system != "Linux" or not shutil.which("systemctl"):
        return None
    raw = command(["systemctl", "list-units", "--state=failed", "--no-legend", "--plain", "--no-pager"])
    return [line.split()[0] for line in raw.splitlines() if line.split()][:10]


def broadcast_address(name):
    try:
        import fcntl
        import socket
        import struct
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as handle:
            raw = fcntl.ioctl(handle.fileno(), 0x8919, struct.pack("256s", name.encode()[:15]))
        return socket.inet_ntoa(raw[20:24])
    except (OSError, ImportError, ValueError):
        return None


def network(system):
    """Default-route interface with its hardware address, which Wake-on-LAN needs once the computer is off."""
    hardware = r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}"
    if system == "Linux":
        routes = []
        for line in text("/proc/net/route").splitlines()[1:]:
            parts = line.split()
            if len(parts) > 6 and parts[1] == "00000000" and re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", parts[0]):
                routes.append((int(parts[6]) if parts[6].isdigit() else 0, parts[0]))
        for _, name in sorted(routes):
            address = text("/sys/class/net/" + name + "/address").strip().lower()
            if re.fullmatch(hardware, address) and address != "00:00:00:00:00:00":
                return {"interface": name, "mac": address, "broadcast": broadcast_address(name),
                        "wireless": os.path.isdir("/sys/class/net/" + name + "/wireless")}
        return None
    if system == "Darwin":
        match = re.search(r"interface:\s*(\w+)", command(["/sbin/route", "-n", "get", "default"]))
        names = ([match[1]] if match else []) + ["en0", "en1"]
        for name in names:
            # A VPN can own the default route; fall back to the built-in adapters, which do have an address.
            raw = command(["/sbin/ifconfig", name])
            address = re.search(r"ether (" + hardware + ")", raw)
            broadcast = re.search(r"broadcast (\d+\.\d+\.\d+\.\d+)", raw)
            if address and broadcast:
                return {"interface": name, "mac": address[1], "broadcast": broadcast[1], "wireless": None}
        return None
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
    try:
        with open(executable, "rb") as stream:
            head = stream.read(500)
    except OSError:
        head = b""
    # Only text launcher scripts count: the native binary itself contains "mise ".
    launcher = head.decode("utf-8", errors="replace") if head.startswith(b"#!") else ""
    if "/mise/" in paths or "mise " in launcher:
        method = "mise"
    elif "/.local/share/claude/" in paths or "/.local/bin/claude" in paths:
        method = "native"
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
            "disk_percent": round(100 * disk.used / disk.total), "disk_free": disk.free, "disk_total": disk.total,
            **memory_info(system), "load": round(os.getloadavg()[0], 2),
            "cpu_percent": cpu_percent(system), "cpu_temperature": cpu_temperature(system),
            "battery": battery_cached(system)}


def collect(services=()):
    system = platform.system()
    # Measure CPU over a short quiet window before the slower version lookups run.
    window = 0.5
    before = process_times() if system == "Linux" else None
    cpu_percent(system)
    if system == "Linux":
        time.sleep(window)
    metrics = collect_metrics(system)
    processes = top_processes(system, before, window)
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
            "disks": disks(system), "processes": processes, "failed_units": failed_units(system),
            "network": network(system),
            "codex": cli, "claude": claude, "chatgpt": desktop_app(system)}


if __name__ == "__main__":
    request = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    result = collect(request.get("services", []))
    result["nonce"] = request.get("nonce")
    print("FLEETLIGHT_V1=" + json.dumps(result, separators=(",", ":")))
