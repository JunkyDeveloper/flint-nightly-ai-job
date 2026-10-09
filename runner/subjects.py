"""Subjects: which ids are tested together, in which order, and what is already finished.

Ids are handled without the `minecraft:` prefix internally (`oak_button`).
"""
import json
import re
from collections import Counter
from pathlib import Path

PREFIX_FAMILIES = ("potted_", "music_disc_", "infested_", "waxed_", "stripped_")
GENERIC_WORDS = {"block", "stem", "bush", "lantern", "torch", "plant", "roots",
                 "sand", "egg", "rod", "bucket", "grass"}
FINAL_STATES = {"unsupported", "covered"}
MAX_ATTEMPTS = 2
NAME_RE = re.compile(r"^[a-z0-9_.-]+$")


def short(id_: str) -> str:
    return id_.strip().removeprefix("minecraft:")


def read_ids(path: Path) -> list[str]:
    """Ids in file order: no comments, no duplicates, no spawn eggs (entities are out of scope)."""
    seen: dict[str, None] = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.endswith("_spawn_egg"):
            seen.setdefault(short(line), None)
    return list(seen)


def load_tags(tags_dir: Path) -> dict[str, set[str]]:
    """All tags under tags_dir/{block,item}, `#refs` resolved, same-named block+item tags merged."""
    if not (tags_dir / "block").is_dir():
        raise FileNotFoundError(f"no vanilla tags at {tags_dir}/block (is /mc-src mounted?)")
    raw: dict[str, dict[str, list]] = {}
    for kind in ("block", "item"):
        root = tags_dir / kind
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.json")):
            raw.setdefault(kind, {})[f.relative_to(root).with_suffix("").as_posix()] = \
                json.loads(f.read_text()).get("values", [])

    def resolve(kind: str, tag: str, seen: frozenset) -> set[str]:
        out: set[str] = set()
        for v in raw.get(kind, {}).get(tag, []):
            v = v.get("id", "") if isinstance(v, dict) else v
            if v.startswith("#"):
                ref = v[1:].split(":")[-1]
                if ref not in seen:
                    out |= resolve(kind, ref, seen | {tag})
            elif v:
                out.add(short(v))
        return out

    merged: dict[str, set[str]] = {}
    for kind, tags in raw.items():
        for tag in tags:
            merged.setdefault(tag, set()).update(resolve(kind, tag, frozenset()))
    return merged


def _singular(word: str) -> str:
    for plural, single in (("leaves", "leaf"), ("ies", "y"), ("shes", "sh"),
                           ("ches", "ch"), ("xes", "x"), ("s", "")):
        if word.endswith(plural):
            return word[: -len(plural)] + single
    return word


def family_tags(tags: dict[str, set[str]], ids: set[str]) -> dict[str, set[str]]:
    """Tags that describe a family, not a behavior: no '/', >=2 ids, >=80% of ids contain the tag word."""
    out = {}
    for tag, members in tags.items():
        members = members & ids
        if "/" in tag or len(members) < 2:
            continue
        word = _singular(tag.split("_")[-1])
        if sum(word in m for m in members) / len(members) >= 0.8:
            out[tag] = members
    return out


def candidate_groups(ids: list[str], tags: dict[str, set[str]]) -> dict[str, list[str]]:
    """Step 1: largest family tag. Step 2 for the rest: shared last word (>=3), prefix, else single."""
    fams = family_tags(tags, set(ids))
    groups: dict[str, list[str]] = {}
    rest = []
    for id_ in ids:
        hits = [t for t, m in fams.items() if id_ in m]
        if hits:
            groups.setdefault(max(hits, key=lambda t: (len(fams[t]), t)), []).append(id_)
        else:
            rest.append(id_)

    last = Counter(i.split("_")[-1] for i in rest if "_" in i)
    for id_ in rest:
        word = id_.split("_")[-1]
        if "_" in id_ and word not in GENERIC_WORDS and last[word] >= 3:
            name = word
        else:
            name = next((p.rstrip("_") for p in PREFIX_FAMILIES if id_.startswith(p)), id_)
        groups.setdefault(name, []).append(id_)
    return groups


def validate_groups(proposed: object, ids: list[str]) -> bool:
    """A reviewed grouping is usable only if it keeps every id exactly once and has safe names."""
    if not isinstance(proposed, dict) or not proposed:
        return False
    flat = [m for v in proposed.values() if isinstance(v, list) for m in v]
    return (all(isinstance(k, str) and NAME_RE.match(k) for k in proposed)
            and all(isinstance(v, list) and v for v in proposed.values())
            and sorted(map(short, flat)) == sorted(ids))


def add_new_ids(groups: dict[str, list[str]], ids: list[str], tags: dict[str, set[str]]) -> list[str]:
    """Frozen groups stay as they are; ids not in any group are grouped and appended. Returns new ids."""
    known = {m for ms in groups.values() for m in ms}
    new = [i for i in ids if i not in known]
    for name, members in candidate_groups(new, tags).items():
        groups.setdefault(name, []).extend(members)
    return new


def read_priority(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.split("#", 1)[0].strip() for line in path.read_text().splitlines()
            if line.split("#", 1)[0].strip()]


def order_subjects(groups: dict[str, list[str]], ids: list[str],
                   priority: list[str]) -> tuple[list[str], list[str]]:
    """priority.txt entries first (id or subject name), then order of first member in implemented.txt."""
    first = {i: n for n, i in enumerate(ids)}
    of_id = {m: name for name, ms in groups.items() for m in ms}
    ordered: list[str] = []
    warnings = []
    for entry in priority:
        name = entry if entry in groups else of_id.get(short(entry))
        if name is None:
            warnings.append(f"priority.txt: unknown entry `{entry}` ignored")
        elif name not in ordered:
            ordered.append(name)
    rest = sorted((g for g in groups if g not in ordered),
                  key=lambda g: min(first.get(m, len(first)) for m in groups[g]))
    return ordered + rest, warnings


def selectable(name: str, state: dict) -> bool:
    entry = state.get(name, {})
    return entry.get("status") not in FINAL_STATES and entry.get("attempts", 0) < MAX_ATTEMPTS


def record(state: dict, name: str, status: str, note: str = "") -> None:
    entry = state.setdefault(name, {"attempts": 0})
    entry["status"] = status
    entry["note"] = note
    if status == "gave_up":
        entry["attempts"] = entry.get("attempts", 0) + 1


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def preview(groups: dict[str, list[str]], order: list[str]) -> str:
    lines = [f"# Subjects ({len(groups)})", "", "| # | Subject | Members |", "|---|---|---|"]
    for n, name in enumerate(order, 1):
        lines.append(f"| {n} | `{name}` | {len(groups[name])}: {', '.join(groups[name])} |")
    return "\n".join(lines) + "\n"
