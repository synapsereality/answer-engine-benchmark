"""Question files: YAML with placeholders you fill with your own brand."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
# Values the shipped template uses so it parses. A run refuses them.
UNFILLED = {"your brand", "example.com", "your category", "your audience", "your city"}


class QuestionError(ValueError):
    pass


def _fill(value, vars_: dict[str, str]):
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            k = m.group(1)
            if k not in vars_:
                raise QuestionError(f"placeholder {{{k}}} has no value; add it under vars: or pass --set {k}=...")
            return str(vars_[k])
        return PLACEHOLDER.sub(sub, value)
    if isinstance(value, list):
        return [_fill(v, vars_) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, vars_) for k, v in value.items()}
    return value


def load(path: str | Path, overrides: dict[str, str] | None = None, *, allow_unfilled: bool = False) -> dict:
    """Read a question file and fill its placeholders.

    Values come from the file's `vars:` block, then from `overrides` (the --set flags).
    Returns {"ours", "brand_terms", "stale_markers", "groups": {name: [question]}}.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    vars_ = {str(k): str(v) for k, v in (raw.get("vars") or {}).items()}
    vars_.update(overrides or {})
    if not allow_unfilled:
        left = sorted(k for k, v in vars_.items() if v.strip().lower() in UNFILLED)
        if left:
            raise QuestionError(
                "these vars still hold the template's example values: " + ", ".join(left)
                + ". Set them in the file or with --set name=value.")
    q = _fill({k: raw.get(k) for k in ("ours", "brand_terms", "stale_markers", "groups")}, vars_)
    if not q.get("ours"):
        raise QuestionError("`ours` is empty: list the domains (or domain/path) that count as your citation")
    if not q.get("brand_terms"):
        raise QuestionError("`brand_terms` is empty: list the names that count as a mention")
    groups = q.get("groups") or {}
    if not isinstance(groups, dict) or not any(groups.values()):
        raise QuestionError("`groups` has no questions")
    for g, qs in groups.items():
        if not isinstance(qs, list) or not all(isinstance(x, str) and x.strip() for x in qs):
            raise QuestionError(f"group {g!r} must be a list of question strings")
    q["stale_markers"] = q.get("stale_markers") or []
    q["groups"] = {g: list(qs) for g, qs in groups.items()}
    return q


def count(q: dict) -> int:
    return sum(len(v) for v in q["groups"].values())
