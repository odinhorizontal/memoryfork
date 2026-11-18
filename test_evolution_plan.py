import sqlite3
import unittest

from evolution_plan import build_plan
from memory_agent import WorkingMemory, initialize, record


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        initialize(self.db)

    def sample(self, version, currency, action, verdict="PASS", project="checkout"):
        order = {"version": version, "currency": currency, "order_id": "seed", "amount": 7}
        actual = 700 if action == "all-major" else 7
        return record(self.db, "seed-session", WorkingMemory(order, project, {}, action),
                      {"actual": actual, "verdict": verdict})

    def test_actions_and_values_follow_the_exact_skills(self):
        # Swap both the initial currency pairing and the version/action mapping.
        self.sample("v1", "USD", "all-minor", "FAIL")
        first = self.sample("v1", "USD", "all-major")
        second = self.sample("v2", "CNY", "all-minor")
        self.sample("v1", "CNY", "all-major", project="other")
        plan = build_plan(self.db, "checkout")
        self.assertEqual(plan["support_event_ids"], [first, second])
        self.assertEqual(plan["candidates"], [
            {"axis": "version", "mapping": {"v1": "all-major", "v2": "all-minor"}},
            {"axis": "currency", "mapping": {"USD": "all-major", "CNY": "all-minor"}},
        ])
        self.assertEqual([(c["version"], c["currency"], c["disagreement"])
                          for c in plan["alternatives"]], [("v1", "CNY", 1), ("v2", "USD", 1)])
        self.assertEqual(plan["probe"], {"version": "v1", "currency": "CNY",
                                        "predictions": {"version": "all-major", "currency": "all-minor"},
                                        "disagreement": 1})

    def test_matching_predictions_do_not_warrant_a_probe(self):
        self.sample("v1", "CNY", "all-minor")
        self.sample("v2", "USD", "all-minor")
        plan = build_plan(self.db, "checkout")
        self.assertEqual(len(plan["candidates"]), 2)
        self.assertEqual([c["disagreement"] for c in plan["alternatives"]], [0, 0])
        self.assertIsNone(plan["probe"])

    def test_failed_history_refutes_a_candidate_and_excludes_seen_order(self):
        self.sample("v1", "CNY", "all-minor")
        self.sample("v2", "USD", "all-major")
        self.sample("v2", "CNY", "all-major", "FAIL")
        plan = build_plan(self.db, "checkout")
        self.assertEqual([c["axis"] for c in plan["candidates"]], ["currency"])
        self.assertEqual([(c["version"], c["currency"]) for c in plan["alternatives"]], [("v1", "USD")])
        self.assertIsNone(plan["probe"])


if __name__ == "__main__":
    unittest.main()
