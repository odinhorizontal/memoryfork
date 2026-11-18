"""Select a probe using exact memory and learn from the two external verdicts."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import evolution_store
from evolution_plan import build_plan
from importer import import_orders
from memory_agent import emit, initialize


def run_probe(db, project, candidates, alternatives, order, reason):
    session = uuid4().hex
    run = evolution_store.stage(db, project, candidates, order)
    emit(type="plan", session=session, candidates=candidates, alternatives=alternatives,
         order=order, reason=reason)

    # Commit to both actions before receiving either grade. Each import starts empty.
    actions = sorted({rule["mapping"][order[rule["axis"]]] for rule in candidates})
    event_ids = []
    with TemporaryDirectory() as scratch:
        fixture = Path(scratch) / "probe.json"
        fixture.write_text(json.dumps([order]), encoding="utf-8")
        for attempt, action in enumerate(actions, start=1):
            path = Path(f"orders-{attempt}.sqlite")
            path.touch(exist_ok=False)
            with closing(sqlite3.connect(path)) as orders_db:
                import_orders(orders_db, fixture, action)
            evolution_store.record_import(db, run, action)
            emit(type="attempt", attempt=attempt, action=action, session=session)
            line = sys.stdin.readline()
            feedback = json.loads(line) if line.strip() else None
            event_id = None
            if (isinstance(feedback, dict) and type(feedback.get("actual")) is int
                    and feedback.get("verdict") in ("PASS", "FAIL")):
                event_id = evolution_store.record_probe(db, run, session, action, feedback)
            event_ids.append(event_id)
    result = evolution_store.resolve(db, run)
    probes, executions = db.execute("""
        SELECT COUNT(DISTINCT experiment_id), COUNT(*) FROM probe_imports WHERE experiment_id=?
    """, (run,)).fetchone()
    emit(type="result", active=result["winner"], revision_id=result["revision_id"],
         status=result["status"], counterexamples=result["counterexamples"],
         probe_count=probes, execution_count=executions, event_ids=event_ids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    parser.add_argument("memory", type=Path)
    args = parser.parse_args()
    with closing(sqlite3.connect(args.memory)) as db:
        db.row_factory = sqlite3.Row
        initialize(db)
        plan = build_plan(db, args.project)
        probe = plan["probe"]
        if probe is None:
            emit(type="result", status="no_probe", active=None, revision_id=None,
                 reason=plan["reason"], probe_count=0, execution_count=0)
            return
        order = dict(order_id="P1", amount=37, version=probe["version"], currency=probe["currency"])
        run_probe(db, args.project, plan["candidates"], plan["alternatives"], order, plan["reason"])


if __name__ == "__main__":
    main()
