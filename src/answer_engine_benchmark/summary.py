"""Aggregates, share of voice, the Markdown report, and the noise check."""

from __future__ import annotations

from collections import defaultdict

from .scoring import is_ours


def _bucket() -> dict:
    return {"runs": 0, "errors": 0, "mentioned": 0, "cited": 0, "stale": 0, "positions": [], "cost": 0.0}


def summarise(records: list[dict]) -> dict:
    """Rates are over answered runs: a failed call is counted as an error, never as a miss."""
    slices: dict[str, dict] = defaultdict(_bucket)
    for r in records:
        for key in ("all", f"engine:{r['engine']}", f"group:{r['group']}", f"{r['group']} x {r['engine']}"):
            b = slices[key]
            b["runs"] += 1
            b["cost"] += r.get("cost_usd") or 0
            if r.get("error"):
                b["errors"] += 1
                continue
            b["mentioned"] += bool(r.get("mentioned"))
            b["cited"] += bool(r.get("cited"))
            b["stale"] += bool(r.get("stale"))
            if r.get("position"):
                b["positions"].append(r["position"])
    out = {}
    for key, b in slices.items():
        n = b["runs"] - b["errors"]
        pos = b.pop("positions")
        out[key] = {
            **b,
            "answered": n,
            "mention_rate": round(b["mentioned"] / n, 4) if n else None,
            "citation_rate": round(b["cited"] / n, 4) if n else None,
            "avg_position": round(sum(pos) / len(pos), 2) if pos else None,
            "cost": round(b["cost"], 4),
        }
    return out


def share_of_voice(records: list[dict], *, groups: list[str] | None = None) -> list[tuple[str, int]]:
    """Domains by the number of answers citing them at least once."""
    counts: dict[str, int] = defaultdict(int)
    for r in records:
        if r.get("error") or (groups and r["group"] not in groups):
            continue
        for d in set(r.get("domains_cited") or []):
            counts[d] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _label(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def render(records: list[dict], ours: list[str], *, anonymise: bool = False, top: int = 15) -> str:
    """A Markdown report. With `anonymise`, every domain that is not yours becomes
    "Source A", "Source B" and so on, which is what you want before sharing it."""
    s = summarise(records)
    questions = {r["question"] for r in records}
    lines = [
        f"# Answer engine benchmark: {len(records)} answers, {len(questions)} questions",
        "",
        "Cited = the answer links to one of your domains. Rates leave out failed calls.",
        "",
        "| slice | answers | errors | mentioned | cited | avg position when cited | cost USD |",
        "|---|---|---|---|---|---|---|",
    ]
    order = ["all"] + sorted(k for k in s if k.startswith("engine:")) + sorted(k for k in s if k.startswith("group:"))
    for k in order:
        b = s[k]
        lines.append(f"| {k} | {b['answered']} | {b['errors']} | {_pct(b['mention_rate'])} | "
                     f"{b['cited']} of {b['answered']} ({_pct(b['citation_rate'])}) | "
                     f"{b['avg_position'] or '-'} | {b['cost']:.4f} |")

    engines = sorted({r["engine"] for r in records})
    groups = list(dict.fromkeys(r["group"] for r in records))
    lines += ["", "## Cited, by group and engine", "", "| group | " + " | ".join(engines) + " |",
              "|---|" + "---|" * len(engines)]
    for g in groups:
        cells = []
        for e in engines:
            b = s.get(f"{g} x {e}")
            cells.append(f"{b['cited']} of {b['answered']}" if b else "-")
        lines.append(f"| {g} | " + " | ".join(cells) + " |")

    sov = share_of_voice(records)
    names: dict[str, str] = {}
    lines += ["", "## Sources cited most", "", "| source | answers citing it |", "|---|---|"]
    for i, (d, n) in enumerate(sov[:top]):
        mine = is_ours("https://" + d, ours)
        if mine:
            label = f"**{d}** (yours)"
        elif anonymise:
            label = names.setdefault(d, f"Source {_label(len(names))}")
        else:
            label = d
        lines.append(f"| {label} | {n} |")

    never = sorted(questions - {r["question"] for r in records if r.get("cited")})
    lines += ["", "## Questions where you were never cited", ""] + [f"- {x}" for x in never or ["none"]]
    stale = [(r["engine"], r["question"], r["stale"]) for r in records if r.get("stale")]
    lines += ["", "## Answers with a stale or wrong marker near your name", ""]
    lines += [f"- {e}: {q} ({', '.join(m)})" for e, q, m in stale[:30]] or ["- none"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------ noise check ----
def pick_questions(q: dict, n: int) -> list[tuple[str, str]]:
    """n questions spread across groups, round robin, in file order. Deterministic, so a
    re-run of the noise check asks the same ones."""
    pools = {g: list(qs) for g, qs in q["groups"].items()}
    picked: list[tuple[str, str]] = []
    while len(picked) < n and any(pools.values()):
        for g in list(pools):
            if pools[g] and len(picked) < n:
                picked.append((g, pools[g].pop(0)))
    return picked


def compare(baseline: list[dict], repeat: list[dict], *, tolerance: int = 1) -> list[dict]:
    """Per question and engine: citations in the repeat against the baseline.

    When the two have a different number of answered runs, the baseline count is scaled
    to the repeat's run count. A pair is `within` when the difference is at most
    `tolerance` citations.
    """
    def tally(recs: list[dict]) -> dict[tuple[str, str], tuple[int, int]]:
        t: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        for r in recs:
            if r.get("error"):
                continue
            k = (r["question"], r["engine"])
            t[k][0] += bool(r.get("cited"))
            t[k][1] += 1
        return {k: (v[0], v[1]) for k, v in t.items()}

    base, rep = tally(baseline), tally(repeat)
    rows = []
    for k in sorted(rep):
        if k not in base:
            continue
        (bc, bn), (rc, rn) = base[k], rep[k]
        expected = bc * rn / bn if bn else 0
        delta = rc - expected
        rows.append({"question": k[0], "engine": k[1], "baseline": f"{bc} of {bn}", "repeat": f"{rc} of {rn}",
                     "delta": round(delta, 2), "within": abs(delta) <= tolerance})
    return rows


def render_compare(rows: list[dict], tolerance: int) -> str:
    ok = sum(r["within"] for r in rows)
    lines = [f"# Noise check: {ok} of {len(rows)} question-engine pairs within +/-{tolerance} citation", "",
             "| question | engine | baseline | repeat | delta | within |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['question']} | {r['engine']} | {r['baseline']} | {r['repeat']} | {r['delta']:+g} | "
              f"{'yes' if r['within'] else 'NO'} |" for r in rows]
    return "\n".join(lines) + "\n"
