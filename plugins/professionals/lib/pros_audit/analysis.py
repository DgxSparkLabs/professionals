"""Task-class rubrics, benchmark datasets and optional statsmodels workflows (spec 037 F3).

Task classes are an operator mapping, stored as `audit/taskclasses/<class>.json` (class -> rubric name + version). The store records no
class on an assignment (events.md), so a class is derived: the rubric named by an assignment's effective verdict. Benchmark datasets are
local JSON files of gold cases (expected criterion outcomes per case id); scoring compares recorded or supplied outcomes with them and
never sends anything anywhere. Stratified summaries are plain counting with Wilson intervals. The GEE workflow needs statsmodels and
numpy: both are imported inside the function, never at module level, and a missing install is a normal, reported answer. Core stays
standard library only."""
import json
import math
import os
import re
import time

from . import evaluation as ev

CLASS_RE = ev.IDENT_RE
DATASET_FORMAT = "pros-audit-benchmark"
STRATA = ("task_class", "professional", "revision", "direct")
MIN_CLUSTERS = 10


class AnalysisError(Exception):
    def __init__(self, msg, code="bad_request"):
        Exception.__init__(self, msg)
        self.code = code


# --------------------------------------------------------------------------- task-class rubrics
def class_dir(home):
    return os.path.join(str(home), "audit", "taskclasses")


def class_add(home, name, rubric, version, criteria=None):
    """Map a task class to a rubric version. With `criteria` [(id, required)] the rubric version is created too (versions are immutable)."""
    if not CLASS_RE.match(name or ""):
        raise AnalysisError("task class name has an invalid form")
    try:
        if criteria:
            ev.rubric_add(home, rubric, version, criteria)
        ev.rubric_load(home, rubric, version)
    except ev.EvalError as e:
        raise AnalysisError(str(e))
    d = class_dir(home)
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, name + ".json")
    if os.path.exists(path):
        raise AnalysisError("task class %s exists; remove its file to remap" % name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"class": name, "rubric": rubric, "version": version}, sort_keys=True))
    return path


def classes(home):
    out = {}
    try:
        names = sorted(os.listdir(class_dir(home)))
    except OSError:
        return out
    for f in names:
        if f.endswith(".json"):
            try:
                with open(os.path.join(class_dir(home), f), encoding="utf-8") as fh:
                    d = json.load(fh)
                out[d["class"]] = d
            except (OSError, ValueError, KeyError):
                continue
    return out


def classify(conn, home, cutoff=None):
    """{assignment msg: task class} from the rubric of each target's effective verdict; unmatched targets are absent (= unclassified)."""
    by_rubric = {}
    for c in classes(home).values():
        by_rubric.setdefault(c["rubric"], c["class"])
    out = {}
    for (t,) in conn.execute("SELECT DISTINCT target FROM evaluation").fetchall():
        best, _ = ev.effective(ev.evaluations(conn, t), cutoff)
        if best and best["rubric"] in by_rubric:
            out[t] = by_rubric[best["rubric"]]
    return out


# --------------------------------------------------------------------------- stratified summaries
def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round((c - r) / d, 4), round((c + r) / d, 4)]


def outcome_rows(conn, home, since=None, until=None, cutoff=None):
    """One row per assignment answered in the window with its effective status and strata values."""
    cut = time.time() if cutoff is None else cutoff
    cls = classify(conn, home, cut)
    rows = []
    q = ("SELECT a.msg, a.parent, a.to_name, a.answered_at, r.revision FROM assignment a LEFT JOIN run r ON r.run = a.run "
         "WHERE a.delivery = 'answered' AND a.answered_at IS NOT NULL AND a.answered_at <= ?")
    for msg, parent, name, ans, rev in conn.execute(q, (cut,)).fetchall():
        if (since is not None and ans < since) or (until is not None and ans >= until):
            continue
        best, _ = ev.effective(ev.evaluations(conn, msg), cut)
        rows.append({"msg": msg, "status": best["status"] if best else "unreviewed", "task_class": cls.get(msg, "unclassified"), "professional": name,
                     "revision": rev or "unknown", "direct": "delegated" if parent else "direct", "root": parent is None})
    return rows


