"""Interactive actions are explicit, fixed commands; never shell-interpolate host data."""
import ipaddress
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess

from .config import validate


UPDATE_COMMANDS = {"omarchy": "omarchy update",
                   "pacman": "sudo env OMARCHY_ALLOW_DIRECT_PACMAN=1 pacman -Syu",
                   "apt": "sudo apt update && sudo apt upgrade",
                   "dnf": "sudo dnf upgrade"}


def terminal_command(host, manager=None):
    validate({"version": 1, "hosts": [host]})
    if manager and manager not in UPDATE_COMMANDS:
        raise ValueError("Package manager is unsupported")
    if host.get("local"):
        if manager:
            return ["sh", "-c", UPDATE_COMMANDS[manager] + '; printf "\\nPress Enter to close…"; read answer']
        return [os.environ.get("SHELL", "/bin/sh")]
    argv = ["ssh", "-t", "-o", "StrictHostKeyChecking=yes", "--", host["alias"]]
    if manager:
        argv.append(UPDATE_COMMANDS[manager] + '; printf "\\nPress Enter to close…"; read answer')
    return argv


def open_terminal(host, manager=None):
    command = terminal_command(host, manager)
    for name, prefix in (("xdg-terminal-exec", []), ("foot", []), ("kitty", []),
                         ("alacritty", ["-e"]), ("gnome-terminal", ["--"]),
                         ("konsole", ["-e"]), ("xterm", ["-e"])):
        executable = shutil.which(name)
        if executable:
            subprocess.Popen([executable] + prefix + command, start_new_session=True)
            return
    raise RuntimeError("Install a terminal such as foot, kitty or GNOME Terminal")


def autostart_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "io.github.fleetlight.Linux.desktop"


def set_autostart(enabled):
    path = autostart_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    executable = Path.home() / ".local/bin/fleetlight"
    if not executable.is_file():
        raise RuntimeError("Run scripts/install.sh before enabling start at login")
    # The installer uses a fixed user-local command; do not consume arbitrary desktop files.
    path.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(executable).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    path.write_text('[Desktop Entry]\nType=Application\nName=Fleetlight\nExec="' + escaped + '" --background\nIcon=io.github.fleetlight.Linux\nTerminal=false\n')


def magic_packet(mac):
    if not isinstance(mac, str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        raise ValueError("No hardware address is known for this computer")
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def wake(mac, broadcast=None):
    """Send a Wake-on-LAN packet on the local network. Nothing is sent beyond broadcast addresses."""
    packet = magic_packet(mac)
    targets = ["255.255.255.255"]
    try:
        if broadcast and ipaddress.ip_address(broadcast).version == 4 and broadcast not in targets:
            targets.append(broadcast)
    except ValueError:
        pass
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as handle:
        handle.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            for port in (9, 7):
                try:
                    handle.sendto(packet, (target, port))
                    sent += 1
                except OSError:
                    continue
    if not sent:
        raise OSError("The wake signal could not be sent from this network")
    return targets
