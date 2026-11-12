"""Usage import (metrics.md section 3, events.md section 6): Claude transcripts -> `exchange` rows, attributed by the join-confidence policy.

Which transcripts: every session id that appears in a stored event (spawn, delivery, hook). No time filter: a transcript row stamped before the
first event or before the session's first delivery is still imported with its own timestamp as a session-only exchange (msg NULL). Only the
operational purge mark skips rows (counted in a `purged_skipped` gap). Pre-window rule for queries: a row belongs to a window by its own timestamp.

Dedup: key (message.id, requestId), first wins; candidates ordered original (no parent_session) first, then earliest first-row time, then file name.
A repeated key with other usage keeps the first and writes an exchange_conflict row. Rows without either id get a per-file key and no_key=1.
Attribution: a delivery header row (regex below) in the session the delivery event names starts `verified` ownership; a plain prompt ends it;
a reply or note header continues it. If another task to the same session is in flight at the exchange time, the exchange is unassigned, overlap=1.
Cost: pinned table (pricing/<id>.json), cache writes split 5m/1h; a total without a split is put in 5m and counted in cache_write_unsplit.
The Claude Code session counter (cost-state row) is stored in session_counter and only feeds the residual view. Only counts and ids are stored, never text."""
import calendar
import glob
import json
import os
import re
import time

from . import derive as D
from . import importer as I
from . import store as S

HEADER = re.compile(r'^(?:Stop hook feedback:\n|\s*<pasted_content id="[0-9a-f]+">\n)?\[pro-msg id=(m-[0-9a-f]{10}) ')
HERE = os.path.dirname(os.path.abspath(__file__))
SUFFIX = re.compile(r"(\[[0-9a-z]+\]|-\d{8})$")


def parse_ts(s):
    try:
        base, _, frac = str(s).rstrip("Z").partition(".")
        return calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S")) + (float("0." + frac) if frac else 0.0)
    except (ValueError, TypeError):
        return None


def prompt_text(r):
    if r.get("type") != "user":
        return None
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list) and c and all(isinstance(b, dict) and b.get("type") == "text" for b in c):
        return "".join(b.get("text", "") for b in c)
    return None


# ---- pricing -------------------------------------------------------------------------------------------------------------------------
def load_pricing(home, table_id, extra_dirs=()):
    """-> table dict or None. Search: $PRO_HOME/audit/pricing, the package pricing dir, extra dirs. The file name is the table id."""
    if not table_id or not re.match(r"^[A-Za-z0-9._-]{1,64}$", str(table_id)):
        return None
    for d in [os.path.join(S.audit_dir(home), "pricing"), os.path.join(HERE, "pricing")] + list(extra_dirs):
        try:
            with open(os.path.join(d, table_id + ".json"), encoding="utf-8") as f:
                t = json.load(f)
            if isinstance(t, dict) and isinstance(t.get("models"), dict):
                return t
        except (OSError, ValueError):
            continue
    return None


def model_rates(table, model):
    """Map a transcript model name onto the table: exact, then the name without a date or [1m] suffix, then the table's aliases."""
    if not table or not model:
        return None
    models = table["models"]
    for cand in (model, SUFFIX.sub("", model), (table.get("aliases") or {}).get(model)):
        if cand in models:
            return models[cand]
    return None


def cost_micros(rates, input_, output, w5, w1, read):
    """USD per million tokens x tokens = micro-USD."""
    if not rates:
        return None
    try:
        return int(round(input_ * rates["input"] + output * rates["output"] + w5 * rates["cache_write_5m"] + w1 * rates["cache_write_1h"] + read * rates["cache_read"]))
    except (KeyError, TypeError):
        return None


def split_usage(u):
    """-> (input, output, w5, w1, unsplit, read). A total without a split lands in 5m and in unsplit."""
    g = lambda k: int(u.get(k) or 0)  # noqa: E731
    total_w = g("cache_creation_input_tokens")
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
    if cc and ("ephemeral_5m_input_tokens" in cc or "ephemeral_1h_input_tokens" in cc):
        w5, w1 = int(cc.get("ephemeral_5m_input_tokens") or 0), int(cc.get("ephemeral_1h_input_tokens") or 0)
        un = 0
        if total_w > w5 + w1:
            w5, un = w5 + (total_w - w5 - w1), total_w - w5 - w1
        return g("input_tokens"), g("output_tokens"), w5, w1, un, g("cache_read_input_tokens")
    return g("input_tokens"), g("output_tokens"), total_w, 0, total_w, g("cache_read_input_tokens")


# ---- reading ---------------------------------------------------------------------------------------------------------------------------
def find_transcript(roots, sid):
    for r in roots:
        for p in [os.path.join(r, sid + ".jsonl")] + sorted(glob.glob(os.path.join(r, "*", sid + ".jsonl"))):
            if os.path.isfile(p):
                return p
    return None


