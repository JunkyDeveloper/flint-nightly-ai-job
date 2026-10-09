import os
import sys
import unittest
from datetime import datetime
from unittest import mock

from runner import run
from runner.__main__ import parse_args, seconds_until_deadline


class RunTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {"GITHUB_TOKEN": "secret-gh", "GH_TOKEN": "secret-gh2",
                                  "ANTHROPIC_API_KEY": "k", "KEEP_ME": "1"})
    def test_tokens_never_inherited(self):
        out = run([sys.executable, "-c", "import os; print(sorted(os.environ))"], timeout=30).stdout
        for name in ("GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY"):
            self.assertNotIn(f"'{name}'", out)
        self.assertIn("'KEEP_ME'", out)

    @mock.patch.dict(os.environ, {"GITHUB_TOKEN": "secret-gh"})
    def test_explicit_env_extra_is_passed(self):
        out = run([sys.executable, "-c", "import os; print(os.environ.get('GITHUB_TOKEN'))"],
                  timeout=30, env_extra={"GITHUB_TOKEN": "x"}).stdout
        self.assertEqual(out.strip(), "x")


class ArgsTests(unittest.TestCase):
    def test_modes(self):
        self.assertTrue(parse_args(["--night"]).night)
        self.assertEqual(parse_args(["--subject", "buttons", "--members", "minecraft:oak_button"]).subject, "buttons")
        with self.assertRaises(SystemExit):
            parse_args(["--subject", "buttons"])
        with self.assertRaises(SystemExit):
            parse_args([])


@mock.patch.dict(os.environ, {"DEADLINE": "08:15"})
class DeadlineTests(unittest.TestCase):
    def test_night_start(self):
        self.assertEqual(seconds_until_deadline(datetime(2026, 10, 10, 3, 30)), 4 * 3600 + 45 * 60)

    def test_after_deadline_in_the_morning_is_negative(self):
        self.assertLess(seconds_until_deadline(datetime(2026, 10, 10, 9, 0)), 0)

    def test_evening_start_targets_next_morning(self):
        self.assertEqual(seconds_until_deadline(datetime(2026, 10, 9, 23, 15)), 9 * 3600)


if __name__ == "__main__":
    unittest.main()
