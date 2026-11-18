"""Choose a discriminating experiment using only this project's Step 2 history."""

from itertools import combinations, product

from importer import MULTIPLIERS


def build_plan(db, project):
    """Relax one scope dimension at a time, retaining the project in both rules."""
    cursor = db.execute("""
        SELECT e.id, e.version, e.currency, e.action, e.verdict, s.action AS skill_action
        FROM events e
        LEFT JOIN observations o ON o.event_id = e.id
        LEFT JOIN skills s ON s.observation_id = o.id
        WHERE e.project = ? ORDER BY e.id
    """, (project,))
    columns = [column[0] for column in cursor.description]
    events = [dict(zip(columns, row)) for row in cursor]
    supports = [e for e in events if e["verdict"] == "PASS" and e["skill_action"] == e["action"]]

    candidates = []
    for axis in ("version", "currency"):
        mapping = {e[axis]: e["skill_action"] for e in supports}
        if not mapping or any(action not in MULTIPLIERS for action in mapping.values()):
            continue
        # Conflicting exact skills on a retained value cannot form a function.
        # A FAIL on the predicted action is also a concrete historical refutation.
        fits = all((mapping[e[axis]] == e["action"]) == (e["verdict"] == "PASS")
                   for e in events if e[axis] in mapping)
        if fits:
            candidates.append({"axis": axis, "mapping": mapping})

    seen = {(e["version"], e["currency"]) for e in events}
    versions = sorted({e["version"] for e in supports})
    currencies = sorted({e["currency"] for e in supports})
    alternatives = []
    for version, currency in product(versions, currencies):
        if (version, currency) in seen:
            continue
        scope = {"version": version, "currency": currency}
        predictions = {c["axis"]: c["mapping"][scope[c["axis"]]] for c in candidates}
        disagreement = sum(a != b for a, b in combinations(predictions.values(), 2))
        alternatives.append({**scope, "predictions": predictions, "disagreement": disagreement})

    # max keeps the first maximum. product above enumerates lexical (version, currency) order.
    best = max(alternatives, key=lambda choice: choice["disagreement"], default=None)
    probe = best if best and best["disagreement"] > 0 else None
    if probe:
        reason = (f"{probe['version']}/{probe['currency']} maximizes disagreeing candidate pairs "
                  f"({probe['disagreement']}). Ties use ascending (version, currency).")
    else:
        reason = "No unexecuted combination separates the surviving candidates."
    return {"project": project, "support_event_ids": [e["id"] for e in supports],
            "candidates": candidates, "alternatives": alternatives, "probe": probe, "reason": reason}
