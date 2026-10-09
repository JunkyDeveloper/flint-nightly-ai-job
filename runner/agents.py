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
    return Result(text=str(data.get("result", "")), session_id=data.get("session_id", ""),
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
                  ["--model", env_str("SUPERVISOR_MODEL", "haiku"), "--max-turns", "1"], 600)
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


def work_on_subject(subject: str, members: list[str], budget_s: float,
                    claude_fn: ClaudeFn = claude, clock: Callable[[], float] = time.monotonic) -> Outcome:
    """Worker plan (PLAN_MODEL) → supervisor review → worker exec (EXEC_MODEL) ↔ supervisor → summary."""
    deadline = clock() + budget_s
    plan_model = env_str("PLAN_MODEL", "opus")
    exec_model = env_str("EXEC_MODEL", "sonnet")
    sup_model = env_str("SUPERVISOR_MODEL", "haiku")
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
        return r

    def resume(model: str) -> list[str]:
        return ["--resume", session, "--model", model, "--permission-mode", "bypassPermissions"]

    r = call(prompt("worker", subject=subject, members=full, spec=str(FLINT_SPEC),
                    flintcli="/usr/local/bin/flintcli", mc_src=str(MC_SRC), server=MC_SERVER),
             ["--model", plan_model, "--permission-mode", "bypassPermissions",
              "--disallowedTools", "Write,Edit,NotebookEdit"])
    phase = "plan"
    for _ in range(env_int("MAX_ROUNDS", 10)):
        sup = call(prompt("supervisor", phase=phase, subject=subject, members=full, message=r.text[-12000:]),
                   ["--model", sup_model, "--max-turns", "1"], worker=False)
        verdict, answer = parse_verdict(sup.text)
        if phase == "plan" and verdict == "DONE":
            verdict = "APPROVE"
        if phase == "exec" and verdict == "APPROVE":
            verdict = "DONE"
        if verdict == "QUESTION":
            r = call(answer, resume(plan_model if phase == "plan" else exec_model))
        elif verdict == "APPROVE":
            phase = "exec"
            r = call(prompt("exec", notes=answer), resume(exec_model))
        elif verdict == "DONE":
            s = call(prompt("summary"), resume(exec_model))
            out.summary = first_json(s.text)
            out.status, out.note = "done", answer
            return out
        elif verdict in ("UNSUPPORTED", "COVERED"):
            out.status, out.note = verdict.lower(), answer
            return out
        else:
            out.note = answer or "supervisor: stuck"
            return out
    out.note = f"no result after {env_int('MAX_ROUNDS', 10)} rounds"
    return out
