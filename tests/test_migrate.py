import json
import os
import unittest
from helpers import *


class Migrate(ProTestCase):
    def test_v1_live_session_survives_registry_creation(self):
        self.summon("alpha")
        for f in ("registry.json", "registry.sum", "registry.json.bak"):
            p = os.path.join(self.home, f)
            if os.path.exists(p):
                os.unlink(p)  # a v1 home: live sessions and run state, no registry
        team = json.loads(self.ok("team", "--json").stdout)
        self.assertEqual([t["name"] for t in team], ["alpha"])
        rows = self.listing()  # first v2 command creates and seeds the registry
        self.assertEqual(rows["alpha"]["live"] in ("idle", "busy", "starting"), True)
        self.assertTrue(rows["alpha"]["enabled"])
        r = self.ok("send", "alpha", "ECHO migrated")
        self.assertIn("status=delivered", r.stdout)

    def test_seed_only_from_trusted_marketplaces(self):
        self.config({"trusted_marketplaces": []})
        self.ok("list", "--json")
        reg = json.load(open(os.path.join(self.home, "registry.json")))
        self.assertEqual(reg["entries"], {})
        r = self.pro("summon", "alpha")
        self.assertEqual(r.returncode, 4)
        r = self.pro("enable", "alpha")
        self.assertEqual(r.returncode, 4)
        self.assertIn("not one you added", r.stderr)

    def test_seed_happens_once(self):
        self.listing()
        self.ok("remove", "alpha")
        self.assertNotIn("alpha", json.load(open(os.path.join(self.home, "registry.json")))["entries"])
        self.assertEqual(self.listing()["alpha"]["state"], "available")  # not re-seeded


if __name__ == "__main__":
    unittest.main()
