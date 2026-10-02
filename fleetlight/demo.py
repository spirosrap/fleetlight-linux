"""Fictional screenshot data. Never includes personal infrastructure."""
import time

GIB = 1024**3


def demo_data():
    hosts = [{"id": "local", "name": "This Computer", "local": True, "services": ["tailscaled", "docker"]},
             {"id": "studio", "name": "Studio Mac", "alias": "studio", "services": []},
             {"id": "server", "name": "Home Server", "alias": "server", "services": ["tailscaled"]},
             {"id": "lab", "name": "Lab Workstation", "alias": "lab", "services": []}]
    details = {
        "local": {"hostname": "laptop", "cpu_percent": 18, "cpus": 8, "memory_total": 16 * GIB, "cpu_temperature": 47.0,
                  "battery": {"percent": 86, "state": "discharging"}, "disk_total": 512 * GIB,
                  "processes": {"cpu": [["firefox", 34], ["gnome-shell", 9], ["code", 6]],
                                "memory": [["firefox", int(2.4 * GIB), 14], ["code", int(1.3 * GIB), 9],
                                           ["gnome-shell", int(0.6 * GIB), 1], ["dockerd", int(0.3 * GIB), 1]]}},
        "studio": {"hostname": "studio", "cpu_percent": 9, "cpus": 10, "memory_total": 32 * GIB, "disk_total": 1000 * GIB,
                   "processes": {"cpu": [["Xcode", 22], ["WindowServer", 7]],
                                 "memory": [["Xcode", int(5.1 * GIB), 3], ["Safari", int(2.2 * GIB), 11]]}},
        "server": {"hostname": "server", "cpu_percent": 31, "cpus": 4, "memory_total": 8 * GIB, "cpu_temperature": 52.5,
                   "disk_total": 256 * GIB,
                   "extra_disks": [{"mount": "/srv/media", "total": 4000 * GIB, "free": 1480 * GIB, "percent": 63}],
                   "processes": {"cpu": [["jellyfin", 61], ["postgres", 12]],
                                 "memory": [["jellyfin", int(1.4 * GIB), 1], ["postgres", int(0.9 * GIB), 7]]}},
        "lab": {"hostname": "lab", "cpu_percent": 64, "cpus": 16, "memory_total": 64 * GIB, "cpu_temperature": 68.0,
                "disk_total": 2000 * GIB,
                "processes": {"cpu": [["python3", 780], ["nvidia-smi", 3]],
                              "memory": [["python3", int(21.5 * GIB), 4], ["jupyter-lab", int(0.8 * GIB), 1]]}},
    }
    snapshots = {}
    for index, host in enumerate(hosts):
        extra = dict(details[host["id"]])
        mac = index == 1
        disk_percent, memory_percent = 34 + index * 9, 26 + index * 7
        total, memory_total = extra["disk_total"], extra["memory_total"]
        free = round(total * (100 - disk_percent) / 100)
        snapshots[host["id"]] = {
            "id": host["id"], "status": "online", "os": "Darwin" if mac else "Linux",
            "distribution": "macOS" if mac else "Arch Linux", "checked_at": time.time(),
            "kernel": "25.0.0" if mac else "6.16.4-arch1-1", "architecture": "arm64" if mac else "x86_64",
            "disk_percent": disk_percent, "disk_free": free, "memory_percent": memory_percent,
            "memory_used": round(memory_total * memory_percent / 100), "swap_total": 0, "swap_used": 0,
            "uptime": 182340 + index * 90000, "load": round(extra["cpu_percent"] * extra["cpus"] / 100, 2),
            "codex": "1.0.0", "check_ms": 342 + index * 120, "failed_units": None if mac else [],
            "disks": [{"mount": "/", "total": total, "free": free, "percent": disk_percent}] + extra.pop("extra_disks", []),
            "network": {"interface": "en0" if mac else "enp3s0", "mac": "02:00:00:00:00:0" + str(index + 1),
                        "broadcast": "203.0.113.255", "wireless": False},
            "chatgpt": {"version": "1.0.0", "provider": "macOS app" if mac else "pacman", "status": "installed"},
            "services": {s: "active" for s in host["services"]}, "package_manager": None if mac else "pacman",
            **extra}
    return {"version": 1, "refresh_seconds": 60, "hosts": hosts}, snapshots
