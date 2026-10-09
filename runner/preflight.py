"""Preflight: everything that must work before any real Claude usage is spent."""
import socket
import time

from . import MC_SERVER, REPO_DIR, env_str
from . import agents, gitops


class PreflightFailed(Exception):
    pass


def wait_for_server(address: str = MC_SERVER, timeout_s: float = 300) -> None:
    host, port = address.rsplit(":", 1)
    end = time.monotonic() + timeout_s
    while True:
        try:
            with socket.create_connection((host, int(port)), timeout=5):
                return
        except OSError as e:
            if time.monotonic() > end:
                raise PreflightFailed(f"Minecraft server not reachable at {address}: {e}")
            time.sleep(5)


def preflight(report) -> None:
    try:
        gitops.clone()
    except gitops.GitError as e:
        raise PreflightFailed(f"clone of {gitops.repo_slug()} failed (GITHUB_TOKEN?): {e}")
    report.note(f"cloned {gitops.repo_slug()}@{gitops.base_branch()}")

    wait_for_server()
    test = env_str("PREFLIGHT_TEST", "tests/connectible/iron_bars_connections.json")
    if not (REPO_DIR / test).exists():
        raise PreflightFailed(f"PREFLIGHT_TEST `{test}` not found in FlintBenchmark")
    ok, out = gitops.verify_test(test)
    if not ok:
        raise PreflightFailed(f"flintcli known-good test `{test}` failed "
                              f"(server/protocol version mismatch or bot not op?): {out}")
    report.note(f"flintcli passed `{test}` against {MC_SERVER}")

    r = agents.ping()
    report.usage_usd += r.cost
    if agents.hit_limit(r):
        raise PreflightFailed(f"Claude usage limit already reached: {r.text[:300]}")
    if r.is_error or "ok" not in r.text.lower():
        raise PreflightFailed(f"Claude token invalid or Claude unavailable: {r.text[:300]}")
    report.note("Claude auth ok")
