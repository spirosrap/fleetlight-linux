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

    def test_cursor_remaining_uses_included_allowance(self):
        remaining, detail = agents.summarize_cursor({
            "planUsage": {"includedSpend": 2000, "limit": 2000, "totalPercentUsed": 5.0, "email": "hidden"},
        })
        self.assertEqual(remaining, 0)
        self.assertIn("included", detail)
        self.assertNotIn("hidden", detail)
        remaining, _ = agents.summarize_cursor({"planUsage": {"totalPercentUsed": 41}})
        self.assertEqual(remaining, 59)
