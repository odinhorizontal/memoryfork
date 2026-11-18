import sqlite3
from contextlib import closing
from pathlib import Path

from importer import import_orders
from scorer import score

DATA = Path(__file__).resolve().parent / "data"


def main():
    for strategy in ("all-major", "all-minor"):
        print(strategy)
        print("order_id version currency actual expected result")
        with closing(sqlite3.connect(":memory:")) as db:
            import_orders(db, DATA / "demo.json", strategy)
            for row in score(db, DATA / "demo.expected.json"):
                print(*row)


if __name__ == "__main__":
    main()
