"""Audit step B spikes (research/): the reference reader's join and dedup rules, and the golden card values.

These pin what research/B1.md, B2.md and B3.md claim, so step C freezes contracts against tested numbers."""
import json
import os
import shutil
import sys
import tempfile
import unittest

import helpers

sys.path.insert(0, os.path.join(helpers.HERE, "..", "research"))
import audit_reader  # noqa: E402
import metrics_ref  # noqa: E402

FX = os.path.join(helpers.HERE, "fixtures", "audit")


def jload(*parts):
    with open(os.path.join(FX, *parts), encoding="utf-8") as f:
        return json.load(f)


def fixture_result():
    msgs, hist = audit_reader.load_records(fixture=os.path.join(FX, "ask_delegation.json"))
    return msgs, audit_reader.join(msgs, hist, os.path.join(FX, "usage", "transcripts"))


def row(kind, content, **kw):
    r = {"type": kind, "timestamp": kw.pop("ts", "2027-01-15T08:00:00.000Z"), "message": {"role": kind}}
    if kind == "user":
        r["message"]["content"] = content
    else:
        r["message"].update(id=kw.pop("mid"), model="m", usage=kw.pop("usage"))
        r["requestId"] = kw.pop("req")
    r.update(kw)
    return r


def usage(a, b=0, c=0, d=0):
    return {"input_tokens": a, "output_tokens": b, "cache_creation_input_tokens": c, "cache_read_input_tokens": d}


class TestReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="audit-research-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, rows):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))

    def test_fixture_totals(self):
        msgs, res = fixture_result()
        u = res["usage"]
        exp = jload("usage", "expected.json")
        self.assertEqual(u["assignment_own"], {exp["root_message"]: 140, exp["child_message"]: 80})
        self.assertEqual(u["root_inclusive"], {exp["root_message"]: 220})
        self.assertEqual(u["session_only"], {exp["root_session"]: 30})
        self.assertEqual((u["total"], u["exchanges"]), (250, 4))
        cov = res["coverage"]["reader"]
        self.assertEqual((cov["rows"], cov["dup_rows"], cov["conflicts"], cov["no_key"]), (7, 3, 0, 0))
        self.assertNotIn(exp["fork_copy_session"], u["session_only"])  # the fork copy adds nothing
        self.assertEqual(res["coverage"]["join_confidence"], {"verified": 2})

    def test_header_rules(self):
        h = "[pro-msg id=m-00000000a%d from=user to=x task=t kind=task reply-to=user chain=x]\nbody"
        self.write("s1.jsonl", [
            row("user", "Stop hook feedback:\n" + h % 1, isMeta=True),
            row("assistant", None, mid="a1", req="r1", usage=usage(5)),
            row("user", '\n\n<pasted_content id="ab12">\n' + h % 2),
            row("assistant", None, mid="a2", req="r2", usage=usage(7)),
            row("user", [{"type": "tool_result", "content": h % 3}]),
            row("assistant", None, mid="a3", req="r3", usage=usage(11)),
            row("user", "please look at " + h % 4),
            row("assistant", None, mid="a4", req="r4", usage=usage(13))])
        ex, dl, cov = audit_reader.read_usage(self.tmp, {})
        self.assertEqual(sorted(dl), ["m-00000000a1", "m-00000000a2"])  # tool output and quoted text never count
        self.assertEqual([e["msg"] for e in ex], ["m-00000000a1", "m-00000000a2", "m-00000000a2", None])
        self.assertEqual(cov["headers"], {"stop-hook": 1, "paste": 1})
        self.assertEqual(cov["inner_headers"], 1)

    def test_first_wins_across_files_and_conflicts(self):
        self.write("zz-original.jsonl", [row("assistant", None, mid="a1", req="r1", usage=usage(5), ts="2027-01-15T08:00:00Z")])
        self.write("aa-copy.jsonl", [row("assistant", None, mid="a1", req="r1", usage=usage(9), ts="2027-01-15T09:00:00Z"),
                                     row("assistant", None, mid="a2", req=None, usage=usage(2), ts="2027-01-15T09:00:01Z")])
        ex, _, cov = audit_reader.read_usage(self.tmp, {})
        self.assertEqual([(e["sid"], sum(e["u"])) for e in ex], [("zz-original", 5), ("aa-copy", 2)])
        self.assertEqual((cov["dup_rows"], cov["conflicts"], cov["no_key"]), (1, 1, 1))


class TestCards(unittest.TestCase):
    def test_golden_cards(self):
        msgs, res = fixture_result()
        evals = jload("rubrics", "evaluations.json")["evaluations"]
        got = json.loads(json.dumps(metrics_ref.cards(msgs, evals, res["usage"]), sort_keys=True))
        self.assertEqual(got, jload("golden", "cards.json"))
        self.assertEqual(got["assigned_work"], {"assignments": 2, "root_tasks": 1, "child_assignments": 1})
        root = got["evaluation"]["root_tasks"]
        self.assertEqual((root["counts"]["partial"], root["coverage"], root["pass_rate"]), (1, [1, 1], [0, 1]))
        self.assertEqual(got["deadline_miss"]["all"]["rate"], [1, 2])
        self.assertEqual(got["deadline_miss"]["root_tasks"]["late_answers_among_missed"], 1)
        t = got["accounted_tokens"]
        self.assertEqual((t["total_unique"], t["linked_task"], t["session_only"]), (250, 220, 30))

    def test_supersession_and_precedence(self):
        evals = jload("rubrics", "evaluations.json")["evaluations"]
        self.assertEqual(metrics_ref.effective(evals, "m-0000000001")["id"], "e3")
        auto_late = dict(evals[1], id="e4", supersedes=None, effective_at=1800000999.0)
        self.assertEqual(metrics_ref.effective(evals + [auto_late], "m-0000000001")["id"], "e3")  # operator outranks
        self.assertIsNone(metrics_ref.effective(evals, "m-missing"))


if __name__ == "__main__":
    unittest.main()
