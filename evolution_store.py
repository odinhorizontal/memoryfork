"""Persist and validate revisions of scoped Step 2 skills using scored events."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def initialize_revisions(db):
    # Additive migration. Exact skills and their original sources stay intact.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY, project TEXT NOT NULL,
            parent_revision_id INTEGER REFERENCES revisions(id),
            version TEXT NOT NULL, currency TEXT NOT NULL, order_id TEXT NOT NULL,
            input_amount INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revisions (
            id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL REFERENCES experiments(id),
            axis TEXT NOT NULL CHECK(axis IN ('version', 'currency')),
            status TEXT NOT NULL CHECK(status IN
                ('pending', 'active', 'rejected', 'superseded', 'rolled_back')),
            UNIQUE(experiment_id, axis)
        );
        CREATE TABLE IF NOT EXISTS revision_rules (
            revision_id INTEGER NOT NULL REFERENCES revisions(id), value TEXT NOT NULL,
            skill_id INTEGER NOT NULL REFERENCES skills(id), PRIMARY KEY(revision_id, value)
        );
        CREATE TABLE IF NOT EXISTS probe_events (
            experiment_id INTEGER NOT NULL REFERENCES experiments(id),
            event_id INTEGER PRIMARY KEY REFERENCES events(id)
        );
        CREATE TABLE IF NOT EXISTS probe_imports (
            experiment_id INTEGER NOT NULL REFERENCES experiments(id), action TEXT NOT NULL,
            PRIMARY KEY(experiment_id, action)
        );
        CREATE TABLE IF NOT EXISTS revision_evidence (
            revision_id INTEGER NOT NULL REFERENCES revisions(id),
            event_id INTEGER NOT NULL REFERENCES events(id),
            role TEXT NOT NULL CHECK(role IN ('support', 'counterexample')),
            PRIMARY KEY(revision_id, event_id)
        );
    """)


def retrieve_revision(db, project, version, currency):
    # A read-only session may open an unmigrated Step 2 file.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='revisions'").fetchone():
        return {}
    row = db.execute("""
        SELECT r.id AS revision_id, r.axis, m.value, s.id AS skill_id,
               o.id AS observation_id, e.id AS event_id, e.session AS source_session, s.action
        FROM experiments x JOIN revisions r ON r.experiment_id=x.id
        JOIN revision_rules m ON m.revision_id=r.id
        JOIN skills s ON s.id=m.skill_id JOIN observations o ON o.id=s.observation_id
        JOIN events e ON e.id=o.event_id
        WHERE x.project=? AND r.status='active'
          AND m.value=CASE r.axis WHEN 'version' THEN ? ELSE ? END
    """, (project, version, currency)).fetchone()
    return dict(row) if row else {}


def active_id(db, project):
    row = db.execute("""
        SELECT r.id FROM revisions r JOIN experiments x ON x.id=r.experiment_id
        WHERE x.project=? AND r.status='active'
    """, (project,)).fetchone()
    return row[0] if row else None


def stage(db, project, candidates, order):
    """Stage the experiment before any probe execution or feedback."""
    initialize_revisions(db)
    with db:
        db.execute("BEGIN IMMEDIATE")
        parent = active_id(db, project)
        run = db.execute("""
            INSERT INTO experiments(project, parent_revision_id, version, currency, order_id, input_amount)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (project, parent, order["version"], order["currency"], order["order_id"], order["amount"])).lastrowid
        sources = db.execute("""
            SELECT s.id, e.version, e.currency, s.action FROM skills s
            JOIN observations o ON o.id=s.observation_id JOIN events e ON e.id=o.event_id
            WHERE e.project=? AND e.verdict='PASS' AND s.action=e.action ORDER BY s.id
        """, (project,)).fetchall()
        for candidate in candidates:
            axis = candidate["axis"]
            revision = db.execute("""
                INSERT INTO revisions(experiment_id, axis, status) VALUES (?, ?, 'pending')
            """, (run, axis)).lastrowid
            for value, action in candidate["mapping"].items():
                source = next((s for s in sources if s[axis] == value and s["action"] == action), None)
                if source is None:
                    raise ValueError("each rule needs an exact skill from this project")
                db.execute("INSERT INTO revision_rules VALUES (?, ?, ?)", (revision, value, source["id"]))
    return run


def record_import(db, run_id, action):
    """Count the completed import even if the grader never returns a verdict."""
    with db:
        db.execute("INSERT INTO probe_imports VALUES (?, ?)", (run_id, action))


def record_probe(db, run_id, session, action, feedback):
    """A probe gets an episodic event but never creates a new Step 2 exact skill."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("""
            SELECT e.id FROM probe_events p JOIN events e ON e.id=p.event_id
            WHERE p.experiment_id=? AND e.session=? AND e.action=? AND e.actual=? AND e.verdict=?
        """, (run_id, session, action, feedback["actual"], feedback["verdict"])).fetchone()
        if existing:
            return existing[0]
        run = db.execute("""
            SELECT x.* FROM experiments x WHERE x.id=? AND EXISTS (
                SELECT 1 FROM revisions r WHERE r.experiment_id=x.id AND r.status='pending')
        """, (run_id,)).fetchone()
        if not run:
            raise ValueError("probe feedback requires a pending experiment")
        event = db.execute("""
            INSERT INTO events(session, project, version, currency, order_id,
                               input_amount, action, actual, verdict) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session, run["project"], run["version"], run["currency"], run["order_id"],
              run["input_amount"], action, feedback["actual"], feedback["verdict"])).lastrowid
        db.execute("INSERT INTO probe_events VALUES (?, ?)", (run_id, event))
    return event


