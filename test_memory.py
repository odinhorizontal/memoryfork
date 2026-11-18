import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from demo_memory import AGENT, DATA, PROJECT, experiment, probe, train
from memory_agent import retrieve


class MemoryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.memory = self.root / "learned.sqlite"

    def test_cross_process_reuse_and_controls(self):
        # Observe the real launches, including the argv received by each fresh interpreter.
        with patch("subprocess.Popen", wraps=subprocess.Popen) as launch:
            training, probes = experiment(self.root)
        self.assertEqual(launch.call_count, 4)
        runs = [
            ("A", PROJECT, "memory-a.json", self.memory),
            ("learned", PROJECT, "memory-b.json", self.memory),
            ("empty", PROJECT, "memory-b.json", self.root / "empty.sqlite"),
            ("other-project", "billing", "memory-b.json", self.memory),
        ]
        for call, (group, project, fixture, library) in zip(launch.call_args_list, runs):
            with self.subTest(launch=group):
                argv, = call.args
                self.assertEqual(argv[:5], [sys.executable, str(AGENT), project,
                                            str(DATA / fixture), str(library)])
                if group == "A":
                    self.assertCountEqual(argv[5:], ["--learn", "--exact"])
                else:
                    self.assertCountEqual(argv[5:], ["--read-only", "--exact"])
                self.assertEqual(call.kwargs["cwd"], self.root / group)
        first, retry = training
        self.assertEqual(first["session"], retry["session"])
        self.assertEqual(first["pid"], retry["pid"])
        self.assertEqual([(a["order_id"], a["action"], a["actual"], a["verdict"]) for a in training],
                         [("A1", "all-major", 125000, "FAIL"), ("A1", "all-minor", 1250, "PASS")])
        self.assertEqual(first["retrieved"], {})
        self.assertEqual((first["project"], first["input_amount"]), (PROJECT, 1250))
        self.assertEqual([r["attempt"] for r in training], [1, 2])
        self.assertEqual(len({first["session"], *(p["session"] for p in probes.values())}), 4)
        for group, p in probes.items():
            with self.subTest(group=group):
                self.assertEqual((p["order_id"], p["target"], p["attempt"]), ("B1", 2750, 1))
                self.assertEqual((p["version"], p["currency"]), ("v1", "CNY"))
                expected_project = "billing" if group == "other-project" else PROJECT
                self.assertEqual((p["project"], p["input_amount"]), (expected_project, 2750))
                self.assertEqual(len(list((self.root / group).glob("orders-*.sqlite"))), 1)
                with closing(sqlite3.connect(self.root / group / "orders-1.sqlite")) as orders:
                    self.assertEqual(orders.execute("SELECT order_id, amount_minor FROM orders").fetchall(),
                                     [("B1", p["actual"])])
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
            self.assertEqual(db.execute(
                "SELECT DISTINCT session, project, version, currency, order_id, input_amount FROM events"
            ).fetchall(), [(first["session"], PROJECT, "v1", "CNY", "A1", 1250)])
            db.row_factory = sqlite3.Row
            for scope in [(PROJECT, "v2", "CNY"), (PROJECT, "v1", "USD")]:
                self.assertFalse(retrieve(db, *scope))

    def test_first_action_passes_and_becomes_reusable_skill(self):
        expected = self.root / "major.expected.json"
        expected.write_text('{"A1": 125000}', encoding="utf-8")
        trained, = train(self.root / "A", self.memory, expected)
        self.assertEqual((trained["retrieved"], trained["action"], trained["actual"], trained["verdict"]),
                         ({}, "all-major", 125000, "PASS"))
        trial = probe(self.root / "B", PROJECT, self.memory)
        self.assertEqual((trial["action"], trial["actual"], trial["target"], trial["verdict"]),
                         ("all-major", 275000, 2750, "FAIL"))
        hit = trial["retrieved"]
        self.assertEqual((hit["action"], hit["source_session"], hit["event_id"]),
                         ("all-major", trained["session"], trained["event_id"]))
        with closing(sqlite3.connect(self.memory)) as db:
            self.assertEqual(db.execute("SELECT id, action FROM skills").fetchall(),
                             [(hit["skill_id"], "all-major")])

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
