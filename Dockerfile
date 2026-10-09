# syntax=docker/dockerfile:1

# ---- build: flintcli, flint-validate, typos -------------------------------------
# azalea a8959f62 uses unstable const generics: nightly 2026-09-29 fails (E0284 in azalea-core bitset.rs),
# nightly-2026-07-19 (rustc eff8269f7 2026-07-18) builds FlintCLI f2474b8 (verified 2026-10-10). Bump only together with FlintCLI.
FROM rustlang/rust:nightly-trixie-2026-07-19 AS build
ARG FLINTCLI_REF=f2474b8
ARG FLINT_CORE_REV=a6f4d12c02cd37877d5b77e81b51477b16ab54c3
ARG TYPOS_VERSION=1.47.2
RUN mkdir -p /out \
 && git clone https://github.com/JunkyDeveloper/FlintCLI.git /src/flintcli \
 && git -C /src/flintcli checkout "$FLINTCLI_REF" \
 # no --locked: FlintCLI f2474b8's Cargo.lock misses one entry (checked 2026-10-09); git deps stay pinned by rev
 && rustc --version \
 && cargo build --release --manifest-path /src/flintcli/Cargo.toml \
 && install -m 755 /src/flintcli/target/release/flintmc /out/flintcli
RUN git clone https://github.com/FlintTestMC/flint-core.git /src/flint-core \
 && git -C /src/flint-core checkout "$FLINT_CORE_REV" \
 && cargo build --release --locked --manifest-path /src/flint-core/Cargo.toml --bin flint-validate \
 && install -m 755 /src/flint-core/target/release/flint-validate /out/flint-validate
RUN cargo install typos-cli --version "$TYPOS_VERSION" --locked --root /typos \
 && install -m 755 /typos/bin/typos /out/typos

# ---- final: runner + claude + tools ---------------------------------------------
FROM debian:trixie-20261005-slim
ARG CLAUDE_CODE_VERSION=2.1.296
ARG FLINTCLI_REF=f2474b8
ARG FLINT_CORE_REV=a6f4d12c02cd37877d5b77e81b51477b16ab54c3
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 git gh nodejs npm ripgrep netcat-openbsd sudo ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force
COPY --from=build /out/ /usr/local/bin/
RUN git clone https://github.com/FlintTestMC/flint-core.git /opt/flint-core \
 && git -C /opt/flint-core checkout "$FLINT_CORE_REV"

# agent = runner (holds GITHUB_TOKEN); worker = every claude process (never sees it).
# Different uids, so the worker can't read the runner's /proc/<pid>/environ.
RUN useradd -m -u 1000 agent \
 && useradd -m -u 1001 worker \
 && echo 'agent ALL=(worker) NOPASSWD: ALL' > /etc/sudoers.d/agent \
 && chmod 440 /etc/sudoers.d/agent \
 && mkdir -p /work/sandbox /state \
 && chown agent:agent /work /state \
 && chown worker:worker /work/sandbox
COPY --chown=worker:worker vendor/skills/flint-tests /home/worker/.claude/skills/flint-tests
COPY runner /opt/runner/runner

ENV PYTHONPATH=/opt/runner \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    FLINTCLI_REF=${FLINTCLI_REF} \
    FLINT_CORE_REV=${FLINT_CORE_REV} \
    CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}
USER agent
WORKDIR /work
ENTRYPOINT ["python3", "-m", "runner"]
