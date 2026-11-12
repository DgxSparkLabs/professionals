"""Audit steps D3 + F1: the loopback HTTP adapter and dashboard (gate cases of http_gate.json, JSON parity with the CLI on the D3 fold
and on the E store built by `pros audit import`, loopback only, management writes only with --manage and the owner token,
professional sessions denied). Stdlib only."""
import glob
import http.client
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

import helpers

HERE = helpers.HERE
sys.path[:0] = [HERE, os.path.join(HERE, "..", "plugins", "professionals", "lib")]
import audit_contract_ref as ref  # noqa: E402
import audit_query_fixture as F  # noqa: E402
from pros_audit import http as H  # noqa: E402
from pros_audit import query as Q  # noqa: E402


class TestGate(unittest.TestCase):
    def test_fixture_cases_and_reference_agree(self):
        g = F.jf("http_gate.json")
        for c in g["cases"]:
            hs = {k: v.replace("LOOPBACK", "127.0.0.1") for k, v in c["headers"].items()}
            st, code = H.gate(c["method"], hs, g["port"], g["token"])
            self.assertEqual((st, code), (c["status"], c["code"]), c["why"])
            self.assertEqual((st, code), ref.http_gate(c["method"], hs, g["port"], g["token"]), c["why"])

    def test_empty_token_never_matches(self):
        self.assertEqual(H.gate("GET", {"Host": "127.0.0.1:1", "Authorization": "Bearer "}, 1, "")[0], 401)


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="pros-audit-http-")
        db = F.new_db(os.path.join(cls.dir, "audit.db"))
        F.load_release(db)
        db.close()
        cls.q = Q.Query(os.path.join(cls.dir, "audit.db"), owner_check=lambda: True, now=lambda: F.NOW)
        cls.token = "ab" * 32
        cls.srv = H.make_server(cls.q, cls.token)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        import shutil
        shutil.rmtree(cls.dir, ignore_errors=True)

    def get(self, path, method="GET", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Authorization": "Bearer " + self.token}
        h.update(headers if headers is not None else {})
        c.putrequest(method, path, skip_host=True)
        for k, v in dict({"Host": "127.0.0.1:%d" % self.port}, **h).items():
            if v is not None:
                c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def test_same_bytes_as_the_cli_layer(self):
        for method, qs, args in (("task_trace", "id=fx-root", {"id": "fx-root"}), ("query_metrics", "since=1800000000&until=1800000200", {"since": "1800000000", "until": "1800000200"}),
                                 ("explain_metric", "metric=cache_read_share&since=1800000000&until=1800000200", {"metric": "cache_read_share", "since": "1800000000", "until": "1800000200"}),
                                 ("list_runs", "", {}), ("list_professionals", "limit=1", {"limit": "1"}), ("evaluation_evidence", "target=m-0000000001", {"target": "m-0000000001"})):
            st, _, body = self.get("/api/v1/%s?%s" % (method, qs))
            self.assertEqual(st, 200, method)
            self.assertEqual(body, (json.dumps(self.q.call(method, args), indent=1, sort_keys=True) + "\n").encode(), method)

    def test_golden_bytes_for_trace(self):
        st, hd, body = self.get("/api/v1/task_trace?id=m-0000000001")
        with open(os.path.join(F.CF, "query", "task_trace.json"), "rb") as f:
            self.assertEqual(body, f.read())
        self.assertEqual(hd["Content-Type"], "application/json; charset=utf-8")
        self.assertFalse([k for k in hd if k.lower().startswith("access-control")], "no CORS headers")

    def test_gate_over_the_wire(self):
        p = "/api/v1/list_runs"
        self.assertEqual(self.get(p, headers={"Authorization": None})[0], 401)
        self.assertEqual(self.get(p, headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self.get(p, headers={"Host": "evil.example:%d" % self.port})[0], 400)
        self.assertEqual(self.get(p, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.get(p, headers={"Origin": "http://127.0.0.1:%d" % self.port})[0], 200)
        self.assertEqual(self.get(p, method="POST")[0], 405)
        self.assertEqual(self.get(p, method="DELETE")[0], 405)
        self.assertEqual(self.get(p, method="OPTIONS")[0], 405)
        st, _, body = self.get(p, headers={"Authorization": None, "Host": "evil.example:1"})
        self.assertEqual(st, 400, "bad host wins over missing token")
        self.assertEqual(json.loads(body)["error"]["code"], "bad_request")

    def test_token_in_query_string_is_not_accepted(self):
        self.assertEqual(self.get("/api/v1/list_runs?token=%s" % self.token, headers={"Authorization": None})[0], 401)
        self.assertEqual(self.get("/api/v1/list_runs?token=%s" % self.token)[0], 400, "unknown argument")

    def test_errors_map_to_status_and_envelope(self):
        for path, status, code in (("/api/v1/list_runs?limitt=3", 400, "bad_request"), ("/api/v1/task_trace?id=nope", 404, "not_found"), ("/api/v1/nosuch", 404, "not_found"),
                                   ("/api/v1/query_metrics?since=1&until=99999999", 413, "too_large"), ("/api/v1/list_runs?limit=1&limit=2", 400, "bad_request"), ("/api/v2/list_runs", 404, "not_found")):
            st, _, body = self.get(path)
            self.assertEqual((st, json.loads(body)["error"]["code"]), (status, code), path)

    def test_head_has_no_body_and_no_write_endpoint_exists(self):
        st, hd, body = self.get("/api/v1/list_runs", method="HEAD")
        self.assertEqual((st, body), (200, b""))
        self.assertEqual(self.get("/api/v1/verdict")[0], 404)

    def test_only_loopback_binds(self):
        self.assertRaises(ValueError, H.make_server, self.q, "t", 0, "0.0.0.0")
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")


class TestToken(unittest.TestCase):
    def test_created_0600_rotated_and_reused(self):
        d = tempfile.mkdtemp(prefix="pros-audit-tok-")
        t1 = H.load_token(d)
        p = H.token_path(d)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(p)).st_mode), 0o700)
        self.assertEqual((len(t1), H.load_token(d)), (64, t1))
        t2 = H.load_token(d, rotate=True)
        self.assertNotEqual(t1, t2)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)


