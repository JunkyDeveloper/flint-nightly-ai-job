"""Claude calls: worker (plan → exec, /flint-tests) and the Haiku supervisor.

Every claude process runs as the `worker` user via sudo (env reset), so it never sees GITHUB_TOKEN.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Callable

from . import FLINT_SPEC, MC_SERVER, MC_SRC, SANDBOX, env_int, env_str, run

PROMPTS = Path(__file__).parent / "prompts"
VERDICTS = {"APPROVE", "QUESTION", "DONE", "STUCK", "UNSUPPORTED", "COVERED"}
LIMIT_RE = re.compile(r"(?:you(?:'|’)ve )?hit your (?:session|weekly|opus|sonnet|fable|usage)? ?limit", re.I)


class UsageLimit(Exception):
    """Claude subscription limit reached: stop the night, carry the subject over."""


class SubjectTimeout(Exception):
    """Per-subject time budget or the night deadline is used up."""


@dataclass
class Result:
    text: str
    session_id: str = ""
    is_error: bool = False
    cost: float = 0.0
    models: list[str] = field(default_factory=list)


ClaudeFn = Callable[[str, list[str], float], Result]


def supervisor_args() -> list[str]:
    """Haiku may look at files (read-only) but needs room to answer after that: with --max-turns 1 a
    single tool call ends the run with error_max_turns and an empty result (seen 2026-10-10).
    `--tools ""` (no tools at all) hangs in 2.1.296, so read-only tools it is."""
    return ["--model", env_str("SUPERVISOR_MODEL", "haiku"), "--tools", "Read,Glob,Grep", "--max-turns", "6"]


def prompt(name: str, **values: str) -> str:
    return Template((PROMPTS / f"{name}.md").read_text()).safe_substitute(values)


def claude(text: str, args: list[str], timeout: float) -> Result:
    """`claude -p` as the worker user in the sandbox. Only the Claude token is passed in."""
    cmd = ["sudo", "-u", "worker", "-H", "--", "env",
           f"CLAUDE_CODE_OAUTH_TOKEN={os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '')}",
           "DISABLE_AUTOUPDATER=1", f"TZ={os.environ.get('TZ', 'UTC')}",
           "claude", "-p", "--output-format", "json", *args]
    # Prompt via stdin: variadic flags like --disallowedTools would swallow a trailing positional prompt.
    proc = run(cmd, timeout=timeout, cwd=SANDBOX, input=text)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return Result(text=(proc.stdout + "\n" + proc.stderr).strip(), is_error=True)
    # An empty result (e.g. error_max_turns) would be undiagnosable in the report: show the subtype.
    text = str(data.get("result") or f"[empty result: {data.get('subtype', '?')}]")
    return Result(text=text, session_id=data.get("session_id", ""),
                  is_error=bool(data.get("is_error")) or proc.returncode != 0,
                  cost=float(data.get("total_cost_usd") or 0),
                  models=sorted((data.get("modelUsage") or {}).keys()))


def hit_limit(result: Result) -> bool:
    """Limit messages come back as short error results; long worker prose that mentions limits doesn't count."""
    return (result.is_error or len(result.text) < 400) and bool(LIMIT_RE.search(result.text))


