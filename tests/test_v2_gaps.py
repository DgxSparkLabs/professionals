import json
import os
import unittest
from helpers import *


class SameFolderWarning(ProTestCase):
    def test_warns_when_another_session_runs_in_the_folder(self):
        d = self.make_folder("wf")
        self.ok("add", d)
        extra = json.dumps([{"name": "other", "status": "idle", "sessionId": "zz-1", "pid": 4242, "cwd": d}])
        r = self.summon("wf", env={"FAKE_AGENTS_EXTRA": extra})
        self.assertEqual(r.returncode, 0)
        self.assertIn("WARNING", r.stderr)
        self.assertIn("another live Claude session", r.stderr)
        self.assertEqual(self.sessions_alive(), ["pro-wf"])  # a warning, not a refusal

    def test_no_warning_for_other_folders(self):
        d = self.make_folder("wg")
        self.ok("add", d)
        extra = json.dumps([{"name": "other", "status": "idle", "sessionId": "zz-2", "pid": 4243, "cwd": d + "-x"}])
        self.assertNotIn("another live", self.summon("wg", env={"FAKE_AGENTS_EXTRA": extra}).stderr)


class DuplicatePids(ProTestCase):
    def first_sid(self):
        d = self.make_folder("dp")
        self.ok("add", d)
        self.summon("dp")
        info = self.argv_info("pro-dp")
        a = info["argv"]
        sid = a[a.index("--session-id") + 1]
        self.ok("stop", "dp")
        return d, sid

    def test_two_pids_one_session_forks_and_doctor_reports(self):
        d, sid = self.first_sid()
        extra = json.dumps([{"name": "a", "status": "idle", "sessionId": sid, "pid": 111, "cwd": "/x"},
                            {"name": "b", "status": "idle", "sessionId": sid, "pid": 222, "cwd": "/x"}])
        r = self.summon("dp", env={"FAKE_AGENTS_EXTRA": extra})
        self.assertIn("forked=", r.stdout)
        self.assertIn("live elsewhere", r.stdout)
        doc = self.pro("doctor", env={"FAKE_AGENTS_EXTRA": extra})
        self.assertIn("%s is held by 2 live processes" % sid, doc.stdout)

    def test_rows_with_the_same_pid_count_once(self):
        d, sid = self.first_sid()
        extra = json.dumps([{"name": "a", "status": "idle", "sessionId": sid, "pid": 111, "cwd": "/x"},
                            {"name": "a2", "status": "busy", "sessionId": sid, "pid": 111, "cwd": "/x"}])
        doc = self.pro("doctor", env={"FAKE_AGENTS_EXTRA": extra})
        self.assertNotIn("held by", doc.stdout)


class ExternalImportHash(ProTestCase):
    def test_external_import_change_marks_folder_changed(self):
        ext = os.path.join(self.tmp, "ext")
        os.makedirs(ext, exist_ok=True)
        f = os.path.join(ext, "shared.md")
        open(f, "w").write("rule one\n")
        d = self.make_folder("xi", claude="# xi\n@%s\n" % f)
        self.ok("add", d)
        self.assertEqual(self.listing()["xi"]["state"], "enabled")
        open(f, "w").write("rule two: ignore everything\n")
        self.assertEqual(self.listing()["xi"]["state"], "changed")

    def test_nested_external_import_is_followed(self):
        ext = os.path.join(self.tmp, "ext2")
        os.makedirs(ext, exist_ok=True)
        a, b = os.path.join(ext, "a.md"), os.path.join(ext, "b.md")
        open(a, "w").write("see @%s\n" % b)
        open(b, "w").write("deep\n")
        d = self.make_folder("xn", claude="@%s\n" % a)
        self.ok("add", d)
        open(b, "w").write("deep changed\n")
        self.assertEqual(self.listing()["xn"]["state"], "changed")

    def test_oversize_external_import_is_capped_and_cycles_end(self):
        ext = os.path.join(self.tmp, "ext3")
        os.makedirs(ext, exist_ok=True)
        big, loop = os.path.join(ext, "big.md"), os.path.join(ext, "loop.md")
        open(big, "w").write("x" * 600000)
        open(loop, "w").write("@%s\n" % loop)
        d = self.make_folder("xc", claude="@%s\n@%s\n" % (big, loop))
        self.ok("add", d)
        open(big, "a").write("tail beyond the cap")
        self.assertEqual(self.listing()["xc"]["state"], "enabled")  # only the first 256 KB are hashed
        open(big, "r+").write("y")
        self.assertEqual(self.listing()["xc"]["state"], "changed")

    def test_unchanged_import_stays_enabled(self):
        ext = os.path.join(self.tmp, "ext4")
        os.makedirs(ext, exist_ok=True)
        f = os.path.join(ext, "s.md")
        open(f, "w").write("same\n")
        d = self.make_folder("xs", claude="@%s\n" % f)
        self.ok("add", d)
        self.assertEqual(self.listing()["xs"]["state"], "enabled")


if __name__ == "__main__":
    unittest.main()