def stratified(conn, home, by, since=None, until=None, cutoff=None, roots_only=False):
    """Counts per stratum with coverage, pass rate (passed / conclusive) and a Wilson interval. Groups under n=5 are marked."""
    if by not in STRATA:
        raise AnalysisError("unknown stratum %r (use %s)" % (by, ", ".join(STRATA)))
    groups = {}
    for r in outcome_rows(conn, home, since, until, cutoff):
        if roots_only and not r["root"]:
            continue
        groups.setdefault(r[by], []).append(r["status"])
    out = []
    for k in sorted(groups):
        st = groups[k]
        counts = {s: st.count(s) for s in ev.STATUSES}
        concl = counts["passed"] + counts["partial"] + counts["failed"]
        out.append({"stratum": k, "n": len(st), "small_sample": len(st) < 5, "counts": counts, "coverage": [concl, len(st)],
                    "pass_rate": [counts["passed"], concl] if concl else None, "pass_ci95": wilson(counts["passed"], concl)})
    return {"by": by, "rule": "pass rate = passed / conclusive (partial and failed count against); Wilson 95% interval; observational", "strata": out}


# --------------------------------------------------------------------------- benchmark datasets
def validate_dataset(ds, home=None):
    """Problems as strings (empty = valid). Format: {format, version 1, name, task_class, rubric, rubric_version, cases:[{id, expected:{criterion: outcome}, status?}]}."""
    bad = []
    if not isinstance(ds, dict) or ds.get("format") != DATASET_FORMAT or ds.get("version") != 1:
        return ["not a %s version 1 file" % DATASET_FORMAT]
    for k in ("name", "task_class", "rubric", "rubric_version"):
        if not isinstance(ds.get(k), str) or not ev.IDENT_RE.match(ds[k]):
            bad.append("%s missing or malformed" % k)
    cases = ds.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > 5000:
        return bad + ["cases must be a list of 1 to 5000 items"]
    rub = None
    if home is not None and not bad:
        try:
            rub = ev.rubric_load(home, ds["rubric"], ds["rubric_version"])
        except ev.EvalError as e:
            bad.append(str(e))
    required = [c["id"] for c in rub["criteria"] if c["required"]] if rub else None
    seen = set()
    for i, c in enumerate(cases):
        w = "case %d" % i
        if not isinstance(c, dict) or set(c) - {"id", "expected", "status", "ref"}:
            bad.append(w + ": unknown or missing fields")
            continue
        if not isinstance(c.get("id"), str) or not ev.IDENT_RE.match(c["id"]) or c["id"] in seen:
            bad.append(w + ": id missing, malformed or repeated")
        seen.add(c.get("id"))
        exp = c.get("expected")
        if not isinstance(exp, dict) or not exp or any(not isinstance(k, str) or v not in ev.OUTCOMES for k, v in exp.items()):
            bad.append(w + ": expected must map criterion ids to passed, failed or not_assessed")
            continue
        if rub:
            ids = {x["id"] for x in rub["criteria"]}
            if set(exp) - ids:
                bad.append(w + ": criteria not in the rubric: %s" % ", ".join(sorted(set(exp) - ids)))
            elif c.get("status") and c["status"] != ev.derive_status(exp, required):
                bad.append(w + ": status %s contradicts its criteria" % c["status"])
        if "ref" in c and (not isinstance(c["ref"], str) or not ev.REF_RE.match(c["ref"])):
            bad.append(w + ": ref must be an opaque kind:token")
    return bad


def load_dataset(path, home=None):
    try:
        with open(path, encoding="utf-8") as f:
            ds = json.load(f)
    except (OSError, ValueError) as e:
        raise AnalysisError("cannot read dataset: %s" % type(e).__name__, "not_found" if isinstance(e, OSError) else "bad_request")
    bad = validate_dataset(ds, home)
    if bad:
        raise AnalysisError("invalid dataset: " + "; ".join(bad[:5]))
    return ds