# ---------------------------------------------------------------------------- step F1: dashboard + management on the E store
CF = os.path.join(HERE, "fixtures", "audit", "contracts")
USAGE = os.path.join(HERE, "fixtures", "audit", "usage")
ROOT, CHILD = "m-0000000001", "m-0000000005"
ENOW = "1800000200"
EWIN = ["--since", "1800000000", "--until", ENOW]
from pros_audit import manage as M  # noqa: E402


def e_env(tmp, home, extra=None):
    e = {k: v for k, v in os.environ.items() if k not in ("PRO_SESSION", "TMUX")}
    e.update(PRO_HOME=home, PRO_AUDIT_NOW=ENOW, PRO_TMUX_SOCKET="audit-f1-none", CLAUDE_CONFIG_DIR=os.path.join(tmp, "claude"))
    e.update(extra or {})
    return e


def e_pros(tmp, home, *args, env=None):
    return subprocess.run([sys.executable, helpers.PRO, "audit"] + list(args), capture_output=True, text=True, env=e_env(tmp, home, env), timeout=60)


def e_home(tmp, name="home"):
    """The step E store: release-scenario spool + usage transcripts -> `pros audit import` (real importer, usage, derive)."""
    home = os.path.join(tmp, name)
    os.makedirs(os.path.join(home, "audit", "spool"), mode=0o700)
    os.chmod(os.path.join(home, "audit"), 0o700)
    proj = os.path.join(tmp, "claude", "projects", "p")
    os.makedirs(proj, exist_ok=True)
    for p in glob.glob(os.path.join(USAGE, "transcripts", "*.jsonl")):
        shutil.copy(p, proj)
    sp = os.path.join(home, "audit", "spool", "fixture-1.jsonl")
    shutil.copy(os.path.join(CF, "events", "release_scenario.jsonl"), sp)
    os.chmod(sp, 0o600)
    os.utime(sp, (1, 1))
    r = e_pros(tmp, home, "import", "--json")
    assert r.returncode == 0, r.stderr
    return home


