"""Run two independent graders against the same learning and inference processes."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory

from demo_memory import AGENT, DATA, PROJECT
from scorer import score


# Private to the grading side. The agent receives a verdict only after an import.
CONTRACTS = {"version": {"v1": 1, "v2": 100}, "currency": {"CNY": 1, "USD": 100}}
EVOLUTION_AGENT = AGENT.with_name("evolution_agent.py")


def grade(directory, attempt, order, axis, reverse=False):
    factor = CONTRACTS[axis][order[axis]]
    target = order["amount"] * (100 // factor if reverse else factor)
    with TemporaryDirectory() as grading_room:
        expected = Path(grading_room) / "expected.json"
        expected.write_text(json.dumps({order["order_id"]: target}), encoding="utf-8")
        database = (directory / f"orders-{attempt}.sqlite").resolve().as_uri()
        with closing(sqlite3.connect(database + "?mode=ro", uri=True)) as db:
            row, = score(db, expected)
    order_id, version, currency, actual, target, verdict = row
    return dict(order_id=order_id, version=version, currency=currency,
                amount=order["amount"], actual=actual, target=target, verdict=verdict)


def session_args(directory, project, memory, order):
    directory.mkdir()
    (directory / "order.json").write_text(json.dumps([order]), encoding="utf-8")
    return [sys.executable, str(AGENT), project, "order.json", os.path.relpath(memory, directory)]


@contextmanager
def learning_process(directory, command):
    with subprocess.Popen(command, cwd=directory, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as process:
        try:
            yield process
        except BaseException:
            process.kill()
            raise
        finally:
            process.stdin.close()
        if process.wait() != 0:
            raise subprocess.CalledProcessError(process.returncode, command)


def send_grade(process, graded):
    feedback = {key: graded[key] for key in ("actual", "verdict")}
    process.stdin.write(json.dumps(feedback) + "\n")
    process.stdin.flush()


def train_order(directory, memory, order, axis, reverse=False):
    command = session_args(directory, PROJECT, memory, order) + ["--learn"]
    attempts = []
    with learning_process(directory, command) as process:
        for line in process.stdout:
            attempt = json.loads(line)
            graded = grade(directory, attempt["attempt"], order, axis, reverse)
            send_grade(process, graded)
            event = json.loads(process.stdout.readline())
            attempts.append({**attempt, **graded, **event})
    return attempts


def evolve(directory, memory, axis, reverse=False):
    directory.mkdir()
    command = [sys.executable, str(EVOLUTION_AGENT), PROJECT, os.path.relpath(memory, directory)]
    attempts = []
    with learning_process(directory, command) as process:
        plan = json.loads(process.stdout.readline())
        for line in process.stdout:
            message = json.loads(line)
            if message["type"] == "result":
                result = message
                break
            if message["type"] != "attempt":
                raise ValueError(f"Unexpected evolution message {message}")
            graded = grade(directory, message["attempt"], plan["order"], axis, reverse)
            attempts.append({**message, **graded})
            send_grade(process, graded)
        else:
            raise RuntimeError("Evolution session ended without a result")
    if len(attempts) != len(result["event_ids"]):
        raise ValueError("Scored attempts do not match persisted event count")
    for trial, event_id in zip(attempts, result["event_ids"]):
        trial["event_id"] = event_id
    return plan, attempts, result


def frozen_trial(directory, project, memory, order, axis, reverse=False, policy=()):
    command = session_args(directory, project, memory, order) + ["--read-only", *policy]
    # check_output returns only once the Agent exits. This process receives no feedback.
    attempt = json.loads(subprocess.check_output(command, cwd=directory, text=True,
                                                 stdin=subprocess.DEVNULL))
    return {**attempt, **grade(directory, attempt["attempt"], order, axis, reverse)}


def experiment(root, axis):
    root.mkdir(parents=True, exist_ok=True)
    memory = root / "memory.sqlite"
    if memory.exists():
        raise FileExistsError(memory)
    seeds = json.loads((DATA / "demo.json").read_text(encoding="utf-8"))
    initial = []
    for index, order in enumerate(seeds, start=1):
        initial.extend(train_order(root / f"training-{index}", memory, order, axis))
    plan, attempts, result = evolve(root / "evolution", memory, axis)
    frozen_sha = hashlib.sha256(memory.read_bytes()).hexdigest()

    scopes = set(product({o["version"] for o in seeds}, {o["currency"] for o in seeds}))
    used = {(a["version"], a["currency"]) for a in initial + attempts}
    (version, currency), = scopes - used
    holdout = dict(order_id="H1", version=version, currency=currency, amount=83)
    blind = frozen_trial(root / "blind", PROJECT, memory, holdout, axis)
    replay = [frozen_trial(root / f"replay-{index}", PROJECT, memory, order, axis)
              for index, order in enumerate(seeds, start=1)]
    return dict(memory=memory, initial=initial, plan=plan, attempts=attempts, result=result,
                blind=blind, replay=replay, frozen_sha=frozen_sha,
                final_sha=hashlib.sha256(memory.read_bytes()).hexdigest())


def scope(row):
    return f"{row['version']}/{row['currency']}"


def describe(candidate):
    mapping = ", ".join(f"{key}={value}" for key, value in sorted(candidate["mapping"].items()))
    return f"{candidate['axis']} ({mapping})"


def show_environment(name, run):
    print(f"\nEnvironment {name} | project {PROJECT}")
    print("Initial evidence (actual / expected in minor units)")
    for row in run["initial"]:
        print(f"  event {row['event_id']}: {row['order_id']} {scope(row)} {row['action']} "
              f"{row['actual']} / {row['target']} {row['verdict']}")
    plan = run["plan"]
    print("Pending rules")
    for candidate in plan["candidates"]:
        print(" ", describe(candidate))
    print("Agent predictions on unseen combinations")
    for alternative in plan["alternatives"]:
        print(f"  {scope(alternative)}: {alternative['predictions']} "
              f"(disagreement {alternative['disagreement']})")
    probe = plan["order"]
    print(f"Selected {probe['order_id']} {scope(probe)} amount {probe['amount']}. {plan['reason']}")
    for row in run["attempts"]:
        print(f"  event {row['event_id']}: {row['action']} {row['actual']} / {row['target']} "
              f"{row['verdict']}")
    result = run["result"]
    print(f"{result['probe_count']} probe, {result['execution_count']} executions")
    for loser, events in result["counterexamples"].items():
        if events:
            print(f"Rejected {loser} by counterexample event(s) {events}")
    if result["active"] is None:
        print("No revision activated. Exact Step 2 skills remain available.")
    else:
        print(f"Activated revision {result['revision_id']}: {result['active']}")
    blind = run["blind"]
    print(f"New session {blind['session']}: {blind['order_id']} {scope(blind)} amount "
          f"{blind['amount']} -> {blind['action']} {blind['actual']} / {blind['target']} "
          f"{blind['verdict']}")
    print("Support replay:", ", ".join(f"{r['order_id']} {r['verdict']}" for r in run["replay"]))


def main():
    with TemporaryDirectory() as tmp:
        runs = []
        for name, axis in [("A", "version"), ("B", "currency")]:
            # Opaque paths keep even the working-directory name independent of the contract.
            with TemporaryDirectory(dir=tmp) as room:
                run = experiment(Path(room), axis)
                show_environment(name, run)
                if run["result"]["active"] != axis:
                    raise SystemExit(f"Environment {name}: revision did not recover the grading contract")
                if any(row["verdict"] != "PASS" for row in [run["blind"], *run["replay"]]):
                    raise SystemExit(f"Environment {name}: frozen session failed grading")
                if run["frozen_sha"] != run["final_sha"]:
                    raise SystemExit(f"Environment {name}: memory changed during frozen sessions")
                runs.append(run)
        print(f"\n{'Learned from the probe':24} {'Environment A':48} Environment B")
        active = [next(c for c in run["plan"]["candidates"] if c["axis"] == run["result"]["active"])
                  for run in runs]
        print(f"{PROJECT:24} {describe(active[0]):48} {describe(active[1])}")


if __name__ == "__main__":
    main()
