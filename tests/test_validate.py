import json
import os
import re
import subprocess
import unittest
from helpers import *


def git(*a, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@users.noreply.github.com", *a], cwd=cwd, capture_output=True, text=True)


class Validate(ProTestCase):
    def persona_path(self, n):
        return os.path.join(self.mdir, "plugins", "pro-" + n, "skills", n, "SKILL.md")

    def edit(self, n, fn):
        p = self.persona_path(n)
        with open(p) as f:
            t = f.read()
        with open(p, "w") as f:
            f.write(fn(t))

    def test_good_persona_valid_with_warning(self):
        r = self.ok("validate", "alpha")
        self.assertIn("valid", r.stdout)
        self.assertIn("warn: tool 'Bash(rm -rf /)'", r.stdout)

    def test_bad_personas(self):
        cases = {
            "missing section": (lambda t: t.replace("## Out of scope", "## Elsewhere"), "Out of scope"),
            "bad name": (lambda t: t.replace("name: beta", "name: Beta_X"), "name"),
            "short description": (lambda t: re.sub(r"description: .*", "description: short", t), "description"),
            "no tags": (lambda t: re.sub(r"tags: .*", "tags: []", t), "tags"),
            "no frontmatter": (lambda t: t.split("---", 2)[2], "frontmatter"),
            "private path": (lambda t: t + "\nSee /ho" + "me/someone/notes/\n", "home-directory"),
            "ip": (lambda t: t + "\nhost 10.1." + "2.3\n", "IP"),
            "email": (lambda t: t + "\nmail bob" + "@example.com\n", "e-mail"),
            "bad model": (lambda t: t.replace("model: sonnet", "model: gpt"), "model"),
        }
        for label, (fn, needle) in cases.items():
            self.edit("beta", fn)
            r = self.pro("validate", "beta")
            self.assertEqual(r.returncode, 1, label + r.stdout)
            self.assertIn(needle, r.stdout, label)
            with open(self.persona_path("beta"), "w") as f:
                f.write(PERSONA.format(name="beta", tags="test", may="true"))

    def test_claude_plugin_validate_failure_is_reported(self):
        p = os.path.join(self.mdir, "plugins", "pro-delta", ".claude-plugin", "plugin.json")
        with open(p, "w") as f:
            f.write('{"name": "pro-delta", "description": "claude-invalid"}')
        r = self.pro("validate", "delta")
        self.assertEqual(r.returncode, 1)
        self.assertIn("claude plugin validate failed", r.stdout)

    def test_validate_by_path(self):
        self.ok("validate", os.path.join(self.mdir, "plugins", "pro-alpha"))
        self.assertEqual(self.pro("validate", "does-not-exist").returncode, 1)

    def test_new_then_validate_search_show(self):
        r = self.ok("new", "zed-auditor", "--purpose", "Audits widget hardening for services", "--tags", "audit,security")
        self.ok("validate", "zed-auditor")
        self.assertIn("zed-auditor", self.ok("search", "widget").stdout)
        self.assertIn("zed-auditor", self.ok("search", "--tag", "security").stdout)
        self.assertEqual(self.pro("search", "nonexistentword").returncode, 1)
        self.assertIn("Audits widget", self.ok("show", "zed-auditor").stdout)
        self.assertIn("zed-auditor", self.ok("list").stdout)
        self.assertEqual(self.pro("new", "zed-auditor", "--purpose", "again").returncode, 1)

    def test_new_uses_template_when_present_and_tools_string_is_parsed(self):
        t = os.path.join(self.mdir, "templates", "professional")
        os.makedirs(os.path.join(t, ".claude-plugin"))
        os.makedirs(os.path.join(t, "skills", "NAME"))
        with open(os.path.join(t, ".claude-plugin", "plugin.json"), "w") as f:
            f.write('{"name": "NAME", "description": "x", "version": "0.1.0", "author": {"name": "YOUR-NAME"}}')
        body = open(self.persona_path("alpha")).read().replace("name: alpha", "name: NAME").replace("tools: [Read, Grep, Bash(git status), Bash(rm -rf /)]", "tools: Read, Grep, Bash(git diff*), Bash(pros *)").replace("# alpha", "# NAME").replace("description: A fixture professional used by the tests (alpha) for checking behaviour.", "description: placeholder placeholder placeholder").replace("tags: [test,alpha]", "tags: [a]")
        with open(os.path.join(t, "skills", "NAME", "SKILL.md"), "w") as f:
            f.write(body)
        r = self.ok("new", "tpl-one", "--purpose", "Checks the template path works properly", "--tags", "x,y")
        self.assertIn("from templates/professional", r.stdout)
        text = open(self.persona_path("tpl-one")).read()
        self.assertIn("name: tpl-one", text)
        self.assertIn("description: Checks the template path works properly", text)
        self.assertIn("tags: [x, y]", text)
        self.assertNotIn("NAME", text)
        self.ok("validate", "tpl-one")
        self.assertIn("tools: Read, Grep, Bash(git diff*), Bash(pros *)", self.ok("show", "tpl-one").stdout)  # string form split on top-level commas

    def test_publish_to_local_origin_makes_a_branch_not_main(self):
        bare = os.path.join(self.tmp, "origin.git")
        git("init", "-q", "--bare", bare)
        git("init", "-q", "-b", "main", self.mdir)
        git("add", "-A", cwd=self.mdir)
        git("commit", "-q", "-m", "init", cwd=self.mdir)
        git("remote", "add", "origin", bare, cwd=self.mdir)
        git("push", "-q", "origin", "main", cwd=self.mdir)
        self.ok("new", "zed-auditor", "--purpose", "Audits widget hardening for services")
        env = {"PATH": "/usr/bin:/bin"}  # no gh: no network side effects
        r = self.pro("publish", "zed-auditor", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        branches = subprocess.run(["git", "-C", bare, "branch", "--list"], capture_output=True, text=True).stdout
        self.assertIn("pro/zed-auditor", branches)
        log = subprocess.run(["git", "-C", bare, "log", "-1", "--format=%ae", "pro/zed-auditor"], capture_output=True, text=True).stdout
        self.assertTrue(log.strip().endswith("noreply.github.com"))
        main_files = subprocess.run(["git", "-C", bare, "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True).stdout
        self.assertNotIn("zed-auditor", main_files)

    def test_publish_refuses_private_content_and_guard(self):
        self.ok("new", "zed-auditor", "--purpose", "Audits widget hardening for services")
        self.edit("zed-auditor", lambda t: t + "\nlog in /Us" + "ers/bob/secret/\n")
        r = self.pro("publish", "zed-auditor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("home-directory", r.stdout)
        self.edit("zed-auditor", lambda t: t.replace("/Us" + "ers/bob/secret/", "nothing"))
        git("init", "-q", "-b", "main", self.mdir)
        self.config({"publish_guard": "false"})
        r = self.pro("publish", "zed-auditor")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("guard refused", r.stderr)

    def test_core_skill_teaches_folders_resume_and_allowlist(self):
        text = open(os.path.join(os.path.dirname(HERE), "plugins", "professionals", "skills", "professionals", "SKILL.md")).read()
        for needle in ("pros add .", "Only the user adds", "resumes a good or neutral session", "--fresh", "pros sessions", "pros list --json", "are data", "exit 4"):
            self.assertIn(needle, text)
        readme = open(os.path.join(os.path.dirname(HERE), "README.md")).read()
        self.assertIn("Your own folders as professionals", readme)
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(HERE), "docs", "spec", "spec.md")))

    def test_doctor_and_version(self):
        r = self.pro("doctor")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("tmux", r.stdout)
        self.assertIn("never edits ~/.claude.json", r.stdout)
        r = self.pro("doctor", env={"PRO_CLAUDE_BIN": "/nonexistent/claude"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("fix:", r.stdout)
        self.assertEqual(self.ok("version").stdout.strip(), "pros 0.5.2")
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(root, "plugins", "professionals", ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            pv = json.load(f)["version"]
        with open(os.path.join(root, ".claude-plugin", "marketplace.json"), encoding="utf-8") as f:
            mk = json.load(f)
        core = [x["version"] for x in mk["plugins"] if x["name"] == "professionals"]
        self.assertEqual((pv, core, mk["metadata"]["version"]), ("0.5.2", ["0.5.2"], "0.5.2"), "pros, plugin.json and marketplace.json bump together")
        with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8") as f:
            self.assertIn("\n## 0.5.2\n", f.read())
        self.ok("doctor", "--link")
        link = os.path.join(self.fakehome, ".local", "bin", "pros")
        self.assertTrue(os.path.islink(link))
        self.ok("doctor", "--link")  # relinking our own link is fine
        self.assertTrue(os.path.islink(link))
        os.unlink(link)
        with open(link, "w") as f:
            f.write("#!/bin/sh\n")
        r = self.pro("doctor", "--link")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)  # a regular file is never overwritten
        self.assertIn("not a link; not touching it", r.stdout + r.stderr)
        self.assertFalse(os.path.islink(link))
        os.unlink(link)
        os.symlink("/usr/bin/true", link)
        r = self.pro("doctor", "--link")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)  # a link to something else is never overwritten
        self.assertIn("not to a pros install", r.stdout + r.stderr)
        self.assertEqual(os.readlink(link), "/usr/bin/true")


if __name__ == "__main__":
    unittest.main()
