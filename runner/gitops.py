"""FlintBenchmark: clone, done check, sandbox, gate, push guard, PR.

The GitHub token is passed only to the git/gh commands that need it, never to claude.
The worker never touches this clone's .git: it works in SANDBOX, and only new/changed
tests/**/*.json files are copied back.
"""
import filecmp
import json
import os
import re
import shutil
from pathlib import Path

from . import MC_SERVER, REPO_DIR, SANDBOX, env_str, run

# git reads the token from the env of that one command; it's never written to .git/config.
CREDENTIAL_HELPER = '!f(){ echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'


class GitError(Exception):
    pass


class PushRefused(Exception):
    pass


def repo_slug() -> str:
    return env_str("FLINT_REPO", "JunkyDeveloper/FlintBenchmark")


def base_branch() -> str:
    return env_str("BASE_BRANCH", "master")


def branch_name(subject: str) -> str:
    name = re.sub(r"[^a-z0-9._-]+", "_", subject.removeprefix("minecraft:").lower()).strip("._-")
    return f"nightly/{name}"


def check_push_target(branch: str, already_exists: bool) -> None:
    """The only thing between the runner and master: never trust a prompt with this."""
    if not branch.startswith("nightly/") or branch == "nightly/":
        raise PushRefused(f"refusing to push `{branch}`: only nightly/* is allowed")
    if already_exists:
        raise PushRefused(f"refusing to push `{branch}`: branch or PR already exists")


def _token_env() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    return {"GITHUB_TOKEN": token, "GH_TOKEN": token}


def git(*args: str, cwd: Path = REPO_DIR, auth: bool = False, timeout: float = 600) -> str:
    cmd = ["git", "-c", "core.hooksPath=/dev/null"]
    if auth:
        cmd += ["-c", "credential.helper=", "-c", f"credential.helper={CREDENTIAL_HELPER}"]
    proc = run([*cmd, *args], cwd=cwd, timeout=timeout, env_extra=_token_env() if auth else None)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def gh(*args: str, input: str | None = None, timeout: float = 120) -> str:
    proc = run(["gh", *args], timeout=timeout, env_extra=_token_env(), input=input)
    if proc.returncode != 0:
        raise GitError(f"gh {' '.join(args[:3])}: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def clone() -> None:
    if REPO_DIR.exists():
        shutil.rmtree(REPO_DIR)
    git("clone", "--depth", "50", "--branch", base_branch(),
        f"https://github.com/{repo_slug()}.git", str(REPO_DIR), cwd=REPO_DIR.parent, auth=True)


def parse_ls_remote(out: str) -> bool:
    return bool(out.strip())


def parse_pr_list(out: str) -> bool:
    return bool(json.loads(out or "[]"))


def is_done(subject: str) -> bool:
    """Done = branch exists OR any PR (open, merged, closed) with that head. Merged branches may be deleted."""
    branch = branch_name(subject)
    if parse_ls_remote(git("ls-remote", "--heads", "origin", f"refs/heads/{branch}", auth=True)):
        return True
    return parse_pr_list(gh("pr", "list", "--repo", repo_slug(), "--head", branch,
                            "--state", "all", "--json", "number", "--limit", "1"))


def start_branch(subject: str) -> None:
    git("checkout", "-f", "-B", branch_name(subject), f"origin/{base_branch()}")
    git("clean", "-fdq", "-e", "node_modules")


def refresh_sandbox() -> None:
    """Fresh worker-owned copy of the clone without .git / node_modules, made by the worker itself."""
    script = ("import shutil,sys,pathlib;d=pathlib.Path(sys.argv[2]);"
              "[shutil.rmtree(p) if p.is_dir() else p.unlink() for p in d.iterdir()];"
              "shutil.copytree(sys.argv[1],d,dirs_exist_ok=True,"
              "ignore=shutil.ignore_patterns('.git','node_modules'))")
    proc = run(["sudo", "-u", "worker", "-H", "--", "python3", "-c", script, REPO_DIR, SANDBOX], timeout=300)
    if proc.returncode != 0:
        raise GitError(f"sandbox copy failed: {proc.stderr.strip()[-500:]}")


def collect_tests(sandbox: Path, repo: Path) -> list[str]:
    """Copy new or changed sandbox tests/**/*.json into the repo. Returns repo-relative paths."""
    changed = []
    root = sandbox / "tests"
    if not root.is_dir():
        return changed
    for src in sorted(root.rglob("*.json")):
        if src.is_symlink():
            continue
        rel = src.relative_to(sandbox)
        dst = repo / rel
        if dst.exists() and filecmp.cmp(src, dst, shallow=False):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        changed.append(rel.as_posix())
    return changed


def verify_test(rel: str) -> tuple[bool, str]:
    proc = run(["flintcli", REPO_DIR / rel, "-s", MC_SERVER, "-q"], timeout=900)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[-800:]


def gate_steps(files: list[str]) -> list[list[str]]:
    """CI's checks, scoped to our files: existing FlintBenchmark tests may be outdated
    (2026-10-09: 13-44 old tests use `use_item_on`, which flint-core rejects)."""
    return [["npm", "ci", "--no-audit", "--no-fund"],
            ["npm", "run", "format"],          # formats in place; we only `git add` our files
            ["typos", *files],
            ["flint-validate", *files]]


def gate(files: list[str]) -> tuple[bool, str]:
    for cmd in gate_steps(files):
        proc = run(cmd, cwd=REPO_DIR, timeout=900)
        if proc.returncode != 0:
            return False, f"`{' '.join(cmd)}` failed: {(proc.stdout + proc.stderr).strip()[-800:]}"
    return True, "ok"


def pr_body(subject: str, summary: dict, files: list[str]) -> str:
    tests = [t for t in summary.get("tests", []) if t.get("file") in files]
    blocks, seen = [], set()
    for t in tests:
        rep = t.get("representative", "?")
        if rep in seen:
            continue
        seen.add(rep)
        covers = ", ".join(c for t2 in tests if t2.get("representative") == rep
                           for c in t2.get("covers", [])) or "—"
        blocks.append(f"- {rep} → implementation `{t.get('implementation', '?')}`; also covers: {covers}")
    cases = [f"- {t.get('case', t.get('file'))}" for t in tests] or [f"- `{f}`" for f in files]
    return "\n".join(["## Blocks", *(blocks or ["- (see test files)"]), "", "## Cases", *cases, "",
                      f"_Generated by flint-ai-cronjob for subject `{subject}`._", ""])


def pr_title(subject: str, summary: dict) -> str:
    ids = {t.get("representative") for t in summary.get("tests", [])}
    ids |= {c for t in summary.get("tests", []) for c in t.get("covers", [])}
    return f"Add Flint tests for {subject} ({len(ids - {None})} blocks)"


def commit_push_pr(subject: str, files: list[str], title: str, body: str) -> str:
    branch = branch_name(subject)
    check_push_target(branch, is_done(subject))
    git("add", "--", *files)
    git("commit", "-m", title)
    git("push", "origin", f"HEAD:refs/heads/{branch}", auth=True)  # no --force, ever
    return gh("pr", "create", "--repo", repo_slug(), "--base", base_branch(), "--head", branch,
              "--title", title, "--body-file", "-", input=body).strip()
