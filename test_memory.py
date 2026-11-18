import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from demo_memory import PROJECT, experiment, train
from memory_agent import retrieve


class MemoryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.memory = self.root / "learned.sqlite"

    def test_cross_process_reuse_and_controls(self):
        training, probes = experiment(self.root)
        first, retry = training
        self.assertEqual(first["session"], retry["session"])
        self.assertEqual(first["pid"], retry["pid"])
        self.assertEqual([(a["order_id"], a["action"], a["actual"], a["verdict"]) for a in training],
                         [("A1", "all-major", 125000, "FAIL"), ("A1", "all-minor", 1250, "PASS")])
        self.assertEqual(len({first["session"], *(p["session"] for p in probes.values())}), 4)
        for group, p in probes.items():
            with self.subTest(group=group):
                self.assertEqual((p["order_id"], p["target"], p["attempt"]), ("B1", 2750, 1))
                self.assertEqual(len(list((self.root / group).glob("orders-*.sqlite"))), 1)
                if group == "learned":
                    self.assertEqual((p["action"], p["actual"], p["verdict"]), ("all-minor", 2750, "PASS"))
                    hit = p["retrieved"]
                    self.assertEqual((hit["source_session"], hit["event_id"]),
                                     (retry["session"], retry["event_id"]))
                else:
                    self.assertEqual((p["retrieved"], p["action"], p["actual"], p["verdict"]),
                                     ({}, "all-major", 275000, "FAIL"))
        with closing(sqlite3.connect(self.memory)) as db:
            self.assertEqual(db.execute("SELECT id, action, actual, verdict FROM events ORDER BY id").fetchall(),
                             [(a["event_id"], a["action"], a["actual"], a["verdict"]) for a in training])
            hit = probes["learned"]["retrieved"]
            self.assertEqual(db.execute("SELECT id, observation_id, action FROM skills").fetchall(),
                             [(hit["skill_id"], hit["observation_id"], "all-minor")])
            self.assertEqual(db.execute("SELECT id, event_id FROM observations").fetchall(),
                             [(hit["observation_id"], retry["event_id"])])
            db.row_factory = sqlite3.Row
            for scope in [(PROJECT, "v2", "CNY"), (PROJECT, "v1", "USD")]:
                self.assertFalse(retrieve(db, *scope))

    def test_two_failures_create_only_events(self):
        expected = self.root / "different.expected.json"
        expected.write_text('{"A1": 7}', encoding="utf-8")
        attempts = train(self.root / "A", self.memory, expected)
        self.assertEqual([a["verdict"] for a in attempts], ["FAIL", "FAIL"])
        with closing(sqlite3.connect(self.memory)) as db:
            for table, count in [("events", 2), ("observations", 0), ("skills", 0)]:
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone(), (count,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
