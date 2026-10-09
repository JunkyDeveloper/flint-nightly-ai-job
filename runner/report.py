"""Nightly report: /state/reports/<date>.md, written even when the run fails."""
import os
from datetime import datetime
from pathlib import Path


class Report:
    def __init__(self, mode: str):
        self.mode = mode
        self.started = datetime.now()
        self.sections: list[str] = []
        self.subjects: list[str] = []
        self.warnings: list[str] = []
        self.usage_usd = 0.0
        self.result = "unknown"

    def note(self, text: str) -> None:
        print(text, flush=True)
        self.sections.append(text)

    def warn(self, text: str) -> None:
        print(f"WARNING: {text}", flush=True)
        self.warnings.append(text)

    def subject(self, name: str, outcome: str, detail: str = "") -> None:
        line = f"| `{name}` | {outcome} | {detail.replace('|', '/').replace(chr(10), ' ')} |"
        print(line, flush=True)
        self.subjects.append(line)

    def render(self) -> str:
        versions = ", ".join(f"{k}={os.environ.get(k, '?')}" for k in
                             ("MC_VERSION", "FLINTCLI_REF", "FLINT_CORE_REV", "CLAUDE_CODE_VERSION"))
        out = [f"# Run {self.started:%Y-%m-%d %H:%M} ({self.mode})", "",
               f"- Result: **{self.result}**",
               f"- Finished: {datetime.now():%H:%M}",
               f"- Versions: {versions}",
               f"- Claude usage (API-equivalent): ${self.usage_usd:.2f}", ""]
        if self.subjects:
            out += ["## Subjects", "", "| Subject | Outcome | Detail |", "|---|---|---|", *self.subjects, ""]
        if self.warnings:
            out += ["## Warnings", "", *[f"- {w}" for w in self.warnings], ""]
        if self.sections:
            out += ["## Log", "", *[f"- {s}" for s in self.sections], ""]
        return "\n".join(out)

    def write(self, state_dir: Path) -> Path:
        path = state_dir / "reports" / f"{self.started:%Y-%m-%d_%H%M}-{self.mode}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render())
        return path
