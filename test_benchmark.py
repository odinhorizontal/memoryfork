import json
import sqlite3
import subprocess
import unittest
from contextlib import closing
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from benchmark import CONFIGS, PROJECT, digest, run_config, save_rows
from benchmark_report import NAMES, render
from memory_agent import retrieve_exact, retrieve_flat


class PairedBenchmarkTests(unittest.TestCase):
    def test_distinct_structures(self):
        self.assertEqual(len(CONFIGS), 8)
        self.assertEqual(set(CONFIGS.values()), set(product(
            ("diagonal", "anti"), ("version", "currency"), (False, True))))

    def test_histories_readonly_inputs_and_ledger_totals(self):
        ledger = []
        with TemporaryDirectory() as tmp:
            for config in CONFIGS:
                with self.subTest(config=config), TemporaryDirectory(dir=tmp) as room:
                    root = Path(room)
                    with patch("subprocess.check_output", wraps=subprocess.check_output) as launches:
                        rows, hashes = run_config(root, config)
                    self.assertEqual(hashes["before"], hashes["after"])
                    history = [r for r in rows if r["method"] == "shared"]
                    held = [r for r in rows if r["task"] == "blind"]
                    self.assertEqual([r["method"] for r in held], list("ABCD"))
                    for r in held[1:]:
                        self.assertEqual(r["order"], held[0]["order"])
                    order = held[0]["order"]
                    successes = [r for r in history if r["verdict"] == "PASS"]
                    probe, = [r for r in successes if r["task"] == "probe"]
                    for group, name in [("ABC", "events.sqlite"), ("D", "memory.sqlite")]:
                        memory = root / name
                        self.assertEqual(hashes["before"][group], digest(memory))
                        with closing(sqlite3.connect(memory)) as db:
                            actual = db.execute("""
                                SELECT id, project, order_id, version, currency, input_amount,
                                       action, actual, verdict FROM events ORDER BY id
                            """).fetchall()
                            self.assertEqual(actual, [
                                (r["event"], PROJECT, r["order"]["order_id"], r["order"]["version"],
                                 r["order"]["currency"], r["order"]["amount"], r["action"],
                                 r["actual"], r["verdict"]) for r in history])
                            self.assertNotIn((order["version"], order["currency"]), [e[3:5] for e in actual])
                            if group == "ABC":
                                db.row_factory = sqlite3.Row
                                for lookup in (retrieve_flat, retrieve_exact):
                                    p = probe["order"]
                                    self.assertEqual(lookup(db, PROJECT, p["version"], p["currency"])
                                                     ["event_id"], probe["event"])
                                self.assertEqual(retrieve_flat(db, "unknown", "v1", "CNY"), {})
                    self.assertEqual(len(launches.call_args_list), 12)
                    self.assertEqual(len({c.kwargs["cwd"] for c in launches.call_args_list}), 12)
                    for call in launches.call_args_list:
                        argv, = call.args
                        self.assertEqual(argv[2:4], [PROJECT, "order.json"])
                        self.assertIn("--read-only", argv)
                        self.assertIn(Path(argv[4]).name, ("memory.sqlite", "events.sqlite"))
                        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
                        self.assertEqual({p.name for p in call.kwargs["cwd"].iterdir()},
                                         {"order.json", "orders-1.sqlite"})
                    ledger.extend(rows)
            path = Path(tmp) / "trials.jsonl"
            save_rows(path, ledger)
            saved = [json.loads(line) for line in path.read_text().splitlines()]
            report = render(saved, 0, "frozen")
            for method, name in NAMES.items():
                group = [r for r in saved if r["method"] == method]
                blind = [r for r in group if r["task"] == "blind"]
                old = [r for r in group if r["task"] == "replay"]
                hits = [r for r in blind if "event_id" in r["source"]]
                self.assertEqual((len(blind), len(old)), (8, 16))
                def cell(subset, correct):
                    n = sum((r["actual"] == r["expected"]) == correct for r in subset)
                    return f"{n}/{len(subset)}（{100*n/len(subset):g}%）" if subset else "0/0（无命中）"
                reuse = "不适用" if method == "A" else cell(hits, False)
                self.assertIn(f"| {method} {name} | {cell(blind, True)} | {cell(old, True)} | {reuse} |",
                              report.splitlines())
