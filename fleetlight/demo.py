"""Fictional screenshot data. Never includes personal infrastructure."""
import time


def demo_data():
    hosts = [{"id": "local", "name": "This Computer", "local": True, "services": ["tailscaled", "docker"]},
             {"id": "studio", "name": "Studio Mac", "alias": "studio", "services": []},
             {"id": "server", "name": "Home Server", "alias": "server", "services": ["tailscaled"]},
             {"id": "lab", "name": "Lab Workstation", "alias": "lab", "services": []}]
    snapshots = {}
    for index, host in enumerate(hosts):
        snapshots[host["id"]] = {"id": host["id"], "status": "online", "os": "Linux" if index != 1 else "Darwin",
            "distribution": "Arch Linux" if index != 1 else "macOS", "checked_at": time.time(),
            "disk_percent": 34 + index * 9, "disk_free": 128 * 1024**3, "memory_percent": 26 + index * 7,
            "uptime": 182340, "load": 1.24, "cpus": 8, "codex": "1.0.0", "check_ms": 342,
            "chatgpt": {"version": "1.0.0", "provider": "pacman" if index != 1 else "macOS app", "status": "installed"},
            "services": {s: "active" for s in host["services"]}, "package_manager": "pacman" if index != 1 else None}
    return {"version": 1, "refresh_seconds": 60, "hosts": hosts}, snapshots