def default_roots():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return [os.path.join(base, "projects")]


def first_ts(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if '"timestamp"' in ln:
                    try:
                        return parse_ts(json.loads(ln).get("timestamp")) or 0.0
                    except ValueError:
                        continue
    except OSError:
        pass
    return 0.0


def read_transcript(path, sid, deliver, kinds):
    """Stream one file. deliver: {msg: set(sessions the delivery events name)}; kinds: {msg: kind}.
    -> (exchanges in order, headers [(msg, t, via)], counter or None, stats). Exchange: dict key,t,model,usage,owner,no_key."""
    ex, seen_keys, headers = [], {}, []
    cur, counter, st, n, turn = None, None, {"rows": 0, "dup_rows": 0, "unverified_headers": 0, "versions": set(), "t_min": None, "t_max": None}, 0, 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if '"type":"api-request' in line[:30]:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            if r.get("version"):
                st["versions"].add(str(r["version"]))
            ts = parse_ts(r.get("timestamp"))
            if ts is not None:
                st["t_min"] = ts if st["t_min"] is None else min(st["t_min"], ts)
                st["t_max"] = ts if st["t_max"] is None else max(st["t_max"], ts)
            if r.get("type") == "cost-state" or ("modelUsage" in r and r.get("type") != "assistant"):
                counter = r
                continue
            txt = prompt_text(r)
            if txt is not None:
                h = HEADER.match(txt)
                if h:
                    mid = h.group(1)
                    if sid in deliver.get(mid, ()):
                        via = "stop-hook" if txt.startswith("Stop hook") else "paste"
                        headers.append((mid, ts, via))
                        if kinds.get(mid) == "task":
                            cur = mid
                    else:
                        st["unverified_headers"] += 1
                        cur = None
                elif not r.get("isMeta"):
                    cur = None
                continue
            m = r.get("message") if r.get("type") == "assistant" else None
            if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                continue
            st["rows"] += 1
            key, no_key = "%s:%s" % (m.get("id"), r.get("requestId")), 0
            if not m.get("id") or not r.get("requestId"):
                key, no_key = "nk-%s-%d" % (sid, lineno), 1
            u = split_usage(m["usage"])
            if key in seen_keys:
                st["dup_rows"] += 1
                if seen_keys[key] != u:
                    ex.append({"conflict": key, "u": u})
                continue
            seen_keys[key] = u
            turn += 1
            ex.append({"key": key, "t": ts, "model": m.get("model"), "u": u, "owner": cur, "no_key": no_key, "turn": turn})
    return ex, headers, counter, st


def counter_totals(row):
    mu = row.get("modelUsage") or (row.get("costState") or {}).get("modelUsage") or {}
    tot = 0
    if isinstance(mu, dict):
        for v in mu.values():
            if isinstance(v, dict):
                tot += sum(int(v.get(k) or 0) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
    cost = row.get("totalCostUSD", (row.get("costState") or {}).get("totalCostUSD"))
    return tot, (int(round(float(cost) * 1e6)) if isinstance(cost, (int, float)) else None)


# ---- store side ------------------------------------------------------------------------------------------------------------------------
def _intervals(conn):
    """{session: [(msg, start, end)]} for task messages: start = first delivery into the session, end = answered, failed, or the session's end."""
    ans, fail, send = {}, {}, {}
    for t, a, m, s in conn.execute("SELECT t, action, msg, session FROM event WHERE action IN ('msg_answered', 'msg_failed', 'session_end') ORDER BY t"):
        if a == "msg_answered":
            ans.setdefault(m, t)
        elif a == "msg_failed":
            fail.setdefault(m, t)
        else:
            send.setdefault(s, []).append(t)
    out = {}
    for m, s, t in conn.execute("SELECT a.msg, e.session, min(e.t) FROM assignment a JOIN event e ON e.msg = a.msg AND e.action = 'msg_injected' WHERE e.session IS NOT NULL GROUP BY a.msg, e.session"):
        later = [x for x in send.get(s, []) if x >= t]
        ends = [x for x in (ans.get(m), fail.get(m), min(later) if later else None) if x is not None]
        out.setdefault(s, []).append((m, t, min(ends) if ends else D.INF))
    return out


def _config_table(conn):
    r = conn.execute("SELECT data FROM event WHERE action='audit_config' ORDER BY t DESC, seq DESC").fetchall()
    for (d,) in r:
        d = json.loads(d)
        if d.get("setting") == "pricing_table":
            return d.get("value")
    return None


def _gap_replace(conn, kind, source, f, t, count, batch=None):
    conn.execute("DELETE FROM gap WHERE kind=? AND source=?", (kind, source))
    if count:
        conn.execute("INSERT INTO gap (kind, source, from_t, to_t, count, batch) VALUES (?,?,?,?,?,?)", (kind, source, f, t, count, batch))


def import_usage(home, roots=None, pricing_table=None, pricing_dirs=()):
    """Read the transcripts of every known session and update exchange, header_seen, session_counter, derived tables, source_meta. Idempotent."""
    roots = roots or default_roots()
    with I.import_lock(home) as got:
        if not got:
            return {"skipped": "another import is running"}
        conn = S.open_store(home)
        try:
            return _import(conn, home, roots, pricing_table, pricing_dirs)
        finally:
            conn.close()


def _import(conn, home, roots, pricing_table, pricing_dirs):
    D.rebuild(conn)
    table_id = pricing_table or _config_table(conn)
    table = load_pricing(home, table_id, pricing_dirs)
    kinds = {}
    for m, d in conn.execute("SELECT msg, data FROM event WHERE action='msg_created'"):
        kinds[m] = json.loads(d)["kind"]
    deliver = {}
    for m, s in conn.execute("SELECT DISTINCT msg, session FROM event WHERE action='msg_injected' AND session IS NOT NULL"):
        deliver.setdefault(m, set()).add(s)
    parents = {s: json.loads(d).get("parent_session") for s, d in conn.execute("SELECT session, data FROM event WHERE action='spawn' AND session IS NOT NULL ORDER BY t")}
    sessions = [r[0] for r in conn.execute("SELECT DISTINCT session FROM event WHERE session IS NOT NULL")]
    mark = conn.execute("SELECT purged_before FROM purge_mark WHERE class='operational'").fetchone()
    mark = mark[0] if mark else None
    found, missing = [], []
    for s in sessions:
        p = find_transcript(roots, s)
        (found if p else missing).append((s, p))
    found.sort(key=lambda x: (1 if parents.get(x[0]) else 0, first_ts(x[1]), os.path.basename(x[1])))
    inter = _intervals(conn)
    tot = {"sessions": 0, "rows": 0, "dup_rows": 0, "new": 0, "conflicts": 0, "purged_skipped": 0, "unverified_headers": 0}
    versions, t_lo, t_hi, counters = set(), None, None, 0
    for sid, path in found:
        ex, headers, counter, st = read_transcript(path, sid, deliver, kinds)
        tot["sessions"] += 1
        tot["rows"] += st["rows"]
        tot["dup_rows"] += st["dup_rows"]
        tot["unverified_headers"] += st["unverified_headers"]
        versions |= st["versions"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for mid, t, via in headers:
                conn.execute("INSERT OR IGNORE INTO header_seen (msg, session, t, via) VALUES (?,?,?,?)", (mid, sid, t, via))
            ivs = inter.get(sid, [])
            for e in ex:
                if "conflict" in e:
                    a = conn.execute("SELECT input + output + cache_write_5m + cache_write_1h + cache_read FROM exchange WHERE key=?", (e["conflict"],)).fetchone()
                    first = a[0] if a else 0
                    conn.execute("INSERT OR IGNORE INTO exchange_conflict (key, other_session, first_total, other_total, seen) VALUES (?,?,?,?,?)",
                                 (e["conflict"], sid, first, e["u"][0] + e["u"][1] + e["u"][2] + e["u"][3] + e["u"][5], time.time()))
                    continue
                i, o, w5, w1, un, rd = e["u"]
                t = e["t"]
                if mark is not None and t is not None and t < mark:
                    tot["purged_skipped"] += 1
                    continue
                msg, conf, ov = None, "unassigned", 0
                if e["owner"]:
                    if t is not None and any(m2 != e["owner"] and s2 <= t <= e2 for m2, s2, e2 in ivs):
                        ov = 1
                    else:
                        msg, conf = e["owner"], "verified"
                rates = model_rates(table, e["model"])
                cost = cost_micros(rates, i, o, w5, w1, rd)
                state = "priced" if cost is not None else "unpriced"
                have = conn.execute("SELECT session, input, output, cache_write_5m, cache_write_1h, cache_read FROM exchange WHERE key=?", (e["key"],)).fetchone()
                if have is None:
                    conn.execute("INSERT INTO exchange (key, session, msg, confidence, overlap, model, t, input, output, cache_write_5m, cache_write_1h, cache_write_unsplit, cache_read, turn, cost_micros, price_state, pricing_table, no_key, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'transcripts')",
                                 (e["key"], sid, msg, conf, ov, e["model"], t, i, o, w5, w1, un, rd, e["turn"], cost, state, table_id if table else None, e["no_key"]))
                    tot["new"] += 1
                elif have[0] == sid:
                    conn.execute("UPDATE exchange SET msg=?, confidence=?, overlap=?, turn=?, cost_micros=?, price_state=?, pricing_table=? WHERE key=?", (msg, conf, ov, e["turn"], cost, state, table_id if table else None, e["key"]))
                else:
                    # a copy in another session: the first one stays the owner
                    if have[1:] != (i, o, w5, w1, rd):
                        conn.execute("INSERT OR IGNORE INTO exchange_conflict (key, other_session, first_total, other_total, seen) VALUES (?,?,?,?,?)", (e["key"], sid, sum(have[1:]), i + o + w5 + w1 + rd, time.time()))
            if counter is not None:
                total, cost = counter_totals(counter)
                conn.execute("INSERT OR REPLACE INTO session_counter (session, total_tokens, cost_micros, read_at) VALUES (?,?,?,?)", (sid, total, cost, time.time()))
                counters += 1
            conn.execute("UPDATE store_state SET generation = generation + 1")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    conn.execute("BEGIN IMMEDIATE")
    nconf = conn.execute("SELECT count(*) FROM exchange_conflict").fetchone()[0]
    _gap_replace(conn, "id_conflict", "transcript", None, None, nconf)
    _gap_replace(conn, "missing_transcript", "transcript", None, None, len(missing))
    _gap_replace(conn, "purged_skipped", "transcript", None, mark, tot["purged_skipped"])
    conn.execute("UPDATE store_state SET pricing_table=?, pricing_dated=? WHERE id=1", (table_id if table else None, (table or {}).get("dated")))
    conn.execute("UPDATE exchange SET cost_micros=NULL, price_state='unpriced', pricing_table=NULL WHERE ? IS NULL AND price_state='priced'", (table_id if table else None,))
    conn.execute("UPDATE store_state SET generation = generation + 1")
    conn.execute("COMMIT")
    _reprice(conn, table, table_id)
    D.rebuild(conn)  # confidence and attempt.verified now see the new header_seen rows
    refresh_source_meta(conn, versions)
    tot.update(conflicts=nconf, missing_transcripts=len(missing), pricing_table=table_id if table else None)
    return tot


def _reprice(conn, table, table_id):
    if not table:
        return
    rows = conn.execute("SELECT key, model, input, output, cache_write_5m, cache_write_1h, cache_read FROM exchange WHERE pricing_table IS NOT ? OR price_state='unpriced'", (table_id,)).fetchall()
    if not rows:
        return
    conn.execute("BEGIN IMMEDIATE")
    for k, model, i, o, w5, w1, rd in rows:
        c = cost_micros(model_rates(table, model), i, o, w5, w1, rd)
        conn.execute("UPDATE exchange SET cost_micros=?, price_state=?, pricing_table=? WHERE key=?", (c, "priced" if c is not None else "unpriced", table_id if c is not None else None, k))
    conn.execute("UPDATE store_state SET generation = generation + 1")
    conn.execute("COMMIT")


def refresh_source_meta(conn, versions=()):
    """source_meta: rows, time range, tool versions and quality per source (the coverage block of every query answer)."""
    now = time.time()
    old = {}
    for s, v in conn.execute("SELECT source, versions FROM source_meta"):
        old[s] = set(json.loads(v))
    conn.execute("BEGIN IMMEDIATE")
    try:
        def put(src, n, lo, hi, quality, vers=()):
            conn.execute("INSERT OR REPLACE INTO source_meta (source, rows, from_t, to_t, versions, quality, updated) VALUES (?,?,?,?,?,?,?)", (src, n, lo, hi, json.dumps(sorted(old.get(src, set()) | set(vers))), quality, now))
        n, lo, hi = conn.execute("SELECT count(*), min(t), max(t) FROM event WHERE origin='live' AND action NOT LIKE 'audit_%'").fetchone()
        put("emitter", n, lo, hi, "observed" if n else "missing")
        n, lo, hi = conn.execute("SELECT count(*), min(t), max(t) FROM event WHERE origin='backfill'").fetchone()
        put("backfill", n, lo, hi, "reconstructed" if n else "missing")
        n, lo, hi = conn.execute("SELECT count(*), min(t), max(t) FROM exchange WHERE source='transcripts'").fetchone()
        miss = conn.execute("SELECT coalesce(sum(count), 0) FROM gap WHERE kind='missing_transcript'").fetchone()[0]
        put("transcript", n, lo, hi, ("partial" if miss else "observed") if n else "missing", versions)
        n, lo, hi = conn.execute("SELECT count(*), min(read_at), max(read_at) FROM session_counter").fetchone()
        put("session_counter", n, lo, hi, "observed" if n else "missing", versions if n else ())
        conn.execute("UPDATE store_state SET generation = generation + 1")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