def req(port, path, token=None, method="GET", body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    h = {"Host": "127.0.0.1:%d" % port}
    if token:
        h["Authorization"] = "Bearer " + token
    if body is not None:
        body = json.dumps(body).encode() if not isinstance(body, bytes) else body
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    c.putrequest(method, path, skip_host=True)
    for k, v in h.items():
        if v is not None:
            c.putheader(k, v)
    c.putheader("Content-Length", str(len(body or b"")))
    c.endheaders()
    if body:
        c.send(body)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, dict(r.getheaders()), out


class TestCliParityOverTheRealServer(unittest.TestCase):
    """`pros audit serve` (a separate process, fixed clock) answers byte for byte what `pros audit ... --json` prints, on the E store."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pros-audit-f1-")
        cls.home = e_home(cls.tmp)
        v = e_pros(cls.tmp, cls.home, "verdict", ROOT, "--status", "passed", "--criterion", "C1=passed", "--json")
        assert v.returncode == 0, v.stderr
        cls.proc = subprocess.Popen([sys.executable, helpers.PRO, "audit", "serve"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e_env(cls.tmp, cls.home))
        line = cls.proc.stdout.readline()
        cls.port = int(line.split("http://127.0.0.1:")[1].split("/")[0])
        cls.dash_line = cls.proc.stdout.readline()
        with open(os.path.join(cls.home, "audit", "http.token"), encoding="utf-8") as f:
            cls.token = f.read().strip()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        for s in (cls.proc.stdout, cls.proc.stderr):
            s.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def cli(self, *args):
        r = e_pros(self.tmp, self.home, *args)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout.encode("utf-8")

    def test_json_endpoints_equal_cli_json(self):
        cases = [("status", "", ["status", "--json"]), ("task_trace", "id=" + ROOT, ["trace", ROOT, "--json"]), ("task_trace", "id=fx-root", ["trace", "fx-root", "--json"]),
                 ("query_metrics", "since=1800000000&until=" + ENOW, ["summary", "--json"] + EWIN),
                 ("query_metrics", "since=1800000000&until=%s&group_by=professional" % ENOW, ["summary", "--json", "--by", "professional"] + EWIN),
                 ("query_metrics", "since=1800000000&until=%s&professional=system-designer" % ENOW, ["summary", "--json", "--professional", "system-designer"] + EWIN)]
        for m in ("assigned_work", "pass_rate", "deadline_miss_rate", "accounted_tokens", "cost_estimate", "evaluation_coverage", "context_footprint"):
            cases.append(("explain_metric", "metric=%s&since=1800000000&until=%s" % (m, ENOW), ["explain", m, "--json"] + EWIN))
        for method, q, cli in cases:
            st, hd, body = req(self.port, "/api/v1/%s?%s" % (method, q) if q else "/api/v1/" + method, self.token)
            self.assertEqual(st, 200, (method, q, body[:300]))
            self.assertEqual(body, self.cli(*cli), (method, q))
        tr = json.loads(req(self.port, "/api/v1/task_trace?id=" + ROOT, self.token)[2])["result"]
        own = {a["msg"]: a["own_tokens"]["total"] for a in tr["assignments"]}
        self.assertEqual((own[ROOT], own[CHILD], tr["usage"]["inclusive"]["total"]), (140, 80, 220))
        self.assertEqual(sum(s["tokens"]["total"] for s in tr["usage"]["session_only"]), 30)
        self.assertIn("pending", [w["outcome"] for w in tr["waits"]])

    def test_errors_equal_cli_errors(self):
        st, _, body = req(self.port, "/api/v1/task_trace?id=nope", self.token)
        r = e_pros(self.tmp, self.home, "trace", "nope", "--json")
        self.assertEqual((st, r.returncode), (404, 3))
        self.assertEqual(json.loads(body), json.loads(r.stderr.split("pros: ", 1)[1]))

    def test_real_server_is_loopback_read_only_and_token_gated(self):
        self.assertIn("management off", self.dash_line)
        self.assertEqual(req(self.port, "/api/v1/status")[0], 401)
        self.assertEqual(req(self.port, "/manage/v1/settings")[0], 401)
        self.assertEqual(req(self.port, "/manage/v1/capture", self.token, "POST", {"mode": "off"})[0], 405, "read-only by default")
        st, _, body = req(self.port, "/manage/v1/settings", self.token)
        self.assertEqual((st, json.loads(body)["result"]["writable"]), (200, False))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.home, "audit", "http.token")).st_mode), 0o600)

    def test_dashboard_page_is_static_and_carries_no_data(self):
        st, hd, page = req(self.port, "/")
        self.assertEqual(st, 200)
        self.assertTrue(hd["Content-Type"].startswith("text/html"))
        self.assertIn("default-src 'none'", hd["Content-Security-Policy"])
        self.assertIn("script-src 'self'", hd["Content-Security-Policy"])
        js = req(self.port, "/dashboard.js")[2].decode()
        css = req(self.port, "/dashboard.css")[2].decode()
        for blob in (page.decode(), js, css):
            for x in (ROOT, CHILD, "system-designer", "fx-root", self.token):
                self.assertNotIn(x, blob)
            self.assertNotIn("http://", blob.replace("http://127.0.0.1", ""), "no external resource")
            self.assertNotIn("https://", blob, "no CDN")
        self.assertNotIn("innerHTML", js, "values are written with textContent only")
        for m in ("task_trace", "query_metrics", "explain_metric", "list_professionals", "list_runs", "evaluation_evidence", "status"):
            self.assertIn('"%s"' % m, js, "the page calls the same query methods as the CLI")
        for f in ("since", "until", "professional", "revision", "model", "task_class", "direct", "group_by"):
            self.assertIn('name="%s"' % f, page.decode(), "filters match the CLI")
        self.assertEqual(req(self.port, "/", headers={"Host": "evil.example:%d" % self.port})[0], 400)
        self.assertEqual(req(self.port, "/", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertFalse([k for k in hd if k.lower().startswith("access-control")])


class TestManagement(unittest.TestCase):
    """--manage server: writes need the token, JSON and a same-origin request; each one is an audit event; the CLI sees the result."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pros-audit-f1m-")
        cls.home = e_home(cls.tmp)
        cls.now = float(ENOW)
        cls.q = Q.Query(os.path.join(cls.home, "audit", "audit.db"), owner_check=lambda: True, now=lambda: cls.now)
        cls.token = "cd" * 32
        cls.mgr = H.Manager(cls.home, cls.q, writable=True, now=lambda: cls.now)
        cls.srv = H.make_server(cls.q, cls.token, manager=cls.mgr)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def post(self, action, body, token=True, headers=None):
        st, _, out = req(self.port, "/manage/v1/" + action, self.token if token else None, "POST", body, headers)
        return st, json.loads(out)

    def gen(self):
        return json.loads(req(self.port, "/api/v1/status", self.token)[2])["snapshot"]["generation"]

    def test_writes_need_the_token_json_and_same_origin(self):
        g0 = self.gen()
        self.assertEqual(self.post("capture", {"mode": "off"}, token=False)[0], 401)
        self.assertEqual(self.post("capture", {"mode": "off"}, headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self.post("capture", {"mode": "off"}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post("capture", {"mode": "off"}, headers={"Host": "evil.example:1"})[0], 400)
        self.assertEqual(self.post("capture", {"mode": "off"}, headers={"Content-Type": "text/plain"})[0], 400, "a simple cross-site form post cannot pass")
        self.assertEqual(self.post("capture", b"not json")[0], 400)
        self.assertEqual(self.post("nosuch", {})[0], 404)
        self.assertEqual(req(self.port, "/api/v1/list_runs", self.token, "POST", {})[0], 405, "the query API stays read-only")
        self.assertEqual(req(self.port, "/manage/v1/capture", self.token, "OPTIONS")[0], 405, "no CORS preflight")
        self.assertEqual(self.gen(), g0, "nothing written by refused requests")

    def test_capture_and_retention_are_recorded(self):
        g0 = self.gen()
        st, j = self.post("capture", {"mode": "off"})
        self.assertEqual((st, j["result"]["capture_mode"]), (200, "off"))
        with open(os.path.join(self.home, "config.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["capture_mode"], "off", "the emitter reads this key")
        self.assertEqual(self.gen(), g0 + 1, "one generation bump per write")
        self.assertEqual(self.post("capture", {"mode": "content"})[0], 400, "content capture is not in the first release")
        self.assertEqual(self.post("retention", {"class": "operational", "days": 90})[0], 200)
        self.assertEqual(self.post("retention", {"class": "operational", "days": -1})[0], 400)
        self.assertEqual(self.post("retention", {"class": "spool", "days": 3})[0], 400)
        s = json.loads(req(self.port, "/manage/v1/settings", self.token)[2])["result"]
        self.assertEqual((s["writable"], s["capture_mode"], s["retention_days"]["operational"]), (True, "off", 90))
        self.post("capture", {"mode": "metadata"})
        c = sqlite3.connect(os.path.join(self.home, "audit", "audit.db"))
        try:
            rows = [json.loads(d) for (d,) in c.execute("SELECT data FROM event WHERE action='audit_config' ORDER BY seq")]
            logs = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='audit_config'").fetchone()[0]
        finally:
            c.close()
        self.assertEqual([r["setting"] for r in rows], ["capture_mode", "retention_metadata_days", "capture_mode"])
        self.assertEqual(logs, 3)

    def test_verdict_entry_shows_in_cli_and_api_alike(self):
        st, j = self.post("verdict", {"target": CHILD, "status": "failed", "criteria": {"R1": "failed"}, "evidence": ["check-run:x1"]})
        self.assertEqual(st, 200, j)
        self.assertEqual(j["result"]["effective"], j["result"]["eval"])
        api = req(self.port, "/api/v1/task_trace?id=" + ROOT, self.token)[2]
        cli = e_pros(self.tmp, self.home, "trace", ROOT, "--json")
        self.assertEqual(api, cli.stdout.encode())
        tr = json.loads(api)["result"]
        self.assertEqual({a["msg"]: a["evaluation"]["status"] for a in tr["assignments"]}[CHILD], "failed")
        self.assertEqual(self.post("verdict", {"target": CHILD, "status": "passed", "criteria": {"R1": "failed"}})[0], 400, "status must match the criteria")
        self.assertEqual(self.post("verdict", {"target": "m-00000000ff", "status": "passed", "criteria": {"C1": "passed"}})[0], 404)
        self.assertEqual(self.post("verdict", {"target": CHILD, "status": "passed", "criteria": {"C1": "passed"}, "sql": "x"})[0], 400)

    def test_saved_reports_and_export_preview(self):
        st, j = self.post("report_save", {"name": "weekly", "spec": {"since": "7d", "professional": "system-designer"}})
        self.assertEqual(st, 200, j)
        rid = j["result"]["id"]
        self.assertEqual(self.post("report_save", {"name": "bad", "spec": {"nope": "1"}})[0], 400, "spec validated")
        self.assertEqual(self.post("report_save", {"name": "bad", "spec": {"since": "7d", "direct": "maybe-not-a-bool" * 20}})[0], 400, "values validated by the query layer")
        self.assertEqual(self.post("report_save", {"name": "../x", "spec": {"since": "7d"}})[0], 400)
        reps = json.loads(req(self.port, "/manage/v1/reports", self.token)[2])["result"]["reports"]
        self.assertEqual([(r["id"], r["name"], r["spec"]["professional"]) for r in reps], [(rid, "weekly", "system-designer")])
        st, j = self.post("report_run", {"name": "weekly"})
        self.assertEqual(st, 200, j)
        self.assertTrue(j["result"]["snapshot"].startswith("rs-"))
        self.assertEqual(self.post("report_delete", {"name": "weekly"})[0], 200)
        self.assertEqual(self.post("report_delete", {"name": "weekly"})[0], 404)
        self.assertEqual(self.post("report_run", {"name": "weekly"})[0], 404)

    def test_saved_reports_parity_with_cli(self):
        """One implementation (reports.py): what the dashboard saves the CLI lists, and the reverse, byte-equal JSON."""
        st, j = self.post("report_save", {"name": "par-dash", "spec": {"since": "30d", "group_by": "revision"}})
        self.assertEqual(st, 200, j)
        r = e_pros(self.tmp, self.home, "report", "save", "par-cli", "--since", "7d", "--by", "professional", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        cli_id = json.loads(r.stdout)["id"]
        r = e_pros(self.tmp, self.home, "report", "save", "par-dash", "--since", "30d", "--by", "revision", "--json")
        self.assertEqual(json.loads(r.stdout)["id"], j["result"]["id"], "same name + spec = same definition id on both surfaces")
        cli = json.loads(e_pros(self.tmp, self.home, "report", "list", "--json").stdout)
        api = json.loads(req(self.port, "/manage/v1/reports", self.token)[2])["result"]
        self.assertEqual(api, cli)
        self.assertIn(cli_id, [x["id"] for x in api["reports"]])
        self.assertEqual([x["versions"] for x in api["reports"] if x["name"] == "par-dash"], [1], "no duplicate version")
        c = sqlite3.connect(os.path.join(self.home, "audit", "audit.db"))
        try:
            logs = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='report_save' AND detail IN ('par-dash','par-cli')").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(logs, 2, "each new definition is one audit_log row, whichever surface saved it")
        self.assertEqual(self.post("report_delete", {"name": "par-cli"})[0], 200)
        r = e_pros(self.tmp, self.home, "report", "delete", "par-dash")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(req(self.port, "/manage/v1/reports", self.token)[2])["result"]["reports"], [])

        st, j = self.post("export_preview", {"since": "1800000000", "until": ENOW, "format": "csv"})
        self.assertEqual(st, 200, j)
        r = e_pros(self.tmp, self.home, "export", "--preview", "--since", "1800000000", "--until", ENOW, "--format", "csv")
        cli = {ln.split(":")[0].strip(): int(ln.split(":")[1].split()[0]) for ln in r.stdout.splitlines() if ln.startswith("  ")}
        self.assertEqual(j["result"]["counts"], cli, "same counts as the CLI preview")
        self.assertEqual(j["result"]["mode"], "redacted")
        self.assertFalse(glob.glob(os.path.join(self.tmp, "**", "manifest.json"), recursive=True), "a preview writes nothing")

    def test_owner_check_applies_to_management(self):
        q = Q.Query(os.path.join(self.home, "audit", "audit.db"), owner_check=lambda: False, now=lambda: self.now)
        srv = H.make_server(q, self.token, manager=H.Manager(self.home, q, writable=True, now=lambda: self.now))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            p = srv.server_address[1]
            self.assertEqual(req(p, "/api/v1/status", self.token)[0], 403)
            self.assertEqual(req(p, "/manage/v1/settings", self.token)[0], 403)
            self.assertEqual(req(p, "/manage/v1/retention", self.token, "POST", {"class": "content", "days": 1})[0], 403)
        finally:
            srv.shutdown()
            srv.server_close()


class TestPurge(unittest.TestCase):
    def test_dry_run_confirm_tombstones_and_no_restore_on_reimport(self):
        tmp = tempfile.mkdtemp(prefix="pros-audit-f1p-")
        self.addCleanup(shutil.rmtree, tmp, True)
        home = e_home(tmp)
        now = float(ENOW)
        q = Q.Query(os.path.join(home, "audit", "audit.db"), owner_check=lambda: True, now=lambda: now)
        mgr = H.Manager(home, q, writable=True, now=lambda: now)
        tok = "ef" * 32
        srv = H.make_server(q, tok, manager=mgr)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        p = srv.server_address[1]
        cut = 1800000001.0  # after the root assignment was created, before the child
        dry = json.loads(req(p, "/manage/v1/purge", tok, "POST", {"class": "operational", "before": cut, "dry_run": True})[2])["result"]
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["counts"]["assignments"], 1)
        self.assertEqual(json.loads(req(p, "/api/v1/task_trace?id=" + ROOT, tok)[2])["result"]["root"], ROOT, "a dry run deletes nothing")
        self.assertEqual(req(p, "/manage/v1/purge", tok, "POST", {"class": "operational", "before": cut})[0], 400, "needs confirm=purge")
        self.assertEqual(req(p, "/manage/v1/purge", tok, "POST", {"class": "operational", "before": now + 10, "confirm": "purge"})[0], 400)
        st, _, body = req(p, "/manage/v1/purge", tok, "POST", {"class": "operational", "before": cut, "confirm": "purge"})
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body)["result"]["counts"], dry["counts"])
        self.assertEqual(req(p, "/api/v1/task_trace?id=" + ROOT, tok)[0], 404)
        c = sqlite3.connect(os.path.join(home, "audit", "audit.db"))
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM event WHERE msg=?", (ROOT,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM evaluation WHERE target=?", (ROOT,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM event_tombstone").fetchone()[0], dry["counts"]["events"])
            self.assertEqual(c.execute("SELECT purged_before FROM purge_mark WHERE class='operational'").fetchone()[0], cut)
            pe = json.loads(c.execute("SELECT data FROM event WHERE action='audit_purge'").fetchone()[0])
        finally:
            c.close()
        self.assertEqual((pe["class"], pe["reason"], pe["rows"]), ("operational", "operator", sum(dry["counts"].values())))
        # the same spool comes back (crash before delete): the importer honours the tombstones and the mark
        sp = os.path.join(home, "audit", "spool", "again.jsonl")
        shutil.copy(os.path.join(CF, "events", "release_scenario.jsonl"), sp)
        os.chmod(sp, 0o600)
        os.utime(sp, (1, 1))
        self.assertEqual(e_pros(tmp, home, "import", "--json").returncode, 0)
        self.assertEqual(e_pros(tmp, home, "trace", ROOT, "--json").returncode, 3, "purged rows are not restored")
        self.assertEqual(req(p, "/manage/v1/purge", tok, "POST", {"class": "exports", "dry_run": True})[0], 400)


class TestProfessionalDenied(unittest.TestCase):
    def test_a_professional_session_cannot_serve(self):
        tmp = tempfile.mkdtemp(prefix="pros-audit-f1d-")
        self.addCleanup(shutil.rmtree, tmp, True)
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, "audit"), mode=0o700)
        for args in (["serve"], ["serve", "--manage"], ["serve", "--rotate"]):
            r = e_pros(tmp, home, *args, env={"PRO_SESSION": "x"})
            self.assertEqual(r.returncode, 4, (args, r.stderr))
            self.assertEqual(r.stdout, "")
        self.assertFalse(os.path.exists(os.path.join(home, "audit", "http.token")), "no token created for a professional")

    def test_non_loopback_bind_refused(self):
        q = Q.Query(":memory:", owner_check=lambda: True)
        for host in ("0.0.0.0", "::", "192.0.2.1", ""):
            self.assertRaises(ValueError, H.make_server, q, "t", 0, host, H.Manager("/nonexistent", q, writable=True))


if __name__ == "__main__":
    unittest.main()
