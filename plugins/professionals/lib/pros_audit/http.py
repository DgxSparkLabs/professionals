"""Loopback HTTP adapter for the audit query API and the local dashboard (docs/spec/audit/privacy.md section 5, step F1).

Reads: GET /api/v1/<method>?<args> returns the same envelope the CLI prints with --json (status too). Every API endpoint needs the
per-install owner token; there is no CORS and no token in a query string. Gate order: method, Host, Origin, token.
Dashboard: GET / (and its .js/.css) is a static page with no data; it reads the token from the URL fragment, which the browser
never sends, and calls the API with it. Host and Origin are checked there too.
Management (capture mode, retention, verdict entry, saved reports, export preview, purge): POST /manage/v1/<action> with a JSON
body, only when the server was started with --manage (read-only by default: POST is 405), and always with the owner token."""
import hmac
import http.server
import json
import os
import secrets
import time
import urllib.parse

from . import query as Q

STATUS = {"bad_request": 400, "unauthorized": 401, "forbidden": 403, "not_found": 404, "too_large": 413, "store_unavailable": 503}
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = {"/": ("dashboard.html", "text/html; charset=utf-8"), "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
          "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8")}
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
MAX_BODY = 64 * 1024
MANAGE_ACTIONS = ("capture", "retention", "verdict", "report_save", "report_delete", "export_preview", "purge")


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


def _origin_gate(h, port):
    if h.get("host") not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
        return 400, "bad_request"
    if "origin" in h and h["origin"] not in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port):
        return 403, "forbidden"
    return 200, None


def gate(method, headers, port, token, writable=False):
    """-> (status, error code or None). Pure, so the fixture cases can be tested without a socket. POST passes only when writable."""
    h = {k.lower(): v for k, v in headers.items()}
    if method not in ("GET", "HEAD") and not (writable and method == "POST"):
        return 405, "bad_request"
    st = _origin_gate(h, port)
    if st[1]:
        return st
    auth = h.get("authorization", "")
    if not auth.startswith("Bearer ") or not token or not hmac.compare_digest(auth[7:].encode(), token.encode()):
        return 401, "unauthorized"
    return 200, None


def _err(code, msg, field=None):
    return STATUS.get(code, 400), _body(Q.AuditError(code, msg, field).envelope())


def _gate_error(st):
    return _body(Q.AuditError({405: "bad_request", 400: "bad_request", 403: "forbidden", 401: "unauthorized"}[st],
                              {405: "read-only: GET only", 400: "bad Host", 403: "foreign Origin", 401: "owner token required"}[st]).envelope())


class Manager:
    """Owner management bound to one PRO_HOME; `writable` is False unless the server was started with --manage."""

    def __init__(self, home, query, writable=False, now=None):
        self.home, self.query, self.writable, self.now = home, query, writable, now or time.time

    def read(self, name):
        from . import manage as M
        if name == "settings":
            return M.settings(self.home, self.writable)
        if name == "reports":
            return M.list_reports(self.home)
        return None

    def write(self, action, b):
        from . import manage as M
        now = self.now()
        if action == "capture":
            return M.set_capture(self.home, b.get("mode"), now)
        if action == "retention":
            return M.set_retention(self.home, b.get("class"), b.get("days"), now)
        if action == "verdict":
            return M.verdict(self.home, b, now)
        if action == "report_save":
            return M.save_report(self.home, self.query, b.get("name"), b.get("method"), b.get("args") or {}, now)
        if action == "report_delete":
            return M.delete_report(self.home, b.get("id"), now)
        if action == "export_preview":
            return M.export_preview(self.home, b.get("since"), b.get("until"), b.get("format") or "jsonl", bool(b.get("raw")), now)
        before = b.get("before")
        if before is None and b.get("class") in M.RETENTION:
            before = M.retention_cutoff(self.home, b["class"], now)
        if not b.get("dry_run") and b.get("confirm") != "purge":
            raise M.ManageError("a purge needs confirm=purge (or dry_run=true)", field="confirm")
        return M.purge(self.home, b.get("class"), before, now, dry_run=bool(b.get("dry_run")))


def _envelope(action, result):
    return {"api_version": Q.API_VERSION, "action": action, "result": result}


