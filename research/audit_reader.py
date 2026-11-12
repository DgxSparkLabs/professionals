#!/usr/bin/env python3
"""Audit research spike (steps B1 and B2): a stdlib reference reader for joins and usage.

Not shipped code. It answers two questions with exact numbers so step C can freeze contracts:
  B1  how a task message (assignment) joins to a Claude session, a run and the model exchanges it caused
  B2  how many tokens each scope consumed, counted once per exchange

Inputs: a records dict like the step A fixtures ({"records": {relative path: content}}) or a PRO_HOME
directory, plus a directory of Claude transcripts (<dir>/<project>/<session>.jsonl or <dir>/<session>.jsonl).
Output: JSON with joins, usage per scope, coverage counters. Prompt text, tool data and paths never leave it.

  python3 research/audit_reader.py --fixture tests/fixtures/audit/ask_delegation.json \
      --transcripts tests/fixtures/audit/usage/transcripts
  python3 research/audit_reader.py --pro-home <dir> --transcripts <dir> --counts-only
"""
import argparse
import glob
import json
import os
import re
import sys

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# A delivery prompt starts with the header, optionally after the stop-hook prefix or a paste wrapper.
HEADER = re.compile(r'^(?:Stop hook feedback:\n|\s*<pasted_content id="[0-9a-f]+">\n)?\[pro-msg id=(m-[0-9a-f]+)')
ANY_HEADER = re.compile(r"\[pro-msg id=(m-[0-9a-f]+)")


def load_records(fixture=None, pro_home=None):
    """-> (msgs by id, history rows by name)."""
    msgs, hist = {}, {}
    if fixture:
        with open(fixture, encoding="utf-8") as f:
            recs = json.load(f)["records"]
        for k, v in recs.items():
            if k.startswith("run/msgs/") and isinstance(v, dict):
                msgs[v["id"]] = v
            elif k.startswith("history/"):
                hist[k[8:-6]] = v
        return msgs, hist
    for p in glob.glob(os.path.join(pro_home, "run", "msgs", "m-*.json")):
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        msgs[m["id"]] = m
    for p in glob.glob(os.path.join(pro_home, "history", "*.jsonl")):
        with open(p, encoding="utf-8") as f:
            hist[os.path.basename(p)[:-6]] = [json.loads(x) for x in f if x.strip()]
    return msgs, hist


def runs_from_history(hist):
    """One run per start row: (name, session id, start, end or None, mode)."""
    out = []
    for name, rows in hist.items():
        open_runs = []
        for r in rows:
            if r.get("ev") == "start":
                mode = "fork" if r.get("forked_from") else ("resume" if r.get("resumed_from") else "fresh")
                run = {"name": name, "sid": r["id"], "start": r["t"], "end": None, "mode": mode}
                open_runs.append(run)
                out.append(run)
            elif r.get("ev") == "end":
                for run in reversed(open_runs):
                    if run["sid"] == r["id"] and run["end"] is None:
                        run["end"] = r["t"]
                        break
    return out


def root_of(m, msgs):
    """Walk parent message ids; -> (root id, depth, broken link or None)."""
    seen, cur, depth = set(), m, 0
    while cur.get("parent"):
        if cur["parent"] in seen or cur["parent"] not in msgs:
            return cur["id"], depth, cur["parent"]
        seen.add(cur["id"])
        cur, depth = msgs[cur["parent"]], depth + 1
    return cur["id"], depth, None


def prompt_text(r):
    """Text of a prompt row (string or text blocks); None for tool results and non-user rows."""
    if r.get("type") != "user":
        return None
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list) and c and all(isinstance(b, dict) and b.get("type") == "text" for b in c):
        return "".join(b.get("text", "") for b in c)
    return None


def transcript_files(tdir):
    files = glob.glob(os.path.join(tdir, "*.jsonl")) + glob.glob(os.path.join(tdir, "*", "*.jsonl"))
    out = []
    for p in files:
        first = ""
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"timestamp"' in line:
                    first = json.loads(line).get("timestamp", "")
                    break
        out.append((first, p))
    return [p for _, p in sorted(out)]  # earliest file first: the original wins over a fork copy


