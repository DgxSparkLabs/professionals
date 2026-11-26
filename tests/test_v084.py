"""0.8.4 (card 1586): a professional never starts on Claude Code's own default model by accident.  With no --model, entry,
persona or config model, pros passes the built-in default `opus`; and the pump reads the model a session really runs on
(the transcript) and tells the user once when it is a blocked one (fable) the user did not store on that professional."""
import json
import os
import unittest

from helpers import ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DefaultModel(ProTestCase):
    def argv(self, s):
        return self.argv_all(s)[-1]["argv"]

    def flag(self, argv, f):
        return argv[argv.index(f) + 1] if f in argv else None

    def test_folder_pro_without_any_model_gets_opus(self):
        self.ok("add", self.make_folder("dm"))
        self.summon("dm")
        self.assertEqual(self.flag(self.argv("pro-dm"), "--model"), "opus")

    def test_config_model_still_wins(self):
        self.config({"model": "claude-sonnet-5-5"})
        self.ok("add", self.make_folder("dn"))
        self.summon("dn")
        self.assertEqual(self.flag(self.argv("pro-dn"), "--model"), "claude-sonnet-5-5")


class SeenModel(ProTestCase):
    def say(self, s, model):
        with open(os.path.join(self.home, "run", "state", s + ".json")) as f:
            tp = json.load(f)["transcript_path"]
        with open(tp, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": "hi"}]}}) + "\n")
        os.utime(tp)

    def notes(self):
        return [m for m in self.msgs() if m["to"] == "user" and m["kind"] == "note" and "blocked" in m["body"]]

    def row(self, name):
        return [r for r in json.loads(self.ok("team", "--json").stdout) if r["name"] == name][0]

    def test_a_blocked_model_seen_at_runtime_is_reported_once(self):
        self.ok("add", self.make_folder("sf"))
        self.summon("sf")
        self.say("pro-sf", "claude-fable-5-2")
        self.ok("pump")
        self.ok("pump")
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("claude-fable-5-2", self.notes()[0]["body"])
        self.assertEqual(self.row("sf")["seen_model"], "claude-fable-5-2")
        self.assertIn("claude-fable-5-2 (blocked)", self.ok("team").stdout)

    def test_allowed_or_stored_models_are_not_reported(self):
        self.ok("add", self.make_folder("sg"))
        self.summon("sg")
        self.say("pro-sg", "claude-opus-5-5")
        self.ok("pump")
        self.assertEqual(self.row("sg")["seen_model"], "claude-opus-5-5")
        self.ok("add", self.make_folder("sh"), "--model", "claude-fable-5-2")  # the user's per-professional exception
        self.summon("sh")
        self.say("pro-sh", "claude-fable-5-2")
        self.ok("pump")
        self.assertEqual(self.notes(), [])


class Documented(unittest.TestCase):
    def test_documented(self):
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            self.assertIn("## 0.8.4", f.read())
        with open(os.path.join(ROOT, "docs", "spec", "spec.md"), encoding="utf-8") as f:
            t = f.read()
        self.assertIn("seen_model", t)
        self.assertIn("built-in default `opus`", t)


if __name__ == "__main__":
    unittest.main()
