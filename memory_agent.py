"""Run one single-order session. Only the training session receives feedback."""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from importer import import_orders


@dataclass
class WorkingMemory:
    order: dict
    project: str
    retrieved: dict
    action: str


def initialize(db):
    db.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, session TEXT NOT NULL, project TEXT NOT NULL,
            version TEXT NOT NULL, currency TEXT NOT NULL, order_id TEXT NOT NULL,
            input_amount INTEGER NOT NULL, action TEXT NOT NULL, actual INTEGER NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN ('PASS', 'FAIL'))
        );
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL UNIQUE REFERENCES events(id),
            observation TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL UNIQUE REFERENCES observations(id),
            action TEXT NOT NULL
        );
    """)


def retrieve(db, project, version, currency):
    # The observation and skill inherit their scope through the source event.
    row = db.execute("""
        SELECT s.id AS skill_id, o.id AS observation_id, e.id AS event_id,
               e.session AS source_session, s.action
        FROM skills s JOIN observations o ON o.id = s.observation_id
        JOIN events e ON e.id = o.event_id
        WHERE e.project = ? AND e.version = ? AND e.currency = ?
        ORDER BY s.id DESC LIMIT 1
    """, (project, version, currency)).fetchone()
    return dict(row) if row else {}


def record(db, session, work, feedback):
    order = work.order
    with db:
        event = db.execute("""
            INSERT INTO events(session, project, version, currency, order_id,
                               input_amount, action, actual, verdict)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session, work.project, order["version"], order["currency"], order["order_id"],
              order["amount"], work.action, feedback["actual"], feedback["verdict"])).lastrowid
        if feedback["verdict"] == "PASS":
            statement = (f"{work.project} {order['version']}/{order['currency']} sample "
                         f"{order['order_id']} passed with {work.action}.")
            observation = db.execute(
                "INSERT INTO observations(event_id, observation) VALUES (?, ?)", (event, statement)
            ).lastrowid
            db.execute("INSERT INTO skills(observation_id, action) VALUES (?, ?)",
                       (observation, work.action))
    return event


def emit(**fields):
    print(json.dumps(fields), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("orders", type=Path)
    parser.add_argument("memory", type=Path)
    parser.add_argument("--learn", action="store_true")
    args = parser.parse_args()
    order, = json.loads(args.orders.read_text(encoding="utf-8"))
    session = uuid4().hex
    with closing(sqlite3.connect(args.memory)) as db:
        db.row_factory = sqlite3.Row
        initialize(db)
        hit = retrieve(db, args.project, order["version"], order["currency"])
        work = WorkingMemory(order, args.project, hit, hit.get("action", "all-major"))
        for attempt in range(1, 3 if args.learn else 2):
            with closing(sqlite3.connect(f"orders-{attempt}.sqlite")) as orders_db:
                import_orders(orders_db, args.orders, work.action)
            emit(attempt=attempt, pid=os.getpid(), session=session,
                 retrieved=work.retrieved, action=work.action)
            if not args.learn:
                return
            feedback = json.loads(sys.stdin.readline())
            event = record(db, session, work, feedback)
            emit(event_id=event)
            if feedback["verdict"] == "PASS":
                return
            work.action = "all-minor"


if __name__ == "__main__":
    main()
