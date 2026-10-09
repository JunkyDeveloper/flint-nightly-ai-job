# Todo — flint-ai-cronjob

See `plan.md` (v19). Owner setup: `SETUP.md`.
Legend: [x] done · [~] code written + unit-tested, needs verification in Docker / on the server · [ ] open

## Local verification 2026-10-09 (no Docker here; real binaries + real 26.2 server)
- [x] old `config/tags` identical to `minecraft_26.2/data/minecraft/tags` → removed, runner reads tags from the source (D30); `version.json` = 26.2
- [x] Mojang 26.2 server jar needs Java 25 → itzg `java25` tag correct
- [x] flintcli `f2474b8` builds (only **without** `--locked`, Dockerfile fixed) and passes `iron_bars_connections.json` + `water_lava_mixing.json` against vanilla 26.2 offline with op'd bot
- [x] flint-validate `a6f4d12c` builds; runner `verify_test` works with real flintcli
- [x] Found: 13 (local) / 44 (fork master) existing tests fail flint-validate (`use_item_on` no longer exists, also on flint-core main) → gate now checks only our files; worker prompt warns
- [x] Found: fork `master` (c972906) is behind your local copy (68591eb) → **sync the fork** before the first run
- [x] `claude -p` JSON fields match parser; `--resume` + `--model` switch works (haiku→sonnet, same session); `/flint-tests` loads via stdin prompt
- [x] Found + fixed: `--disallowedTools` swallowed the positional prompt → prompt now via stdin (regression test)

## Phase 0 — Decide
- [x] Questions, `SPEC.md` (v2 approved, 2.1 = D26–D29), inputs moved to `config/`
- [x] 26.2 source in `minecraft_26.2/` (gitignored), tags read from it (D30)
- [ ] **You:** sync fork `JunkyDeveloper/FlintBenchmark` with upstream (GitHub → Sync fork)
- [ ] **You:** `.env` with `CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_TOKEN`, git identity, `TZ` (SETUP §2, §4, §5); usage credits off

## Phase 1 — First AI PR (one subject, started by hand)
- [x] T1 Repo skeleton: vendored skill (checksum = global), `.gitignore`, `.env.example`
- [~] T2 Dockerfile build stage: flintcli `f2474b8`, flint-validate `a6f4d12c`, typos 1.47.2 (Rust nightly 2026-09-29)
  - Verify: `docker compose build`
- [~] T3 Dockerfile final stage: debian trixie, python3, claude 2.1.296 (no autoupdate), gh, node, ripgrep, netcat, users `agent`/`worker`, skill
  - Verify: `docker compose run --rm --entrypoint sh agent -c 'flintcli --help && flint-validate --help && typos --version && claude --version && sudo -u worker -H ls ~worker/.claude/skills/flint-tests'`
- [~] T4 `compose.yaml`: MC 26.2 offline, op bot, flat, no ports, healthcheck; agent shares its network
  - Verify: host `nc -z localhost 25565` fails (SETUP §7)
- [x] T5 Runner core: modes, scrubbed `run()`, report (unit tests)
- [~] T6 Git ops 1: HTTPS clone with per-command token, done check (branch OR PR), branch naming (unit tests)
- [~] T7 Preflight: clone → server → flintcli known-good test → Claude ping
  - Verify: `docker compose run --rm agent --preflight` → **Checkpoint 1a**
- [~] T8 ~~opusplan spike~~ → D27: explicit models per phase. Verify in Checkpoint 1: report `models:` shows opus + sonnet + haiku
- [x] T9 Prompts: worker (plan), exec, summary, supervisor, grouping
- [~] T10 Agents: plan → review → exec ↔ supervisor → summary; round cap, time budget, usage-limit stop (unit tests with fake claude)
  - Verify by hand: supervisor classifies real worker stops sensibly (watch the first runs' reports)
- [~] T11 Git ops 2: sandbox (worker user, no .git), collect `tests/**/*.json`, re-run each with flintcli (D29), gate, push guard, PR (unit tests)
  - Verify: inside a worker session `env | grep -i token` shows no GitHub token; worker can't `git push`
- [~] T12 `--subject` mode end to end
- [ ] **Checkpoint 1:** `--subject buttons …` → PR in the fork, tests pass on re-run, CI green (SETUP §8)

## Phase 2 — Night operation
- [~] Grouping: tags + name rule (274 subjects on current data), Haiku review once → frozen `state/groups.json`, new ids appended (unit tests)
- [~] `priority.txt` ordering, unknown entries → report warning (unit tests)
- [~] Subject state (`unsupported`/`covered`/`gave_up` ×2), done check before each subject (unit tests)
- [~] `MAX_SUBJECTS_PER_NIGHT`, `DEADLINE`, per-subject budget, usage-limit stop, per-subject error isolation
- [~] systemd service + timer (03:00 + random ≤1h, 5h hard kill, world wiped before/after)
- [ ] **Checkpoint 2:** `sudo systemctl start flint-ai-cronjob.service` with `MAX_SUBJECTS_PER_NIGHT=3` → 3 subjects in priority order, report complete

## Phase 3 — Server deploy
- [ ] **You:** SETUP §1–§9 on the server
- [ ] **Checkpoint 3:** after one real night, PRs waiting in the fork at 09:00

## Known ceilings (later)
- New id for a family that already has a PR is skipped (plan: "Known ceilings")
- MC version bump: replace `minecraft_26.2/` (brings tags), `VERSION` in compose, FlintCLI pin together
- itzg `OPS` with offline mode: if the bot isn't op (flintcli gets kicked for spam), op it via RCON
