"""Audit step D3: the loopback HTTP adapter (gate cases of http_gate.json, JSON parity with the CLI layer, no write). Stdlib only."""
import http.client
import json
import os
import stat
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


if __name__ == "__main__":
    unittest.main()
