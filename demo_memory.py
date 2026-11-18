"""Drive the independent grader and launch each agent in its own directory."""

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from demo import DATA
from memory_agent import initialize
from scorer import score

AGENT = DATA.parent / "memory_agent.py"
PROJECT = "checkout"


def grade(directory, attempt, expected):
    orders = (directory / f"orders-{attempt}.sqlite").resolve().as_uri()
    with closing(sqlite3.connect(orders + "?mode=ro", uri=True)) as db:
        row, = score(db, expected)
    order_id, version, currency, actual, target, verdict = row
    return dict(order_id=order_id, version=version, currency=currency,
                actual=actual, target=target, verdict=verdict)


def command(project, orders, memory):
    return [sys.executable, str(AGENT), project, str(orders.resolve()), str(memory.resolve()),
            "--exact"]


def train(directory, memory, expected):
    directory.mkdir()
    cmd = command(PROJECT, DATA / "memory-a.json", memory) + ["--learn"]
    attempts = []
    with subprocess.Popen(cmd, cwd=directory, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as agent:
        try:
            for line in agent.stdout:
                attempt = json.loads(line)
                feedback = grade(directory, attempt["attempt"], expected)
                agent.stdin.write(json.dumps(dict(actual=feedback["actual"],
                                                  verdict=feedback["verdict"])) + "\n")
                agent.stdin.flush()
                attempts.append({**attempt, **feedback, **json.loads(agent.stdout.readline())})
        finally:
            agent.stdin.close()
        if agent.wait() != 0:
            raise subprocess.CalledProcessError(agent.returncode, cmd)
    return attempts


def probe(directory, project, memory):
    directory.mkdir()
    if not memory.exists() or memory.stat().st_size == 0:
        with closing(sqlite3.connect(memory)) as db:
            initialize(db)
    cmd = command(project, DATA / "memory-b.json", memory) + ["--read-only"]
    attempt = json.loads(subprocess.check_output(cmd, cwd=directory, text=True,
                                                 stdin=subprocess.DEVNULL))
    return {**attempt, **grade(directory, 1, DATA / "memory-b.expected.json")}


def experiment(root):
    memory = root / "learned.sqlite"
    training = train(root / "A", memory, DATA / "memory-a.expected.json")
    probes = {}
    for group, project, library in (
        ("learned", PROJECT, memory),
        ("empty", PROJECT, root / "empty.sqlite"),
        ("other-project", "billing", memory),
    ):
        probes[group] = probe(root / group, project, library)
    return training, probes


def main():
    with TemporaryDirectory() as tmp:
        training, probes = experiment(Path(tmp))
        print("group pid project version currency order input lookup action actual expected result provenance")
        for group, row in [*(("A", r) for r in training), *probes.items()]:
            hit = row["retrieved"]
            source = "-"
            if group == "A":
                source = f"session={row['session']} event={row['event_id']}"
            elif hit:
                source = (f"skill={hit['skill_id']} observation={hit['observation_id']} "
                          f"session={hit['source_session']} event={hit['event_id']}")
            print(group, row["pid"], row["project"], row["version"], row["currency"],
                  row["order_id"], row["input_amount"], "hit" if hit else "miss", row["action"],
                  row["actual"], row["target"], row["verdict"], source)
            if hit:
                print(f"  observation {hit['observation_id']}: {hit['observation']}")


if __name__ == "__main__":
    main()
