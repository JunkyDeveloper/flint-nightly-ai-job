# Plan: flint-ai-cronjob — nightly AI workflow

Status: **v19** — 2026-10-09. Phase 1 + 2 code written (unit tests green). Docker/server verification pending (no Docker on the dev machine). Owner setup: `SETUP.md`.

## Goal

Unattended nightly run in Docker. A Claude Code agent uses the `flint-tests` skill to write Flint tests for Minecraft blocks/items. It verifies each test against a real vanilla server using `flintcli`, and checks behavior against the decompiled Minecraft source.

- **Start window:** 03:00–04:00
- **Hard stop:** 08:00–09:00 (done or killed, report written)

## Inputs

| Input | Source | How it gets into the container |
|-------|--------|--------------------------------|
| Vanilla Minecraft server | Official server jar (via `itzg/minecraft-server` image) | Own container (see topology) |
| Decompiled MC source | You copy it to the server once (26.2) | **Bind mount, read-only** → `/mc-src` |
| `implemented.txt`, `priority.txt` | `config/` in this repo; vanilla tags come from the source (D30) | **Bind mount, read-only** → `/config` (D24) |
| `flint-tests` skill | `~/.claude/skills/synced/<uuid>/flint-tests/SKILL.md` | **Vendored copy in repo** → `COPY` into image at `~/.claude/skills/flint-tests/` |
| `flintcli` binary | `https://github.com/JunkyDeveloper/FlintCLI` @ `master` (pin: `f2474b8`) | **Multi-stage Dockerfile**: build stage compiles, final stage copies only the binary → `/usr/local/bin/flintcli` |
| flint-core `test_spec.rs` | flint-core repo | Cloned at build time at the **same rev FlintCLI depends on** (`a6f4d12c`) → `/opt/flint-core` |
| FlintBenchmark (examples + output target) | `https://github.com/JunkyDeveloper/FlintBenchmark.git` (HTTPS: the PAT doesn't work over SSH), base `master`, tests in `tests/` | **Working git repo**: fresh clone per run → `/work/FlintBenchmark`. Agent commits + pushes a branch |

## Runtime topology

```
server (Linux, Docker, always on)
 └─ systemd timer (03:30 ± 30min, server TZ!) → docker compose run … (outer timeout 08:30)
     │
     ├─ service: minecraft  (itzg/minecraft-server, pinned tag)
     │    TYPE=VANILLA  VERSION=26.2   (image tag with matching Java)
     │    ONLINE_MODE=FALSE  ENFORCE_SECURE_PROFILE=FALSE
     │    ops.json mounted (offline UUID 40fb02f0-…; OPS= fails: online name lookup)
     │    no `ports:` → not reachable from host or LAN
     │    healthcheck: mc-health
     │    LEVEL_TYPE=FLAT, peaceful, fixed seed, world fresh per run
     │
     └─ service: agent  (our image)
          network_mode: "service:minecraft"  → server is localhost:25565
          depends_on: minecraft (healthy)
          mounts:
            /mc-src        ← host decompiled source   (ro)
            /config        ← this repo's config/      (ro)
            /state         ← run history + reports    (rw, volume)
            /work          ← FlintBenchmark clone     (rw, wiped per run)
            env_file: .env ← CLAUDE_CODE_OAUTH_TOKEN, GITHUB_TOKEN, git identity, limits (D22)
          contains: python3 (runner), claude CLI, flintcli, flint-validate, flint-tests skill,
                    /opt/flint-core, git, gh, node+npm, typos, netcat, ripgrep; runs as non-root
```

**Why `network_mode: service:minecraft`:** the skill hard-codes `nc -z localhost 25565` and `-s localhost:25565`. Sharing the network namespace makes `localhost` work without editing the skill. The port is never published, so offline mode is only reachable from inside the workflow. That's what makes offline mode acceptable.

## Agent image (Dockerfile, multi-stage)

```
stage flintcli-build
  FROM rustlang/rust:nightly-trixie-2026-07-19   # newer nightlies break azalea a8959f62
  source: https://github.com/JunkyDeveloper/FlintCLI @ ARG FLINTCLI_REF=f2474b8
  cargo build --release --locked
  → /out/flintcli  (binary name: check Cargo [[bin]], package is "flintmc")
  flint-core @ a6f4d12c: cargo build --release --bin flint-validate → /out/flint-validate

stage final
  FROM debian:<pinned>-slim
  install: python3 (≥3.12), claude CLI (pinned, DISABLE_AUTOUPDATER=1), git, gh, node+npm, typos, netcat-openbsd, ripgrep, ca-certs
  COPY --from=flintcli-build /out/flintcli /usr/local/bin/flintcli
  COPY vendor/skills/flint-tests  /home/agent/.claude/skills/flint-tests
  git clone flint-core && checkout a6f4d12c → /opt/flint-core   # = FlintCLI's flint-core rev
  COPY --from=flintcli-build /out/flint-validate /usr/local/bin/flint-validate   # same check as FlintBenchmark CI
  COPY runner/ /opt/runner/runner                 # ENTRYPOINT ["python3", "-m", "runner"]
  USER agent
  LABEL flintcli.ref, flint-core.rev, mc.version, claude.version   # shows in report
```

## Version lock (most likely silent failure)

The values in each row must match each other. Pin them in one place (`.env` / build args) and check them before every run.

| Must match | Current value | Why |
|------------|---------------|-----|
| MC server version = decompiled source version = azalea protocol version | **26.2**. FlintCLI `f2474b8` pins azalea `a8959f62` = protocol 776, `VERSION_NAME "26.2"` (verified 2026-10-09 in azalea source) | Bot can't connect if the protocol differs. The agent reasons about the wrong code if the source differs |
| flint-core spec rev = FlintCLI's `flint-core` dependency | `a6f4d12c` (local checkout ~/flint/FlintCLI is an older fork: ignore it) | The agent writes JSON in a spec version the CLI can't parse |
| Vendored skill = your global skill | Checksum compared at build time | Avoids drift between your local workflow and the nightly one |
| itzg image Java version ≥ what 26.2 needs | pin image tag | Server won't start otherwise |
| `flint-validate` version = FlintBenchmark CI's | same flint-core ref | Local gate green but CI red otherwise |

**Preflight (each run, before any AI spend):** clone FlintBenchmark → server healthy → `flintcli` runs 1 known-good test from the clone → passes → tiny Haiku call answers. A failed preflight aborts the run and puts the reason in the report.

## Agent architecture (D16): runner + Haiku supervisor + Opus/Sonnet worker

Your idea: a **Haiku** agent drives the night, groups subjects using the tags, and starts a **worker** on `opusplan` that runs the skill. Haiku answers the skill's questions. That also solves the interactive-skill problem (P2).

### Who does what

| Role | What | Why this role |
|------|------|---------------|
| **Runner** (`python -m runner`, stdlib, no LLM) | Timer start, preflight, deadline, budgets, subject selection, branch-exists check, pre-push gate, push guard (`nightly/*` only), PR creation, report | Safety and time limits must be deterministic. An LLM must never decide whether to push to `master` |
| **Haiku supervisor** (`claude -p --model haiku`, short calls) | (a) review grouping once, (b) answer worker questions, (c) classify each worker stop as DONE / QUESTION / STUCK, (d) write the PR `## Cases` sentences | Cheap judgment calls. Short calls instead of one 5-hour session: no context overflow, and a crash costs one call, not the night |
| **Worker** (`claude -p`, `/flint-tests` skill) | Explore mc-src, propose cases, write + run tests | The expensive, capable part |

The supervisor runs as **many short calls made by the runner**, not one long agent that runs everything. That's the one change from your idea. The runner keeps control of time and git, and Haiku only gets the decisions that need judgment.

### Worker = `opusplan`, done explicitly in 2 phases

`opusplan` uses Opus **only in plan mode**, Sonnet otherwise. In `-p` that means starting in plan mode, and leaving plan mode needs an approval. The skill fits this split exactly:

```
Phase A (plan)   claude -p --model opusplan --permission-mode plan --output-format json
                 "/flint-tests  subject: <family> members: <ids>  paths: …"
                 → skill setup, mc-src exploration, implementing class per member,
                   proposed case list (= the plan)                        [Opus]
                 Haiku reviews the plan → approve / ask for changes
Phase B (exec)   claude -p --resume <session_id> --permission-mode bypassPermissions
                 "Plan approved: <Haiku's answer>. Implement."
                 → write + run tests, fix loop                            [Sonnet]
```
If `opusplan` with `--resume` + a permission-mode switch doesn't behave as expected (verify in the Phase 1 spike), fallback: Phase A `--model opus`, Phase B `--resume … --model sonnet`. Same result, explicit.

### Question loop

```
worker turn ends → runner passes last message + context to Haiku
Haiku → DONE        → runner collects tests, gate, push, PR
      → QUESTION    → answer → claude -p --resume <id> "<answer>"
      → STUCK       → runner records gave_up, moves on
cap: max 10 Q&A rounds per subject, + per-subject time budget
```
**Haiku's context** (fixed prompt): subject + members, fixed paths (`/opt/flint-core/src/test_spec.rs`, `/usr/local/bin/flintcli`, `/mc-src`, `localhost:25565`), rules (blocks/items only; **one representative per distinct implementation**, no tests for other members of the same implementation; max fix attempts per test; a test must match the source, not just pass), and "no human available: decide, never defer".

Known skill questions and their answers:
1. Setup paths → fixed, known (can also go straight into the first prompt)
2. "What to test?" → already in the first prompt (subject)
3. "Confirm the case list" → Haiku reviews against the rules
4. Unbounded fix loop → Haiku answers "give up on this case" after N attempts
5. "Another block?" → runner ends the session

The skill stays **vendored unchanged** (your local workflow keeps working).

## FlintBenchmark git workflow

```
per run:
  git clone --depth=50 https://github.com/JunkyDeveloper/FlintBenchmark.git /work/FlintBenchmark      # never the host checkouts in ~/flint/FlintBenchmark/*
  git switch -c nightly/<subject>                                     # one branch per subject, no date → existence = already done
  npm ci
agent writes tests/…/*.json, each test passes against the server
pre-push gate (same as CI):
  npm run format              # auto-fix, then format:check must pass
  typos tests/
  flint-validate tests --recursive
  gate fails → no push, reason in report
git commit (author: your name + email, D8) → git push origin <branch>
gh pr create --repo JunkyDeveloper/FlintBenchmark --base master --head <branch>  (body below)
```

Rules:
- Push **only** `nightly/*` branches. Never `master`, never force-push. `master` on the fork is **unprotected**, and a fine-grained PAT can't restrict branches, so the **runner guard is the only protection**. Optional, free: a GitHub ruleset on the fork that blocks pushes to `master`.
- FlintBenchmark is a **fork**: `gh pr create` targets the *upstream parent* by default → always pass `--repo JunkyDeveloper/FlintBenchmark --base master`.
- Credential in **`.env`** as `GITHUB_TOKEN` (D22), never in the image. The worker never sees it (see Token isolation).
- Commit identity: your name + email from `.env` (`GIT_AUTHOR_NAME/EMAIL`, `GIT_COMMITTER_*`). They are the same for every commit.
- If the branch already exists remotely, the subject counts as done and is skipped (see Subject queue). Never push into an existing branch.
- Host checkouts in `~/flint/FlintBenchmark/{main,bars,…}` are never mounted, so your local work stays untouched.

### Token isolation (D22)

Both tokens come from `.env`, so they're environment variables of the container. The worker runs Bash, so anything in its environment is usable by the model.
- The runner starts every `claude` process with a **scrubbed env**: `env -u GITHUB_TOKEN -u GH_TOKEN claude …`. The worker only gets `CLAUDE_CODE_OAUTH_TOKEN`.
- `/work` clone: the remote URL has **no token in it**, and no git credential helper is configured. A `git push` from the worker fails.
- Only the runner's own push/PR step gets the token, per command: `GH_TOKEN=… gh pr create …`, `git -c credential.helper='!f(){ echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f' push …`.
- Verify: from inside a worker session, `env | grep -i token` shows no GitHub token, and `git push` fails.

### PR format

One PR per subject. The agent fills it from its own result (one sentence per case):

```
Title: Add Flint tests for <subject> (<n> blocks)

## Blocks
- minecraft:<representative>  → implementation `<Class>`; also covers: <other ids with same implementation>
- ...

## Cases
- <case 1 in one sentence>
- <case 2 in one sentence>
- ...
```
Only passing tests are committed, so `Cases` lists exactly those. Cases that failed or were skipped go into the morning report, not the PR.

## Job model (v2)

One job = one subject (family):
```
input:  subject + member ids, time budget, max Q&A rounds, max fix attempts
steps:  worker phase A (Opus plan) → Haiku review → worker phase B (Sonnet exec)
        ↔ Haiku question loop → keep only passing tests that match the source
output: commit on nightly/<subject> + PR (Haiku writes ## Cases) + report line
        (passed / gave up / unsupported / covered)
```
The runner iterates until no subjects are left, the deadline hits, or the usage limit hits. Unfinished subjects carry over (`/state`).

## Subject queue (P1)

**Source:** `config/implemented.txt`, one `minecraft:<id>` per line. 1079 ids (2026-10-09), all valid and unique. It's in this repo and mounted read-only → `/config/implemented.txt`, so you can update it without rebuilding the image.

**Selection per night (stateless first, minimal state second):**
```
for id in implemented.txt (file order):
  skip if id ends with _spawn_egg                     # scope: blocks + items only, no entities (D14)
group remaining ids into subjects (family rule below)
order subjects: priority.txt first (D20), then order of first appearance in implemented.txt
for subject in that order:
  skip if done: remote branch nightly/<subject> exists (git ls-remote)
               OR any PR with head nightly/<subject> (gh pr list --state all --head …)
  skip if /state/subjects.json says unsupported/covered/gave_up(≥2 tries)
  → run subject
```
- `<subject>` = family name (`nightly/buttons`) or, for single ids, the id without `minecraft:` (`:` is not allowed in git refs): `minecraft:bell` → `nightly/bell`.
- Branch **or** PR (open, merged, or closed) with that head = done. GitHub may delete branches after merge, so the PR check is required. A closed, unmerged PR also counts as done: you rejected it. Delete that PR's branch and note it in `priority.txt` to retry.
- Losing `/state` only costs a retry of failed subjects.
- `/state/subjects.json` only records outcomes that produce no branch:
  `unsupported` (skill rejects it), `covered` (agent finds existing tests already cover it), `gave_up` (+ attempt count).
- The agent decides `covered` itself (skill step 4 reads existing tests). A grep isn't reliable: only ~66 of the ids appear in the 50 existing tests at all, and mostly as scaffolding (`stone`, `air`, `water`).

### Family grouping (D15, default on)

A subject is a **family** of ids, not a single id. The runner computes it the same way every time, in 2 steps:

**Computed candidates, then Haiku review, then frozen.** Grouping must be **stable**: the branch name `nightly/<subject>` is the done marker, so subjects must not be renamed between nights. Flow:
1. Runner computes candidate groups (steps 1+2 below, same output every time)
2. **Haiku reviews once:** split mixed groups, merge obvious leftovers, name subjects. Output → `/state/groups.json`
3. Later nights: `groups.json` is **frozen**. Only ids that are new in `implemented.txt` go through steps 1–2 again, and Haiku appends them (to an existing group without a branch, or as a new subject)

**Step 1, vanilla tags (exact, preferred).**
- Load `/mc-src/data/minecraft/tags/{block,item}` (D30), resolve `#tag` references recursively, merge same-named block/item tags, and keep only implemented ids.
- **Family tag** = a tag without a `/` in its name (drops `mineable/*`, `enchantable/*`) with ≥2 members, where ≥80% of members contain the tag's singular last word (`wooden_buttons` → "button").
  - This drops behavior tags like `mineable/pickaxe` (341), `sulfur_cube_swallowable`, `frog_prefer_jump_to`.
  - 72 family tags qualify.
- Id is in several family tags → take the **largest** (`buttons` over `wooden_buttons`, `all_signs` over `wall_signs`). The agent splits by implementing class anyway.

**Step 2, name rule (fallback for ids in no family tag).**
1. Last word shared by ≥3 remaining ids → family (`*_stained_glass_pane` → `pane`)
2. Prefix families: `potted_`, `music_disc_`, `infested_`, `waxed_`, `stripped_`
3. Never group on generic words: `block`, `stem`, `bush`, `lantern`, `torch`, `plant`, `roots`, `sand`, `egg`, `rod`, `bucket`, `grass`
4. Otherwise: single-id subject

Result on the current files: **991 ids → 274 subjects** (implemented; with the last-word-before-prefix tuning).
- Largest: slabs 68, stairs 64, all_signs 48, flower_pots 39, walls 32, banners 32.
- Name-rule families: stripped 22, pane 17, glass 16, leaves 11, fan 10, sapling 9, wood 9, copper 8, waxed 8, coral 5, bulb 4, grate 4, tulip 4, bud 3, froglight 3, hyphae 2.
- Tuning for implementation: apply the last-word rule before the prefix rule, so `waxed_copper_bulb` joins `bulb` instead of `waxed`.
- Each run writes the full subject → members table to `/state/subjects-preview.md`, so you can check the grouping at any time.

**Groups are a scheduling unit, not a claim that all members behave the same.** Tags remove most false groups, but a large tag mixes classes too (`buttons` = wooden + stone, `all_signs` = standing + wall + hanging). The agent therefore:
- looks up each member's implementing class in `/mc-src` (e.g. all `*_slab` → `SlabBlock`; `waxed_cut_copper_slab` → a copper slab class)
- **D18: test each implementation once.** One representative per distinct implementation, tested thoroughly. Other members with the same implementation get **no** tests, only listed as "covered by" in the PR. (1 slab out of 60 is enough)
- "Same implementation" = same class **and** no member-specific behavior in the source (e.g. a block whose registration passes special properties that change behavior counts as its own implementation). The agent decides from the source and writes the reason in the PR.
- So a false group is still tested correctly, and PRs stay small.

Known ceilings (ponytail):
- An id added to `implemented.txt` later, for a family that already has a branch, is skipped. Upgrade: store tested members in `/state` and run a follow-up subject for new members.

### Priority (D20)

`config/priority.txt` in this repo, mounted read-only → `/config/priority.txt`. You edit it any time, no rebuild needed.
```
# one entry per line, top = first; '#' = comment
minecraft:oak_door        # an id → its whole subject moves up
doors                     # or a subject name (as in subjects-preview.md)
```
- Subjects matching an entry run first, in file order. Everything else follows in `implemented.txt` order.
- Missing or empty file → plain file order.
- Unknown entry (typo, id not in `implemented.txt`) → warning in the report, ignored.
- Already-done subjects (branch exists) stay skipped even if listed.

**Scope (D14):** blocks and items only. The 88 `*_spawn_egg` ids are filtered out → **991 subjects** (count as of 2026-10-09; file changes over time). Anything else that turns out to need entities is marked `unsupported` by the agent.

**Throughput:** 274 subjects at `MAX_SUBJECTS_PER_NIGHT=3` (D17) → **~92 nights (~3 months)**. With D18, big families cost about as much as their number of distinct implementations, not their member count, so a 64-slab family fits one night easily.

## Components

| # | Component | Responsibility | Depends on |
|---|-----------|----------------|------------|
| C1 | Scheduler | systemd timer + outer timeout | C2, C9 |
| C2 | Agent image | Multi-stage build above | — |
| C9 | Compose stack | minecraft + agent services, mounts, network | C2 |
| C3 | Runner | Modes, deadlines, budgets, question loop. `runner/__main__.py` | C2 |
| C4 | Worker | `claude -p` phase A (Opus plan) / phase B (Sonnet exec) with `/flint-tests`, resume by session id. `runner/agents.py` + `prompts/worker.md` | C2, Claude token |
| C10 | Haiku supervisor | Short `claude -p --model haiku` calls: grouping review, plan review, question answers, DONE/QUESTION/STUCK, PR cases text. `runner/agents.py` + `prompts/supervisor.md`, `grouping.md` | C2, Claude token |
| C5 | Subjects | Tags, grouping, priority, selection, `groups.json`/`subjects.json`. `runner/subjects.py` | C3 |
| C6 | Git ops | Clone, branch, gate, push guard, PR, done check. `runner/gitops.py` | C2, GitHub PAT |
| C7 | Report + preflight | Per subject: outcome, usage, versions; preflight checks. `runner/report.py`, `runner/preflight.py` | C3 |
| C8 | State | `/state` volume: `groups.json`, `subjects.json`, `reports/`, `subjects-preview.md` | C3 |

```
C2 image ─► C9 compose ─► C3 runner (preflight) ─► C4 worker ⇄ C10 Haiku ─► C5 jobs
                              │                       │
C1 scheduler ─────────────────┘        C6 workspace ──┘   C3 ─► C7 report, C8 state
```

## Phases (vertical slices)

### Phase 0 — Decide
Answer the open questions and write `SPEC.md`.
**Checkpoint 0:** versions pinned, tests repo chosen, agent architecture chosen (D16).

### Phase 1 — First AI PR (one subject, started by hand)
`docker compose run agent --subject <name> --members <ids>` → compose up → preflight (server, `flintcli` known-good test, tiny Haiku call) → worker A (Opus plan) ⇄ Haiku → worker B (Sonnet exec) → gate → push `nightly/<subject>` → PR on the fork → report.
AI from the first slice (your call). Preflight stays inside it: it costs almost nothing and stops a broken setup before Claude usage is spent.
**Checkpoint 1a:** preflight green from clean state.
**Checkpoint 1:** one real PR for a small subject (e.g. `buttons`). Tests pass on re-run, no stall on a skill question, CI green on the branch.

### Phase 2 — Night operation
Grouping (tags + name rule + Haiku review, frozen), `priority.txt`, subject state, `MAX_SUBJECTS_PER_NIGHT`, deadline, usage-limit stop, failure isolation, systemd timer.
**Checkpoint 2:** a shortened simulated night runs 3 subjects in priority order. A hanging subject is killed and the next one runs. The report is complete.

### Phase 3 — Server deploy
Stack + timer on the server.
**Checkpoint 3:** after one real night, PRs are waiting in the fork at 09:00.

## Decided

| # | Decision |
|---|----------|
| D1 | Target repo: FlintBenchmark (`JunkyDeveloper/FlintBenchmark`, base `master`), fresh clone per run, push `nightly/*` branches |
| D2 | Minecraft version: **26.2** (server + decompiled source + tags), because FlintCLI's azalea speaks 26.2 (was 26.3). Bump all together when FlintCLI moves to 26.3 |
| D3 | FlintCLI built from git URL at a pinned commit (see D5) |
| D4 | World: flat, fresh per run |
| D5 | FlintCLI: `JunkyDeveloper/FlintCLI` master, pinned `f2474b8` (azalea `a8959f62`, flint-core `a6f4d12c`) |
| D6 | ~~Secrets via docker compose `secrets:`~~ → replaced by D22 |
| D7 | PR per subject: block + one sentence per case |
| D8 | Commits under your name + email |
| D9 | GitHub auth: one fine-grained PAT (fork only, Contents RW + Pull requests RW) |
| D10 | Runs on a server (always on), not this machine |
| D11 | Delivery = the PRs. You review them in the morning. No notification channel |
| D12 | Quiet failure accepted: report file on server only, no issue/alert |
| D13 | Server: x86_64, resources sufficient. Images built `linux/amd64` |
| D14 | Scope: blocks + items only. `*_spawn_egg` pre-filtered (1079 → 991 subjects) |
| D15 | Subjects = families: vanilla tags first, name rule as fallback, agent splits by implementing class. 991 ids → 274 subjects |
| D16 | Agents: deterministic runner + Haiku supervisor (short calls) + worker (Opus plan → Sonnet exec, `/flint-tests` unchanged). Resolves P2 |
| D17 | Claude auth: `claude setup-token` (1-year OAuth token) → `.env` → `CLAUDE_CODE_OAUTH_TOKEN`. API key = fallback. Usage credits off. `MAX_SUBJECTS_PER_NIGHT` cap |
| D18 | Test each implementation once: 1 representative per distinct implementation, others listed as covered. PRs stay small, no PR split needed |
| D19 | `MAX_SUBJECTS_PER_NIGHT=3` confirmed |
| D20 | `priority.txt` (ids or subject names) runs first, rest in file order |
| D21 | No AI-free skeleton phase: Phase 1 goes straight to one real AI PR (preflight kept inside) |
| D22 | Tokens (`CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_TOKEN`) in `.env`, not compose secrets. Worker/Haiku never get `GITHUB_TOKEN` |
| D23 | Runner: Python 3 (3.12+), standard library only, `python -m runner` |
| D24 | Owner inputs in `config/` (implemented.txt, priority.txt), one ro mount → `/config` |
| D25 | This repo will most likely be a git repo the server clones; design doesn't depend on it |
| D26 | Two users in the container: runner `agent` (has GITHUB_TOKEN), every claude process as `worker` via sudo in a sandbox copy without `.git`. Runner copies back only `tests/**/*.json` |
| D27 | Worker models set explicitly per phase (`--model $PLAN_MODEL`, resume with `--model $EXEC_MODEL`) instead of `opusplan` + plan mode: deterministic, no plan-mode approval in `-p`. Plan phase gets read-only tools (`--disallowedTools Write,Edit,NotebookEdit`). Report shows models used, which verifies it |
| D28 | PR `## Cases` comes from the worker's final JSON summary, and the runner builds the PR body. No extra Haiku call |
| D29 | Runner re-runs every collected test with `flintcli` before committing. Only passing tests reach the PR |
| D30 | Vanilla tags read from `/mc-src/data/minecraft/tags` (the decompiled 26.2 source) instead of a copy in `config/`: smaller repo, can't drift from the source |
| D31 | Interrupted runs: runner keeps `/state/current.json` (subject, phase, session, last message) while a subject runs; kept on SIGTERM / night deadline / usage limit. Sandbox + Claude sessions in named volumes. Next run: Haiku (`interrupted.md`) decides RESUME (same session, `resume.md`) or RESTART; that subject goes first; gave_up after 3 interruptions |
| D32 | Fix rounds: the runner's independent checks (flintcli re-run, `typos`, `flint-validate` per file) send failures back into the worker's session (`fix.md`, ≤ `FIX_ROUNDS`=2, within the subject budget); only then are tests dropped. Worker prompts also require `flint-validate`. Found on the first real run: 1 of 14 tests failed `cleanup region must contain the origin`, which flintcli doesn't check |

## Claude auth with Pro in Docker (D17, was P3)

Source: Claude Code docs (authentication, costs, model-config) + Claude Help Center "Use Claude Code with your Pro or Max plan", read 2026-10-09.

### How login works without a browser in the container

```
once, on your desktop (has a browser):
  claude setup-token            → browser login with your Pro account
                                → prints a one-year OAuth token (documented "for CI pipelines, scripts")
put it into .env on the server (chmod 600, gitignored):
  CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat…
compose:
  env_file: .env
runner:
  unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN   # these would win over the subscription (auth precedence)
```
- No `/login`, no persisted `~/.claude` volume needed. Sessions only need to live for one run (`--resume` within the same container).
- **Expiry:** the token lasts 1 year. Preflight makes 1 tiny call (`claude -p --model haiku "reply ok"`). If it fails → report "Claude token expired/invalid", no subjects run. Put a calendar reminder at 11 months.
- Pin the Claude Code version in the image and disable auto-update (`DISABLE_AUTOUPDATER=1`), so the CLI doesn't change under the nightly run.

### Usage limits (the real constraint)

- Pro has a **5-hour session limit** and a **weekly limit**.
- **Both are shared with your normal Claude and Claude Code use.** The night run spends the same budget you use during the day.
- 03:30–08:30 is exactly one 5h window, so each night starts a fresh session window. The **weekly** limit is what will run out: 7 heavy nights can leave you with nothing during the week.
- When a limit is hit, Claude Code reports "You've hit your session limit" / "You've hit your weekly limit" with the reset time.
  - Runner detects these messages, stops cleanly, carries the subject over, and writes the reason in the report.
  - Never wait for the reset. Turn auto-continue off (`autoContinueAtUsageLimit`), to be verified in the spike.
- **Usage credits:** if enabled on your account, usage past the limit is **billed**. Keep them **off**, or set a spend limit, so the night run can never cost extra money.
- Model choice matters: Opus uses up the limit fastest. Make the worker models configurable (`PLAN_MODEL=opus`, `EXEC_MODEL=sonnet`, `SUPERVISOR_MODEL=haiku` in `.env`). If the weekly limit becomes a problem, switch planning to Sonnet first.
- **Self-limit:** `MAX_SUBJECTS_PER_NIGHT` in `.env` (start: 3) keeps part of the weekly budget for you. Raise it once real usage is known.

### Fallback: API key

`ANTHROPIC_API_KEY` from the Claude Console in `.env` instead of the OAuth token. Pay per token, hard spend limit in the Console, no shared limits with your Pro use. Switch by swapping the secret, no code change.

### Terms

`setup-token` is documented for scripts and CI, so headless Claude Code with a subscription token is a supported use of the tool. Still read the consumer terms for your Pro plan once yourself before relying on it every night. I'm not the authority on that.

## Open questions

None. `SPEC.md` v2 approved. Phase 0 closes when your 3 inputs are ready (26.2 source + tags, `.env`, usage credits off).

## Risks

| Risk | Mitigation |
|------|------------|
| Version mismatch (server ↔ azalea ↔ mc-src ↔ spec) | Single pin file + preflight |
| Agent stalls on skill question | Haiku supervisor answers (D16) + Checkpoint 1 |
| Fix loop never ends / test is shaped to pass | Max attempts. Final review step: "test matches mc-src behavior" |
| Offline-mode server exposed | No `ports:`. Checkpoint 1 verifies from host |
| Agent with skip-permissions damages host | Only `/work` + `/state` writable. `/mc-src` ro. Non-root. No docker socket |
| Unpinned Rust nightly breaks build | Pinned `nightly-trixie-2026-07-19` (2026-09-29 already fails on azalea). Bump only with FlintCLI |
| Synced skill path changes (UUID dir) | Vendored copy + documented refresh command |
| Pushed branch fails CI | Same 3 checks as CI run before push |
| Push credential leaks | Repo-scoped fine-grained PAT, `.env` chmod 600 + gitignored, expiry + rotate |
| Worker (has a shell) uses GITHUB_TOKEN to push itself, bypassing the guard | Token isolation: claude processes get a scrubbed env, no credential helper in `/work` (D22) |
| Tokens visible via `docker inspect` / `/proc` (env vars) | Accepted on a private single-user server (D22) |
| Pro usage limit hit mid-night | Detect limit message, stop cleanly, carry over (D17) |
| Night run eats your daytime/weekly Pro budget | `MAX_SUBJECTS_PER_NIGHT`, cheaper plan model, or API key fallback |
| Usage credits bill money unattended | Keep usage credits off / spend limit |
| OAuth token expires after 1 year | Preflight auth call + calendar reminder |
| `ANTHROPIC_API_KEY` set by accident overrides subscription | Runner unsets it unless fallback mode |
| Queue too long for throughput | Spawn-egg filter + family grouping (991 → 275) |
| Merged PR's branch auto-deleted → subject retested | Done check = branch OR any PR with that head |
| Grouping changes between nights → done markers break | Haiku groups once, `/state/groups.json` frozen, only new ids appended |
| Haiku approves a bad plan / answers wrong | Fixed rules in Haiku prompt; a test must pass against the real server and match the source; you review the PR |
| Worker never ends | Max Q&A rounds + per-subject time budget + STUCK classification |
| False family groups | Tags first; agent checks implementing class per member, one test per distinct implementation |
| Two members share a class but behave differently | "Same implementation" also requires no member-specific behavior in the source; agent writes the reason in the PR, you review |
| Tags out of sync with MC version | Gone: tags are read from the decompiled source itself (D30) |
| Same subject retried forever | `gave_up` after 2 tries in `/state/subjects.json` |
| FlintCLI master moves | Pinned commit via build arg; bump on purpose |
| Runaway usage / over deadline | Per-subject time budget, `MAX_SUBJECTS_PER_NIGHT`, global deadline, outer kill |
| Silent failure (no PRs looks like nothing to do) | Accepted (D12). Report file `/state/reports/<date>.md` always written |
| PR opened against upstream instead of fork | `--repo JunkyDeveloper/FlintBenchmark` always explicit |
| Push to unprotected `master` | Runner guard; optional ruleset |
| Server reboot / missed run | systemd `Persistent=true` only if start still inside window, else skip night |