def score_dataset(ds, results, home=None):
    """Compare `results` {case id: {criterion: outcome}} with the gold cases: per-case exact agreement, per-criterion agreement, status agreement."""
    required = None
    if home is not None:
        rub = ev.rubric_load(home, ds["rubric"], ds["rubric_version"])
        required = [c["id"] for c in rub["criteria"] if c["required"]]
    per, crit_ok, crit_n, status_ok, status_n = [], 0, 0, 0, 0
    missing = []
    for c in ds["cases"]:
        got = results.get(c["id"])
        if got is None:
            missing.append(c["id"])
            continue
        agree = {k: got.get(k, "not_assessed") == v for k, v in c["expected"].items()}
        crit_ok, crit_n = crit_ok + sum(agree.values()), crit_n + len(agree)
        row = {"id": c["id"], "agree": all(agree.values()), "differs": sorted(k for k, v in agree.items() if not v)}
        if required:
            es, gs = ev.derive_status(c["expected"], required), ev.derive_status(got, required)
            row["status"], status_n, status_ok = [es, gs], status_n + 1, status_ok + (es == gs)
        per.append(row)
    return {"dataset": ds["name"], "task_class": ds["task_class"], "cases": len(ds["cases"]), "scored": len(per), "missing": missing,
            "case_agreement": [sum(1 for r in per if r["agree"]), len(per)], "criterion_agreement": [crit_ok, crit_n],
            "status_agreement": [status_ok, status_n] if required else None, "per_case": per}


# --------------------------------------------------------------------------- optional statsmodels (GEE)
def statsmodels_available():
    try:
        import statsmodels.api  # noqa: F401
        import numpy  # noqa: F401
    except Exception:
        return False
    return True


def gee_frame(conn, home, cluster_by="professional", exposure="revision", since=None, until=None, cutoff=None):
    """Pure-Python data prep: conclusive outcomes as y (1 passed, 0 partial/failed), the exposure stratum and the cluster id."""
    if cluster_by not in STRATA or exposure not in STRATA:
        raise AnalysisError("cluster_by and exposure must be one of %s" % ", ".join(STRATA))
    rows = [r for r in outcome_rows(conn, home, since, until, cutoff) if r["status"] in ("passed", "partial", "failed")]
    return [{"y": 1 if r["status"] == "passed" else 0, "x": r[exposure], "cluster": r[cluster_by]} for r in rows]


def gee_pass(conn, home, cluster_by="professional", exposure="revision", since=None, until=None, cutoff=None, min_clusters=MIN_CLUSTERS):
    """Pass probability by exposure with outcomes clustered (GEE, binomial, exchangeable). Returns {"ok": False, "reason": ...} when it cannot run:
    statsmodels missing, too few clusters, or no variation in the exposure. Never raises for those."""
    data = gee_frame(conn, home, cluster_by, exposure, since, until, cutoff)
    clusters, levels = sorted({d["cluster"] for d in data}), sorted({d["x"] for d in data})
    base = {"n": len(data), "clusters": len(clusters), "levels": levels, "cluster_by": cluster_by, "exposure": exposure}
    if not statsmodels_available():
        return dict(base, ok=False, reason="statsmodels is not installed (optional; pip install statsmodels)")
    if len(clusters) < min_clusters:
        return dict(base, ok=False, reason="only %d cluster(s); need at least %d for robust GEE errors" % (len(clusters), min_clusters))
    if len(levels) < 2:
        return dict(base, ok=False, reason="the exposure has a single level")
    import numpy as np
    import statsmodels.api as sm
    X = np.array([[1.0] + [1.0 if d["x"] == lv else 0.0 for lv in levels[1:]] for d in data])
    y = np.array([float(d["y"]) for d in data])
    g = np.array([clusters.index(d["cluster"]) for d in data])
    try:
        res = sm.GEE(y, X, groups=g, family=sm.families.Binomial(), cov_struct=sm.cov_struct.Exchangeable()).fit()
    except Exception as e:  # separation, singular design: reported, not raised
        return dict(base, ok=False, reason="fit failed: %s" % type(e).__name__)
    names = ["intercept (%s)" % levels[0]] + ["%s vs %s" % (lv, levels[0]) for lv in levels[1:]]
    return dict(base, ok=True, family="binomial/logit", cov_struct="exchangeable", se="robust",
                terms=[{"term": n, "coef": round(float(b), 4), "se": round(float(s), 4), "p": round(float(p), 4)} for n, b, s, p in zip(names, res.params, res.bse, res.pvalues)],
                note="observational association, not a causal effect")