def read_usage(tdir, msgs):
    """Walk transcripts; dedup exchanges first-wins on (message.id, requestId); attribute each new exchange."""
    seen, ex, cov = {}, [], {"rows": 0, "dup_rows": 0, "conflicts": 0, "no_key": 0, "headers": {}, "inner_headers": 0}
    deliveries = {}
    for p in transcript_files(tdir):
        sid, cur, via = os.path.basename(p)[:-6], None, None
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"type":"api-request' in line[:30]:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                t = prompt_text(r)
                if t is not None:
                    h = HEADER.match(t)
                    if h:
                        cur, via = h.group(1), "stop-hook" if t.startswith("Stop hook") else "paste"
                        deliveries.setdefault(cur, []).append({"sid": sid, "via": via, "t": r.get("timestamp")})
                        cov["headers"][via] = cov["headers"].get(via, 0) + 1
                    elif not r.get("isMeta"):
                        cov["inner_headers"] += 1 if ANY_HEADER.search(t) else 0
                        cur, via = None, None  # a plain prompt (manual or session start): session-level usage
                    continue
                m = r.get("message") if r.get("type") == "assistant" else None
                if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                    continue
                cov["rows"] += 1
                u = tuple(int(m["usage"].get(k) or 0) for k in USAGE_KEYS)
                key = (m.get("id"), r.get("requestId"))
                if not key[0] or not key[1]:
                    cov["no_key"] += 1
                    key = (p, cov["rows"])
                if key in seen:
                    cov["dup_rows"] += 1
                    cov["conflicts"] += 1 if seen[key] != u else 0
                    continue
                seen[key] = u
                ex.append({"sid": sid, "msg": cur, "via": via, "model": m.get("model"), "u": u, "t": r.get("timestamp")})
    return ex, deliveries, cov


def join(msgs, hist, tdir):
    runs = runs_from_history(hist)
    ex, deliveries, cov = read_usage(tdir, msgs) if tdir else ([], {}, {})
    out = {"assignments": {}, "coverage": {}, "usage": {}}
    jc = {}
    for mid, m in sorted(msgs.items()):
        if m["kind"] != "task":
            continue
        root, depth, broken = root_of(m, msgs)
        t = m.get("injected_at")
        cand = [r for r in runs if r["name"] == m["to"] and t and r["start"] - 1 <= t <= (r["end"] or 9e12) + 1]
        win = sorted({r["sid"] for r in cand})
        seen_in = sorted({d["sid"] for d in deliveries.get(mid, [])})
        if seen_in and (not win or set(seen_in) & set(win)):
            conf = "verified"
        elif len(win) == 1:
            conf = "mapped"
        else:
            conf = "unassigned"
        jc[conf] = jc.get(conf, 0) + 1
        out["assignments"][mid] = {"task": m["task"], "root": root, "depth": depth, "broken_parent": broken,
                                   "to": m["to"], "state": m["state"], "via": m.get("via"), "dup": bool(m.get("dup")),
                                   "window_sessions": win, "transcript_sessions": seen_in,
                                   "deliveries_seen": len(deliveries.get(mid, [])), "confidence": conf}
    out["coverage"]["join_confidence"] = jc
    out["coverage"]["reader"] = cov
    own, sess, cats = {}, {}, [0, 0, 0, 0]
    for e in ex:
        n = sum(e["u"])
        cats = [a + b for a, b in zip(cats, e["u"])]
        if e["msg"] in out["assignments"]:
            own[e["msg"]] = own.get(e["msg"], 0) + n
        else:
            sess[e["sid"]] = sess.get(e["sid"], 0) + n
    inclusive = {}
    for mid, n in own.items():
        root = out["assignments"][mid]["root"]
        inclusive[root] = inclusive.get(root, 0) + n
    out["usage"] = {"exchanges": len(ex), "by_category": dict(zip(USAGE_KEYS, cats)), "total": sum(cats),
                    "assignment_own": own, "root_inclusive": inclusive, "session_only": sess}
    return out


def counts_only(res):
    """Drop ids: only the counters, for private data."""
    a = res["assignments"].values()
    hist = {}
    for x in a:
        k = "%s/%s/seen=%d/win=%d" % (x["via"], x["confidence"], x["deliveries_seen"], len(x["window_sessions"]))
        hist[k] = hist.get(k, 0) + 1
    u = res["usage"]
    return {"assignments": len(res["assignments"]), "by_via_confidence": hist, "coverage": res["coverage"],
            "usage": {"exchanges": u["exchanges"], "total": u["total"], "by_category": u["by_category"],
                      "assigned": sum(u["assignment_own"].values()), "session_only": sum(u["session_only"].values())}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture")
    ap.add_argument("--pro-home")
    ap.add_argument("--transcripts")
    ap.add_argument("--counts-only", action="store_true")
    a = ap.parse_args(argv)
    msgs, hist = load_records(a.fixture, a.pro_home)
    res = join(msgs, hist, a.transcripts)
    json.dump(counts_only(res) if a.counts_only else res, sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
