"""Compare persisted amounts against a fixture keyed by order ID."""

import json


def score(db, expected_path):
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    actual = {
        order_id: (version, currency, amount)
        for order_id, version, currency, amount in db.execute(
            "SELECT order_id, version, currency, amount_minor FROM orders"
        )
    }
    for order_id in sorted(actual.keys() | expected.keys()):
        version, currency, amount = actual.get(order_id, ("-", "-", None))
        target = expected.get(order_id)
        passed = order_id in actual and order_id in expected and amount == target
        yield order_id, version, currency, amount, target, "PASS" if passed else "FAIL"
