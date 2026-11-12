// pros audit dashboard: a static page over the loopback query API (the same envelopes the CLI prints with --json).
// The owner token comes from the URL fragment (#token=...), which the browser never sends; it is kept in memory and
// sessionStorage of this origin only. Every value is written with textContent; nothing from the store becomes HTML.
"use strict";
(function () {
  var TOKEN = "";
  var STATE = { view: "overview", trace: null, pro: null, settings: null };
  var FILTERS = ["professional", "revision", "model", "task_class", "direct"];
  var METRICS_SPEND = ["accounted_tokens", "cost_estimate", "cache_read_share", "unpriced_share", "attribution_share", "context_footprint"];
  var METRICS_QUALITY = ["evaluation_coverage", "pass_rate", "deadline_miss_rate", "reply_rate", "failures_by_reason", "delivery_retries"];

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k];
      else if (k === "cls") e.className = attrs[k];
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), attrs[k]);
      else e.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { if (c != null) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return e;
  }
  function ts(t) { return t == null ? "-" : new Date(t * 1000).toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z"); }
  function ratio(r) { return r == null ? "n/a" : r[0] + "/" + r[1]; }
  function num(x) { return x == null ? "-" : String(x); }
  function link(text, fn) { return el("a", { cls: "link", text: text, href: "#", onclick: function (ev) { ev.preventDefault(); fn(); } }); }
  function table(head, rows) {
    var t = el("table", {}, [el("thead", {}, [el("tr", {}, head.map(function (h) { return el("th", { text: h }); }))])]);
    var b = el("tbody");
    rows.forEach(function (r) {
      b.appendChild(el("tr", {}, r.map(function (c) { return c instanceof Node ? el("td", {}, [c]) : el("td", { text: num(c) }); })));
    });
    t.appendChild(b);
    return el("div", { cls: "scroll" }, [t]);
  }
  function err(msg) { $("err").textContent = msg || ""; }

  function filterArgs(withWindow) {
    var f = $("filters"), a = {};
    if (withWindow) {
      a.since = f.since.value.trim() || "30d";
      if (f.until.value.trim()) a.until = f.until.value.trim();
    }
    FILTERS.forEach(function (k) { var v = f[k].value.trim(); if (v) a[k] = v; });
    return a;
  }
  function qs(args) {
    return Object.keys(args).filter(function (k) { return args[k] !== "" && args[k] != null; })
      .map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(args[k]); }).join("&");
  }
  function api(method, args) {
    var q = qs(args || {});
    return fetch("/api/v1/" + method + (q ? "?" + q : ""), { headers: { Authorization: "Bearer " + TOKEN } })
      .then(function (r) { return r.json().then(function (j) { return check(r, j); }); });
  }
  function manageGet(name) {
    return fetch("/manage/v1/" + name, { headers: { Authorization: "Bearer " + TOKEN } })
      .then(function (r) { return r.json().then(function (j) { return check(r, j); }); });
  }
  function managePost(action, body) {
    return fetch("/manage/v1/" + action, { method: "POST", headers: { Authorization: "Bearer " + TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return check(r, j); }); });
  }
  function check(r, j) {
    if (r.status === 401) { $("token-box").hidden = false; }
    if (j.error) throw new Error(j.error.code + ": " + j.error.message + (j.error.field ? " (" + j.error.field + ")" : ""));
    if (j.snapshot) head(j);
    return j;
  }
  function head(env) {
    var s = env.snapshot, c = env.coverage;
    $("snap").textContent = "snapshot " + s.id + " · generation " + s.generation + " · pricing " + s.pricing.table_id + " " + s.pricing.dated + " · cutoff " + ts(c.cutoff);
    var d = $("degraded");
    d.hidden = !c.degraded;
    d.textContent = c.degraded ? "DEGRADED: " + (c.degraded_cause || "unknown cause") + " (figures may be incomplete)" : "";
  }
  function coverageBlock(c) {
    var rows = c.sources.map(function (s) { return [s.source, s.quality, ts(s.from_t), ts(s.to_t), s.rows]; });
    var box = el("div", {}, [el("h3", { text: "Coverage" }), table(["source", "quality", "from", "to", "rows"], rows)]);
    if (c.pre_audit_before != null) box.appendChild(el("p", { cls: "muted", text: "Before " + ts(c.pre_audit_before) + " only backfilled data exists." }));
    if (c.gaps.length) box.appendChild(table(["gap", "count"], c.gaps.map(function (g) { return [g.kind, g.count]; })));
    return box;
  }
  function card(title, big, lines) {
    return el("div", { cls: "card" }, [el("div", { cls: "muted", text: title }), el("div", { cls: "big", text: big })].concat(lines.map(function (l) { return el("div", { text: l }); })));
  }
  function show(nodes) { var v = $("view"); v.textContent = ""; nodes.forEach(function (n) { if (n) v.appendChild(n); }); }

  // ------------------------------------------------------------------ views
  function overview() {
    var a = filterArgs(true), gb = $("filters").group_by.value.trim();
    if (gb) a.group_by = gb;
    return Promise.all([api("status"), api("query_metrics", a)]).then(function (r) {
      var st = r[0].result, env = r[1], cards = env.result.cards, w = env.result.window;
      var aw = cards.assigned_work, ev = cards.evaluation, dl = cards.deadline_miss, du = cards.durations, tk = cards.accounted_tokens;
      var grid = el("div", { cls: "cards" }, [
        card("Assigned work", String(aw.assignments), [aw.root_tasks + " root, " + aw.child_assignments + " child", "refusals " + JSON.stringify(aw.refusals_by_reason)]),
        card("Evaluation (root tasks)", ratio(ev.root_tasks.pass_rate), ["pass rate; coverage " + ratio(ev.root_tasks.coverage), "not completed " + ev.root_tasks.not_completed]),
        card("Evaluation (child)", ratio(ev.child_assignments.pass_rate), ["pass rate; coverage " + ratio(ev.child_assignments.coverage)]),
        card("Deadline misses", ratio(dl.all.rate), ["late answers among missed " + dl.all.late_answers_among_missed, "not yet due " + dl.all.not_yet_due]),
        card("Queue time", num(du.queue.p50_s) + " s", ["p95 " + num(du.queue.p95_s) + " s; n " + du.queue.n + " of " + du.queue.eligible]),
        card("Reply latency", num(du.reply_latency_from_first_delivery.p50_s) + " s", ["p95 " + num(du.reply_latency_from_first_delivery.p95_s) + " s"]),
        card("Accounted tokens", String(tk.total_unique), [tk.linked_task + " linked + " + tk.session_only + " session-only", "cost " + num(tk.cost_usd_api_equivalent) + " USD API-equivalent (" + tk.price_state + ")"])
      ]);
      var out = [el("h2", { text: "Overview " + ts(w.from) + " .. " + ts(w.to) }), grid,
        el("p", { cls: "muted", text: "Store rows: " + Object.keys(st).sort().map(function (k) { return k + " " + st[k]; }).join(", ") })];
      if (env.result.groups && env.result.groups.length) {
        out.push(el("h3", { text: "Groups" }));
        out.push(table(["key", "n", "assignments", "tokens", "small sample"], env.result.groups.map(function (g) {
          return [Object.keys(g.key).map(function (k) { return k + "=" + g.key[k]; }).join(", "), g.n, g.cards.assigned_work.assignments, g.cards.accounted_tokens.total_unique, g.small_sample ? "yes" : ""];
        })));
      }
      out.push(coverageBlock(env.coverage));
      return out;
    });
  }

  function professionals() {
    var a = filterArgs(false), w = filterArgs(true);
    var args = { since: w.since, limit: 200 };
    if (w.until) args.until = w.until;
    return api("list_professionals", args).then(function (env) {
      var rows = env.result.items.map(function (p) {
        return [link(p.name, function () { STATE.pro = p.name; go("professionals"); }), p.origin, p.assignments, p.runs, ts(p.first_seen), ts(p.last_seen), p.removed ? "removed" : ""];
      });
      var out = [el("h2", { text: "Professionals" }), table(["name", "origin", "assignments", "runs", "first seen", "last seen", ""], rows)];
      if (!STATE.pro) return out;
      var ea = Object.assign({}, w, { metric: "assigned_work", professional: STATE.pro });
      return Promise.all([api("explain_metric", ea), api("list_runs", { professional: STATE.pro, limit: 50 })]).then(function (r) {
        var ex = r[0].result;
        out.push(el("h3", { text: STATE.pro + ": assignments in the window (" + ex.included_total + ")" }));
        out.push(el("ul", {}, ex.included_ids.map(function (id) { return el("li", {}, [link(id, function () { openTrace(id); })]); })));
        out.push(el("h3", { text: "Runs" }));
        out.push(table(["run", "kind", "session", "started", "ended", "end reason", "assignments"], r[1].result.items.map(function (x) {
          return [x.run, x.kind, x.session, ts(x.started), ts(x.ended), x.end_reason, x.assignments];
        })));
        return out;
      });
    });
  }

  function openTrace(id) { STATE.trace = id; go("trace"); }

  function timeline(r) {
    var pts = [];
    r.assignments.forEach(function (a) { [a.created, a.first_delivery, a.answered_at, a.deadline].forEach(function (t) { if (t != null) pts.push(t); }); });
    r.waits.forEach(function (x) { if (x.start != null) pts.push(x.start); if (x.end != null) pts.push(x.end); });
    r.spans.forEach(function (s) { pts.push(s.start); if (s.end != null) pts.push(s.end); });
    if (!pts.length) return el("p", { cls: "muted", text: "No times recorded." });
    var t0 = Math.min.apply(null, pts), t1 = Math.max.apply(null, pts), span = Math.max(t1 - t0, 0.001);
    function bar(from, to, cls, title) {
      var l = (from - t0) / span * 100, wd = Math.max(((to == null ? t1 : to) - from) / span * 100, 0.4);
      var b = el("span", { cls: cls, title: title });
      b.style.left = l.toFixed(2) + "%";  // CSSOM, allowed by the page's CSP (no inline style attributes)
      b.style.width = wd.toFixed(2) + "%";
      return b;
    }
    var rows = r.assignments.map(function (a) {
      var bars = [];
      if (a.first_delivery != null) bars.push(bar(a.created, a.first_delivery, "queue", "queued"));
      bars.push(bar(a.first_delivery != null ? a.first_delivery : a.created, a.answered_at, a.deadline_status === "late" || a.deadline_status === "missed" ? "late" : "", a.delivery + ", deadline " + a.deadline_status));
      r.spans.filter(function (s) { return s.msg === a.msg; }).forEach(function (s) { bars.push(bar(s.start, s.end, "", s.kind + (s.name ? " " + s.name : ""))); });
      return [a.msg + " → " + a.to, el("div", { cls: "tl" }, bars)];
    });
    r.waits.forEach(function (x) { rows.push(["wait " + x.wait + " (" + x.caller + ")", el("div", { cls: "tl" }, [bar(x.start, x.end, "wait", "outcome " + x.outcome + ", exit " + x.exit)])]); });
    return el("div", {}, [el("p", { cls: "muted", text: ts(t0) + " .. " + ts(t1) + " (" + (t1 - t0).toFixed(2) + " s; parallel bars can overlap; grey = queue, red = past deadline, amber = wait)" }),
      table(["row", "timeline"], rows)]);
  }

  function tree(r) {
    var kids = {};
    r.assignments.forEach(function (a) { (kids[a.parent || ""] = kids[a.parent || ""] || []).push(a); });
    function node(a) {
      var o = a.own_tokens, e = a.evaluation;
      var li = el("li", {}, [el("strong", { text: a.to }), " ", el("span", { cls: "muted", text: a.msg + " task " + a.task }),
        el("div", { text: "delivery " + a.delivery + " · deadline " + a.deadline_status + " · evaluation " + (e ? e.status : "unreviewed") + " · confidence " + a.confidence }),
        el("div", { cls: "muted", text: "created " + ts(a.created) + ", delivered " + ts(a.first_delivery) + ", answered " + ts(a.answered_at) + (o ? " · own tokens " + o.total + " (" + o.price_state + ")" : " · no usage observed") }),
        link("evidence", function () { evidence(a.msg); })]);
      var ch = kids[a.msg] || [];
      if (ch.length) li.appendChild(el("ul", { cls: "tree" }, ch.map(node)));
      return li;
    }
    var roots = r.assignments.filter(function (a) { return !a.parent || !r.assignments.some(function (b) { return b.msg === a.parent; }); });
    return el("ul", { cls: "tree" }, roots.map(node));
  }

  function evidence(target) {
    api("evaluation_evidence", { target: target }).then(function (env) {
      var r = env.result, box = $("evidence-box");
      if (!box) return;
      box.textContent = "";
      box.appendChild(el("h3", { text: "Evaluation evidence for " + r.target }));
      box.appendChild(el("p", { cls: "muted", text: r.precedence_note }));
      box.appendChild(table(["eval", "status", "kind", "effective at", "rubric", "superseded by", "criteria", "evidence refs"], r.history.map(function (h) {
        var crit = (h.criteria || []).map(function (c) { return c.id + "=" + c.outcome + (c.evidence ? "@" + c.evidence : ""); }).join(", ");
        return [h.eval + (r.effective && r.effective.eval === h.eval ? " (effective)" : ""), h.status, h.evaluator_kind, ts(h.effective_at), h.rubric ? h.rubric + " " + h.rubric_version : "", h.superseded_by || "", crit, (h.evidence || []).join(", ")];
      })));
    }).catch(function (e) { err(e.message); });
  }

  function trace() {
    var form = el("form", { onsubmit: function (ev) { ev.preventDefault(); openTrace(this.elements.id.value.trim()); } },
      [el("label", {}, ["message or task id ", el("input", { name: "id", value: STATE.trace || "", size: 24 })]), " ", el("button", { type: "submit", text: "Open" })]);
    if (!STATE.trace) return Promise.resolve([el("h2", { text: "Trace" }), form, el("p", { cls: "muted", text: "Pick an assignment under Professionals, or type an id." })]);
    return api("task_trace", { id: STATE.trace }).then(function (env) {
      var r = env.result, u = r.usage;
      var spend = table(["scope", "input", "output", "cache write", "cache read", "total", "cost USD (API-eq.)", "price state"],
        Object.keys(u.own).map(function (m) { var o = u.own[m]; return ["own " + m, o.input, o.output, o.cache_write, o.cache_read, o.total, o.cost_usd_api_equivalent, o.price_state]; })
          .concat([["inclusive (trace)", u.inclusive.input, u.inclusive.output, u.inclusive.cache_write, u.inclusive.cache_read, u.inclusive.total, u.inclusive.cost_usd_api_equivalent, u.inclusive.price_state]])
          .concat(u.session_only.map(function (s) { var o = s.tokens; return ["session-only " + s.session, o.input, o.output, o.cache_write, o.cache_read, o.total, o.cost_usd_api_equivalent, o.price_state]; })));
      return [el("h2", { text: "Trace of " + r.root }), form, el("p", { cls: "muted", text: "tasks: " + r.tasks.join(", ") }),
        el("h3", { text: "Delegation tree" }), tree(r),
        el("h3", { text: "Waits (waiter outcomes, not task outcomes)" }),
        table(["wait", "caller", "outcome", "exit", "returned", "shadowed reply"], r.waits.map(function (w) { return [w.wait, w.caller, w.outcome, w.exit, w.returned, w.shadowed_reply ? "yes" : ""]; })),
        el("h3", { text: "Timeline" }), timeline(r),
        el("h3", { text: "Spend" }), spend, el("p", { cls: "muted", text: "unavailable: " + num(u.unavailable) + " · " + u.residual_note }),
        el("h3", { text: "Spans" }),
        table(["span", "kind", "name", "msg", "start", "end", "quality"], r.spans.map(function (s) { return [s.span, s.kind, s.name || "", s.msg, ts(s.start), ts(s.end), s.quality]; })),
        r.gaps.length ? el("pre", { text: "gaps: " + JSON.stringify(r.gaps, null, 1) }) : null,
        el("div", { id: "evidence-box" }), coverageBlock(env.coverage)];
    });
  }

  function explainTable(names) {
    var a = filterArgs(true);
    return Promise.all(names.map(function (m) { return api("explain_metric", Object.assign({ metric: m }, a)); })).then(function (envs) {
      return table(["metric", "numerator", "denominator", "unit", "definition", "coverage", "exclusions"], envs.map(function (e) {
        var r = e.result;
        return [r.metric, JSON.stringify(r.numerator), JSON.stringify(r.denominator), r.unit, r.definition, r.coverage_note, r.exclusions.map(function (x) { return x.reason + " " + x.count; }).join(", ")];
      }));
    });
  }

  function spendView() {
    return Promise.all([api("query_metrics", filterArgs(true)), explainTable(METRICS_SPEND)]).then(function (r) {
      var t = r[0].result.cards.accounted_tokens;
      var cats = Object.keys(t.by_category).map(function (k) { return [k, t.by_category[k]]; });
      var conf = Object.keys(t.confidence).map(function (k) { return ["confidence " + k, t.confidence[k]]; });
      return [el("h2", { text: "Spend and context" }),
        el("div", { cls: "cards" }, [card("Tokens", String(t.total_unique), [t.exchanges + " exchanges", t.linked_task + " linked, " + t.session_only + " session-only"]),
          card("Cost (API-equivalent)", num(t.cost_usd_api_equivalent) + " USD", [t.price_state + "; unpriced share " + ratio(t.unpriced_share)])]),
        table(["category", "tokens"], cats.concat(conf).concat([["cache write 5m", t.cache_write_split["5m"]], ["cache write 1h", t.cache_write_split["1h"]], ["cache write unsplit", t.cache_write_split.unsplit]])),
        el("h3", { text: "Explained figures" }), r[1], coverageBlock(r[0].coverage)];
    });
  }

  function qualityView() {
    return Promise.all([api("query_metrics", filterArgs(true)), explainTable(METRICS_QUALITY)]).then(function (r) {
      var ev = r[0].result.cards.evaluation;
      var rows = ["root_tasks", "child_assignments"].map(function (k) {
        var u = ev[k];
        return [u.unit, ratio(u.coverage), ratio(u.pass_rate), Object.keys(u.counts).map(function (c) { return c + " " + u.counts[c]; }).join(", "), u.not_completed];
      });
      return [el("h2", { text: "Quality and coverage" }), table(["unit", "coverage", "pass rate", "counts", "not completed"], rows),
        el("h3", { text: "Explained figures" }), r[1], coverageBlock(r[0].coverage)];
    });
  }

  function manageView() {
    return Promise.all([manageGet("settings"), manageGet("reports")]).then(function (r) {
      var s = r[0].result, reps = r[1].result.reports, out = [el("h2", { text: "Manage" })];
      STATE.settings = s;
      out.push(el("p", { cls: s.writable ? "ok" : "muted", text: s.writable ? "Management is on for this server: changes are recorded as audit events." : "Read-only server: start it with `pros audit serve --manage` to change settings here." }));
      function form(legend, fields, action, build, after) {
        var f = el("form", {}, fields);
        var out2 = el("pre", { hidden: "hidden" });
        f.addEventListener("submit", function (ev) {
          ev.preventDefault();
          err("");
          var body;
          try { body = build(f.elements); } catch (x) { err(x.message); return; }
          managePost(action, body).then(function (j) { out2.hidden = false; out2.textContent = JSON.stringify(j.result, null, 1); if (after) after(j); }).catch(function (x) { err(x.message); });
        });
        var btn = el("button", { type: "submit", text: legend });
        if (!s.writable) btn.setAttribute("disabled", "disabled");
        f.appendChild(btn);
        return el("fieldset", {}, [el("legend", { text: legend }), f, out2]);
      }
      var modes = el("select", { name: "mode" }, s.capture_modes.map(function (m) { var o = el("option", { text: m }); if (m === s.capture_mode) o.setAttribute("selected", "selected"); return o; }));
      out.push(form("Capture mode", [el("p", { cls: "muted", text: "now: " + s.capture_mode + (s.env_override ? " (PRO_AUDIT overrides it)" : "") }), modes, " "], "capture",
        function (e) { return { mode: e.mode.value }; }));
      var cls = el("select", { name: "class" }, Object.keys(s.retention_days).map(function (k) { return el("option", { text: k }); }));
      out.push(form("Retention", [el("p", { cls: "muted", text: Object.keys(s.retention_days).map(function (k) { return k + " " + s.retention_days[k] + " days"; }).join(" · ") }), cls, " days ", el("input", { name: "days", size: 6 }), " "], "retention",
        function (e) { return { "class": e["class"].value, days: parseInt(e.days.value, 10) }; }));
      out.push(form("Record verdict", [el("label", {}, ["target ", el("input", { name: "target", size: 16, value: STATE.trace && /^m-/.test(STATE.trace) ? STATE.trace : "" })]), " ",
        el("label", {}, ["status ", el("select", { name: "status" }, ["passed", "partial", "failed", "inconclusive"].map(function (x) { return el("option", { text: x }); }))]), " ",
        el("label", {}, ["criteria ", el("input", { name: "criteria", size: 28, placeholder: "C1=passed, C2=failed" })]), " ",
        el("label", {}, ["rubric ", el("input", { name: "rubric", size: 14 })]), " ",
        el("label", {}, ["evidence ", el("input", { name: "evidence", size: 22, placeholder: "kind:token, ..." })]), " "], "verdict",
        function (e) {
          var crit = {};
          e.criteria.value.split(",").map(function (x) { return x.trim(); }).filter(Boolean).forEach(function (x) { var p = x.split("="); crit[p[0].trim()] = (p[1] || "").trim(); });
          var b = { target: e.target.value.trim(), status: e.status.value, criteria: crit };
          if (e.rubric.value.trim()) b.rubric = e.rubric.value.trim();
          var refs = e.evidence.value.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
          if (refs.length) b.evidence = refs;
          return b;
        }));
      out.push(form("Save current overview as a report", [el("label", {}, ["name ", el("input", { name: "name", size: 20 })]), " "], "report_save",
        function (e) { var a = filterArgs(true), gb = $("filters").group_by.value.trim(); if (gb) a.group_by = gb; return { name: e.name.value.trim(), method: "query_metrics", args: a }; },
        function () { go("manage"); }));
      out.push(el("h3", { text: "Saved reports" }));
      out.push(table(["name", "id", "method", "args", "created", ""], reps.map(function (p) {
        return [p.name, p.id, p.definition.method, JSON.stringify(p.definition.args), ts(p.created),
          s.writable ? link("delete", function () { managePost("report_delete", { id: p.id }).then(function () { go("manage"); }).catch(function (x) { err(x.message); }); }) : ""];
      })));
      out.push(form("Export preview", [el("label", {}, ["format ", el("select", { name: "format" }, ["jsonl", "csv", "html"].map(function (x) { return el("option", { text: x }); }))]), " ",
        el("p", { cls: "muted", text: "Uses the window above. Nothing is written; run `pros audit export` in a terminal to write it." })], "export_preview",
        function (e) { var a = filterArgs(true); return { since: a.since, until: a.until, format: e.format.value }; }));
      out.push(form("Purge", [el("label", {}, ["class ", el("select", { name: "class" }, ["operational", "content", "reports"].map(function (x) { return el("option", { text: x }); }))]), " ",
        el("label", {}, ["before (epoch; empty = retention) ", el("input", { name: "before", size: 14 })]), " ",
        el("label", {}, ["type purge to confirm ", el("input", { name: "confirm", size: 8 })]), " ",
        el("p", { cls: "muted", text: "Without the confirmation word this is a dry run that only counts rows." })], "purge",
        function (e) {
          var b = { "class": e["class"].value };
          if (e.before.value.trim()) b.before = parseFloat(e.before.value);
          if (e.confirm.value.trim() === "purge") b.confirm = "purge"; else b.dry_run = true;
          return b;
        }));
      return out;
    });
  }

  var VIEWS = { overview: overview, professionals: professionals, trace: trace, spend: spendView, quality: qualityView, manage: manageView };

  function go(view) {
    STATE.view = view;
    Array.prototype.forEach.call(document.querySelectorAll("#tabs button"), function (b) { b.className = b.getAttribute("data-view") === view ? "on" : ""; });
    err("");
    if (!TOKEN) { $("token-box").hidden = false; show([]); return; }
    show([el("p", { cls: "muted", text: "loading…" })]);
    VIEWS[view]().then(show).catch(function (e) { show([]); err(e.message); });
  }

  function init() {
    var m = /(?:^#|&)token=([0-9a-fA-F]{16,128})/.exec(location.hash || "");
    if (m) {
      TOKEN = m[1];
      try { sessionStorage.setItem("pros-audit-token", TOKEN); } catch (e) { /* storage blocked: memory only */ }
      history.replaceState(null, "", location.pathname + (location.hash || "").replace(/(^#|&)token=[^&]*&?/, "$1").replace(/^#$/, ""));
    } else {
      try { TOKEN = sessionStorage.getItem("pros-audit-token") || ""; } catch (e) { TOKEN = ""; }
    }
    $("token-save").addEventListener("click", function () {
      TOKEN = $("token-input").value.trim();
      try { sessionStorage.setItem("pros-audit-token", TOKEN); } catch (e) { /* memory only */ }
      $("token-input").value = "";
      $("token-box").hidden = true;
      go(STATE.view);
    });
    Array.prototype.forEach.call(document.querySelectorAll("#tabs button"), function (b) {
      b.addEventListener("click", function () { go(b.getAttribute("data-view")); });
    });
    $("filters").addEventListener("submit", function (ev) { ev.preventDefault(); go(STATE.view); });
    var v = /(?:^#|&)view=([a-z]+)/.exec(location.hash || ""), t = /(?:^#|&)id=([A-Za-z0-9._-]{1,64})/.exec(location.hash || "");
    if (t) STATE.trace = t[1];
    go(v && VIEWS[v[1]] ? v[1] : "overview");
  }
  init();
})();