def replay(db, revision, project_events, order):
    """Replay the persisted action and verdict of each sample against the proposed scope."""
    rules = dict(db.execute("""
        SELECT m.value, s.action FROM revision_rules m JOIN skills s ON s.id=m.skill_id
        WHERE m.revision_id=?
    """, (revision["id"],)))
    evidence = []
    consistent = True
    for event in project_events:
        prediction = rules.get(event[revision["axis"]])
        used_prediction = prediction == event["action"]
        passed = event["verdict"] == "PASS"
        if prediction is None or used_prediction != passed:
            consistent = False
        # Counterexamples are actual executions of this rule's own prediction.
        if used_prediction:
            evidence.append((revision["id"], event["id"], "support" if passed else "counterexample"))
    db.executemany("INSERT OR IGNORE INTO revision_evidence VALUES (?, ?, ?)", evidence)
    probe_prediction = rules.get(order[revision["axis"]])
    return consistent, probe_prediction


def resolve(db, run_id):
    """Promote exactly one historically consistent candidate after complete, distinct scores."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        run = db.execute("SELECT * FROM experiments WHERE id=?", (run_id,)).fetchone()
        revisions = db.execute("SELECT * FROM revisions WHERE experiment_id=? ORDER BY id", (run_id,)).fetchall()
        if not run or not revisions:
            raise ValueError("unknown or empty experiment")
        if all(r["status"] == "pending" for r in revisions):
            history = db.execute("SELECT * FROM events WHERE project=? ORDER BY id", (run["project"],)).fetchall()
            probes = db.execute("""
                SELECT e.* FROM probe_events p JOIN events e ON p.event_id=e.id
                WHERE p.experiment_id=? ORDER BY e.id
            """, (run_id,)).fetchall()
            winners = []
            predictions = set()
            for revision in revisions:
                consistent, prediction = replay(db, revision, history, run)
                predictions.add(prediction)
                if consistent:
                    winners.append(revision["id"])
            # Each candidate action needs a distinct outcome on the same probe.
            complete = (len(predictions) == len(probes) == 2
                        and predictions == {e["action"] for e in probes}
                        and {e["verdict"] for e in probes} == {"PASS", "FAIL"}
                        and len({e["actual"] for e in probes}) == 2)
            if (complete and len(winners) == 1
                    and active_id(db, run["project"]) == run["parent_revision_id"]):
                db.execute("UPDATE revisions SET status='superseded' WHERE id=?", (run["parent_revision_id"],))
                db.execute("UPDATE revisions SET status='rejected' WHERE experiment_id=?", (run_id,))
                db.execute("UPDATE revisions SET status='active' WHERE id=?", (winners[0],))
        active = db.execute("""
            SELECT id, axis FROM revisions WHERE experiment_id=? AND status='active'
        """, (run_id,)).fetchone()
        counters = {}
        for row in db.execute("""
            SELECT r.axis, ev.event_id FROM revisions r JOIN revision_evidence ev ON ev.revision_id=r.id
            WHERE r.experiment_id=? AND ev.role='counterexample' ORDER BY ev.event_id
        """, (run_id,)):
            counters.setdefault(row["axis"], []).append(row["event_id"])
        prior_status = next(r["status"] for r in revisions if r["status"] != "rejected")
        return dict(status="active" if active else prior_status,
                    revision_id=active["id"] if active else None,
                    winner=active["axis"] if active else None, counterexamples=counters)


def rollback(db, project):
    """Restore the active revision's parent, or the original exact skills."""
    with db:
        db.execute("BEGIN IMMEDIATE")
        current = active_id(db, project)
        if current is None:
            raise ValueError("project has no active revision")
        parent, = db.execute("""
            SELECT x.parent_revision_id FROM experiments x JOIN revisions r ON r.experiment_id=x.id
            WHERE r.id=?
        """, (current,)).fetchone()
        db.execute("UPDATE revisions SET status='rolled_back' WHERE id=?", (current,))
        db.execute("UPDATE revisions SET status='active' WHERE id=?", (parent,))
    return parent


def main():
    parser = argparse.ArgumentParser(description="Restore an active revision's parent.")
    parser.add_argument("command", choices=["rollback"])
    parser.add_argument("project")
    parser.add_argument("memory", type=Path)
    args = parser.parse_args()
    with closing(sqlite3.connect(args.memory.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        print(json.dumps(dict(previous_revision_id=rollback(db, args.project))))


if __name__ == "__main__":
    main()
