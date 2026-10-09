"""Entry point: python -m runner (--preflight | --dry-run | --subject NAME --members IDS | --night)."""
import argparse
import signal
import sys
import time
from datetime import datetime, timedelta

from . import CONFIG, REPO_DIR, SANDBOX, STATE, TAGS, env_int, env_str
from . import agents, gitops, subjects
from .preflight import PreflightFailed, preflight
from .report import Report

CURRENT = STATE / "current.json"     # exists only while a subject is unfinished
MAX_INTERRUPTIONS = 3


class Stopped(BaseException):
    """SIGTERM (timeout / systemd / docker stop). BaseException so per-subject `except Exception` can't eat it."""


def _on_sigterm(signum, frame):
    raise Stopped()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="runner", description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true", help="only run the preflight checks")
    mode.add_argument("--dry-run", action="store_true", help="print subjects + order; no Claude, no GitHub")
    mode.add_argument("--subject", help="run one subject by hand (needs --members)")
    mode.add_argument("--night", action="store_true", help="full nightly run")
    p.add_argument("--members", help="comma-separated ids for --subject, e.g. minecraft:oak_button,...")
    args = p.parse_args(argv)
    if args.subject and not args.members:
        p.error("--subject needs --members")
    return args


def seconds_until_deadline(now: datetime | None = None) -> float:
    now = now or datetime.now()
    hh, mm = map(int, env_str("DEADLINE", "08:15").split(":"))
    end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if end <= now and now.hour >= 12:      # started in the evening for a morning deadline
        end += timedelta(days=1)
    return (end - now).total_seconds()


def check_tests(files: list[str]) -> tuple[list[str], list[str]]:
    """Runner's own checks: flintcli re-run against the server, then CI's per-file checks."""
    ran, problems = [], []
    for rel in files:
        ok, log = gitops.verify_test(rel)
        if ok:
            ran.append(rel)
        else:
            problems.append(f"`{rel}`: flintcli: {log[-300:]}")
    good, gate_problems = gitops.gate(ran) if ran else ([], [])
    return good, problems + gate_problems


