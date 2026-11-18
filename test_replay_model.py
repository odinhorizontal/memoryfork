import hashlib
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import replay_model


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.saved = replay_model.create_run(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_reopen_with_no_agent_and_new_run_in_its_own_directory(self):
        second = replay_model.create_run(self.root)
        self.assertNotEqual(self.saved["id"], second["id"])
        self.assertEqual(replay_model.list_runs(self.root)[:2], [second["id"], self.saved["id"]])
        with patch.object(replay_model, "experiment", side_effect=AssertionError):
            self.assertEqual(replay_model.load_run(self.root, self.saved["id"]), self.saved)

    def test_incomplete_run_is_not_published(self):
        existing = replay_model.list_runs(self.root)
        with patch.object(replay_model, "experiment", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                replay_model.create_run(self.root)
        self.assertEqual(replay_model.list_runs(self.root), existing)

    def test_event_numbers_are_scoped_to_run_and_environment(self):
        a, b = [self.saved["environments"][env]["initial"][0] for env in "AB"]
        self.assertEqual(a["event_id"], b["event_id"])
        self.assertNotEqual(a["event_key"], b["event_key"])
        for env, trial in zip("AB", [a, b]):
            event = self.saved["environments"][env]["memory"]["events"][0]
            self.assertEqual(trial["event_key"], f'{self.saved["id"]}/{env}/{event["id"]}')
            self.assertEqual(trial["event_key"], event["event_key"])
            self.assertEqual(trial["session"], event["session"])

    def test_counterexample_points_to_its_prediction_and_persisted_event(self):
        for trace in self.saved["environments"].values():
            loser, = trace["result"]["counterexample_keys"]
            key, = trace["result"]["counterexample_keys"][loser]
            execution, = [r for r in trace["attempts"] if r["event_key"] == key]
            candidate, = [r for r in trace["plan"]["candidates"] if r["axis"] == loser]
            self.assertEqual(execution["action"], candidate["mapping"][trace["plan"]["order"][loser]])
            self.assertEqual(execution["verdict"], "FAIL")
            event, = [r for r in trace["memory"]["events"] if r["event_key"] == key]
            for field in ("actual", "order_id", "action", "session", "verdict"):
                self.assertEqual(event[field], execution[field])
            revision, = [r for r in trace["memory"]["revisions"] if r["axis"] == loser]
            evidence = [r for r in trace["memory"]["revision_evidence"]
                        if r["role"] == "counterexample" and r["revision_id"] == revision["id"]]
            self.assertEqual([r["event_key"] for r in evidence], [key])

    def test_matched_inputs_new_sessions_real_scores_and_immutable_source(self):
        for env, expected, disabled_score in [("A", 8300, "PASS"), ("B", 83, "FAIL")]:
            trace = self.saved["environments"][env]
            source = self.root / self.saved["id"] / f"{env}.sqlite"
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            sessions = set()
            for _ in range(2):
                result = replay_model.compare(self.root, self.saved["id"], env)
                self.assertEqual(result["sha_before"], result["sha_after"])
                self.assertEqual(result["sha_before"], before)
                self.assertIsNone(result["previous_revision_id"])
                enabled, disabled = result["enabled"], result["disabled"]
                for row in [enabled, disabled]:
                    self.assertEqual({k: row[k] for k in result["order"]}, result["order"])
                    self.assertEqual(row["target"], expected)
                    sessions.add(row["session"])
                self.assertEqual(enabled["policy"], "revision")
                self.assertEqual(enabled["retrieved"], trace["blind"]["retrieved"])
                self.assertEqual(enabled["action"], trace["blind"]["action"])
                self.assertEqual((enabled["actual"], enabled["verdict"]), (expected, "PASS"))
                self.assertEqual(disabled["policy"], "default")
                self.assertEqual(disabled["retrieved"], {})
                self.assertEqual(disabled["action"], "all-major")
                self.assertEqual((disabled["actual"], disabled["verdict"]), (8300, disabled_score))
            self.assertEqual(len(sessions), 4)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                self.assertEqual(db.execute("SELECT id FROM revisions WHERE status='active'").fetchone(),
                                 (trace["result"]["revision_id"],))


if __name__ == "__main__":
    unittest.main()