# --------------------------------------------------------------------------- CLI
def add_parsers(sub):
    a = sub.add_parser("analyze", help="task classes, stratified summaries, benchmark datasets, optional GEE (owner only)")
    s = a.add_subparsers(dest="ansub", required=True)
    c = s.add_parser("class-add", help="map a task class to a rubric version (--criterion creates the rubric version)")
    c.add_argument("name")
    c.add_argument("--rubric", required=True)
    c.add_argument("--rubric-version", required=True)
    c.add_argument("--criterion", action="append", default=[], help="ID or ID:optional (repeat); creates the rubric version")
    s.add_parser("class-list").add_argument("--json", action="store_true")
    for n in ("strata", "gee"):
        p = s.add_parser(n)
        p.add_argument("--since", default="30d")
        p.add_argument("--until")
        p.add_argument("--json", action="store_true")
        if n == "strata":
            p.add_argument("--by", choices=STRATA, default="task_class")
            p.add_argument("--roots-only", action="store_true")
        else:
            p.add_argument("--cluster-by", choices=STRATA, default="professional")
            p.add_argument("--exposure", choices=STRATA, default="revision")
            p.add_argument("--min-clusters", type=int, default=MIN_CLUSTERS)
    b = s.add_parser("benchmark-check", help="validate a benchmark dataset file")
    b.add_argument("file")
    b = s.add_parser("benchmark-score", help="score supplied outcomes (JSON {case id: {criterion: outcome}}) against a dataset")
    b.add_argument("file")
    b.add_argument("results")
    b.add_argument("--json", action="store_true")


def run_cli(a, home, out, err, now=None):
    from . import cli, export as ex
    now = time.time() if now is None else now
    try:
        s = a.ansub
        if s == "class-add":
            crit = [(c.split(":")[0], not c.endswith(":optional")) for c in a.criterion] or None
            class_add(home, a.name, a.rubric, a.rubric_version, crit)
            print("task class %s -> rubric %s %s" % (a.name, a.rubric, a.rubric_version), file=out)
        elif s == "class-list":
            cs = classes(home)
            print(json.dumps(cs, sort_keys=True) if a.json else "\n".join("%s -> %s %s" % (c["class"], c["rubric"], c["version"]) for c in cs.values()), file=out)
        elif s == "benchmark-check":
            ds = load_dataset(a.file, home)
            print("dataset %s ok: %d case(s), class %s, rubric %s %s" % (ds["name"], len(ds["cases"]), ds["task_class"], ds["rubric"], ds["rubric_version"]), file=out)
        elif s == "benchmark-score":
            ds = load_dataset(a.file, home)
            with open(a.results, encoding="utf-8") as f:
                res = score_dataset(ds, json.load(f), home)
            print(json.dumps(res, sort_keys=True) if a.json else "scored %d of %d; case agreement %d/%d, criterion agreement %d/%d, missing %d" % (
                res["scored"], res["cases"], *res["case_agreement"], *res["criterion_agreement"], len(res["missing"])), file=out)
        else:
            conn = cli.open_db(home)
            try:
                since, until = ex.parse_when(a.since, now), ex.parse_when(a.until, now)
                if s == "strata":
                    r = stratified(conn, home, a.by, since, until, now, a.roots_only)
                    if a.json:
                        print(json.dumps(r, sort_keys=True), file=out)
                    else:
                        for x in r["strata"]:
                            print("%s: n=%d%s, pass %s, coverage %d/%d, ci95 %s" % (x["stratum"], x["n"], " (small sample)" if x["small_sample"] else "", "n/a" if not x["pass_rate"] else "%d/%d" % tuple(x["pass_rate"]), *x["coverage"], x["pass_ci95"]), file=out)
                        print(r["rule"], file=out)
                else:
                    r = gee_pass(conn, home, a.cluster_by, a.exposure, since, until, now, a.min_clusters)
                    print(json.dumps(r, sort_keys=True, indent=1), file=out)
                    if not r["ok"]:
                        print("pros: GEE not run: %s" % r["reason"], file=err)
                        return 0
            finally:
                conn.close()
        return 0
    except (AnalysisError, ev.EvalError, ex.ExportError) as e:
        print("pros: %s" % e, file=err)
        return 3 if getattr(e, "code", "") == "not_found" else 2
    except (OSError, ValueError) as e:
        print("pros: %s" % type(e).__name__, file=err)
        return 2
