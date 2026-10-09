# Setup: what goes where

Everything you need to provide, where it goes, and how to check it. Spec: `SPEC.md`. Design: `tasks/plan.md`.

## What you provide

| What | Where it goes | Notes |
|------|---------------|-------|
| Claude token | `.env` → `CLAUDE_CODE_OAUTH_TOKEN=` | From `claude setup-token` (step 4). Valid 1 year |
| GitHub token | `.env` → `GITHUB_TOKEN=` | Fine-grained PAT (step 5). Only the runner uses it, never Claude |
| Your name + email | `.env` → `GIT_AUTHOR_NAME/EMAIL`, `GIT_COMMITTER_NAME/EMAIL` (prefilled in `.env.example`) | Commits are made as you |
| Decompiled MC **26.2** source | `minecraft_26.2/` next to `compose.yaml` (gitignored, ~850 MB) | Mounted read-only at `/mc-src`. The vanilla tags are read from its `data/minecraft/tags/` |
| What to test | `config/implemented.txt` | One `minecraft:<id>` per line. Edit any time, no rebuild |
| What to test first | `config/priority.txt` | Ids or subject names, top first. Edit any time |
| Timezone | `.env` → `TZ=` **and** the server (`timedatectl`) | Timer and deadline use local time |

`.env` lives only on the server, next to `compose.yaml`. It's in `.gitignore`. Never commit it.

## 1. Server prerequisites

- x86_64 Linux, Docker Engine + compose plugin (`docker compose version` works)
- Timezone: `sudo timedatectl set-timezone Europe/Berlin` (yours)
- About 10 GB free disk (Rust build stage, images, MC server)

## 2. Put the repo on the server

```bash
sudo git clone <this repo> /opt/flint-ai-cronjob    # or rsync/scp the folder
cd /opt/flint-ai-cronjob
mkdir -p state && sudo chown 1000:1000 state         # runner (uid 1000) writes reports + state here
cp .env.example .env && chmod 600 .env
```
If you choose another path than `/opt/flint-ai-cronjob`, change `WorkingDirectory=` in `deploy/flint-ai-cronjob.service`.

## 3. Minecraft 26.2 source and tags

- Copy the decompiled 26.2 source to the server as `minecraft_26.2/` in the repo folder (it's gitignored, so `git clone` doesn't bring it: use `rsync`/`scp`).
- Tags come straight from `minecraft_26.2/data/minecraft/tags/{block,item}`. Nothing to copy. A new source folder brings matching tags with it.
- **Must be 26.2**: FlintCLI `f2474b8` uses azalea `a8959f62`, which speaks 26.2 (protocol 776).

## 4. Claude token (Pro subscription)

On a machine with a browser (your desktop):
```bash
claude setup-token
```
Log in with your Pro account. It prints a token (`sk-ant-oat…`). Put it in the server's `.env`:
```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat…
```
- Also check in your claude.ai account settings that **usage credits / extra usage are off**. Otherwise usage past the limit gets billed.
- The token expires after 1 year. Put a reminder in your calendar at 11 months. When it expires, preflight fails with "Claude token invalid".
- Don't set `ANTHROPIC_API_KEY` in `.env` unless you deliberately switch to the API-key fallback.

## 5. GitHub token

github.com → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token:
- Repository access: **Only select repositories** → `JunkyDeveloper/FlintBenchmark`
- Permissions → Repository: **Contents: Read and write**, **Pull requests: Read and write** (Metadata: read is added automatically)
- Expiration: your choice (note the date)

Put it in `.env`:
```
GITHUB_TOKEN=github_pat_…
```
Optional, recommended: a ruleset on the fork that blocks pushes to `master` (Settings → Rules → Rulesets). The runner already refuses anything except `nightly/*`, but this is a second lock.

## 5b. Sync the fork

