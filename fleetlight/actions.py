"""Interactive actions are explicit, fixed commands; never shell-interpolate host data."""
import ipaddress
import os
from pathlib import Path
import re
import shlex
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


def service_restart_script(system, spec):
    """Fixed shell script that restarts one configured service and prints its state afterwards.

    Ports and web addresses have no unit to restart. System units need passwordless sudo for systemctl.
    """
    from .probe import valid_service
    if not valid_service(spec) or spec.startswith(("port:", "http://", "https://")):
        raise ValueError("Only units and launchd jobs can be restarted")
    user = spec.startswith("user:")
    name = shlex.quote(spec[5:] if user else spec)
    if system == "Darwin":
        if user:
            raise ValueError("user: units are systemd units; Macs use launchd labels")
        return ('launchctl kickstart -k "gui/$(id -u)/"' + name + ' 2>&1 || exit 3; sleep 3; '
                'launchctl list ' + name + ' | grep -q \'"PID"\' && echo STATE:active || echo STATE:inactive')
    if system != "Linux":
        raise ValueError("Services can only be restarted on Linux and macOS")
    if user:
        tool = 'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"; systemctl --user'
        return (tool + ' reset-failed ' + name + ' 2>/dev/null; systemctl --user restart ' + name + ' 2>&1 || exit 3; '
                'sleep 3; echo "STATE:$(systemctl --user is-active ' + name + ')"')
    return ('sudo -n systemctl reset-failed ' + name + ' 2>/dev/null; sudo -n systemctl restart ' + name + ' 2>&1 || exit 3; '
            'sleep 3; echo "STATE:$(systemctl is-active ' + name + ')"')


def restart_service(host, system, spec):
    """Restart a service on a computer; returns (succeeded, message)."""
    from .monitor import run_process, service_label
    from .updates import connection
    script = service_restart_script(system, spec)
    try:
        code, output, error = run_process(connection(host, "/bin/sh -c " + shlex.quote(script)), timeout=60)
    except (OSError, TimeoutError):
        return False, "Could not reach " + host["name"] + " to restart " + service_label(spec)
    text = (output + "\n" + error).strip()
    state = next((line[6:] for line in text.splitlines() if line.startswith("STATE:")), None)
    if code == 0 and state == "active":
        return True, service_label(spec) + " restarted and is running"
    if "password is required" in text or "a terminal is required" in text:
        return False, "Restarting " + service_label(spec) + " needs passwordless sudo for systemctl on " + host["name"]
    detail = state if state else (text.splitlines()[-1][:160] if text else "exit " + str(code))
    return False, service_label(spec) + " did not start: " + detail
