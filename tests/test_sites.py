import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from fleetlight import config, sites


def site(**fields):
    value = {"id": "catalogue", "name": "Example catalogue",
             "url": "https://example.com/data/refresh-status.json", "max_age_hours": 4}
    value.update(fields)
    return value


class SiteConfigTests(unittest.TestCase):
    def test_https_sites_are_optional(self):
        value = config.default_config()
        self.assertEqual(config.validate(value)["hosts"][0]["id"], "local")
        value["sites"] = [site()]
        self.assertEqual(len(config.validate(value)["sites"]), 1)

    def test_rejects_http_and_credentials(self):
        value = config.default_config()
        for url in ("http://example.com/status.json", "https://user:pass@example.com/status.json",
                    "https://localhost/status.json"):
            value["sites"] = [site(url=url)]
            with self.subTest(url=url), self.assertRaises(ValueError):
                config.validate(value)

    def test_site_ids_must_not_collide_with_hosts(self):
        value = config.default_config()
        value["sites"] = [site(id="local")]
        with self.assertRaises(ValueError):
            config.validate(value)


class SiteFreshnessTests(unittest.TestCase):
    def test_recent_ok_status_is_current(self):
        now = 1_000_000
        result = sites.evaluate(site(), {
            "status": "ok", "generated_at": "1970-01-12T13:46:40Z", "product_count": 12,
        }, now=now)
        self.assertEqual(result["state"], "ok")
        self.assertFalse(sites.issues(result))
        self.assertEqual(result["product_count"], 12)

    def test_stale_catalogue_needs_attention(self):
        now = 1_000_000
        result = sites.evaluate(site(), {
            "status": "ok", "generated_at": now - 5 * 3600,
        }, now=now)
        self.assertEqual(result["state"], "stale")
        self.assertTrue(any("last updated 5h ago" in item for item in result["issues"]))

    def test_failed_refresh_alerts_even_when_timestamp_is_recent(self):
        now = 1_000_000
        result = sites.evaluate(site(), {
            "status": "failed", "generated_at": now - 60, "error": "Catalogue refresh failed.",
        }, now=now)
        self.assertIn("Example catalogue refresh failed", result["issues"])

    def test_nested_timestamp_key(self):
        now = 1_000_000
        result = sites.evaluate(site(timestamp_key="current.generated_at"), {
            "current": {"generated_at": now - 10 * 3600},
        }, now=now)
        self.assertTrue(any("10h ago" in item for item in result["issues"]))

    def test_http_error_is_unavailable(self):
        error = HTTPError("https://example.com/data/refresh-status.json", 500, "Error", hdrs=None, fp=io.BytesIO())
        try:
            with patch("fleetlight.sites.fetch", side_effect=error):
                result = sites.check_one(site(), now=1_000_000)
        finally:
            error.close()
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["issues"], ["Example catalogue returned HTTP 500"])

    def test_status_document_uses_generated_at(self):
        payload = {
            "status": "ok",
            "checked_at": "2026-01-02T12:30:00.000Z",
            "generated_at": "2026-01-02T12:00:00.000Z",
            "product_count": 120,
            "retry_after_seconds": 3600,
        }
        now = sites.parse_timestamp(payload["generated_at"]) + 30 * 60
        result = sites.evaluate(site(), payload, now=now)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["product_count"], 120)
        stale = sites.evaluate(site(), payload, now=now + 5 * 3600)
        self.assertEqual(stale["state"], "stale")
