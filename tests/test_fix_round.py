import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runner.__main__ as m
from runner import agents
from runner.agents import Outcome, Result
from runner.report import Report


@mock.patch.dict(os.environ, {"SUBJECT_TIMEOUT_MIN": "90", "FIX_ROUNDS": "2"})
class FixRoundTests(unittest.TestCase):
    """Runner check failures go back into the worker's session instead of giving up."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.sandbox = tmp / "sandbox"
        (self.sandbox / "tests").mkdir(parents=True)
        for f in ("ok.json", "bad.json"):
            (self.sandbox / "tests" / f).write_text("{}")
        self.bad_fixed = False
        self.fix_calls = []
        patches = [
            mock.patch.object(m, "CURRENT", tmp / "current.json"),
            mock.patch.object(m, "SANDBOX", self.sandbox),
            mock.patch.object(m.gitops, "is_done", return_value=False),
            mock.patch.object(m.gitops, "start_branch"),
            mock.patch.object(m.gitops, "refresh_sandbox"),
            mock.patch.object(m.gitops, "collect_tests", return_value=["tests/ok.json", "tests/bad.json"]),
            mock.patch.object(m.gitops, "verify_test", return_value=(True, "")),
            mock.patch.object(m.gitops, "gate", side_effect=self.fake_gate),
            mock.patch.object(m.agents, "work_on_subject",
                              return_value=Outcome(status="done", session="w1", summary={"tests": []})),
            mock.patch.object(m.agents, "fix_tests", side_effect=self.fake_fix),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.commit = mock.patch.object(m.gitops, "commit_push_pr", return_value="https://pr/1").start()
        self.addCleanup(mock.patch.stopall)
        self.report, self.state = Report("test"), {}

    def fake_gate(self, files):
        bad = [f for f in files if f == "tests/bad.json" and not self.bad_fixed]
        return [f for f in files if f not in bad], [f"`{f}`: flint-validate: cleanup region must contain the origin"
                                                    for f in bad]

    def fake_fix(self, session, problems, timeout):
        self.fix_calls.append((session, problems))
        return Result("fixed", cost=0.5)

    def test_worker_fixes_it_and_all_tests_ship(self):
        def fix(session, problems, timeout):
            self.bad_fixed = True
            return self.fake_fix(session, problems, timeout)
        m.agents.fix_tests.side_effect = fix
        m.run_subject("buttons", ["oak_button"], 5400, self.report, self.state)
        self.assertEqual(len(self.fix_calls), 1)
        self.assertEqual(self.fix_calls[0][0], "w1")
        self.assertIn("cleanup region", self.fix_calls[0][1][0])
        self.assertEqual(self.commit.call_args.args[1], ["tests/ok.json", "tests/bad.json"])

    def test_unfixable_test_dropped_after_max_rounds_rest_ships(self):
        m.run_subject("buttons", ["oak_button"], 5400, self.report, self.state)
        self.assertEqual(len(self.fix_calls), 2)
        self.assertEqual(self.commit.call_args.args[1], ["tests/ok.json"])
        self.assertTrue(any("bad.json" in w for w in self.report.warnings))

    def test_test_deleted_by_worker_is_not_shipped(self):
        def fix(session, problems, timeout):
            (self.sandbox / "tests" / "bad.json").unlink()
            return self.fake_fix(session, problems, timeout)
        m.agents.fix_tests.side_effect = fix
        m.run_subject("buttons", ["oak_button"], 5400, self.report, self.state)
        self.assertEqual(self.commit.call_args.args[1], ["tests/ok.json"])


if __name__ == "__main__":
    unittest.main()
