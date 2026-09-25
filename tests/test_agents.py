import time
import unittest

from fleetlight import agents, config


class AgentQuotaTests(unittest.TestCase):
    def test_defaults_enable_every_agent(self):
        everything = {"codex": True, "cursor": True, "claude": True}
        self.assertEqual(config.enabled_agents(config.default_config()), everything)
        self.assertEqual(config.enabled_agents({"version": 1, "hosts": []}), everything)

    def test_agent_toggles_are_validated(self):
        value = config.default_config()
        value["agents"] = {"codex": False, "cursor": True}
        self.assertEqual(config.enabled_agents(config.validate(value)), {"codex": False, "cursor": True, "claude": True})
        value["agents"] = {"claude": False}
        self.assertEqual(config.enabled_agents(config.validate(value))["claude"], False)
        value["agents"] = {"gemini": True}
        with self.assertRaises(ValueError):
            config.validate(value)

    def test_codex_remaining_ignores_account_identity(self):
        windows = agents.summarize_codex({
            "primary": {"usedPercent": 75, "windowDurationMins": 10080, "resetsAt": 0},
            "secondary": None,
            "planType": "pro",
            "accountId": "should-not-be-read",
        })
        self.assertEqual(windows[0]["remaining_percent"], 25)
        self.assertEqual(windows[0]["label"], "weekly")
        self.assertNotIn("accountId", str(windows))

    def test_codex_reset_keeps_countdown_and_shows_the_day(self):
        reset = time.time() + 3 * 86400 + 12 * 3600
        windows = agents.summarize_codex({
            "primary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": reset},
        })
        self.assertTrue(windows[0]["reset"].startswith("3d "))
        self.assertEqual(windows[0]["reset_day"], time.strftime("%a %d %b %H:%M", time.localtime(reset)))
        self.assertEqual(agents.format_codex_window(windows[0]),
                         f"60% weekly · {windows[0]['reset']} · {windows[0]['reset_day']}")
        remaining, detail = agents.summarize_cursor({
            "planUsage": {"totalPercentUsed": 5.0},
            "billingCycleEnd": reset,
        })
        self.assertEqual(remaining, 95)
        self.assertIn("95% remaining this period", detail)
        self.assertIn(windows[0]["reset"], detail)
        self.assertNotIn(windows[0]["reset_day"], detail)

    def test_cursor_remaining_uses_plan_percent(self):
        remaining, detail = agents.summarize_cursor({
            "planUsage": {"includedSpend": 2000, "limit": 2000, "totalPercentUsed": 5.0, "email": "hidden"},
            "billingCycleEnd": "1792330885000",
        })
        self.assertEqual(remaining, 95)
        self.assertIn("95% remaining", detail)
        self.assertNotIn("hidden", detail)
        remaining, _ = agents.summarize_cursor({"planUsage": {"totalPercentUsed": 41}})
        self.assertEqual(remaining, 59)

    def test_claude_windows_use_utilization(self):
        reset = time.time() + 3 * 86400 + 3600
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(reset))
        windows = agents.summarize_claude({
            "five_hour": {"utilization": 12.4, "resets_at": stamp},
            "seven_day": {"utilization": 40.0, "resets_at": None},
            "seven_day_opus": None,
            "extra_usage": {"utilization": 99.0},
        })
        self.assertEqual([(item["label"], item["remaining_percent"]) for item in windows], [("5h", 88), ("weekly", 60)])
        self.assertTrue(windows[0]["reset"].startswith("3d "))
        self.assertEqual(windows[1]["reset"], "")
        self.assertEqual(agents.summarize_claude({"five_hour": {"utilization": None}}), [])
        self.assertEqual(agents.summarize_claude(None), [])

    def test_claude_expired_sign_in_is_not_refreshed(self):
        credentials = {"accessToken": "secret", "expiresAt": (time.time() - 60) * 1000, "subscriptionType": "pro"}
        original = agents.claude_credentials
        agents.claude_credentials = lambda: credentials
        try:
            result = agents.collect_claude()
        finally:
            agents.claude_credentials = original
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("secret", str(result))

    def test_cursor_plan_name(self):
        self.assertEqual(agents.cursor_plan_name({"planInfo": {"planName": "Pro", "price": "$20/mo"}}), "Pro")
        self.assertIsNone(agents.cursor_plan_name({"planInfo": {"planName": "  "}}))
        self.assertIsNone(agents.cursor_plan_name({"planInfo": None}))
        self.assertIsNone(agents.cursor_plan_name([]))
