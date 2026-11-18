import sqlite3
import unittest

from evolution_store import record_probe, resolve, rollback, stage
from memory_agent import WorkingMemory, initialize, record, retrieve


class RevisionTests(unittest.TestCase):
    candidates = [
        {"axis": "version", "mapping": {"v1": "all-minor", "v2": "all-major"}},
        {"axis": "currency", "mapping": {"USD": "all-major", "CNY": "all-minor"}},
    ]
    order = dict(order_id="probe", version="v1", currency="USD", amount=317)

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        initialize(self.db)
        for version, currency, action, verdict in (
            ("v1", "CNY", "all-major", "FAIL"),
            ("v1", "CNY", "all-minor", "PASS"),
            ("v2", "USD", "all-major", "PASS"),
        ):
            order = dict(order_id=version, amount=19, version=version, currency=currency)
            record(self.db, "training", WorkingMemory(order, "shop", {}, action),
                   dict(actual=(1900 if action == "all-major" else 19), verdict=verdict))

    def start(self):
        return stage(self.db, "shop", self.candidates, self.order)

    def feedback(self, run, minor="PASS", major="FAIL"):
        events = []
        for action, actual, verdict in [("all-minor", 317, minor), ("all-major", 31700, major)]:
            if verdict:
                events.append(record_probe(self.db, run, "probe-session", action,
                                           dict(actual=actual, verdict=verdict)))
        return events

    def snapshot_exact(self):
        return {table: list(map(tuple, self.db.execute("SELECT * FROM " + table + " ORDER BY id")))
                for table in ("events", "observations", "skills")}

    def test_migrates_original_tables_preserving_ids_and_provenance(self):
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"events", "skills", "observations"})
        original = self.snapshot_exact()
        exact = retrieve(self.db, "shop", "v1", "CNY")
        run = self.start()
        self.assertEqual(self.snapshot_exact(), original)
        self.assertEqual(retrieve(self.db, "shop", "v1", "CNY"), exact)
        self.assertEqual(retrieve(self.db, "shop", "v1", "USD"), {})

        passed, failed = self.feedback(run)
        result = resolve(self.db, run)
        self.assertEqual(result["winner"], "version")
        self.assertEqual(result["counterexamples"], {"currency": [failed]})
        hit = retrieve(self.db, "shop", "v1", "USD")
        self.assertEqual(hit["revision_id"], result["revision_id"])
        for key in ("skill_id", "observation_id", "event_id", "action"):
            self.assertEqual(hit[key], exact[key])
        supported = list(self.db.execute("""
            SELECT event_id FROM revision_evidence
            WHERE revision_id=? AND role='support' ORDER BY event_id
        """, (hit["revision_id"],)))
        self.assertEqual([row[0] for row in supported], [2, 3, passed])

        self.assertIsNone(rollback(self.db, "shop"))
        self.assertEqual(retrieve(self.db, "shop", "v1", "USD"), {})
        self.assertEqual(retrieve(self.db, "shop", "v1", "CNY"), exact)
        status = self.db.execute("SELECT status FROM revisions WHERE id=?", (hit["revision_id"],)).fetchone()[0]
        self.assertEqual(status, "rolled_back")
        self.assertEqual(len(self.db.execute("PRAGMA foreign_key_check").fetchall()), 0)

    def test_partial_or_absent_feedback_stays_pending(self):
        run = self.start()
        self.assertEqual(resolve(self.db, run)["status"], "pending")
        self.feedback(run, major=None)
        self.assertEqual(resolve(self.db, run)["status"], "pending")
        self.assertEqual(retrieve(self.db, "shop", "v1", "USD"), {})

    def test_identical_feedback_retry_is_idempotent_but_conflicts_block_release(self):
        run = self.start()
        passed, _ = self.feedback(run)
        self.assertEqual(record_probe(self.db, run, "probe-session", "all-minor",
                                      dict(actual=317, verdict="PASS")), passed)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM probe_events").fetchone()[0], 2)
        record_probe(self.db, run, "probe-session", "all-minor", dict(actual=317, verdict="FAIL"))
        self.assertEqual(resolve(self.db, run)["status"], "pending")

    def test_stale_experiment_cannot_supersede_new_active_revision(self):
        first, second = self.start(), self.start()
        self.feedback(first)
        self.feedback(second)
        published = resolve(self.db, second)["revision_id"]
        self.assertIsNotNone(published)
        self.assertEqual(resolve(self.db, first)["status"], "pending")
        self.assertEqual(retrieve(self.db, "shop", "v1", "USD")["revision_id"], published)

    def test_two_passing_actions_do_not_select_a_rule(self):
        run = self.start()
        self.feedback(run, major="PASS")
        self.assertIsNone(resolve(self.db, run)["revision_id"])

    def test_two_failing_actions_do_not_select_a_rule(self):
        run = self.start()
        self.feedback(run, minor="FAIL")
        self.assertIsNone(resolve(self.db, run)["revision_id"])

    def test_rules_cannot_override_a_conflicting_original_sample(self):
        # This sample passed with an action opposite to the proposed version rule.
        conflict = dict(order_id="prior", version="v2", currency="CNY", amount=529)
        record(self.db, "training", WorkingMemory(conflict, "shop", {}, "all-minor"),
               dict(actual=529, verdict="PASS"))
        run = self.start()
        self.feedback(run)
        result = resolve(self.db, run)
        self.assertIsNone(result["winner"])
        self.assertEqual(retrieve(self.db, "shop", "v2", "CNY")["action"], "all-minor")


if __name__ == "__main__":
    unittest.main()
