import json
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

from demo_evolve import EVOLUTION_AGENT, experiment, frozen_trial, train_order
from demo_memory import PROJECT, command as step_two_command


class EvolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.runs = {axis: experiment(Path(mkdtemp(dir=cls.root)), axis)
                    for axis in ("version", "currency")}

    def test_same_visible_evidence_and_plan(self):
        version, currency = self.runs.values()
        fields = ("version", "currency", "order_id", "amount", "action", "actual", "verdict")

        def visible(run):
            return [tuple(row[key] for key in fields) for row in run["initial"]]

        self.assertEqual(visible(version), visible(currency))
        self.assertEqual([(v, c, a, verdict) for v, c, _, _, a, _, verdict in visible(version)], [
            ("v1", "CNY", "all-major", "FAIL"), ("v1", "CNY", "all-minor", "PASS"),
            ("v2", "USD", "all-major", "PASS"),
        ])
        self.assertNotEqual(version["memory"], currency["memory"])
        for field in ("order", "candidates", "alternatives"):
            self.assertEqual(version["plan"][field], currency["plan"][field])
        candidates = version["plan"]["candidates"]
        self.assertEqual({c["axis"] for c in candidates}, {"version", "currency"})
        for c in candidates:
            for row in version["initial"]:
                predicted = c["mapping"][row[c["axis"]]]
                self.assertEqual(predicted == row["action"], row["verdict"] == "PASS")

    def test_one_discriminating_probe_two_isolated_imports(self):
        for axis, run in self.runs.items():
            with self.subTest(axis=axis):
                order = run["plan"]["order"]
                chosen = (order["version"], order["currency"])
                crosses = {("v1", "USD"), ("v2", "CNY")}
                self.assertIn(chosen, crosses)
                trials = run["attempts"]
                self.assertEqual((run["result"]["probe_count"], run["result"]["execution_count"]),
                                 (1, 2))
                self.assertEqual([(r["version"], r["currency"], r["order_id"]) for r in trials],
                                 [(*chosen, order["order_id"])] * 2)
                self.assertEqual({r["action"] for r in trials}, {"all-major", "all-minor"})
                self.assertEqual({r["verdict"] for r in trials}, {"PASS", "FAIL"})
                self.assertEqual({r["actual"] for r in trials}, {order["amount"], order["amount"] * 100})
                counter = next(r for r in trials if r["verdict"] == "FAIL")
                loser = "currency" if axis == "version" else "version"
                self.assertEqual(run["result"]["counterexamples"][loser], [counter["event_id"]])
                blind = run["blind"]
                self.assertEqual(crosses - {chosen}, {(blind["version"], blind["currency"])})
                # Inspect every pre-freeze order database, including ungraded imports.
                room = run["memory"].parent
                paths = [*room.glob("training-*/*.sqlite"), *(room / "evolution").glob("*.sqlite")]
                imports = []
                for path in paths:
                    with closing(sqlite3.connect(path)) as db:
                        single_order, = db.execute("SELECT version, currency, order_id, amount_minor FROM orders")
                        imports.append(single_order)
                self.assertCountEqual(imports, [(r["version"], r["currency"], r["order_id"], r["actual"])
                                                for r in run["initial"] + trials])
                with closing(sqlite3.connect(run["memory"])) as db:
                    events = db.execute("SELECT version, currency, order_id, input_amount FROM events").fetchall()
                self.assertEqual(len(events), 5)
                self.assertNotIn((blind["version"], blind["currency"]), [r[:2] for r in events])
                self.assertNotIn(blind["order_id"], [r[2] for r in events])
                self.assertNotIn(blind["amount"], [0] + [r[3] for r in events])

    def test_frozen_session_reuses_winner_and_replays_support(self):
        for axis, run in self.runs.items():
            with self.subTest(axis=axis):
                self.assertEqual(run["result"]["active"], axis)
                self.assertEqual(run["frozen_sha"], run["final_sha"])
                blind = run["blind"]
                self.assertEqual(blind["attempt"], 1)
                self.assertEqual(blind["verdict"], "PASS")
                self.assertEqual(blind["retrieved"]["revision_id"], run["result"]["revision_id"])
                # Replay this holdout through Step 2 against the very same active revision.
                root = run["memory"].parent
                directory = root / "exact-holdout"
                directory.mkdir()
                args = step_two_command(PROJECT, root / "blind" / "order.json", run["memory"])
                output = subprocess.check_output(args + ["--read-only"], cwd=directory,
                                                 stdin=subprocess.DEVNULL, text=True)
                exact = json.loads(output)
                self.assertEqual((exact["retrieved"], exact["action"]), ({}, "all-major"))
                self.assertNotIn(blind["session"], {r["session"] for r in run["initial"] + run["attempts"]})
                self.assertEqual([(r["action"], r["verdict"]) for r in run["replay"]],
                                 [("all-minor", "PASS"), ("all-major", "PASS")])

    def test_project_boundary_and_rollback_in_fresh_sessions(self):
        # Roll back private snapshot copies so every test sees the same frozen revision.
        script = Path(__file__).with_name("evolution_store.py")
        for axis, run in self.runs.items():
            with self.subTest(axis=axis):
                memory = self.root / f"rollback-{axis}.sqlite"
                shutil.copyfile(run["memory"], memory)
                order = {k: run["blind"][k] for k in ("order_id", "version", "currency", "amount")}
                other = frozen_trial(self.root / f"billing-{axis}", "billing", memory, order, axis)
                self.assertEqual((other["retrieved"], other["action"]), ({}, "all-major"))
                subprocess.run([sys.executable, str(script), "rollback", PROJECT, str(memory)], check=True,
                               capture_output=True)
                for i, sample in enumerate([order, run["plan"]["order"], run["initial"][0]]):
                    sample = {k: sample[k] for k in order}
                    row = frozen_trial(self.root / f"after-{axis}-{i}", PROJECT, memory, sample, axis)
                    if i < 2:
                        self.assertEqual((row["retrieved"], row["action"]), ({}, "all-major"))
                    else:
                        self.assertIn("skill_id", row["retrieved"])
                        self.assertEqual((row["action"], row["verdict"]), ("all-minor", "PASS"))

    def test_incomplete_or_ambiguous_grades_keep_exact_memory(self):
        seed_memory = self.root / "seed.sqlite"
        for i, row in enumerate(self.runs["version"]["replay"]):
            order = {k: row[k] for k in ("version", "currency", "order_id", "amount")}
            train_order(self.root / f"seed-{i}", seed_memory, order, "version")
        for name, verdicts in [("missing", ["PASS"]), ("both", ["PASS", "PASS"]),
                               ("neither", ["FAIL", "FAIL"])]:
            with self.subTest(case=name):
                room = self.root / name
                room.mkdir()
                memory = room / "memory.sqlite"
                shutil.copyfile(seed_memory, memory)
                imported = self.runs["version"]["attempts"]
                replies = "".join(json.dumps({"actual": row["actual"], "verdict": verdict}) + "\n"
                                  for row, verdict in zip(imported, verdicts))
                command = [sys.executable, str(EVOLUTION_AGENT), PROJECT, memory.name]
                output = subprocess.check_output(command, cwd=room, input=replies, text=True)
                plan, _, _, result = map(json.loads, output.splitlines())
                self.assertEqual((result["probe_count"], result["execution_count"]), (1, 2))
                self.assertIsNone(result["active"])
                self.assertIsNone(result["revision_id"])
                row = frozen_trial(room / "inference", PROJECT, memory, plan["order"], "version")
                self.assertEqual((row["retrieved"], row["action"]), ({}, "all-major"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
