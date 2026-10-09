"""flint-ai-cronjob runner: nightly Flint test generation. See SPEC.md and tasks/plan.md."""
import os
import subprocess
from pathlib import Path

# Never inherited by child processes. Only gitops passes the GitHub token explicitly, per command.
SCRUBBED = ("GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

CONFIG = Path(os.environ.get("CONFIG_DIR", "/config"))
STATE = Path(os.environ.get("STATE_DIR", "/state"))
WORK = Path(os.environ.get("WORK_DIR", "/work"))
REPO_DIR = WORK / "repo"        # runner-owned FlintBenchmark clone (has .git)
SANDBOX = WORK / "sandbox"      # worker-owned copy without .git; claude works here
MC_SRC = Path(os.environ.get("MC_SRC_DIR", "/mc-src"))
TAGS = MC_SRC / "data" / "minecraft" / "tags"   # vanilla tags straight from the 26.2 source
FLINT_SPEC = Path("/opt/flint-core/src/test_spec.rs")
MC_SERVER = os.environ.get("MC_SERVER", "localhost:25565")


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def env_str(name: str, default: str) -> str:
    return os.environ.get(name) or default


def run(cmd: list, *, timeout: float, env_extra: dict[str, str] | None = None,
        cwd: Path | str | None = None, input: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run an external command with a scrubbed env. No shell, always a timeout."""
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
    env.update(env_extra or {})
    return subprocess.run([str(c) for c in cmd], env=env, cwd=cwd, timeout=timeout,
                          text=True, capture_output=True, input=input)