def first_json(text: str) -> dict | None:
    """First JSON object in text (models like to wrap it in prose or ``` fences)."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_verdict(text: str) -> tuple[str, str]:
    obj = first_json(text) or {}
    verdict = str(obj.get("verdict", "")).upper()
    if verdict not in VERDICTS:
        return "STUCK", f"unparsable supervisor output: {text[:200]}"
    return verdict, str(obj.get("answer", ""))


def ping(claude_fn: ClaudeFn = claude) -> Result:
    return claude_fn("Reply with exactly: ok",
                     ["--model", env_str("SUPERVISOR_MODEL", "haiku"), "--max-turns", "1"], 120)


def review_groups(candidates: dict[str, list[str]], claude_fn: ClaudeFn = claude) -> dict | None:
    """One-time Haiku review of the computed grouping. Caller validates the answer."""
    r = claude_fn(prompt("grouping", candidates=json.dumps(candidates, indent=1)),
                  supervisor_args(), 600)
    if hit_limit(r):
        raise UsageLimit(r.text)
    return first_json(r.text)


@dataclass
class Outcome:
    status: str              # done | unsupported | covered | gave_up
    note: str = ""
    summary: dict | None = None
    cost: float = 0.0
    models: list[str] = field(default_factory=list)
    session: str = ""        # worker session, for fix rounds after the runner's own checks


def fix_tests(session: str, problems: list[str], timeout: float, claude_fn: ClaudeFn = claude) -> Result:
    """Send the runner's check failures back into the worker's session (EXEC_MODEL) to fix them."""
    if timeout < 60:
        raise SubjectTimeout("no time left for a fix round")
    r = claude_fn(prompt("fix", problems="\n".join(f"- {p}" for p in problems)),
                  ["--resume", session, "--model", env_str("EXEC_MODEL", "sonnet"),
                   "--permission-mode", "bypassPermissions"], timeout)
    if hit_limit(r):
        raise UsageLimit(r.text)
    return r


Progress = Callable[[str, str, str], None]   # (session_id, phase, last worker message)
PLAN_READ_ONLY = ["--disallowedTools", "Write,Edit,NotebookEdit"]


def check_interrupted(marker: dict, files: list[str], claude_fn: ClaudeFn = claude) -> tuple[str, str]:
    """Previous run stopped mid-subject. Haiku decides: RESUME the saved session or RESTART fresh."""
    if not marker.get("session"):
        return "RESTART", "no saved worker session"
    r = claude_fn(prompt("interrupted", subject=marker.get("subject", "?"), phase=marker.get("phase", "?"),
                         interruptions=str(marker.get("interruptions", 1)), started=marker.get("started", "?"),
                         files="\n".join(f"- {f}" for f in files) or "(none)",
                         message=str(marker.get("last_message", ""))[-6000:]),
                  supervisor_args(), 300)
    if hit_limit(r):
        raise UsageLimit(r.text)
    obj = first_json(r.text) or {}
    decision = str(obj.get("decision", "")).upper()
    if decision not in ("RESUME", "RESTART"):
        return "RESTART", f"unparsable decision: {r.text[:200]}"
    return decision, str(obj.get("reason", ""))


def work_on_subject(subject: str, members: list[str], budget_s: float,
                    claude_fn: ClaudeFn = claude, clock: Callable[[], float] = time.monotonic,
                    resume_from: dict | None = None, on_progress: Progress | None = None) -> Outcome:
    """Worker plan (PLAN_MODEL) → supervisor review → worker exec (EXEC_MODEL) ↔ supervisor → summary.

    resume_from = {"session", "phase"} continues an interrupted run's worker session instead of starting fresh.
    on_progress is called after every worker turn (the runner persists it to /state/current.json)."""
    deadline = clock() + budget_s
    plan_model = env_str("PLAN_MODEL", "opus")
    exec_model = env_str("EXEC_MODEL", "sonnet")
    full = ", ".join(f"minecraft:{m}" for m in members)
    out = Outcome(status="gave_up")
    session = ""

    def call(text: str, args: list[str], worker: bool = True) -> Result:
        nonlocal session
        remaining = deadline - clock()
        if remaining < 30:
            raise SubjectTimeout("time budget used up")
        r = claude_fn(text, args, remaining)
        out.cost += r.cost
        out.models = sorted(set(out.models) | set(r.models))
        if hit_limit(r):
            raise UsageLimit(r.text)
        if worker:  # supervisor calls are separate sessions; never resume them
            session = r.session_id or session
            if on_progress:
                on_progress(session, phase, r.text)
        return r

    def resume(for_phase: str) -> list[str]:
        model = plan_model if for_phase == "plan" else exec_model
        return (["--resume", session, "--model", model, "--permission-mode", "bypassPermissions"]
                + (PLAN_READ_ONLY if for_phase == "plan" else []))

    if resume_from and resume_from.get("session"):
        session, phase = resume_from["session"], resume_from.get("phase", "plan")
        r = call(prompt("resume", phase=phase), resume(phase))
    else:
        phase = "plan"
        r = call(prompt("worker", subject=subject, members=full, spec=str(FLINT_SPEC),
                        flintcli="/usr/local/bin/flintcli", mc_src=str(MC_SRC), server=MC_SERVER),
                 ["--model", plan_model, "--permission-mode", "bypassPermissions", *PLAN_READ_ONLY])
    for _ in range(env_int("MAX_ROUNDS", 10)):
        sup_prompt = prompt("supervisor", phase=phase, subject=subject, members=full, message=r.text[-12000:])
        verdict, answer = parse_verdict(call(sup_prompt, supervisor_args(), worker=False).text)
        if verdict == "STUCK" and answer.startswith("unparsable"):     # one retry before giving up
            verdict, answer = parse_verdict(call(sup_prompt, supervisor_args(), worker=False).text)
        if phase == "plan" and verdict == "DONE":
            verdict = "APPROVE"
        if phase == "exec" and verdict == "APPROVE":
            verdict = "DONE"
        if verdict == "QUESTION":
            r = call(answer, resume(phase))
        elif verdict == "APPROVE":
            phase = "exec"
            r = call(prompt("exec", notes=answer), resume("exec"))
        elif verdict == "DONE":
            s = call(prompt("summary"), resume("exec"))
            out.summary = first_json(s.text)
            out.status, out.note, out.session = "done", answer, session
            return out
        elif verdict in ("UNSUPPORTED", "COVERED"):
            out.status, out.note = verdict.lower(), answer
            return out
        else:
            out.note = answer or "supervisor: stuck"
            return out
    out.note = f"no result after {env_int('MAX_ROUNDS', 10)} rounds"
    return out
