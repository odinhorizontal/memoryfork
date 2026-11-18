"""Publish complete Step 3 experiments and compare isolated frozen sessions."""

import hashlib
import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from demo_evolve import PROJECT, experiment, frozen_trial
from evolution_store import rollback


# Only the orchestration/grading process uses this mapping.
AXES = {"A": "version", "B": "currency"}
RUN_ID = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}")
TABLES = ("events", "observations", "skills", "experiments", "revisions",
          "revision_rules", "revision_evidence", "probe_events")
ORDER_FIELDS = ("order_id", "version", "currency", "amount")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify(path, expected):
    actual = _sha(path)
    if actual != expected:
        raise ValueError("Frozen memory checksum mismatch")
    return actual


def _event_key(run_id, env, number):
    return f"{run_id}/{env}/{number}"


def _scope_event(row, run_id, env):
    if row.get("event_id") is not None:
        row["event_key"] = _event_key(run_id, env, row["event_id"])


def _scope_trial(row, run_id, env):
    row.update(run_id=run_id, environment=env)
    _scope_event(row, run_id, env)
    if row.get("retrieved"):
        _scope_event(row["retrieved"], run_id, env)


def list_runs(root):
    """Newest first. Incomplete staging directories are never visible."""
    root = Path(root)
    if not root.exists():
        return []
    return sorted((p.name for p in root.iterdir()
                   if RUN_ID.fullmatch(p.name) and (p / "trace.json").is_file()), reverse=True)


def create_run(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%S%fZ-") + uuid4().hex[:12]
    run = {"id": run_id, "created_at": now.isoformat(), "project": PROJECT, "environments": {}}
    # Publish trace and both databases with one same-volume directory rename.
    with TemporaryDirectory(dir=root) as staging:
        bundle = Path(staging) / "bundle"
        bundle.mkdir()
        for env, axis in AXES.items():
            # Training/inference only see opaque directories and their original inputs.
            with TemporaryDirectory() as room:
                trace = experiment(Path(room), axis)
                if trace["final_sha"] != trace["frozen_sha"]:
                    raise ValueError("Memory changed during experiment")
                dest = bundle / f"{env}.sqlite"
                shutil.copyfile(trace.pop("memory"), dest)
                _verify(dest, trace["frozen_sha"])
            for row in [*trace["initial"], *trace["attempts"], trace["blind"], *trace["replay"]]:
                _scope_trial(row, run_id, env)
            result = trace["result"]
            result["counterexample_keys"] = {
                axis_name: [_event_key(run_id, env, n) for n in numbers]
                for axis_name, numbers in result["counterexamples"].items()
            }
            run["environments"][env] = trace
        (bundle / "trace.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
        bundle.rename(root / run_id)
    return load_run(root, run_id)


def _load_trace(root, run_id):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid run ID")
    bundle = Path(root) / run_id
    run = json.loads((bundle / "trace.json").read_text(encoding="utf-8"))
    if run["id"] != run_id:
        raise ValueError("Run identity mismatch")
    return bundle, run


def _memory_rows(memory, run_id, env):
    with closing(sqlite3.connect(memory.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                  for table in TABLES}
    for row in tables["events"]:
        row.update(event_key=_event_key(run_id, env, row["id"]), run_id=run_id, environment=env)
    for table in ("observations", "revision_evidence", "probe_events"):
        for row in tables[table]:
            _scope_event(row, run_id, env)
    return tables


def load_run(root, run_id):
    bundle, run = _load_trace(root, run_id)
    for env in AXES:
        trace = run["environments"][env]
        memory = bundle / f"{env}.sqlite"
        _verify(memory, trace["frozen_sha"])
        trace["memory"] = _memory_rows(memory, run_id, env)
    return run


def compare(root, run_id, env):
    """Grade the saved holdout in two new Agent processes on independent copies."""
    if env not in AXES:
        raise ValueError("Unknown environment")
    bundle, run = _load_trace(root, run_id)
    trace = run["environments"][env]
    original = bundle / f"{env}.sqlite"
    sha_before = _verify(original, trace["frozen_sha"])
    order = {field: trace["blind"][field] for field in ORDER_FIELDS}
    comparison = dict(order=order, run_id=run_id, environment=env, sha_before=sha_before)
    try:
        with TemporaryDirectory() as tmp:
            for branch in ("enabled", "disabled"):
                # Give both Agent invocations the same relative path and working directory name.
                room = Path(tmp) / uuid4().hex
                room.mkdir()
                memory = room / "memory.sqlite"
                shutil.copyfile(original, memory)
                _verify(memory, sha_before)
                if branch == "disabled":
                    with closing(sqlite3.connect(memory)) as db:
                        db.execute("PRAGMA foreign_keys=ON")
                        comparison["previous_revision_id"] = rollback(db, PROJECT)
                row = frozen_trial(room / "session", PROJECT, memory, order, AXES[env])
                _scope_trial(row, run_id, env)
                hit = row["retrieved"]
                row["policy"] = ("revision" if "revision_id" in hit else
                                 "exact" if hit else "default")
                comparison[branch] = row
    finally:
        comparison["sha_after"] = _verify(original, sha_before)
    return comparison
