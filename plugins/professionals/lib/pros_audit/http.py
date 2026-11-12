"""Loopback HTTP adapter for the audit query API (docs/spec/audit/privacy.md section 5). Read-only: GET /api/v1/<method>?<args> returns the same envelope the CLI prints.

Gate order: method, Host, Origin, token. Every endpoint needs the per-install owner token; there is no CORS, no write and no token in a query string."""
import hmac
import http.server
import json
import os
import secrets
import urllib.parse

from . import query as Q

STATUS = {"bad_request": 400, "unauthorized": 401, "forbidden": 403, "not_found": 404, "too_large": 413, "store_unavailable": 503}


def token_path(home):
    return os.path.join(str(home), "audit", "http.token")


def load_token(home, rotate=False):
    """Read the owner token (mode 0600), creating or rotating it on request. Returns None if absent and not created."""
    p = token_path(home)
    if rotate or not os.path.exists(p):
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
        os.replace(p + ".tmp", p)
    with open(p, encoding="utf-8") as f:
        return f.read().strip()


def gate(method, headers, port, token):
    """-> (status, error code or None). Pure, so the fixture cases can be tested without a socket."""
    h = {k.lower(): v for k, v in headers.items()}
    if method not in ("GET", "HEAD"):
        return 405, "bad_request"
    if h.get("host") not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
        return 400, "bad_request"
    if "origin" in h and h["origin"] not in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port):
        return 403, "forbidden"
    auth = h.get("authorization", "")
    if not auth.startswith("Bearer ") or not token or not hmac.compare_digest(auth[7:].encode(), token.encode()):
        return 401, "unauthorized"
    return 200, None


def handle(query, path, method, headers, port, token):
    """-> (status, body bytes). The whole adapter minus the socket."""
    st, code = gate(method, headers, port, token)
    if code:
        return st, _body(Q.AuditError(code, {405: "read-only: GET only", 400: "bad Host", 403: "foreign Origin", 401: "owner token required"}[st]).envelope())
    u = urllib.parse.urlsplit(path)
    m = u.path.split("/")
    if len(m) != 4 or m[:3] != ["", "api", "v1"] or m[3] not in Q.ARGS:
        return 404, _body(Q.AuditError("not_found", "unknown endpoint").envelope())
    args = {}
    for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        if k in args:
            return 400, _body(Q.AuditError("bad_request", "repeated argument", k).envelope())
        args[k] = v
    env = query.call(m[3], args)
    return (STATUS.get(env["error"]["code"], 400) if "error" in env else 200), _body(env)


def _body(env):
    return (json.dumps(env, indent=1, sort_keys=True) + "\n").encode("utf-8")


def make_server(query, token, port=0, host="127.0.0.1"):
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the audit adapter binds loopback only")
    holder = {}

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "pros-audit"

        def _go(self, method):
            st, body = handle(query, self.path, method, dict(self.headers.items()), holder["port"], token)
            self.send_response(st)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(body)

        do_GET = lambda self: self._go("GET")
        do_HEAD = lambda self: self._go("HEAD")
        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = lambda self: self._go(self.command)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    holder["port"] = srv.server_address[1]
    return srv


def serve_cli(query, home, port, rotate, out):
    token = load_token(home, rotate)
    srv = make_server(query, token, port)
    print("audit API on http://127.0.0.1:%d/api/v1/<method> (token in %s, send as Authorization: Bearer)" % (srv.server_address[1], token_path(home)), file=out, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
