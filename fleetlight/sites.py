"""HTTPS freshness checks for configured websites. Never follows non-HTTPS redirects."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__

MAX_BODY = 256 * 1024
TIMESTAMP_KEYS = ("generated_at", "current.generated_at", "updated_at", "timestamp", "checked_at",
                  "stats.timestamp")
FAILED_STATUS = {"failed", "error", "down", "stale", "unhealthy"}
OK_STATUS = {"ok", "success", "healthy", "current"}


class HttpsRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not str(newurl).startswith("https://"):
            raise URLError("Redirect is not HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def configured(configuration):
    value = configuration.get("sites") if isinstance(configuration, dict) else []
    return value if isinstance(value, list) else []


def lookup(data, path):
    current = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def parse_timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        return seconds if seconds > 0 else None
    if not isinstance(value, str) or not 8 <= len(value) <= 40:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    hours = seconds // 3600
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def timestamp_of(data, key=None):
    if key:
        return parse_timestamp(lookup(data, key))
    for candidate in TIMESTAMP_KEYS:
        value = parse_timestamp(lookup(data, candidate))
        if value:
            return value
    return None


def evaluate(site, data, now=None, status_code=200):
    now = time_now(now)
    name = site.get("name") or "Website"
    limit = int(site.get("max_age_hours") or 4) * 3600
    issues = []
    generated = timestamp_of(data, site.get("timestamp_key")) if isinstance(data, dict) else None
    reported = None
    if isinstance(data, dict):
        reported = data.get("status")
        if isinstance(reported, str) and reported.casefold() in FAILED_STATUS:
            issues.append(name + " refresh failed")
        elif isinstance(reported, str) and reported.casefold() not in OK_STATUS and reported.strip():
            issues.append(name + " reported " + reported)
    if generated is None:
        issues.append(name + " did not include an update time")
    else:
        age = now - generated
        if age > limit:
            issues.append(name + " last updated " + duration(age) + " ago (alert after " +
                          duration(limit) + ")")
    return {
        "id": site.get("id"), "name": name, "url": site.get("url"), "state": "stale" if issues else "ok",
        "status": reported if isinstance(reported, str) else None, "generated_at": generated,
        "product_count": data.get("product_count") if isinstance(data, dict) else None,
        "status_code": status_code, "checked_at": now, "issues": issues,
        "detail": issues[0] if issues else name + " updated " + duration(now - (generated or now)) + " ago",
    }


def time_now(now=None):
    return time.time() if now is None else now


def fetch(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("Only HTTPS URLs without credentials are supported")
    request = Request(url, headers={"User-Agent": "Fleetlight/" + __version__, "Accept": "application/json"})
    opener = build_opener(HttpsRedirectHandler)
    with opener.open(request, timeout=10) as response:
        if response.geturl() and not response.geturl().startswith("https://"):
            raise ValueError("Redirect is not HTTPS")
        raw = response.read(MAX_BODY + 1)
        code = getattr(response, "status", None) or response.getcode()
    if len(raw) > MAX_BODY:
        raise ValueError("Status document is too large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Status document must be a JSON object")
    return code, data


def check_one(site, now=None):
    name = site.get("name") or "Website"
    try:
        code, data = fetch(site["url"])
        return evaluate(site, data, now=now, status_code=code)
    except HTTPError as error:
        return unreachable(site, name, "returned HTTP " + str(error.code), now, error.code)
    except (OSError, URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return unreachable(site, name, "could not be checked", now)


def unreachable(site, name, reason, now, status_code=None):
    message = name + " " + reason
    return {"id": site.get("id"), "name": name, "url": site.get("url"), "state": "unavailable",
            "status": None, "generated_at": None, "product_count": None, "status_code": status_code,
            "checked_at": time_now(now), "issues": [message], "detail": message}


def check_all(sites, now=None):
    result = {}
    wanted = [site for site in sites if isinstance(site, dict) and site.get("id") and site.get("url")]
    if not wanted:
        return result
    with ThreadPoolExecutor(max_workers=min(4, len(wanted))) as pool:
        futures = {pool.submit(check_one, site, now): site["id"] for site in wanted}
        for future in as_completed(futures):
            try:
                result[futures[future]] = future.result()
            except Exception:
                ident = futures[future]
                result[ident] = unreachable({"id": ident}, ident, "could not be checked", now)
    return result


def issues(status):
    if not isinstance(status, dict):
        return []
    found = status.get("issues")
    return [item for item in found if isinstance(item, str)] if isinstance(found, list) else []
