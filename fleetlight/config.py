"""Private, per-user configuration. Nothing is discovered or enrolled silently."""
import json
import os
from pathlib import Path
import re
import tempfile


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "fleetlight" / "fleet.json"


def state_path():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "fleetlight" / "history.json"


def default_config():
    return {"version": 1, "refresh_seconds": 60, "hosts": [
        {"id": "local", "name": "This Computer", "local": True, "services": []}
    ]}


def validate(config):
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("Expected configuration version 1")
    interval = config.get("refresh_seconds", 60)
    if type(interval) is not int or not 15 <= interval <= 3600:
        raise ValueError("Refresh interval must be between 15 and 3600 seconds")
    hosts = config.get("hosts")
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 32:
        raise ValueError("Configure between 1 and 32 computers")
    ids = set()
    for host in hosts:
        if not isinstance(host, dict):
            raise ValueError("Each computer must be an object")
        ident = host.get("id", "")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", ident) or ident in ids:
            raise ValueError("Computer IDs must be unique and use letters, numbers, dots or dashes")
        ids.add(ident)
        if not isinstance(host.get("name"), str) or not 1 <= len(host["name"]) <= 80:
            raise ValueError("Computer names must contain 1–80 characters")
        if type(host.get("local", False)) is not bool:
            raise ValueError("local must be true or false")
        if not host.get("local"):
            alias = host.get("alias", "")
            if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,200}", alias):
                raise ValueError("Use an SSH alias or user@hostname without spaces or options")
        services = host.get("services", [])
        if not isinstance(services, list) or len(services) > 20:
            raise ValueError("At most 20 services are supported per computer")
        if any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,100}", s) for s in services):
            raise ValueError("Use systemd service names without paths or command options")
        optional = host.get("optional_services", [])
        if not isinstance(optional, list) or any(not isinstance(s, str) or s not in services for s in optional):
            raise ValueError("optional_services must be a list of configured service names")
    return config


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".fleetlight-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(path=None):
    path = Path(path or config_path())
    if not path.exists():
        result = default_config()
        atomic_json(path, result)
        return result
    if path.stat().st_size > 128 * 1024:
        raise ValueError("Configuration is too large")
    return validate(json.loads(path.read_text()))
