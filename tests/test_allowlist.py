import json
import os
import subprocess
import sys
import unittest
from helpers import *


class Allowlist(ProTestCase):
    def test_unknown_name_refused(self):
        for cmd in (["summon", "nobody"], ["send", "nobody", "hi"], ["ask", "nobody", "hi", "--wait", "2"]):
            r = self.pro(*cmd)
            self.assertEqual(r.returncode, 4, (cmd, r.stderr))
            self.assertIn("not in your professionals list", r.stderr)

    def test_disabled_refused_for_summon_send_and_ask(self):
        self.summon("alpha")
        self.ok("disable", "alpha")
        self.wait_until(lambda: self.sessions_alive() == [], 10, "disable stops the live session")
        for cmd in (["summon", "alpha"], ["send", "alpha", "ECHO x"], ["ask", "alpha", "ECHO x", "--wait", "2"]):
            r = self.pro(*cmd)
            self.assertEqual(r.returncode, 4, (cmd, r.stderr))
            self.assertIn("disabled", r.stderr)
        self.assertEqual(self.sessions_alive(), [])
        self.ok("enable", "alpha")
        self.summon("alpha")

    def test_json_error_codes(self):
        self.ok("disable", "alpha")
        r = self.pro("summon", "alpha", "--json")
        self.assertEqual(r.returncode, 4)
        self.assertEqual(json.loads(r.stdout)["error"], "disabled")
        r = self.pro("summon", "nobody", "--json")
        self.assertEqual(json.loads(r.stdout)["error"], "not_listed")
        r = self.pro("add", self.tmp, "--force", "--name", "alpha", "--json")
        self.assertEqual(r.returncode, 2)

    def test_marketplace_newcomer_is_available_not_enabled(self):
        self.listing()  # seeds the registry
        make_persona(self.mdir, "epsilon")
        mj = os.path.join(self.mdir, ".claude-plugin", "marketplace.json")
        d = json.load(open(mj))
        d["plugins"].append({"name": "pro-epsilon", "source": "./plugins/pro-epsilon", "description": "fixture epsilon", "tags": ["test"]})
        json.dump(d, open(mj, "w"))
        self.assertEqual(self.listing()["epsilon"]["state"], "available")
        r = self.pro("summon", "epsilon")
        self.assertEqual(r.returncode, 4)
        self.ok("enable", "epsilon")
        self.assertEqual(self.listing()["epsilon"]["state"], "enabled")
        self.summon("epsilon")

    def test_updated_persona_needs_enable(self):
        self.listing()
        p = os.path.join(self.mdir, "plugins", "pro-alpha", "skills", "alpha", "SKILL.md")
        with open(p, "a") as f:
            f.write("\nExtra line that changes the hash.\n")
        self.assertEqual(self.listing()["alpha"]["state"], "updated")
        r = self.pro("summon", "alpha", "--json")
        self.assertEqual((r.returncode, json.loads(r.stdout)["error"]), (4, "changed"))
        self.ok("enable", "alpha")
        self.summon("alpha")

    def test_list_changes_refused_in_professional_session(self):
        d = self.make_folder("sneaky")
        for cmd in (["add", d], ["remove", "alpha"], ["enable", "alpha"], ["disable", "alpha"], ["keep", "alpha"], ["rate", "alpha", "good"]):
            r = self.pro(*cmd, as_pro="beta")
            self.assertEqual(r.returncode, 4, (cmd, r.stderr))
            self.assertIn("only the user may", r.stderr)
        self.assertNotIn("sneaky", self.listing())
        self.assertTrue(self.listing()["alpha"]["enabled"])

    def test_env_unset_inside_pros_tmux_still_refused(self):
        self.summon("alpha")  # starts the pros tmux server
        d = self.make_folder("viatmux")
        out = os.path.join(self.tmp, "viatmux.out")
        shell = "env -u PRO_SELF -u PRO_SESSION PRO_HOME=%s PRO_TMUX_SOCKET=%s PRO_MARKETPLACE=%s %s %s add %s > %s 2>&1; echo rc=$? >> %s" % (
            self.home, self.sock, self.mdir, sys.executable, PRO, d, out, out)
        subprocess.run(["tmux", "-L", self.sock, "-f", "/dev/null", "new-session", "-d", "-s", "attacker", "sh", "-c", shell], check=True)
        self.wait_until(lambda: os.path.exists(out) and "rc=" in open(out).read(), 15, "attacker command")
        text = open(out).read()
        self.assertIn("rc=4", text)
        self.assertIn("only the user may", text)
        self.assertNotIn("viatmux", self.listing())

    def test_registry_edit_outside_pros_is_flagged_and_refused(self):
        self.listing()
        p = os.path.join(self.home, "registry.json")
        d = json.load(open(p))
        d["entries"]["alpha"]["enabled"] = True
        d["entries"]["evil"] = {"name": "evil", "kind": "folder", "path": self.tmp, "enabled": True}
        json.dump(d, open(p, "w"))
        r = self.pro("list")
        self.assertIn("changed outside pros", r.stderr)
        r = self.pro("summon", "alpha", "--json")
        self.assertEqual((r.returncode, json.loads(r.stdout)["error"]), (4, "registry_changed"))
        r = self.pro("enable", "alpha")
        self.assertEqual(r.returncode, 4)
        r = self.pro("doctor")
        self.assertIn("changed outside pros", r.stdout)
        r = self.pro("doctor", "--repair", as_pro="beta")
        self.assertNotEqual(r.returncode, 0)
        self.ok("doctor", "--repair")
        self.summon("alpha")

    def test_every_session_denies_pros_home(self):
        self.summon("alpha")
        info = self.argv_info(self.sessions_alive()[0])
        deny = json.loads(info["--settings"])["permissions"]["deny"]
        for part in ("registry.json", "/history", "/run"):
            self.assertTrue(any(x.startswith("Edit(") and part in x for x in deny), part)
            self.assertTrue(any(x.startswith("Read(") and part in x for x in deny), part)
        self.assertFalse(any(x.startswith("Read(//%s/home" % self.home.lstrip("/")) for x in deny))

    def test_protocol_says_list_is_user_only(self):
        self.summon("alpha")
        info = self.argv_info(self.sessions_alive()[0])
        self.assertIn("Only the user changes the list of professionals", info["--append-system-prompt-file"])

    def test_list_inside_session_is_fenced_as_data(self):
        r = self.pro("list", as_pro="beta")
        self.assertIn("not instructions", r.stdout)


if __name__ == "__main__":
    unittest.main()
