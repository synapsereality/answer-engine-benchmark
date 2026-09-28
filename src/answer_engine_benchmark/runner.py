"""Ask every question on every engine, N times, and append each answer to a JSONL file."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from .engines import Bound, redact
from .scoring import Resolver, score


def ask_once(engine: Bound, question: str) -> tuple[dict, str]:
    try:
        return engine.ask(question), ""
    except Exception as e:  # noqa: BLE001 - one failed call is a row, not a crash
        msg = redact(f"{type(e).__name__}: {e}", [engine.key])[:300]
        return {"text": "", "urls": [], "model": engine.model, "usage": {}, "cost_usd": 0.0}, msg


def run(q: dict, engines: dict[str, Bound], *, runs: int = 3, out: Path,
        on_result: Callable[[dict], None] | None = None, sleep: float = 1.0,
        resolver: Resolver | None = None, label: str = "baseline") -> list[dict]:
    """Every question x engine x run. Each record is written and flushed as it lands, so
    an interrupted run keeps what it paid for."""
    resolver = resolver or Resolver(out.parent / "redirect_cache.json")
    records = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        for group, qs in q["groups"].items():
            for question in qs:
                for name, eng in engines.items():
                    for i in range(runs):
                        t0 = time.time()
                        ans, err = ask_once(eng, question)
                        resolver.warm(ans.get("urls", []))
                        ans["urls"] = [resolver(u) for u in ans.get("urls", [])]
                        rec = {
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "label": label,
                            "group": group,
                            "question": question,
                            "engine": name,
                            "model": ans.get("model") or eng.model,
                            "run": i + 1,
                            "seconds": round(time.time() - t0, 1),
                            "error": err,
                            "answer": ans.get("text", ""),
                            "urls": ans.get("urls", []),
                            "usage": ans.get("usage", {}),
                            "cost_usd": ans.get("cost_usd"),
                            **score(ans, q["ours"], q["brand_terms"], stale_markers=q["stale_markers"]),
                        }
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        f.flush()
                        records.append(rec)
                        if on_result:
                            on_result(rec)
                        if sleep:
                            time.sleep(sleep)
    resolver.save()
    return records


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rescore(records: list[dict], q: dict) -> list[dict]:
    """Score stored answers again, for example after changing `ours` or `stale_markers`.
    Spends nothing."""
    for r in records:
        r.update(score({"text": r.get("answer", ""), "urls": r.get("urls", [])},
                       q["ours"], q["brand_terms"], stale_markers=q["stale_markers"]))
    return records
