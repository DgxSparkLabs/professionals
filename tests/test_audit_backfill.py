"""Audit step D2: historical backfill from run/msgs/*.json and history/<name>.jsonl (events.md section 4) on the step A fixtures."""
import json
import os
import sys
import unittest

import helpers
import test_audit_capture as TC

LIB = os.path.join(helpers.HERE, "..", "plugins", "professionals", "lib")
sys.path.insert(0, LIB)
from pros_audit import backfill as BF  # noqa: E402
from pros_audit import importer as IMP  # noqa: E402
import audit_schema as sch  # noqa: E402

FX = os.path.join(helpers.HERE, "fixtures", "audit")
EVENT = sch.load("event-v1.json")


class Backfill(TC.Tmp):
    def materialize(self, extra=None):
        with open(os.path.join(FX, "ask_delegation.json"), encoding="utf-8") as f:
            recs = json.load(f)["records"]
        recs.update(extra or {})
        for k, v in recs.items():
            if not (k.startswith("run/msgs/") or k.startswith("history/") or k == "registry.json"):
                continue
            p = os.path.join(self.home, k)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                if k.endswith(".jsonl"):
                    f.write("".join(json.dumps(r) + "\n" for r in v))
                else:
                    json.dump(v, f)

    def test_events_are_valid_marked_and_idempotent(self):
        self.materialize()
        evs, info = BF.build(self.home, set(), set())
        self.assertGreater(len(evs), 10)
        for e in evs:
            self.assertEqual(sch.validate(e, EVENT), [], e["action"])
            self.assertEqual((e["origin"], bool(e["provenance"])), ("backfill", True))
        r = BF.backfill(self.home)
        self.assertEqual((r["events"], r["duplicates"], r["invalid"]), (len(evs), 0, 0))
        self.assertEqual((r["msgs"], r["runs"]), (5, 2))  # five message records, two history runs
        db = self.db()
        n = db.execute("SELECT count(*) FROM event WHERE origin='backfill'").fetchone()[0]
        r2 = BF.backfill(self.home)
        self.assertEqual((r2["events"], r2["duplicates"]), (0, len(evs)))
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE origin='backfill'").fetchone()[0], n)
        # deterministic run id: r- + hash12(history id + ":" + start t); no guessed fields
        row = db.execute("SELECT run, pro_name, session FROM event WHERE action='spawn' AND pro_name='system-designer'").fetchone()
        self.assertEqual(row[0], BF.run_id("00000000-0000-4000-8000-000000000002", 1800000000.08))
        self.assertEqual(db.execute("SELECT provenance FROM event WHERE action='msg_created' LIMIT 1").fetchone()[0], "msgs")
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE action='msg_injected' AND run IS NOT NULL").fetchone()[0], 2)  # the two task deliveries sit in exactly one run each

    def test_live_events_win_and_cutoff_is_the_first_live_event(self):
        self.materialize()
        BF.backfill(self.home)
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM gap WHERE kind='pre_audit'").fetchone()[0], 0)  # no live event yet: no cutoff to name
        TC.put_spool(self.home, "a.0.jsonl", TC.jl("events", "release_scenario.jsonl"), age=60)
        IMP.drain(self.home)
        r = BF.backfill(self.home)
        gap = db.execute("SELECT source, from_t, to_t, count FROM gap WHERE kind='pre_audit'").fetchone()
        self.assertEqual(gap[0], "msgs")
        self.assertEqual(gap[2], db.execute("SELECT min(t) FROM event WHERE origin='live' AND action NOT IN ('audit_import','audit_gap','audit_config')").fetchone()[0])
        self.assertEqual(r["cutoff"], gap[2])
        # the fixture's live events carry the same message ids: the live ones are authoritative, backfill rows of those messages stay as they were (not duplicated per action)
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE action='msg_created' AND msg='m-0000000001'").fetchone()[0], 2)
        self.assertEqual(r["events"], 0)

    def test_purge_mark_and_tombstone_are_not_restored(self):
        self.materialize()
        db = self.db()
        db.execute("INSERT INTO purge_mark (class, purged_before, rows, at) VALUES ('operational', 1800000001.0, 0, 0)")
        db.execute("INSERT INTO event_tombstone (id, removed_at, reason) VALUES (?, 0, 'correction')", (BF._eid("created:m-0000000005"),))
        r = BF.backfill(self.home)
        self.assertGreater(r["purged_skipped"], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE origin='backfill' AND t < 1800000001.0").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE id=?", (BF._eid("created:m-0000000005"),)).fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT kind, source FROM gap WHERE kind='purged_skipped'").fetchone(), ("purged_skipped", "msgs"))
        r2 = BF.backfill(self.home)  # a rerun does not bring them back
        self.assertEqual(r2["events"], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE id=?", (BF._eid("created:m-0000000005"),)).fetchone()[0], 0)

    def test_history_rotation_and_task_counts_ignored(self):
        rows = [{"entry": "alpha", "ev": "start", "id": "00000000-0000-4000-8000-%012d" % i, "t": 1700000000.0 + i, "task_counts": {"answered": 99}} for i in range(500)]
        self.materialize({"history/alpha.jsonl": rows})
        r = BF.backfill(self.home)
        db = self.db()
        self.assertEqual(db.execute("SELECT kind, source, count FROM gap WHERE kind='history_rotation'").fetchone(), ("history_rotation", "history", 500))
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE action='spawn' AND pro_name='alpha'").fetchone()[0], 500)
        self.assertNotIn("99", " ".join(x[0] for x in db.execute("SELECT data FROM event")))

    def test_backfill_feeds_derived_tables(self):
        self.materialize()
        BF.backfill(self.home)
        from pros_audit import usage as U
        U.import_usage(self.home, roots=[os.path.join(self.home, "nowhere")])
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM assignment").fetchone()[0], db.execute("SELECT count(*) FROM event WHERE action='msg_created' AND data LIKE '%\"kind\": \"task\"%'").fetchone()[0])
        self.assertEqual(db.execute("SELECT confidence FROM assignment WHERE msg='m-0000000005'").fetchone()[0], "mapped")  # run known from history, no transcript header
        self.assertEqual(db.execute("SELECT quality FROM source_meta WHERE source='backfill'").fetchone()[0], "reconstructed")


if __name__ == "__main__":
    unittest.main()
