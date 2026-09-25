"""Private, per-user configuration. Nothing is discovered or enrolled silently."""
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "fleetlight" / "fleet.json"


def state_path():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "fleetlight" / "history.json"


AGENTS = ("codex", "cursor", "claude")


def default_config():
    return {"version": 1, "refresh_seconds": 60, "auto_updates": False,
            "agents": {"codex": True, "cursor": True, "claude": True}, "hosts": [
        {"id": "local", "name": "This Computer", "local": True, "services": []}
    ]}


def enabled_agents(configuration):
    value = configuration.get("agents") if isinstance(configuration, dict) else {}
    if not isinstance(value, dict):
        value = {}
    return {name: bool(value.get(name, True)) for name in AGENTS}


def auto_updates_enabled(configuration):
    return isinstance(configuration, dict) and configuration.get("auto_updates") is True


def validate(config):
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("Expected configuration version 1")
    interval = config.get("refresh_seconds", 60)
    if type(interval) is not int or not 15 <= interval <= 3600:
        raise ValueError("Refresh interval must be between 15 and 3600 seconds")
    if "auto_updates" in config and type(config["auto_updates"]) is not bool:
        raise ValueError("auto_updates must be true or false")
    if "agents" in config:
        agents = config["agents"]
        if not isinstance(agents, dict) or any(name not in AGENTS or type(enabled) is not bool for name, enabled in agents.items()):
            raise ValueError("agents must enable or disable only Codex, Cursor and Claude")
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
    if "sites" in config:
        sites = config["sites"]
        if not isinstance(sites, list) or len(sites) > 16:
            raise ValueError("Configure at most 16 websites")
        for site in sites:
            if not isinstance(site, dict):
                raise ValueError("Each website must be an object")
            ident = site.get("id", "")
            if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", ident) or ident in ids:
                raise ValueError("Website IDs must be unique and use letters, numbers, dots or dashes")
            ids.add(ident)
            if not isinstance(site.get("name"), str) or not 1 <= len(site["name"]) <= 80:
                raise ValueError("Website names must contain 1–80 characters")
            url = site.get("url", "")
            if not isinstance(url, str) or len(url) > 300:
                raise ValueError("Website URLs must be HTTPS addresses without credentials")
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname
                    or "." not in parsed.hostname or parsed.hostname.endswith(".")
                    or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", parsed.hostname)):
                raise ValueError("Website URLs must be HTTPS addresses without credentials")
            age = site.get("max_age_hours", 4)
            if type(age) is not int or not 1 <= age <= 168:
                raise ValueError("Website max_age_hours must be between 1 and 168")
            key = site.get("timestamp_key")
            if key is not None and (not isinstance(key, str) or not re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_]{0,40}(?:\.[A-Za-z][A-Za-z0-9_]{0,40}){0,4}", key)):
                raise ValueError("timestamp_key must be a dotted JSON field name")
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
