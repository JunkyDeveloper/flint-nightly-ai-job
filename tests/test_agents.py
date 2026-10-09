import os
import unittest
from unittest import mock

from runner import agents
from runner.agents import Result


def scripted(replies):
    """Fake claude: returns replies in order and records (prompt, args)."""
    calls = []

    def fn(text, args, timeout):
        calls.append((text, args))
        return replies.pop(0)
    return fn, calls


def sup(verdict, answer=""):
    return Result(text=f'Sure. {{"verdict": "{verdict}", "answer": "{answer}"}}', session_id="sup")


class ClaudeCallTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GITHUB_TOKEN": "gh"})
    def test_prompt_goes_to_stdin_and_runs_as_worker(self):
        fake = mock.Mock(return_value=mock.Mock(
            returncode=0, stderr="",
            stdout='{"result": "ok", "session_id": "s1", "total_cost_usd": 0.01, "modelUsage": {"claude-haiku-5-5": {}}}'))
        with mock.patch.object(agents, "run", fake):
            r = agents.claude("PROMPT TEXT", ["--disallowedTools", "Write,Edit"], 60)
        cmd, kwargs = fake.call_args.args[0], fake.call_args.kwargs
        self.assertEqual(kwargs["input"], "PROMPT TEXT")
        self.assertNotIn("PROMPT TEXT", cmd)
        self.assertEqual(cmd[:3], ["sudo", "-u", "worker"])
        self.assertFalse(any("GITHUB_TOKEN" in c for c in cmd))
        self.assertEqual((r.text, r.session_id, r.models), ("ok", "s1", ["claude-haiku-5-5"]))


class ParseTests(unittest.TestCase):
    def test_verdict_in_prose_and_fences(self):
        self.assertEqual(agents.parse_verdict('```json\n{"verdict": "approve", "answer": "go"}\n```'),
                         ("APPROVE", "go"))

    def test_garbage_is_stuck(self):
        self.assertEqual(agents.parse_verdict("I think it is fine")[0], "STUCK")
        self.assertEqual(agents.parse_verdict('{"verdict": "MERGE"}')[0], "STUCK")

    def test_usage_limit_detection(self):
        self.assertTrue(agents.hit_limit(Result("You've hit your session limit · resets 8am", is_error=True)))
        self.assertTrue(agents.hit_limit(Result("You've hit your weekly limit")))
        self.assertFalse(agents.hit_limit(Result("all tests passed")))
        long_prose = "x" * 500 + " if you hit your session limit, wait " + "y" * 500
        self.assertFalse(agents.hit_limit(Result(long_prose)))


@mock.patch.dict(os.environ, {"MAX_ROUNDS": "4", "PLAN_MODEL": "opus", "EXEC_MODEL": "sonnet"})
class LoopTests(unittest.TestCase):
    def test_happy_path_plan_exec_summary(self):
        fn, calls = scripted([
            Result("plan: test oak_button", session_id="w1"), sup("APPROVE", "ok"),
            Result("wrote tests", session_id="w1"), sup("DONE", "2 tests"),
            Result('{"tests": [{"file": "tests/x.json"}]}', session_id="w1")])
        out = agents.work_on_subject("buttons", ["oak_button"], 3600, claude_fn=fn)
        self.assertEqual(out.status, "done")
        self.assertEqual(out.summary, {"tests": [{"file": "tests/x.json"}]})
        exec_args = calls[2][1]
        self.assertEqual(exec_args[:4], ["--resume", "w1", "--model", "sonnet"])
        self.assertIn("--disallowedTools", calls[0][1])               # plan phase can't write files

    def test_question_is_answered_in_same_worker_session(self):
        fn, calls = scripted([
            Result("which path?", session_id="w1"), sup("QUESTION", "use /mc-src"),
            Result("plan", session_id="w1"), sup("UNSUPPORTED", "entities")])
        out = agents.work_on_subject("eggs", ["egg"], 3600, claude_fn=fn)
        self.assertEqual(calls[2][0], "use /mc-src")
        self.assertEqual(calls[2][1][:4], ["--resume", "w1", "--model", "opus"])   # never the supervisor session
        self.assertEqual(out.status, "unsupported")

    def test_round_cap(self):
        replies = [Result("?", session_id="w1")]
        for _ in range(4):
            replies += [sup("QUESTION", "keep going"), Result("?", session_id="w1")]
        fn, _ = scripted(replies)
        out = agents.work_on_subject("x", ["x"], 3600, claude_fn=fn)
        self.assertEqual(out.status, "gave_up")
        self.assertIn("rounds", out.note)

    def test_usage_limit_raises(self):
        fn, _ = scripted([Result("You've hit your weekly limit", is_error=True)])
        with self.assertRaises(agents.UsageLimit):
            agents.work_on_subject("x", ["x"], 3600, claude_fn=fn)

    def test_plan_stays_read_only_after_question(self):
        fn, calls = scripted([
            Result("which file?", session_id="w1"), sup("QUESTION", "tests/"),
            Result("plan", session_id="w1"), sup("UNSUPPORTED", "x")])
        agents.work_on_subject("x", ["x"], 3600, claude_fn=fn)
        self.assertIn("--disallowedTools", calls[2][1])

    def test_resume_continues_saved_session_and_reports_progress(self):
        seen = []
        fn, calls = scripted([
            Result("continued, wrote tests", session_id="w9"), sup("DONE", "ok"),
            Result('{"tests": []}', session_id="w9")])
        out = agents.work_on_subject("x", ["x"], 3600, claude_fn=fn,
                                     resume_from={"session": "w9", "phase": "exec"},
                                     on_progress=lambda s, p, t: seen.append((s, p)))
        self.assertEqual(calls[0][1][:4], ["--resume", "w9", "--model", "sonnet"])
        self.assertIn("stopped", calls[0][0])                       # resume prompt, not the fresh worker prompt
        self.assertEqual(out.status, "done")
        self.assertEqual(seen[0], ("w9", "exec"))

    def test_time_budget(self):
        fn, _ = scripted([Result("plan", session_id="w1")])
        with self.assertRaises(agents.SubjectTimeout):
            agents.work_on_subject("x", ["x"], 10, claude_fn=fn)


class InterruptedTests(unittest.TestCase):
    def test_no_session_restarts_without_asking(self):
        fn, calls = scripted([])
        self.assertEqual(agents.check_interrupted({"subject": "x"}, [], claude_fn=fn)[0], "RESTART")
        self.assertEqual(calls, [])

    def test_haiku_decision(self):
        fn, _ = scripted([Result('{"decision": "resume", "reason": "2 tests written"}')])
        self.assertEqual(agents.check_interrupted({"subject": "x", "session": "s"}, ["tests/a.json"], claude_fn=fn),
                         ("RESUME", "2 tests written"))
        fn, _ = scripted([Result("no idea")])
        self.assertEqual(agents.check_interrupted({"subject": "x", "session": "s"}, [], claude_fn=fn)[0], "RESTART")


if __name__ == "__main__":
    unittest.main()
