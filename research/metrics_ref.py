#!/usr/bin/env python3
"""Audit spike B3: reference computation of the five first-release cards (research/B3.md has the rules).

Reads the step A fixture, the rubric/evaluation fixtures and the usage transcripts; writes nothing.
  python3 research/metrics_ref.py            prints the cards as JSON (the golden file is this output)
The real implementation (step D) must reproduce tests/fixtures/audit/golden/cards.json exactly.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import audit_reader  # noqa: E402

FX = os.path.join(HERE, "..", "tests", "fixtures", "audit")
CONCLUSIVE = ("passed", "partial", "failed")
VOCAB = ("passed", "partial", "failed", "inconclusive", "unreviewed")
WINDOW = (1800000000.0, 1800000100.0)  # [from, to): the selected window; cutoff = its end


def nearest_rank(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = -(-p * len(xs) // 100)
    return xs[max(1, int(k)) - 1]


def dist(xs, eligible):
    return {"n": len(xs), "eligible": eligible, "coverage": [len(xs), eligible],
            "p50_s": None if not xs else round(nearest_rank(xs, 50), 2), "p95_s": None if not xs else round(nearest_rank(xs, 95), 2)}


def effective(evals, target):
    """Latest non-superseded evaluation for a target; operator outranks automated at the same target."""
    gone = {e["supersedes"] for e in evals if e.get("supersedes")}
    live = [e for e in evals if e["target"]["id"] == target and e["id"] not in gone]
    if not live:
        return None
    return sorted(live, key=lambda e: (e["evaluator"]["kind"] == "operator", e["effective_at"]))[-1]


def cards(msgs, evals, usage, window=WINDOW):
    lo, cut = window
    tasks = [m for m in msgs.values() if m["kind"] == "task" and lo <= m["created"] < cut]
    roots = [m for m in tasks if not m.get("parent")]
    kids = [m for m in tasks if m.get("parent")]
    out = {"window": {"from": lo, "to": cut, "cutoff": cut, "event_time": "assignment created"}}
    out["assigned_work"] = {"assignments": len(tasks), "root_tasks": len(roots), "child_assignments": len(kids)}

    def evals_for(group, unit):
        done = [m for m in group if m["state"] == "answered"]
        counts = dict.fromkeys(VOCAB, 0)
        for m in done:
            e = effective(evals, m["id"])
            counts[e["status"] if e else "unreviewed"] += 1
        concl = sum(counts[k] for k in CONCLUSIVE)
        return {"unit": unit, "completed_eligible": len(done), "conclusive": concl, "counts": counts,
                "coverage": [concl, len(done)], "pass_rate": [counts["passed"], concl]}
    out["evaluation"] = {"root_tasks": evals_for(roots, "root task"), "child_assignments": evals_for(kids, "child assignment")}

    def misses(group):
        due = [m for m in group if m["deadline"] <= cut and m["state"] != "cancelled"]
        miss = [m for m in due if not (m.get("answered_at") and m["answered_at"] <= m["deadline"])]
        late = [m for m in miss if m.get("answered_at") and m["answered_at"] <= cut]
        return {"eligible_elapsed": len(due), "missed": len(miss), "late_answers_among_missed": len(late),
                "rate": [len(miss), len(due)], "cancelled": sum(1 for m in group if m["state"] == "cancelled"),
                "not_yet_due": sum(1 for m in group if m["deadline"] > cut)}
    out["deadline_miss"] = {"all": misses(tasks), "root_tasks": misses(roots), "child_assignments": misses(kids),
                            "rule": "deadline vs answered_at; the overdue flag is evidence only"}
    q = [m["injected_at"] - m["created"] for m in tasks if m.get("injected_at")]
    r = [m["answered_at"] - m["injected_at"] for m in tasks if m.get("injected_at") and m.get("answered_at")]
    out["durations"] = {"queue": dist(q, len(tasks)), "reply_latency_from_first_delivery": dist(r, len(tasks)),
                        "active_model_tool_time": dist([], len(tasks)), "percentile": "nearest rank"}
    u = usage
    linked = sum(u["assignment_own"].values())
    sess = sum(u["session_only"].values())
    out["accounted_tokens"] = {"total_unique": u["total"], "exchanges": u["exchanges"], "linked_task": linked,
                               "session_only": sess, "unavailable": None, "by_category": u["by_category"],
                               "root_inclusive": u["root_inclusive"], "unavailable_note": "no session counter in source"}
    return out


def main():
    msgs, hist = audit_reader.load_records(fixture=os.path.join(FX, "ask_delegation.json"))
    with open(os.path.join(FX, "rubrics", "evaluations.json"), encoding="utf-8") as f:
        evals = json.load(f)["evaluations"]
    res = audit_reader.join(msgs, hist, os.path.join(FX, "usage", "transcripts"))
    json.dump(cards(msgs, evals, res["usage"]), sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
