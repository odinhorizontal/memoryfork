import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from demo import DATA
from importer import import_orders
from scorer import score


class ExperimentTests(unittest.TestCase):
    def test_amounts_and_verdicts(self):
        for sample, first, second, currencies in [
            ("demo", "D1", "D2", ("CNY", "USD")),
            ("cross", "X1", "X2", ("USD", "CNY")),
        ]:
            for strategy, amounts, verdicts in [
                ("all-major", (125000, 1200), ("FAIL", "PASS")),
                ("all-minor", (1250, 12), ("PASS", "FAIL")),
            ]:
                with self.subTest(sample=sample, strategy=strategy), closing(sqlite3.connect(":memory:")) as db:
                    import_orders(db, DATA / f"{sample}.json", strategy)
                    self.assertEqual(list(score(db, DATA / f"{sample}.expected.json")), [
                        (first, "v1", currencies[0], amounts[0], 1250, verdicts[0]),
                        (second, "v2", currencies[1], amounts[1], 1200, verdicts[1]),
                    ])

    def test_independent_grading(self):
        with TemporaryDirectory() as tmp, closing(sqlite3.connect(":memory:")) as db:
            import_orders(db, DATA / "demo.json", "all-major")
            db.execute("UPDATE orders SET amount_minor = 99 WHERE order_id = 'D1'")
            expected = Path(tmp) / "expected.json"
            expected.write_text('{"D1": 99, "missing": 100}', encoding="utf-8")
            self.assertEqual(list(score(db, expected)), [
                ("D1", "v1", "CNY", 99, 99, "PASS"),
                ("D2", "v2", "USD", 1200, None, "FAIL"),
                ("missing", "-", "-", None, 100, "FAIL"),
            ])

    def test_repeat_demo(self):
        command = [sys.executable, str(DATA.parent / "demo.py")]
        rows = [
            "order_id version currency actual expected result",
            "D1 v1 CNY 125000 1250 FAIL",
            "D2 v2 USD 1200 1200 PASS",
        ]
        expected = ["all-major", *rows, "all-minor", rows[0],
                    "D1 v1 CNY 1250 1250 PASS", "D2 v2 USD 12 1200 FAIL"]
        for _ in range(2):
            self.assertEqual(subprocess.check_output(command, text=True).splitlines(), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