def run_subject(name: str, members: list[str], budget_s: float, report: Report, state: dict,
                resume_from: dict | None = None, interruptions: int = 0) -> None:
    """One subject end to end. Raises UsageLimit / Stopped; everything else is recorded and swallowed.

    /state/current.json tracks the subject while it runs. It's removed on every final outcome and
    kept when the run is cut off (usage limit, night deadline, SIGTERM), so the next run can resume."""
    marker = {"subject": name, "members": members, "phase": (resume_from or {}).get("phase", "plan"),
              "session": (resume_from or {}).get("session", ""), "interruptions": interruptions,
              "started": datetime.now().isoformat(timespec="minutes"), "last_message": ""}

    def progress(session: str, phase: str, text: str) -> None:
        marker.update(session=session, phase=phase, last_message=text[-6000:])
        subjects.save_json(CURRENT, marker)

    deadline_bound = budget_s < env_int("SUBJECT_TIMEOUT_MIN", 90) * 60
    deadline = time.monotonic() + budget_s
    try:
        if gitops.is_done(name):
            report.subject(name, "skipped", "branch or PR already exists")
            CURRENT.unlink(missing_ok=True)
            return
        subjects.save_json(CURRENT, marker)
        gitops.start_branch(name)
        if not resume_from:
            gitops.refresh_sandbox()
        out = agents.work_on_subject(name, members, budget_s, resume_from=resume_from, on_progress=progress)
        report.usage_usd += out.cost
        if out.status != "done":
            CURRENT.unlink(missing_ok=True)
            subjects.record(state, name, out.status, out.note)
            report.subject(name, out.status, out.note)
            return

        # Check independently; send failures back into the worker's session, up to FIX_ROUNDS times.
        candidates: list[str] = []
        fix_rounds = env_int("FIX_ROUNDS", 2)
        for round_ in range(fix_rounds + 1):
            candidates += [f for f in gitops.collect_tests(SANDBOX, REPO_DIR) if f not in candidates]
            candidates = [f for f in candidates if (SANDBOX / f).exists()]     # worker may delete a test
            passing, problems = check_tests(candidates)
            remaining = deadline - time.monotonic()
            if not problems or round_ == fix_rounds or not out.session or remaining < 300:
                break
            report.note(f"{name}: fix round {round_ + 1}: {len(problems)} problem(s) sent back to the worker")
            report.usage_usd += agents.fix_tests(out.session, problems, remaining).cost
        CURRENT.unlink(missing_ok=True)
        for p in problems:
            report.warn(f"{name}: dropped {p}")
        if not passing:
            subjects.record(state, name, "gave_up", "no test passed the runner's checks")
            report.subject(name, "gave_up", "no test passed the runner's checks")
            return
        summary = out.summary or {}
        for t in summary.get("not_tested", []):
            report.note(f"{name}: not tested {t.get('id')} ({t.get('reason')}): {t.get('note', '')}")
        url = gitops.commit_push_pr(name, passing, gitops.pr_title(name, summary),
                                    gitops.pr_body(name, summary, passing))
        subjects.record(state, name, "done", url)
        report.subject(name, "PR", f"{url} ({len(passing)} tests, models: {', '.join(out.models)})")
    except agents.UsageLimit:
        report.subject(name, "interrupted", "usage limit; resumes next run")
        raise
    except agents.SubjectTimeout as e:
        if deadline_bound:                   # the night ended, not the subject's own budget
            report.subject(name, "interrupted", "night deadline; resumes next run")
            return
        CURRENT.unlink(missing_ok=True)
        subjects.record(state, name, "gave_up", str(e))
        report.subject(name, "gave_up", str(e))
    except Stopped:
        report.subject(name, "interrupted", "stopped (SIGTERM); resumes next run")
        raise
    except Exception as e:  # one subject must never kill the night
        CURRENT.unlink(missing_ok=True)
        subjects.record(state, name, "gave_up", f"{type(e).__name__}: {e}")
        report.subject(name, "error", f"{type(e).__name__}: {e}")


def interrupted_work(report: Report, state: dict) -> dict | None:
    """Was the previous run stopped mid-subject? Haiku decides RESUME vs RESTART. Returns what to run first."""
    marker = subjects.load_json(CURRENT, None)
    if not marker:
        return None
    name, n = marker["subject"], marker.get("interruptions", 0) + 1
    report.warn(f"previous run was stopped during `{name}` (phase {marker.get('phase')}, "
                f"started {marker.get('started')}, interruption #{n})")
    if gitops.is_done(name):
        CURRENT.unlink(missing_ok=True)
        return None
    if n >= MAX_INTERRUPTIONS:
        subjects.record(state, name, "gave_up", f"interrupted {n} times")
        report.subject(name, "gave_up", f"interrupted {n} times")
        CURRENT.unlink(missing_ok=True)
        return None
    files = gitops.changed_tests(SANDBOX, REPO_DIR)
    decision, reason = agents.check_interrupted(marker, files)
    report.note(f"interrupted `{name}`: Haiku says {decision} ({reason}); {len(files)} leftover test file(s)")
    resume = {"session": marker["session"], "phase": marker.get("phase", "plan")} if decision == "RESUME" else None
    return {"name": name, "members": marker["members"], "resume_from": resume, "interruptions": n}


def load_groups(ids: list[str], tags: dict, report: Report, review: bool) -> dict[str, list[str]]:
    """Frozen /state/groups.json. Created once (Haiku-reviewed); later only new ids are appended."""
    path = STATE / "groups.json"
    groups = subjects.load_json(path, None)
    if groups is None:
        groups = subjects.candidate_groups(ids, tags)
        if review:
            proposed = agents.review_groups(groups)
            if subjects.validate_groups(proposed, ids):
                groups = {k: [subjects.short(m) for m in v] for k, v in proposed.items()}
                report.note(f"grouping reviewed by Haiku: {len(groups)} subjects")
            else:
                report.warn("Haiku grouping review unusable; kept computed grouping")
        subjects.save_json(path, groups)
    else:
        new = subjects.add_new_ids(groups, ids, tags)
        if new:
            report.note(f"{len(new)} new ids grouped: {', '.join(new[:20])}")
            subjects.save_json(path, groups)
    return groups


