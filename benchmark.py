"""Run the frozen, paired experiment. Contract selection lives on the grader side."""

import argparse
import hashlib
import json
import sqlite3
import subprocess
import time
from contextlib import closing
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmark_report import render
from demo_evolve import DATA, PROJECT, evolve, frozen_trial, train_order
from memory_agent import WorkingMemory, initialize, record


ROOT = Path(__file__).resolve().parent
POLICIES = {"A": ("--no-memory",), "B": ("--flat",), "C": ("--exact",), "D": ()}
SCOPES = set(product(("v1", "v2"), ("CNY", "USD")))
FIELDS = ("order_id", "version", "currency", "amount")
CONFIGS = {f"{i:02}": values for i, values in enumerate(
    product(("diagonal", "anti"), ("version", "currency"), (False, True)), 1)}


def order_of(row):
    return {key: row[key] for key in FIELDS}


def scope(order):
    return order["version"], order["currency"]


def journal(config, method, task, trial):
    hit = trial.get("retrieved", {})
    source = {k: v for k, v in hit.items() if k not in ("source_session", "observation", "action")}
    return dict(config=config, method=method, task=task, project=PROJECT, order=order_of(trial),
                action=trial["action"], source=source, event=trial.get("event_id"),
                actual=trial["actual"], expected=trial["target"], verdict=trial["verdict"])


def events(memory):
    with closing(sqlite3.connect(memory.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT * FROM events ORDER BY id").fetchall()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_config(room, config):
    pairing, axis, reverse = CONFIGS[config]
    seeds = json.loads((DATA / "demo.json").read_text(encoding="utf-8"))
    if pairing == "anti":
        seeds[0]["currency"], seeds[1]["currency"] = seeds[1]["currency"], seeds[0]["currency"]
    memory = room / "memory.sqlite"
    basic = room / "events.sqlite"
    if memory.exists() or basic.exists():
        raise FileExistsError("Each configuration requires a fresh room")
    training = []
    for i, order in enumerate(seeds):
        training.extend(train_order(room / f"train-{i}", memory, order, axis, reverse))
    plan, probes, result = evolve(room / "probe", memory, axis, reverse)
    if (result["probe_count"], result["execution_count"]) != (1, 2):
        raise ValueError("Expected one completed two-action probe")
    learned = training + probes
    # Materialize Step 2 skills for every successful experience, including the probe.
    # B reads only events and A skips lookup. No imports are repeated to build this view.
    with closing(sqlite3.connect(basic)) as db:
        initialize(db)
        for trial in learned:
            work = WorkingMemory(order_of(trial), PROJECT, {}, trial["action"])
            event = record(db, trial["session"], work, trial)
            if event != trial["event_id"]:
                raise ValueError("Event provenance differs between memories")
    if events(basic) != events(memory):
        raise ValueError("The policies must receive identical event histories")
    (version, currency), = SCOPES - {scope(r) for r in learned}
    holdout = dict(order_id="H1", amount=83, version=version, currency=currency)
    trials = [journal(config, "shared", "train", t) for t in training]
    for t in probes:
        row = journal(config, "shared", "probe", t)
        row["source"] = {"candidate": next(c["axis"] for c in plan["candidates"]
                             if c["mapping"][t[c["axis"]]] == t["action"])}
        trials.append(row)

    memories = {"ABC": basic, "D": memory}
    before = {name: digest(path) for name, path in memories.items()}
    for method, flags in POLICIES.items():
        library = memory if method == "D" else basic
        for index, order in enumerate([holdout, *seeds]):
            trial = frozen_trial(room / f"trial-{method}-{index}", PROJECT, library, order,
                                 axis, reverse, policy=flags)
            if trial["attempt"] != 1:
                raise ValueError("Expected one read-only decision")
            trials.append(journal(config, method, "replay" if index else "blind", trial))
    after = {name: digest(path) for name, path in memories.items()}
    if before != after:
        raise ValueError("Frozen memory changed during evaluation")
    return trials, dict(before=before, after=after)


def save_rows(path, rows):
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
                    encoding="utf-8")


def freeze_sha():
    # Report the last commit touching the protocol or any decision/scoring dependency.
    paths = ["docs/benchmark-protocol.md", "benchmark.py", "benchmark_report.py", "benchmark.ps1",
             "data/demo.json", "demo_evolve.py", "demo_memory.py", "demo.py", "evolution_agent.py",
             "evolution_plan.py", "evolution_store.py", "memory_agent.py", "scorer.py", "importer.py",
             "run-python.ps1", "test_benchmark.py"]
    try:
        return subprocess.check_output(["git", "log", "-1", "--format=%H", "--", *paths],
                                       cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable (source archive)"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "benchmark-output")
    args = parser.parse_args()
    start = time.perf_counter()
    rows, audit = [], {}
    for config in CONFIGS:
        with TemporaryDirectory() as scratch:
            trials, audit[config] = run_config(Path(scratch), config)
            rows.extend(trials)
    seconds = time.perf_counter() - start
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / "trials.jsonl"
    save_rows(path, rows)
    persisted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    report = render(persisted, seconds, freeze_sha())
    (args.output / "report.md").write_text(report, encoding="utf-8")
    (args.output / "hashes.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(f"{len(rows)} imports in {seconds:.2f}s. Results: {args.output}")


if __name__ == "__main__":
    main()
