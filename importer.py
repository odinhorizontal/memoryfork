"""Import synthetic orders using one fixed interpretation of every amount."""

import json

MULTIPLIERS = {"all-major": 100, "all-minor": 1}


def import_orders(db, orders_path, strategy):
    factor = MULTIPLIERS[strategy]
    orders = json.loads(orders_path.read_text(encoding="utf-8"))
    with db:
        db.execute("""
            CREATE TABLE orders (
                order_id TEXT NOT NULL PRIMARY KEY,
                version TEXT NOT NULL CHECK (version IN ('v1', 'v2')),
                currency TEXT NOT NULL CHECK (currency IN ('CNY', 'USD')),
                amount_minor INTEGER NOT NULL
            )
        """)
        for order in orders:
            amount = order["amount"]
            if type(amount) is not int:
                raise ValueError("amount must be an integer")
            db.execute(
                "INSERT INTO orders VALUES (?, ?, ?, ?)",
                (order["order_id"], order["version"], order["currency"], amount * factor),
            )
