import os
import tempfile
import unittest
from pathlib import Path

from runner import gitops


class BranchTests(unittest.TestCase):
    def test_branch_name_is_ref_safe(self):
        self.assertEqual(gitops.branch_name("buttons"), "nightly/buttons")
        self.assertEqual(gitops.branch_name("minecraft:bell"), "nightly/bell")
        self.assertEqual(gitops.branch_name("weird name:x"), "nightly/weird_name_x")

    def test_push_guard(self):
        gitops.check_push_target("nightly/buttons", already_exists=False)
        for bad in ("master", "main", "nightly/", "feature/x", "refs/heads/master"):
            with self.assertRaises(gitops.PushRefused):
                gitops.check_push_target(bad, already_exists=False)
        with self.assertRaises(gitops.PushRefused):
            gitops.check_push_target("nightly/buttons", already_exists=True)

    def test_done_parsing(self):
        self.assertFalse(gitops.parse_ls_remote(""))
        self.assertTrue(gitops.parse_ls_remote("abc123\trefs/heads/nightly/buttons\n"))
        self.assertFalse(gitops.parse_pr_list("[]"))
        self.assertTrue(gitops.parse_pr_list('[{"number": 7}]'))


class GateTests(unittest.TestCase):
    def test_checks_only_our_files(self):
        steps = gitops.gate_steps(["tests/a/new.json"])
        self.assertIn(["flint-validate", "tests/a/new.json"], steps)
        self.assertIn(["typos", "tests/a/new.json"], steps)
        self.assertFalse(any("--recursive" in s for s in steps))


class CollectTests(unittest.TestCase):
    def test_only_new_or_changed_test_json_is_copied(self):
        sandbox, repo = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (sandbox / "tests/a").mkdir(parents=True)
        (repo / "tests/a").mkdir(parents=True)
        (sandbox / "tests/a/same.json").write_text("{}")
        (repo / "tests/a/same.json").write_text("{}")
        (sandbox / "tests/a/changed.json").write_text('{"x": 2}')
        (repo / "tests/a/changed.json").write_text('{"x": 1}')
        (sandbox / "tests/a/new.json").write_text("{}")
        (sandbox / "tests/a/notes.txt").write_text("ignored")
        (sandbox / "package.json").write_text("{}")                     # outside tests/: ignored
        os.symlink("/etc/passwd", sandbox / "tests/a/link.json")         # symlink: ignored
        self.assertEqual(gitops.changed_tests(sandbox, repo), ["tests/a/changed.json", "tests/a/new.json"])
        self.assertEqual((repo / "tests/a/changed.json").read_text(), '{"x": 1}')      # listing doesn't copy
        self.assertEqual(gitops.collect_tests(sandbox, repo), ["tests/a/changed.json", "tests/a/new.json"])
        self.assertEqual((repo / "tests/a/changed.json").read_text(), '{"x": 2}')
        self.assertFalse((repo / "package.json").exists())


class PrTests(unittest.TestCase):
    summary = {"tests": [
        {"file": "tests/b/oak.json", "representative": "minecraft:oak_button", "implementation": "ButtonBlock",
         "covers": ["minecraft:birch_button"], "case": "Pressing the button powers it for 30 ticks."},
        {"file": "tests/b/oak2.json", "representative": "minecraft:oak_button", "implementation": "ButtonBlock",
         "covers": [], "case": "An arrow presses a wooden button."},
        {"file": "tests/b/failed.json", "representative": "minecraft:stone_button", "implementation": "ButtonBlock",
         "covers": [], "case": "dropped"}]}

    def test_body_lists_blocks_and_only_passing_cases(self):
        body = gitops.pr_body("buttons", self.summary, ["tests/b/oak.json", "tests/b/oak2.json"])
        self.assertIn("## Blocks\n- minecraft:oak_button → implementation `ButtonBlock`; also covers: minecraft:birch_button", body)
        self.assertIn("- Pressing the button powers it for 30 ticks.", body)
        self.assertNotIn("dropped", body)

    def test_title_counts_blocks(self):
        self.assertEqual(gitops.pr_title("buttons", self.summary), "Add Flint tests for buttons (3 blocks)")


if __name__ == "__main__":
    unittest.main()