def handle(query, path, method, headers, port, token, manager=None, body=b""):
    """-> (status, content type, body bytes, extra headers). The whole adapter minus the socket."""
    u = urllib.parse.urlsplit(path)
    h = {k.lower(): v for k, v in headers.items()}
    j = "application/json; charset=utf-8"
    if u.path in STATIC and method in ("GET", "HEAD"):
        st, code = _origin_gate(h, port)
        if code:
            return st, j, _gate_error(st), {}
        name, ctype = STATIC[u.path]
        with open(os.path.join(HERE, name), "rb") as f:
            return 200, ctype, f.read(), {"Content-Security-Policy": CSP}
    writable = bool(manager and manager.writable and u.path.startswith("/manage/v1/"))
    st, code = gate(method, headers, port, token, writable)
    if code:
        return st, j, _gate_error(st), {}
    m = u.path.split("/")
    if len(m) == 4 and m[:3] == ["", "manage", "v1"] and manager is not None:
        return _manage(manager, m[3], method, h, u.query, body) + ({},)
    if len(m) != 4 or m[:3] != ["", "api", "v1"] or (m[3] not in Q.ARGS and m[3] != "status") or method == "POST":
        return 404, j, _body(Q.AuditError("not_found", "unknown endpoint").envelope()), {}
    args = {}
    for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        if k in args:
            return 400, j, _body(Q.AuditError("bad_request", "repeated argument", k).envelope()), {}
        args[k] = v
    if m[3] == "status":
        if args:
            return 400, j, _body(Q.AuditError("bad_request", "unknown argument", sorted(args)[0]).envelope()), {}
        try:
            env = query.status()
        except Q.AuditError as e:
            env = e.envelope()
    else:
        env = query.call(m[3], args)
    return (STATUS.get(env["error"]["code"], 400) if "error" in env else 200), j, _body(env), {}


def _manage(manager, name, method, h, qs, body):
    from . import manage as M
    j = "application/json; charset=utf-8"
    try:
        if manager.query is not None:
            manager.query._guard()  # the same owner check as every read
    except Q.AuditError as e:
        return (STATUS.get(e.code, 400), j, _body(e.envelope()))
    if method in ("GET", "HEAD"):
        if qs:
            return (400, j, _body(Q.AuditError("bad_request", "no arguments here").envelope()),)
        r = manager.read(name)
        if r is None:
            return (404, j, _body(Q.AuditError("not_found", "unknown endpoint").envelope()),)
        return 200, j, _body(_envelope(name, r))
    if name not in MANAGE_ACTIONS:
        return (404, j, _body(Q.AuditError("not_found", "unknown action").envelope()),)
    if (h.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
        return (400, j, _body(Q.AuditError("bad_request", "Content-Type must be application/json").envelope()),)
    try:
        b = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return (400, j, _body(Q.AuditError("bad_request", "body is not JSON").envelope()),)
    if not isinstance(b, dict):
        return (400, j, _body(Q.AuditError("bad_request", "body must be an object").envelope()),)
    try:
        return 200, j, _body(_envelope(name, manager.write(name, b)))
    except M.ManageError as e:
        return (STATUS.get(e.code, 400), j, _body(Q.AuditError(e.code if e.code in Q.CODES else "bad_request", str(e)[:200], e.field).envelope()),)
    except Q.AuditError as e:
        return (STATUS.get(e.code, 400), j, _body(e.envelope()),)


def _body(env):
    return (json.dumps(env, indent=1, sort_keys=True) + "\n").encode("utf-8")


def make_server(query, token, port=0, host="127.0.0.1", manager=None):
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the audit adapter binds loopback only")
    holder = {}

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "pros-audit"

        def _go(self, method):
            body = b""
            if method == "POST":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > MAX_BODY:
                    st, ct, out, extra = 413, "application/json; charset=utf-8", _body(Q.AuditError("too_large", "body too large").envelope()), {}
                    return self._send(method, st, ct, out, extra, close=True)
                body = self.rfile.read(n) if n else b""
            st, ct, out, extra = handle(query, self.path, method, dict(self.headers.items()), holder["port"], token, manager, body)
            self._send(method, st, ct, out, extra)

        def _send(self, method, st, ct, out, extra, close=False):
            self.send_response(st)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in extra.items():
                self.send_header(k, v)
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(out)

        do_GET = lambda self: self._go("GET")
        do_HEAD = lambda self: self._go("HEAD")
        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = lambda self: self._go(self.command)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    holder["port"] = srv.server_address[1]
    return srv


def serve_cli(query, home, port, rotate, out, manage=False, now=None):
    token = load_token(home, rotate)
    mgr = Manager(home, query, writable=manage, now=now)
    srv = make_server(query, token, port, manager=mgr)
    p = srv.server_address[1]
    print("audit API on http://127.0.0.1:%d/api/v1/<method> (token in %s, send as Authorization: Bearer)" % (p, token_path(home)), file=out, flush=True)
    print("dashboard: http://127.0.0.1:%d/#token=<the token in that file>; management %s" % (p, "ON (owner token required)" if manage else "off (read-only; --manage to enable)"),
          file=out, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
