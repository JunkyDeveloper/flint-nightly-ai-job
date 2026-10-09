# SPEC: flint-ai-cronjob

Version 2.1, 2026-10-09 (v2 approved; 2.1 = implementation details D26–D29). How it's built: `tasks/plan.md` (decisions D1–D22). Task list: `tasks/todo.md`.

## Objective

Every night, an unattended AI workflow writes [Flint](https://github.com/FlintTestMC/flint-core) tests for the Minecraft blocks and items listed in `implemented.txt`. Each test is verified against a real vanilla server and against the decompiled Minecraft source. The results come back as pull requests on the FlintBenchmark fork, which the owner reviews in the morning.

**User:** one person, the owner. They maintain `implemented.txt` / `priority.txt` and review PRs. No one else operates or reads the system.

## Success Criteria

**Behavior (per night):**
1. A run starts between 03:00 and 04:00 and has stopped by 09:00.
2. Each finished subject produces exactly one PR on `JunkyDeveloper/FlintBenchmark` (base `master`). It contains only tests that:
   - pass against the 26.2 vanilla server with `flintcli`
   - match the behavior in the decompiled source (not shaped just to pass)
   - pass the same checks as the repo's CI (format, typos, `flint-validate`)
3. Each distinct implementation in a subject is tested exactly once (one representative). The PR lists which ids it covers and why.
4. Nothing is ever pushed to `master`, and nothing is force-pushed.
5. The run never needs a human. Every skill question is answered automatically, or the subject is given up.
6. At most `MAX_SUBJECTS_PER_NIGHT` (3) subjects per night. On a usage limit the run stops cleanly and the rest carry over.
7. `/state/reports/<date>.md` exists after every run, including failed ones.

**Code (testable now):**
8. `python3 -m unittest discover -s tests` passes. It doesn't need Docker, network or Claude.
9. `--dry-run` on the current config prints the subject list (274 subjects for 991 ids) without calling Claude or GitHub.
10. Inside a worker session, `env` contains no `GITHUB_TOKEN` / `GH_TOKEN`, and `git push` fails.

## Scope

**In:**
- Blocks and items from `implemented.txt`, minus `*_spawn_egg`.
- Grouping ids into subjects using vanilla tags + naming, reviewed once by Haiku.
- One PR per subject.
- A report file per night.

**Out (non-goals):**
- Entities, world generation, anything else the skill doesn't support (marked `unsupported`).
- Notifications or alerts. A quiet failure is accepted.
- Merging PRs, changing `master`, touching the upstream FlintBenchmark repo.
- Fixing FlintCLI or flint-core. If they're incompatible with 26.2, that's a precondition.
- Running on the developer machine (server only). Any web UI or dashboard.

## Tech Stack

| Part | Choice | Pinned at |
|------|--------|-----------|
| Runner | **Python 3 (3.12+), standard library only** (`subprocess`, `json`, `pathlib`, `argparse`) | base image tag |
| Container | Docker + docker compose, `linux/amd64` | — |
| MC server | `itzg/minecraft-server`, `TYPE=VANILLA`, `VERSION=26.2` | image tag (Phase 1 start) |
| flintcli | `JunkyDeveloper/FlintCLI` @ `f2474b8` (azalea `a8959f62` = 26.2), Rust `nightly-2026-07-19` (newer nightlies break azalea) | commit + nightly date |
| flint-validate / spec | `FlintTestMC/flint-core` @ `a6f4d12c` | commit |
| AI | Claude Code CLI (`claude -p`), Pro OAuth token. Opus = plan, Sonnet = exec, Haiku = supervisor | CLI version, `DISABLE_AUTOUPDATER=1` |
| Skill | `flint-tests`, vendored unchanged | file checksum |
| Git/PR | `git`, `gh` | image |
| FlintBenchmark checks | node + `npm ci` (repo's own lockfile), `typos` | repo lockfile, typos version |
| Scheduler | systemd timer on the server | — |

Why Python: the grouping logic (tag resolution, name rule) and the Haiku JSON handling are too much for bash. The stdlib covers everything, so no pip dependencies, nothing to install, and nothing to audit.

## Commands

```bash
# Unit tests (local, no Docker / network / Claude)
python3 -m unittest discover -s tests

# Build images
docker compose build

# Preflight only: server up, flintcli known-good test, tiny Haiku call
docker compose run --rm agent --preflight

# Show subjects + order, no Claude, no GitHub, no Docker
CONFIG_DIR=config STATE_DIR=state MC_SRC_DIR=minecraft_26.2 python3 -m runner --dry-run

# One subject by hand (Phase 1)
docker compose run --rm agent --subject buttons --members minecraft:oak_button,minecraft:stone_button

# Full night (what the timer runs)
docker compose run --rm agent --night

# Remove the MC server + its world after a manual run (state/ is kept)
docker compose rm -fsv minecraft

# Refresh the vendored skill from the global one
cp ~/.claude/skills/synced/*/flint-tests/SKILL.md vendor/skills/flint-tests/SKILL.md

# Server: install + enable the timer
sudo cp deploy/flint-ai-cronjob.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now flint-ai-cronjob.timer
systemctl list-timers flint-ai-cronjob.timer
```
There's no linter or formatter dependency. Lint = the unit tests + `python3 -m py_compile runner/*.py`.

## Project Structure

```
SPEC.md                    → this spec
SETUP.md                   → owner setup: tokens, paths, checks, timer
tasks/plan.md, todo.md     → plan + task list
Dockerfile                 → multi-stage: flintcli-build → final agent image
compose.yaml               → services: minecraft, agent
.env.example               → every key, no values; .env is gitignored (chmod 600)
config/                    → owner-edited inputs, mounted ro → /config
  implemented.txt
  priority.txt
minecraft_26.2/            → decompiled 26.2 source (gitignored), mounted ro → /mc-src;
                             vanilla tags are read from its data/minecraft/tags/{block,item} (D30)
runner/                    → Python package, run as `python -m runner`
  __main__.py              → argparse, modes (--preflight/--dry-run/--subject/--night)
  subjects.py              → tags, grouping, priority, selection, subjects.json
  agents.py                → worker (A/B) + Haiku calls, question loop, scrubbed env
  gitops.py                → clone, gate, push guard, PR
  preflight.py, report.py
  prompts/                 → worker.md (plan), exec.md, summary.md, supervisor.md, grouping.md
tests/                     → test_*.py (stdlib unittest), fixtures built in temp dirs
vendor/skills/flint-tests/ → vendored skill, unchanged
deploy/                    → systemd .service + .timer
state/                     → bind mount /state (gitignored): groups.json, subjects.json, reports/, subjects-preview.md
```
Inside the container: `/config` ro, `/mc-src` ro, `/state` rw (bind mount `./state`), `/work` (container filesystem, fresh per run): `/work/repo` = runner's FlintBenchmark clone, `/work/sandbox` = worker's copy without `.git`.

**Two users (D26):** the runner runs as `agent` (uid 1000) and holds `GITHUB_TOKEN`. Every `claude` process runs as `worker` (uid 1001) via `sudo` with a reset env, in `/work/sandbox`. A different uid can't read the runner's `/proc/<pid>/environ`. After the worker finishes, the runner copies only new/changed `tests/**/*.json` (no symlinks) into its clone, re-runs each one with `flintcli`, and commits only the passing ones.

## Code Style

- **Standard library only.** Functions over classes, and a class only when it holds state. Type hints on public functions.
- **Every external command goes through one helper,** with an explicit env and timeout. No `shell=True`.
- Pure logic (grouping, priority, selection, parsing the Haiku verdict) has no I/O, so it can be unit-tested.
- Prompts live in `runner/prompts/*.md`, not in Python strings.
- Errors in one subject never crash the night: catch at the subject boundary, record it, continue.
- Names: `snake_case`, module = noun (`gitops`), function = verb (`select_subjects`).

```python
SCRUBBED = ("GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

def run(cmd: list[str], *, timeout: int, env_extra: dict[str, str] | None = None,
        scrub: bool = True) -> subprocess.CompletedProcess[str]:
    """Run an external command. Claude calls always scrub; only gitops passes the GitHub token."""
    env = {k: v for k, v in os.environ.items() if not (scrub and k in SCRUBBED)}
    env.update(env_extra or {})
    return subprocess.run(cmd, env=env, timeout=timeout, text=True, capture_output=True)


def push_branch(repo: Path, branch: str) -> None:
    if not branch.startswith("nightly/"):            # guard lives in code, never in a prompt
        raise PushRefused(branch)
    run(["git", "-C", str(repo), "push", "origin", f"HEAD:refs/heads/{branch}"],
        timeout=120, scrub=False)                     # no --force, ever
```

## Testing Strategy

| Level | What | How | Where |
|-------|------|-----|-------|
| Unit (always) | grouping (tag resolution, family filter, name rule, denylist), priority order, subject selection + lifecycle states, push guard, branch naming (`:` stripped), Haiku verdict parsing, usage-limit message detection, env scrubbing | `python3 -m unittest`, tiny fixtures, no I/O | `tests/test_*.py` |
| Contract | Haiku supervisor on 5 recorded worker stops (setup question, case list, fix loop, done, nonsense) | real Haiku call, run by hand, not in the unit suite | `tests/recorded/` + `--subject` debug flag |
| Integration | preflight green; one subject → PR on the fork | `docker compose run` on the real stack | Checkpoint 1a / 1 in `tasks/todo.md` |
| Night | shortened window, 3 subjects, hanging-subject kill | real stack, timer fired by hand | Checkpoint 2 |

- Every bug fixed in pure logic gets a unit test first (Prove-It).
- No coverage number. Required: every row in the Unit column has at least one test.
- Tests never call Claude, GitHub, or Docker.

## Boundaries

**Always:**
- Run the unit tests before every commit.
- Run preflight before the first Claude call of a run.
- Start every `claude` process with the scrubbed env.
- Write the report, also on failure.
- Keep every version pinned (MC, FlintCLI, flint-core, Rust nightly, Claude CLI, images).
- Update `SPEC.md` / `tasks/plan.md` before changing behavior they describe.

**Ask first:**
- Adding any dependency (pip, npm in this repo, apt beyond the listed tools).
- Changing models, `MAX_SUBJECTS_PER_NIGHT`, time budgets or Q&A caps.
- Changing the grouping rule once `groups.json` is frozen.
- Editing the vendored skill (it should stay equal to the global one).
- Bumping MC version, FlintCLI or flint-core pins (they move together).
- Changing the PR format or branch naming (branch names are the done markers).

**Never:**
- Push to `master`, force-push, or push to an existing `nightly/*` branch.
- Give `GITHUB_TOKEN` to a Claude process, or put a token in a remote URL, image layer, log, report or PR.
- Publish the Minecraft port.
- Commit `.env` or any token.
- Mount the host's FlintBenchmark checkouts, or the Docker socket.
- Let an LLM decide a git action (push, branch, PR target).
- Wait for a usage-limit reset or use usage credits.

## Constraints

- **Versions are locked:** MC server = decompiled source = tags = **26.2**. flint-core spec rev = FlintCLI's flint-core rev. Preflight blocks the run on mismatch.
- **Network:** the MC server is offline-mode, with op for `flintmc_testbot`, and its port is never published. The agent uses the shared network namespace at `localhost:25565`.
- **Host:** always-on x86_64 Linux server with Docker. The timer uses the server timezone, which must be the owner's.
- **Claude:** Pro subscription. 5h + weekly limits are shared with the owner's daytime use. Usage credits off. API key = fallback.
- **Container:** non-root (`agent` runner, `worker` for Claude, D26). Only `/work` and `/state` are writable.

## Inputs

| Input | Location | Mode |
|-------|----------|------|
| `implemented.txt`, `priority.txt` | `config/` → `/config` | ro |
| Decompiled MC 26.2 source + vanilla tags (`data/minecraft/tags`) | `minecraft_26.2/` (gitignored) → `/mc-src` | ro |
| `flint-tests` skill | `vendor/` → image `~/.claude/skills/` | baked in |
| `flintcli`, `flint-validate`, flint-core spec | built/cloned in Dockerfile at pinned commits | baked in |
| FlintBenchmark | fresh clone per run → `/work` | rw |
| `CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_TOKEN`, git identity, limits, models | `.env` | secret / config |

## Architecture (summary)

```
systemd timer (server) → docker compose run agent --night
  minecraft  : vanilla 26.2, offline, flat world, fresh per run, no published port
  agent      : python -m runner
                 ├─ preflight (server up, flintcli known-good test, tiny Haiku call)
                 ├─ subjects = groups.json (Haiku-reviewed once, frozen) ordered by priority.txt
                 ├─ per subject (max 3):
                 │    worker A (PLAN_MODEL=opus, read-only tools, /flint-tests) → case plan
                 │    Haiku: review plan, answer questions, DONE/QUESTION/STUCK
                 │    worker B (EXEC_MODEL=sonnet, --resume) → write + run tests → JSON summary
                 │    runner: copy tests back → re-run each with flintcli → gate → commit → push nightly/<subject> → PR
                 └─ report → /state/reports/<date>.md
```

## Subject lifecycle

| State | Stored as | Picked again? |
|-------|-----------|---------------|
| done | remote branch `nightly/<subject>` exists | never |
| unsupported | `/state/subjects.json` | never |
| covered (existing tests already cover it) | `/state/subjects.json` | never |
| gave_up | `/state/subjects.json` + attempt count | until 2 attempts |
| unfinished (deadline / usage limit) | not stored | yes, next night |

## PR format

```
Title: Add Flint tests for <subject> (<n> blocks)

## Blocks
- minecraft:<representative> → implementation `<Class>`; also covers: <ids>

## Cases
- <one sentence per case>
```

## Owner actions before Phase 1

1. Decompiled **26.2** source in `minecraft_26.2/` (it includes the tags).
2. `.env` on the server (chmod 600): `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) + `GITHUB_TOKEN` (fine-grained PAT, fork only).
3. Usage credits off on the Pro account. Read the Pro consumer terms on unattended use.

## Open Questions

None. Resolved 2026-10-09: runner = Python 3 stdlib (D23); inputs live in `config/` (D24, done); this repo will most likely be a git repo the server clones (D25). Nothing in the design depends on that: the server only needs the files, so `rsync`/`scp` works the same way.