On github.com/JunkyDeveloper/FlintBenchmark → **Sync fork**. The fork's `master` (c972906) was behind on 2026-10-09. The worker learns from the existing tests, and the PRs should start from the current state.

## 6. Build

```bash
docker compose build        # first build compiles flintcli, flint-validate, typos: takes a while
```

## 7. Checks, in this order

```bash
# Unit tests (any machine with python3, no Docker needed)
python3 -m unittest discover -s tests

# Subject list without Claude, GitHub or Docker
CONFIG_DIR=config STATE_DIR=state MC_SRC_DIR=minecraft_26.2 python3 -m runner --dry-run

# Preflight: clone, MC server, flintcli known-good test, tiny Claude call   (Checkpoint 1a)
docker compose run --rm agent --preflight
docker compose rm -fsv minecraft
cat state/reports/*-preflight.md

# Check the server port is NOT reachable from the host (must fail):
nc -z localhost 25565 && echo "EXPOSED - fix compose.yaml" || echo "ok, not exposed"
```

## 8. First real subject by hand (Checkpoint 1)

Take a small subject from the dry-run list, e.g. `buttons`:
```bash
docker compose run --rm agent --subject buttons \
  --members minecraft:oak_button,minecraft:stone_button,minecraft:birch_button
docker compose rm -fsv minecraft
cat state/reports/*-subject.md
```
Expected: a PR `Add Flint tests for buttons (…)` in `JunkyDeveloper/FlintBenchmark`, from branch `nightly/buttons`.
The report shows which models ran (`models:`). Check that the plan phase used Opus and the exec phase Sonnet.

## 9. Enable the nightly timer

```bash
sudo cp deploy/flint-ai-cronjob.service deploy/flint-ai-cronjob.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flint-ai-cronjob.timer
systemctl list-timers flint-ai-cronjob.timer      # next run between 03:00 and 04:00
```
Test a night right now (it stops at `DEADLINE` or after 5h): `sudo systemctl start flint-ai-cronjob.service`, logs: `journalctl -u flint-ai-cronjob -f`.

## Daily use

- **Mornings:** review the PRs in the fork. Reports: `state/reports/`.
- **Nothing came?** Read the newest report. Failures are quiet by design.
- **Change what's tested:** edit `config/implemented.txt` / `config/priority.txt`. No rebuild needed.
- **Grouping:** created once, then frozen in `state/groups.json`. Full list: `state/subjects-preview.md`. To regroup from scratch, delete `state/groups.json`.
- **Retry a subject that gave up or was rejected:** remove its entry from `state/subjects.json`. If a PR/branch `nightly/<subject>` exists, delete the branch and close/delete the PR first, otherwise it counts as done.
- **More or fewer subjects per night:** `MAX_SUBJECTS_PER_NIGHT` in `.env` (shares your Pro weekly limit).
- **A run was stopped** (timeout, reboot, usage limit): nothing to do. The next run sees `state/current.json`, Haiku decides to resume or restart, and that subject runs first. After 3 interruptions it's given up. To throw the unfinished work away instead: `rm state/current.json`.
- **Reset the worker's leftover files/sessions completely:** `docker volume rm flint-ai-cronjob_sandbox flint-ai-cronjob_claude-sessions` (don't use `docker compose down -v`).
- **Skill changed locally:** `cp ~/.claude/skills/synced/*/flint-tests/SKILL.md vendor/skills/flint-tests/SKILL.md`, then rebuild.

## Preflight failure messages

| Message | Fix |
|---------|-----|
| `clone of … failed (GITHUB_TOKEN?)` | Token missing/expired or no access to the repo |
| `Minecraft server not reachable` | `docker compose logs minecraft` (EULA, Java, download) |
| `flintcli known-good test … failed` | MC version ≠ 26.2, or bot not op (check `OPS` worked in the server log) |
| `Claude token invalid` | Redo step 4 |
| `Claude usage limit already reached` | Wait for the reset. The night is skipped |
