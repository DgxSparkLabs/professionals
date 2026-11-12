"""Audit step D2: usage import from transcripts, attribution, pricing, residual, derived tables, source_meta, store v2 migration.

Fixtures: tests/fixtures/audit/usage (4 unique exchanges, 140/80/220/30 = 250), contracts/events/release_scenario.jsonl, contracts/scenarios/*, pricing fx-2027-01."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

import helpers
import test_audit_capture as TC

LIB = os.path.join(helpers.HERE, "..", "plugins", "professionals", "lib")
sys.path.insert(0, LIB)
from pros_audit import importer as IMP  # noqa: E402
from pros_audit import store as ST  # noqa: E402
from pros_audit import usage as U  # noqa: E402
import audit_schema as sch  # noqa: E402

FX = os.path.join(helpers.HERE, "fixtures", "audit")
TRANS = os.path.join(FX, "usage", "transcripts")
PRICING = [os.path.join(FX, "contracts", "pricing")]
S1, S2, SF = "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002", "00000000-0000-4000-8000-0000000000f1"
T0 = 1800000000.0


def total(r):
    return sum(r)


class Base(TC.Tmp):
    def setUp(self):
        super().setUp()
        self.tdir = os.path.join(self.tmp, "tr")
        os.makedirs(self.tdir)

    def copy_transcripts(self, names=None):
        for f in os.listdir(TRANS):
            if names is None or f[:-6] in names:
                shutil.copy(os.path.join(TRANS, f), self.tdir)

    def load(self, *parts):
        TC.put_spool(self.home, "a.0.jsonl", TC.jl(*parts), age=60)
        return IMP.drain(self.home)

    def usage(self, **kw):
        kw.setdefault("pricing_table", "fx-2027-01")
        kw.setdefault("pricing_dirs", PRICING)
        return U.import_usage(self.home, roots=[self.tdir], **kw)

    def own(self, db):
        return {m: t for m, t in db.execute("SELECT msg, sum(input + output + cache_write_5m + cache_write_1h + cache_read) FROM exchange GROUP BY msg")}

    def write_tr(self, sid, rows):
        with open(os.path.join(self.tdir, sid + ".jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")


def prompt(sid, t, text, meta=False):
    r = {"type": "user", "sessionId": sid, "timestamp": U_ts(t), "message": {"role": "user", "content": text}}
    if meta:
        r["isMeta"] = True
    return r


def U_ts(t):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int(round((t % 1) * 1000))


def asst(sid, t, mid, tokens=10, model="claude-sonnet-4-5", usage=None):
    u = usage or {"input_tokens": tokens, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    return {"type": "assistant", "sessionId": sid, "timestamp": U_ts(t), "requestId": "req_" + mid, "version": "2.1.322",
            "message": {"id": mid, "role": "assistant", "model": model, "content": [{"type": "text", "text": "x"}], "usage": u}}


def hdr(mid, extra=""):
    return "[pro-msg id=%s from=user to=system-designer] %s" % (mid, extra)


class ReleaseTotals(Base):
    def test_release_fixture_totals_and_fork_copy(self):
        self.load("events", "release_scenario.jsonl")
        # the fork copy is a known session of its own (spawned from the original), as in a real fork
        TC.put_spool(self.home, "b.0.jsonl", [{"v": 1, "id": "feedfeedfeedfeed-1", "t": T0 + 5, "action": "spawn", "actor": "system", "origin": "live", "pro": "p-0000000000a1", "pro_name": "system-designer",
                                              "run": "r-0000000000b9", "session": SF, "data": {"kind": "fork", "live_count": 1, "origin": "marketplace", "parent_session": S2, "revision": "111111111111", "scope": "project", "tmux": "pro-system-designer--000002"}}], age=60)
        IMP.drain(self.home)
        self.copy_transcripts()
        r = self.usage()
        self.assertEqual((r["sessions"], r["new"], r["conflicts"]), (3, 4, 0))
        self.assertGreater(r["dup_rows"], 0)
        db = self.db()
        own = self.own(db)
        self.assertEqual((own["m-0000000001"], own["m-0000000005"], own[None], sum(own.values())), (140, 80, 30, 250))
        self.assertEqual(db.execute("SELECT count(*) FROM exchange WHERE session=?", (SF,)).fetchone()[0], 0)  # the copy counts once, under the original
        self.assertEqual(db.execute("SELECT count(*) FROM exchange").fetchone()[0], 4)
        # trace total: root own + child own = 220
        self.assertEqual(own["m-0000000001"] + own["m-0000000005"], 220)
        self.assertEqual(db.execute("SELECT confidence FROM assignment WHERE msg='m-0000000005'").fetchone()[0], "verified")
        self.assertEqual(db.execute("SELECT root, depth FROM assignment WHERE msg='m-0000000005'").fetchone(), ("m-0000000001", 1))
        # pre-window rule: the 30-token exchange is stamped before the first event and is kept, session-only
        t_first = db.execute("SELECT min(t) FROM event WHERE origin='live' AND action != 'audit_import'").fetchone()[0]
        row = db.execute("SELECT t, msg, confidence, overlap, session FROM exchange WHERE msg IS NULL").fetchone()
        self.assertLess(row[0], t_first)
        self.assertEqual((row[1], row[2], row[3], row[4]), (None, "unassigned", 0, S2))
        # priced from the pinned table: fx1 = 50 in, 20 out, 30 read = 459 micro-USD; fx3 unsplit write 20 in 5m
        self.assertEqual(db.execute("SELECT cost_micros, price_state, pricing_table FROM exchange WHERE key='msg_fx1:req_fx1'").fetchone(), (459, "priced", "fx-2027-01"))
        self.assertEqual(db.execute("SELECT cost_micros, cache_write_5m, cache_write_1h, cache_write_unsplit FROM exchange WHERE key='msg_fx3:req_fx3'").fetchone(), (495, 20, 0, 20))
        self.assertEqual(db.execute("SELECT pricing_table, pricing_dated FROM store_state").fetchone(), ("fx-2027-01", "2027-01-01"))

    def test_restart_and_reimport_preserve_totals(self):
        self.load("events", "release_scenario.jsonl")
        self.copy_transcripts()
        self.usage()
        db = self.db()
        snap = lambda: (db.execute("SELECT * FROM exchange ORDER BY key").fetchall(), db.execute("SELECT * FROM header_seen ORDER BY msg").fetchall(),  # noqa: E731
                        db.execute("SELECT * FROM assignment ORDER BY msg").fetchall(), db.execute("SELECT * FROM attempt ORDER BY msg, n").fetchall())
        a = snap()
        self.usage()
        self.assertEqual(snap(), a)
        TC.put_spool(self.home, "z.0.jsonl", TC.jl("events", "release_scenario.jsonl"), age=60)  # a spool replay after a crash
        IMP.drain(self.home)
        self.usage()
        self.assertEqual(snap(), a)
        self.assertEqual(sum(self.own(db).values()), 250)
        self.assertEqual(db.execute("SELECT verified FROM attempt WHERE msg='m-0000000001'").fetchone()[0], 1)

    def test_transcript_gone_after_import_keeps_exchanges(self):
        self.load("events", "release_scenario.jsonl")
        self.copy_transcripts()
        self.usage()
        for f in os.listdir(self.tdir):
            os.unlink(os.path.join(self.tdir, f))
        r = self.usage()
        db = self.db()
        self.assertEqual(sum(self.own(db).values()), 250)
        self.assertEqual(r["missing_transcripts"], 2)
        self.assertEqual(db.execute("SELECT count FROM gap WHERE kind='missing_transcript'").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT confidence FROM assignment WHERE msg='m-0000000001'").fetchone()[0], "verified")  # header_seen outlives the file

    def test_conflict_is_counted_once_and_kept(self):
        self.load("events", "release_scenario.jsonl")
        self.copy_transcripts()
        rows = [json.loads(x) for x in open(os.path.join(TRANS, S2 + ".jsonl"), encoding="utf-8")]
        for r in rows:
            if r.get("requestId") == "req_fx1":
                r = json.loads(json.dumps(r))
                r["message"]["usage"]["output_tokens"] = 99  # the same key again with other usage, in the same file
                self.write_tr(S2, rows + [r])
                break
        r = self.usage()
        db = self.db()
        self.assertEqual(r["conflicts"], 1)
        self.assertEqual(db.execute("SELECT first_total, other_total FROM exchange_conflict").fetchone(), (100, 179))
        self.assertEqual(db.execute("SELECT count FROM gap WHERE kind='id_conflict' AND source='transcript'").fetchone()[0], 1)
        self.assertEqual(self.own(db)["m-0000000001"], 140)  # first wins
        self.usage()
        self.assertEqual(db.execute("SELECT count(*) FROM exchange_conflict").fetchone()[0], 1)

    def test_session_counter_residual_and_source_meta(self):
        self.load("events", "release_scenario.jsonl")
        self.copy_transcripts()
        rows = [json.loads(x) for x in open(os.path.join(TRANS, S2 + ".jsonl"), encoding="utf-8")]
        rows.append({"type": "cost-state", "sessionId": S2, "modelUsage": {"claude-sonnet-4-5": {"inputTokens": 100, "outputTokens": 60, "cacheReadInputTokens": 100, "cacheCreationInputTokens": 40}}, "totalCostUSD": 0.0012})
        self.write_tr(S2, rows)
        self.usage()
        db = self.db()
        self.assertEqual(db.execute("SELECT counter_tokens, transcript_tokens, residual_tokens, counter_cost_micros FROM session_residual WHERE session=?", (S2,)).fetchone(), (300, 170, 130, 1200))
        meta = {r[0]: r[1:] for r in db.execute("SELECT source, rows, from_t, to_t, quality, versions FROM source_meta")}
        self.assertEqual(set(meta), {"emitter", "backfill", "transcript", "session_counter"})
        self.assertEqual(meta["transcript"][0], 4)
        self.assertEqual((meta["transcript"][3], json.loads(meta["transcript"][4])), ("observed", ["2.1.322"]))
        self.assertEqual((meta["session_counter"][0], meta["session_counter"][3]), (1, "observed"))
        self.assertEqual((meta["emitter"][0], meta["backfill"][0], meta["backfill"][3]), (23, 0, "missing"))

    def test_unpriced_without_a_table_and_cache_split(self):
        self.load("events", "release_scenario.jsonl")
        u = {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 3, "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 30}}
        self.write_tr(S2, [prompt(S2, T0 + 1.23, hdr("m-0000000001")), asst(S2, T0 + 2, "mA", usage=u), asst(S2, T0 + 3, "mB", model="mystery-model-1", tokens=7)])
        r = self.usage(pricing_table=None)
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM exchange WHERE price_state='unpriced' AND cost_micros IS NULL").fetchone()[0], 2)
        self.assertEqual(self.own(db)["m-0000000001"], 1 + 2 + 10 + 30 + 3 + 7)  # tokens are counted either way
        r = self.usage()  # a pinned table arrives later: known model priced, unknown still unpriced
        self.assertEqual(db.execute("SELECT cost_micros, cache_write_5m, cache_write_1h, cache_write_unsplit FROM exchange WHERE key='mA:req_mA'").fetchone(), (251, 10, 30, 0))
        self.assertEqual(db.execute("SELECT price_state FROM exchange WHERE key='mB:req_mB'").fetchone()[0], "unpriced")
        self.assertEqual(U.model_rates(U.load_pricing(self.home, "fx-2027-01", PRICING), "claude-sonnet-4-5-20250929")["input"], 3)  # dated suffix maps onto the table

    def test_purge_mark_is_honoured(self):
        self.load("events", "release_scenario.jsonl")
        self.copy_transcripts()
        db = self.db()
        db.execute("INSERT INTO purge_mark (class, purged_before, rows, at) VALUES ('operational', ?, 0, 0)", (T0 + 2.5,))
        r = self.usage()
        self.assertEqual(r["purged_skipped"], 2)  # fx0 and fx1 are older than the mark
        self.assertEqual(db.execute("SELECT count(*) FROM exchange").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT count FROM gap WHERE kind='purged_skipped' AND source='transcript'").fetchone()[0], 2)


class Attribution(Base):
    def test_overlap_is_unassigned_session_level(self):
        self.load("scenarios", "overlap.jsonl")
        self.write_tr(S2, [prompt(S2, T0 + 2.0, hdr("m-0000000001")), asst(S2, T0 + 3, "o1"), prompt(S2, T0 + 4.0, "Stop hook feedback:\n" + hdr("m-0000000002")),
                           asst(S2, T0 + 5, "o2", 20), asst(S2, T0 + 9.5, "o3", 40)])
        self.usage()
        db = self.db()
        got = {k: v for k, *v in db.execute("SELECT key, msg, confidence, overlap FROM exchange")}
        self.assertEqual(got["o1:req_o1"], [ "m-0000000001", "verified", 0])
        self.assertEqual(got["o2:req_o2"], [None, "unassigned", 1])  # m1 still in flight: not attributed, flagged
        self.assertEqual(got["o3:req_o3"], ["m-0000000002", "verified", 0])
        self.assertEqual(db.execute("SELECT sum(overlap) FROM exchange").fetchone()[0], 1)

    def test_mapped_without_header_owns_no_usage(self):
        self.load("scenarios", "mapped_no_header.jsonl")
        self.write_tr(S2, [prompt(S2, T0 + 500.5, "plain prompt"), asst(S2, T0 + 502, "n1")])
        self.usage()
        db = self.db()
        self.assertEqual(db.execute("SELECT confidence FROM assignment WHERE msg='m-00000000c1'").fetchone()[0], "mapped")
        self.assertEqual(db.execute("SELECT msg, confidence FROM exchange").fetchone(), (None, "unassigned"))
        self.assertEqual(db.execute("SELECT verified FROM attempt").fetchone()[0], 0)

    def test_header_in_another_session_or_in_tool_result_is_not_verified(self):
        self.load("scenarios", "mapped_no_header.jsonl")
        tool = {"type": "user", "sessionId": S2, "timestamp": U_ts(T0 + 501), "message": {"role": "user", "content": [{"type": "tool_result", "content": hdr("m-00000000c1")}]}}
        self.write_tr(S2, [tool, asst(S2, T0 + 502, "n1")])
        self.write_tr(S1, [prompt(S1, T0 + 501, hdr("m-00000000c1")), asst(S1, T0 + 502, "n2")])  # the delivery names S2, not S1
        self.usage()
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM header_seen").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM exchange WHERE msg IS NOT NULL").fetchone()[0], 0)

    def test_reply_header_continues_the_task(self):
        self.load("events", "release_scenario.jsonl")
        # m-0000000003 is a reply; its header arriving mid-task does not take over the usage after it
        TC.put_spool(self.home, "c.0.jsonl", [{"v": 1, "id": "abababababababab-1", "t": T0 + 20, "action": "msg_injected", "actor": "pump", "origin": "live", "pro": "p-0000000000a1", "pro_name": "system-designer", "run": "r-0000000000b1",
                                              "session": S2, "msg": "m-0000000003", "data": {"attempt": 1, "dup": False, "tmux": "pro-system-designer--000001", "via": "paste"}}], age=60)
        IMP.drain(self.home)
        self.write_tr(S2, [prompt(S2, T0 + 1.23, hdr("m-0000000001")), asst(S2, T0 + 2, "r1", 10), prompt(S2, T0 + 20, "[pro-msg id=m-0000000003 from=x to=y] reply"), asst(S2, T0 + 21, "r2", 5)])
        self.usage()
        db = self.db()
        self.assertEqual(self.own(db)["m-0000000001"], 15)
        self.assertEqual(db.execute("SELECT msg FROM header_seen ORDER BY msg").fetchall(), [("m-0000000001",), ("m-0000000003",)])


class Derived(Base):
    def test_derived_tables_and_pro_name(self):
        self.load("events", "release_scenario.jsonl")
        self.copy_transcripts()
        self.usage()
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE pro_name IS NOT NULL").fetchone()[0], sum(1 for e in TC.jl("events", "release_scenario.jsonl") if "pro_name" in e))
        self.assertEqual(sorted(db.execute("SELECT name, origin FROM professional").fetchall()), [("reliability-reviewer", "marketplace"), ("system-designer", "marketplace")])
        self.assertEqual(db.execute("SELECT count(*) FROM run").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT run, session, ended IS NOT NULL FROM run WHERE pro='p-0000000000a2'").fetchone(), ("r-0000000000b2", S1, 1))
        self.assertEqual(db.execute("SELECT delivery, confidence, first_delivery IS NOT NULL FROM assignment WHERE msg='m-0000000001'").fetchone(), ("answered", "verified", 1))
        self.assertEqual(db.execute("SELECT count(*) FROM wait").fetchone()[0], 3)
        self.assertEqual(db.execute("SELECT revision FROM revision ORDER BY revision").fetchall(), [("111111111111",), ("222222222222",)])
        g = db.execute("SELECT generation FROM store_state").fetchone()[0]
        self.assertGreater(g, 0)


class StoreV2(TC.Tmp):
    def test_migration_from_v1_keeps_data(self):
        os.makedirs(os.path.join(self.home, "audit"), mode=0o700)
        p = ST.db_path(self.home)
        c = sqlite3.connect(p)
        with open(os.path.join(ST.HERE, "store-v1.sql"), encoding="utf-8") as f:
            c.executescript(f.read() + "INSERT INTO schema_version VALUES (1, 0, 'initial');")
        c.execute("INSERT INTO event (id, payload_sha, t, action, actor, origin, data) VALUES ('0000000000000001-1', 'x', 1, 'msg_read', 'user', 'live', '{}')")
        c.commit()
        c.close()
        db = ST.open_store(self.home)
        self.assertEqual(db.execute("SELECT max(version) FROM schema_version").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT id, pro_name FROM event").fetchall(), [("0000000000000001-1", None)])
        self.assertEqual(db.execute("SELECT count(*) FROM source_meta").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT generation FROM store_state").fetchone()[0], 1)
        db.close()
        self.assertEqual(ST.open_store(self.home).execute("SELECT generation FROM store_state").fetchone()[0], 1)  # the step runs once

    def test_v2_delta_is_the_contract(self):
        with open(os.path.join(sch.SCHEMAS, "store-v2.sql"), encoding="utf-8") as f, open(os.path.join(ST.HERE, "store-v2.sql"), encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read())


if __name__ == "__main__":
    unittest.main()
