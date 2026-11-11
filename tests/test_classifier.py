"""Pane classifier against real Claude Code 2.1.322 captures (fixtures/) plus synthetic cases."""
import importlib.machinery
import importlib.util
import os
import unittest
from helpers import FIXTURES, PRO

loader = importlib.machinery.SourceFileLoader("pro_mod", PRO)
spec = importlib.util.spec_from_loader("pro_mod", loader)
pro = importlib.util.module_from_spec(spec)
loader.exec_module(pro)


def fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class Classifier(unittest.TestCase):
    def test_real_fixtures(self):
        self.assertEqual(pro.classify_pane(fx("trust.txt")), "trust")
        self.assertEqual(pro.classify_pane(fx("idle.ansi")), "prompt")  # ghost suggestion is not a draft
        self.assertEqual(pro.classify_pane(fx("draft.ansi")), "draft")
        self.assertEqual(pro.classify_pane(fx("working.ansi")), "working")
        self.assertEqual(pro.classify_pane(fx("permission.ansi")), "permission")

    def test_trust_keys_pointer_on_no(self):
        self.assertEqual(pro.trust_keys(fx("trust.txt")), ["Down", "Enter"])
        self.assertEqual(pro.trust_keys(fx("trust.txt").replace("❯ No, exit", "  No, exit").replace("  Yes, I trust", "❯ Yes, I trust")), ["Enter"])

    def test_synthetic(self):
        self.assertEqual(pro.classify_pane("some output\n[pro: exited 1]\n"), "gone")
        self.assertEqual(pro.classify_pane("You've hit your usage limit. Resets at 5pm\n❯ \n"), "limit")

    def test_unknown_is_never_idle(self):
        for text in ("", "$ ", "user@host:~$ \n", "random\ntext\n"):
            self.assertEqual(pro.classify_pane(text), "unknown", text)


if __name__ == "__main__":
    unittest.main()
