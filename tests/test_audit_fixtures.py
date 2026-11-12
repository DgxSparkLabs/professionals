"""Audit step A fixtures (tests/fixtures/audit, made by tests/audit_fixtures.py): shape, scrubbing, the facts the
audit design relies on, an independent usage recount, and a drift check against a fresh run of the runtime."""
import json
import os
import re
import tempfile
import unittest

import audit_fixtures
import helpers

FX = os.path.join(helpers.HERE, "fixtures", "audit")


def load(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return json.load(f)


def msgs(sc):
    return {v["id"]: v for k, v in sc["records"].items() if k.startswith("run/msgs/")}


def transcripts():
    d = os.path.join(FX, "usage", "transcripts")
    out = {}
    for f in sorted(os.listdir(d)):
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            out[f[:-6]] = [json.loads(x) for x in fh if x.strip()]
    return out


def recount(files):
    """Reference rule: dedup on (message.id, requestId); a prompt row with a [pro-msg id=..] header opens that
    assignment's window in its session; a plain prompt closes it; usage outside a window is session-only."""
    seen, by_msg, by_session, raw = set(), {}, {}, 0
    for sid, rows in files.items():
        cur = None
        for r in rows:
            c = r.get("message", {}).get("content")
            if r["type"] == "user" and isinstance(c, str):
                m = re.search(r"\[pro-msg id=(m-[0-9a-f]+)", c)
                cur = m.group(1) if m else None
            if r["type"] != "assistant":
                continue
            raw += 1
            key = (r["message"]["id"], r["requestId"])
            if key in seen:
                continue
            seen.add(key)
            n = sum(r["message"]["usage"][k] for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens"))
            if cur:
                by_msg[cur] = by_msg.get(cur, 0) + n
            else:
                by_session[sid] = by_session.get(sid, 0) + n
    return by_msg, by_session, len(seen), raw


class TestAuditFixtures(unittest.TestCase):
    def setUp(self):
        self.deleg, self.resume, self.exp = load("ask_delegation.json"), load("resume_fork.json"), load("usage/expected.json")

    def all_text(self):
        parts = []
        for base, _, files in os.walk(FX):
            for f in files:
                with open(os.path.join(base, f), encoding="utf-8") as fh:
                    parts.append(fh.read())
        return "\n".join(parts)

    def test_scrubbed(self):
        text = self.all_text()
        for rx, what in [(r"/(home|Users)/[A-Za-z0-9._-]+/", "home path"), (r"/tmp/", "temp path"), (r"protest-", "test dir"),
                         (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "IP"), (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", "e-mail"),
                         (r"\b[0-9a-f]{8}-(?!0000-4000-8000)[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "raw uuid")]:
            self.assertIsNone(re.search(rx, text), "fixture holds a %s" % what)
        for sc in (self.deleg, self.resume):  # every time field was rebased to T0
            for v in re.findall(r'"(?:t|created|deadline|started|added_at)": ([0-9.]+)', json.dumps(sc)):
                self.assertTrue(audit_fixtures.T0 - 1 <= float(v) < audit_fixtures.T0 + 7200, v)

    def test_canaries_planted(self):
        text = self.all_text()
        for c in self.exp["canaries"]:
            self.assertIn(c, text, "export tests need every canary present in the raw fixtures")

    def test_delegation_facts(self):
        ms = msgs(self.deleg)
        root = ms[self.exp["root_message"]]
        child = ms[self.exp["child_message"]]
        self.assertEqual((root["from"], root["to"], root["task"], root["parent"]), ("user", "system-designer", "fx-root", None))
        self.assertEqual(child["parent"], root["id"])  # the only causal link to the root
        self.assertNotEqual(child["task"], root["task"])  # a delegated child gets its own task id
        self.assertEqual(child["chain"], ["system-designer", "reliability-reviewer"])
        self.assertLessEqual(child["deadline"], root["deadline"] - 20)
        self.assertTrue(root["overdue"])
        self.assertEqual(root["state"], "answered")
        self.assertGreater(root["answered_at"], root["deadline"])  # late reply kept, miss not erased
        replies = [m for m in ms.values() if m["kind"] == "reply" and m["parent"] == root["id"]]
        self.assertEqual([m["from"] for m in replies], ["system-designer"])
        cmds = self.deleg["commands"]
        ask = next(c for c in cmds if c["argv"][0] == "ask")
        self.assertEqual(ask["exit"], 3)
        self.assertIn("pending", ask["out"])
        waits = [c for c in cmds if c["argv"][0] == "wait"]
        self.assertEqual([c["exit"] for c in waits], [5, 0])  # T-X1 (0.4.0): the late real reply wins over the timeout note
        self.assertIn("kind=reply", waits[1]["out"])

    def test_resume_fork_lineage(self):
        starts = [r for r in self.resume["records"]["history/alpha.jsonl"] if r["ev"] == "start"]
        self.assertEqual(len(starts), 3)
        first = starts[0]["id"]
        self.assertNotIn("resumed_from", starts[0])
        self.assertEqual((starts[1]["id"], starts[1].get("resumed_from")), (first, first))  # resume keeps the id: runs need their own id
        self.assertEqual(starts[2].get("forked_from"), first)
        self.assertNotEqual(starts[2]["id"], first)

    def test_usage_recount(self):
        files = transcripts()
        by_msg, by_session, unique, raw = recount(files)
        t = self.exp["tokens"]
        self.assertEqual(by_msg.get(self.exp["root_message"]), t["root_task_own"])
        self.assertEqual(by_msg.get(self.exp["child_message"]), t["child_task_own"])
        self.assertEqual(by_msg[self.exp["root_message"]] + by_msg[self.exp["child_message"]], t["trace_total"])
        self.assertEqual(by_session, {self.exp["root_session"]: t["root_session_only"]})  # the fork copy adds nothing
        self.assertEqual(sum(by_msg.values()) + sum(by_session.values()), t["all_unique"])
        self.assertEqual((unique, raw), (self.exp["exchanges"]["unique"], self.exp["exchanges"]["raw_assistant_rows"]))
        self.assertGreater(raw, unique)
        ms = msgs(self.deleg)
        hist = self.deleg["records"]
        self.assertIn(self.exp["root_session"], json.dumps(hist["history/system-designer.jsonl"]))
        self.assertIn(self.exp["root_message"], ms)

    def test_schema_drift(self):
        """A fresh run of the runtime writes no record key the fixtures do not know (else regenerate them)."""
        schema = load("schema.json")
        sc = audit_fixtures.run_scenario(audit_fixtures.resume_fork)
        fresh = audit_fixtures.schema_of([sc])
        for fam, keys in fresh.items():
            known = set(schema.get(fam, []))
            self.assertTrue(set(keys) <= known, "%s: new keys %s; run python3 tests/audit_fixtures.py" % (fam, sorted(set(keys) - known)))

    def test_generator_writes_usage(self):
        with tempfile.TemporaryDirectory() as d:
            files, exp = audit_fixtures.usage_set(self.deleg)
            self.assertEqual(exp, self.exp)
            for name, rows in files.items():
                audit_fixtures.write(d, "usage/" + name, rows)
                with open(os.path.join(FX, "usage", name), encoding="utf-8") as f:
                    self.assertEqual([json.loads(x) for x in f if x.strip()], rows)


if __name__ == "__main__":
    unittest.main()
