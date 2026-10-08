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

    def test_claude_usage_is_checked_rarely_and_backs_off_when_rate_limited(self):
        calls = []
        replies = [dict(agents.unavailable("claude", "Claude usage is rate limited"), rate_limited=True)] * 4
        replies.append({"id": "claude", "name": "Claude", "state": "ok", "remaining_percent": 80, "detail": ""})
        original, schedule = agents.COLLECTORS["claude"], dict(agents.claude_schedule)
        agents.COLLECTORS["claude"] = lambda: calls.append(1) or dict(replies[len(calls) - 1])
        agents.claude_schedule.update(next=0.0, delay=agents.CLAUDE_INTERVAL)
        try:
            first = agents.collect(["claude"])["claude"]
            self.assertIn("retrying in 10 min", first["detail"])
            self.assertEqual(agents.collect(["claude"]), {})
            delays = []
            for _ in range(4):
                agents.claude_schedule["next"] = 0.0
                agents.collect(["claude"])
                delays.append(agents.claude_schedule["delay"])
        finally:
            agents.COLLECTORS["claude"] = original
            agents.claude_schedule.clear()
            agents.claude_schedule.update(schedule)
        self.assertEqual(len(calls), 5)
        self.assertEqual(delays, [1200, 1800, 1800, agents.CLAUDE_INTERVAL])

    def test_cursor_plan_name(self):
        self.assertEqual(agents.cursor_plan_name({"planInfo": {"planName": "Pro", "price": "$20/mo"}}), "Pro $20")
        self.assertEqual(agents.cursor_plan_name({"planInfo": {"planName": "Ultra"}}), "Ultra")
        self.assertIsNone(agents.cursor_plan_name({"planInfo": {"planName": "  "}}))
        self.assertIsNone(agents.cursor_plan_name({"planInfo": None}))
        self.assertIsNone(agents.cursor_plan_name([]))

    def test_claude_plan_name_includes_tier(self):
        self.assertEqual(agents.claude_plan_name("pro", "default_claude_ai"), "Pro $20")
        self.assertEqual(agents.claude_plan_name("max", "default_claude_max_5x"), "Max 5x $100")
        self.assertEqual(agents.claude_plan_name("max", "default_claude_max_20x"), "Max 20x $200")
        self.assertEqual(agents.claude_plan_name("team", None), "Team")
        self.assertEqual(agents.claude_plan_name(None, None), "")

    def test_claude_profile_plan_reflects_upgrades(self):
        payload = {"account": {"email": "hidden"},
                   "organization": {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_5x"}}
        self.assertEqual(agents.claude_profile_plan(payload), "Max 5x $100")
        self.assertEqual(agents.claude_profile_plan({"organization": {"organization_type": "claude_pro"}}), "Pro $20")
        self.assertIsNone(agents.claude_profile_plan({"organization": None}))
        self.assertIsNone(agents.claude_profile_plan([]))

    def test_codex_renewal_prefers_cancellation_then_renewal(self):
        payload = {"entitlement": {"renews_at": "2026-10-25T20:38:01+00:00", "cancels_at": None}, "active_until": "2026-10-25T20:38:01Z"}
        renews = agents.iso_timestamp("2026-10-25T20:38:01Z")
        self.assertEqual(agents.codex_renewal(payload), {"at": renews, "ends": False})
        payload["entitlement"]["cancels_at"] = "2026-11-01T00:00:00+00:00"
        self.assertEqual(agents.codex_renewal(payload), {"at": agents.iso_timestamp("2026-11-01T00:00:00Z"), "ends": True})
        self.assertEqual(agents.codex_renewal({"entitlement": {}, "active_until": "2026-10-25T20:38:01Z"}), {"at": renews, "ends": False})
        self.assertIsNone(agents.codex_renewal({"entitlement": None}))
        self.assertIsNone(agents.codex_renewal([]))

    def test_cursor_renewal_is_the_billing_cycle_end(self):
        self.assertEqual(agents.cursor_renewal({"planInfo": {"billingCycleEnd": "1792330885000"}}), {"at": 1792330885.0, "ends": False})
        self.assertEqual(agents.cursor_renewal({"billingCycleEnd": 1792330885000}), {"at": 1792330885.0, "ends": False})
        self.assertIsNone(agents.cursor_renewal({"planInfo": {}}))
        self.assertIsNone(agents.cursor_renewal(None))

    def test_claude_renewal_estimates_the_next_monthly_anniversary(self):
        payload = {"organization": {"subscription_status": "active", "subscription_created_at": "2026-01-31T08:48:49Z"}}
        now = agents.iso_timestamp("2026-10-08T10:00:00Z")
        estimate = agents.claude_renewal(payload, now)
        self.assertEqual(estimate["estimated"], True)
        self.assertEqual(estimate["ends"], False)
        # October has a 31st, so the anniversary falls on it; a shorter month would use its last day.
        self.assertEqual(agents.iso_timestamp("2026-10-31T08:48:49Z"), estimate["at"])
        february = agents.claude_renewal(payload, agents.iso_timestamp("2026-02-01T00:00:00Z"))
        self.assertEqual(agents.iso_timestamp("2026-02-28T08:48:49Z"), february["at"])
        self.assertIsNone(agents.claude_renewal({"organization": {"subscription_status": "canceled", "subscription_created_at": "2026-01-31T08:48:49Z"}}, now))
        self.assertIsNone(agents.claude_renewal({"organization": {"subscription_status": "active"}}, now))
        self.assertIsNone(agents.claude_renewal([], now))

    def test_codex_product_name(self):
        self.assertEqual(agents.codex_product_name({"entitlement": {"subscription_plan": "chatgptprolite"}}), "Pro $100")
        self.assertEqual(agents.codex_product_name({"entitlement": {"subscription_plan": "chatgptpro"}}), "Pro $200")
        self.assertIsNone(agents.codex_product_name({"entitlement": {"subscription_plan": "unknown"}}))
        self.assertIsNone(agents.codex_product_name({"entitlement": None}))
        self.assertIsNone(agents.codex_product_name([]))