def night(report: Report, dry_run: bool) -> None:
    ids = subjects.read_ids(CONFIG / "implemented.txt")
    tags = subjects.load_tags(TAGS)
    if dry_run:
        groups = subjects.load_json(STATE / "groups.json", None) or subjects.candidate_groups(ids, tags)
        subjects.add_new_ids(groups, ids, tags)
    else:
        groups = load_groups(ids, tags, report, review=True)
    order, warnings = subjects.order_subjects(groups, ids, subjects.read_priority(CONFIG / "priority.txt"))
    for w in warnings:
        report.warn(w)
    preview = subjects.preview(groups, order)
    if dry_run:
        print(preview)
        report.note(f"{len(ids)} ids -> {len(groups)} subjects")
        return
    (STATE / "subjects-preview.md").write_text(preview)

    state = subjects.load_json(STATE / "subjects.json", {})
    started, limit = 0, env_int("MAX_SUBJECTS_PER_NIGHT", 3)
    try:
        first = interrupted_work(report, state)
        if first:
            started += 1
            budget = min(env_int("SUBJECT_TIMEOUT_MIN", 90) * 60, seconds_until_deadline())
            run_subject(first["name"], first["members"], budget, report, state,
                        first["resume_from"], first["interruptions"])
            subjects.save_json(STATE / "subjects.json", state)
        for name in order:
            if first and name == first["name"]:
                continue
            if started >= limit:
                break
            remaining = seconds_until_deadline()
            if remaining < 600:
                report.note("deadline reached, stopping")
                break
            if not subjects.selectable(name, state):
                continue
            if gitops.is_done(name):
                continue
            started += 1
            budget = min(env_int("SUBJECT_TIMEOUT_MIN", 90) * 60, remaining)
            run_subject(name, groups[name], budget, report, state)
            subjects.save_json(STATE / "subjects.json", state)
    except agents.UsageLimit as e:
        report.warn(f"Claude usage limit reached, stopping: {str(e)[:300]}")
    finally:
        subjects.save_json(STATE / "subjects.json", state)
    report.note(f"{started} subject(s) started, limit {limit}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    mode = ("preflight" if args.preflight else "dry-run" if args.dry_run
            else "subject" if args.subject else "night")
    report = Report(mode)
    code = 0
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        if args.dry_run:
            night(report, dry_run=True)
        else:
            preflight(report)
            if args.subject:
                members = [subjects.short(m) for m in args.members.split(",") if m.strip()]
                state = subjects.load_json(STATE / "subjects.json", {})
                marker = subjects.load_json(CURRENT, None)
                if marker and marker["subject"] != args.subject:
                    report.warn(f"unfinished `{marker['subject']}` from an earlier run is dropped by this manual run")
                first = interrupted_work(report, state) if marker and marker["subject"] == args.subject else None
                try:
                    run_subject(args.subject, members, env_int("SUBJECT_TIMEOUT_MIN", 90) * 60, report, state,
                                (first or {}).get("resume_from"), (first or {}).get("interruptions", 0))
                except agents.UsageLimit as e:
                    report.warn(f"Claude usage limit reached: {str(e)[:300]}")
                subjects.save_json(STATE / "subjects.json", state)
            elif args.night:
                night(report, dry_run=False)
        report.result = "ok"
    except Stopped:
        report.result = "stopped by SIGTERM (unfinished work kept in state/current.json for the next run)"
        code = 143
    except PreflightFailed as e:
        report.result = f"preflight failed: {e}"
        code = 2
    except Exception as e:
        report.result = f"crashed: {type(e).__name__}: {e}"
        code = 1
    finally:
        if not args.dry_run:
            print(f"report: {report.write(STATE)}", flush=True)
    print(f"result: {report.result}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
