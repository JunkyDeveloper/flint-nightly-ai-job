import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runner.__main__ as m
from runner import agents
from runner.report import Report


@mock.patch.dict(os.environ, {"SUBJECT_TIMEOUT_MIN": "90"})
class MarkerTests(unittest.TestCase):
    """state/current.json: kept when the run is cut off, removed on every final outcome."""

    def setUp(self):
        self.current = Path(tempfile.mkdtemp()) / "current.json"
        patches = [mock.patch.object(m, "CURRENT", self.current),
                   mock.patch.object(m.gitops, "is_done", return_value=False),
                   mock.patch.object(m.gitops, "start_branch"),
                   mock.patch.object(m.gitops, "refresh_sandbox"),
                   mock.patch.object(m.gitops, "changed_tests", return_value=[])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.report, self.state = Report("test"), {}

    def work(self, effect):
        """Fake worker: writes progress once, then does `effect`."""
        def fn(name, members, budget, resume_from=None, on_progress=None):
            on_progress("sess-1", "exec", "halfway")
            if isinstance(effect, BaseException):
                raise effect
            return effect
        return mock.patch.object(m.agents, "work_on_subject", side_effect=fn)

    def test_usage_limit_keeps_marker_with_session(self):
        with self.work(agents.UsageLimit("limit")), self.assertRaises(agents.UsageLimit):
            m.run_subject("buttons", ["oak_button"], 5400, self.report, self.state)
        marker = m.subjects.load_json(self.current, None)
        self.assertEqual((marker["subject"], marker["session"], marker["phase"]), ("buttons", "sess-1", "exec"))

    def test_sigterm_keeps_marker(self):
        with self.work(m.Stopped()), self.assertRaises(m.Stopped):
            m.run_subject("buttons", ["oak_button"], 5400, self.report, self.state)
        self.assertTrue(self.current.exists())

    def test_night_deadline_keeps_marker_but_own_budget_gives_up(self):
        with self.work(agents.SubjectTimeout("t")):
            m.run_subject("buttons", ["oak_button"], 600, self.report, self.state)      # cut by the night
        self.assertTrue(self.current.exists())
        self.assertNotIn("buttons", self.state)
        with self.work(agents.SubjectTimeout("t")):
            m.run_subject("buttons", ["oak_button"], 5400, self.report, self.state)     # its own 90 min
        self.assertFalse(self.current.exists())
        self.assertEqual(self.state["buttons"]["status"], "gave_up")

    def test_final_outcome_removes_marker(self):
        with self.work(agents.Outcome(status="unsupported", note="entities")):
            m.run_subject("eggs", ["egg"], 5400, self.report, self.state)
        self.assertFalse(self.current.exists())

    def test_too_many_interruptions_give_up(self):
        m.subjects.save_json(self.current, {"subject": "buttons", "members": ["oak_button"],
                                            "session": "s", "interruptions": m.MAX_INTERRUPTIONS - 1})
        self.assertIsNone(m.interrupted_work(self.report, self.state))
        self.assertEqual(self.state["buttons"]["status"], "gave_up")
        self.assertFalse(self.current.exists())

    def test_interrupted_subject_resumes_when_haiku_says_so(self):
        m.subjects.save_json(self.current, {"subject": "buttons", "members": ["oak_button"],
                                            "session": "s", "phase": "exec", "interruptions": 0})
        with mock.patch.object(m.agents, "check_interrupted", return_value=("RESUME", "tests exist")):
            first = m.interrupted_work(self.report, self.state)
        self.assertEqual(first["resume_from"], {"session": "s", "phase": "exec"})
        self.assertEqual(first["interruptions"], 1)


if __name__ == "__main__":
    unittest.main()
