import time
import unittest

from fleetlight import agents, config


class AgentQuotaTests(unittest.TestCase):
    def test_defaults_enable_codex_and_cursor(self):
        self.assertEqual(config.enabled_agents(config.default_config()), {"codex": True, "cursor": True})
        self.assertEqual(config.enabled_agents({"version": 1, "hosts": []}), {"codex": True, "cursor": True})

    def test_agent_toggles_are_validated(self):
        value = config.default_config()
        value["agents"] = {"codex": False, "cursor": True}
        self.assertEqual(config.enabled_agents(config.validate(value)), {"codex": False, "cursor": True})
        value["agents"] = {"claude": True}
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
