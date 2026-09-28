"""aeb: ask AI answer engines your buyers' questions and count who they cite.

Exit codes: 0 done, 1 a run finished but every call failed, 2 bad input.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .engines import ENGINES, available
from .questions import QuestionError, count, load
from .runner import read_jsonl, rescore, run
from .summary import compare, pick_questions, render, render_compare

DOCS = "https://synapsereality.io/open-source/answer-engine-benchmark/"


def _kv(raw: str) -> tuple[str, str]:
    k, sep, v = raw.partition("=")
    if not sep or not k.strip():
        raise argparse.ArgumentTypeError(f"expected name=value, got {raw!r}")
    return k.strip(), v.strip()


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("questions", help="question file (YAML)")
    p.add_argument("--set", action="append", type=_kv, default=[], metavar="NAME=VALUE",
                   help="fill a {placeholder}, e.g. --set brand='Acme Ltd' (repeatable)")


def _engine_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--engine", action="append", choices=sorted(ENGINES), help="only these engines (repeatable)")
    p.add_argument("--model", action="append", type=_kv, default=[], metavar="ENGINE=MODEL",
                   help="override a model, e.g. --model claude=claude-sonnet-5")
    p.add_argument("--runs", type=int, default=3, help="times to ask each question on each engine (default 3)")
    p.add_argument("--max-calls", type=int, default=500,
                   help="refuse to start a run that needs more API calls than this (default 500)")
    p.add_argument("--sleep", type=float, default=1.0, help="seconds between calls (default 1)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="aeb", description=__doc__, epilog=f"Docs: {DOCS}",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"aeb {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="validate a question file and show what a run would cost in calls")
    _common(p)
    _engine_flags(p)
    p.add_argument("--allow-unfilled", action="store_true", help="accept the template's example values")

    p = sub.add_parser("run", help="ask every question on every engine and write answers.jsonl + report.md")
    _common(p)
    _engine_flags(p)
    p.add_argument("--out", required=True, type=Path, help="output folder")
    p.add_argument("--group", action="append", help="only these question groups (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="first question of each group, 1 run: a cheap smoke test")
    p.add_argument("--anonymise", action="store_true", help="label other domains Source A, B, ... in report.md")

    p = sub.add_parser("report", help="rebuild report.md from answers.jsonl (spends nothing)")
    p.add_argument("answers", type=Path, help="answers.jsonl")
    p.add_argument("questions", nargs="?", help="question file: re-score with its ours/brand_terms first")
    p.add_argument("--set", action="append", type=_kv, default=[], metavar="NAME=VALUE")
    p.add_argument("--anonymise", action="store_true")
    p.add_argument("--out", type=Path, help="write here instead of stdout")

    p = sub.add_parser("noise", help="re-ask a sample of questions and compare with the baseline")
    _common(p)
    _engine_flags(p)
    p.add_argument("--baseline", required=True, type=Path, help="answers.jsonl from the first run")
    p.add_argument("--out", required=True, type=Path, help="output folder for the repeat run")
    p.add_argument("--sample", type=int, default=5, help="questions to re-ask, spread across groups (default 5)")
    p.add_argument("--tolerance", type=int, default=1, help="allowed difference in citations (default 1)")
    return ap


def _plan(q: dict, engines: dict, runs: int, max_calls: int) -> int | None:
    calls = count(q) * len(engines) * runs
    names = ", ".join(f"{n} ({e.model})" for n, e in engines.items()) or "none"
    print(f"{count(q)} questions x {len(engines)} engines x {runs} runs = {calls} calls. Engines: {names}")
    if calls > max_calls:
        print(f"aeb: {calls} calls is over --max-calls {max_calls}. Raise it if you mean it.", file=sys.stderr)
        return None
    return calls


def _progress(rec: dict) -> None:
    flag = "ERR " if rec["error"] else "CITE" if rec["cited"] else "ment" if rec["mentioned"] else "  - "
    cost = rec["cost_usd"] or 0
    print(f"{flag} {rec['engine']:<10} {rec['group']:<11} pos={rec['position'] or '-':<3} ${cost:.4f} "
          f"{rec['question'][:70]} {rec['error'][:80]}", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "report":
            recs = read_jsonl(args.answers)
            ours = []
            if args.questions:
                q = load(args.questions, dict(args.set), allow_unfilled=True)
                recs, ours = rescore(recs, q), q["ours"]
            text = render(recs, ours, anonymise=args.anonymise)
            if args.out:
                args.out.write_text(text, encoding="utf-8")
            else:
                print(text, end="")
            return 0

        q = load(args.questions, dict(args.set), allow_unfilled=getattr(args, "allow_unfilled", False))
        engines = available(only=args.engine, models=dict(args.model))
        if args.cmd == "check":
            print(f"ok: {count(q)} questions in {len(q['groups'])} groups; ours = {q['ours']}")
            missing = [f"{n} ({e.env_key})" for n, e in ENGINES.items()
                       if n not in engines and (not args.engine or n in args.engine)]
            if missing:
                print("no key set for: " + ", ".join(missing))
            _plan(q, engines, args.runs, args.max_calls)
            return 0

        if not engines:
            keys = ", ".join(e.env_key for e in ENGINES.values())
            print(f"aeb: no engine has a key. Set one or more of: {keys}", file=sys.stderr)
            return 2

        if args.cmd == "run":
            if args.group:
                q["groups"] = {g: v for g, v in q["groups"].items() if g in args.group}
            if args.dry_run:
                q["groups"] = {g: v[:1] for g, v in q["groups"].items()}
                args.runs = 1
            if _plan(q, engines, args.runs, args.max_calls) is None:
                return 2
            answers = args.out / "answers.jsonl"
            recs = run(q, engines, runs=args.runs, out=answers, on_result=_progress, sleep=args.sleep)
            everything = read_jsonl(answers)
            (args.out / "report.md").write_text(render(everything, q["ours"], anonymise=args.anonymise),
                                                encoding="utf-8")
            cost = sum(r.get("cost_usd") or 0 for r in recs)
            print(f"wrote {answers} (+{len(recs)} answers) and report.md. This run cost about ${cost:.3f}")
            return 1 if recs and all(r["error"] for r in recs) else 0

        if args.cmd == "noise":
            baseline = read_jsonl(args.baseline)
            picked = pick_questions(q, args.sample)
            groups: dict[str, list[str]] = {}
            for g, question in picked:
                groups.setdefault(g, []).append(question)
            q["groups"] = groups
            if _plan(q, engines, args.runs, args.max_calls) is None:
                return 2
            answers = args.out / "repeat.jsonl"
            run(q, engines, runs=args.runs, out=answers, on_result=_progress, sleep=args.sleep, label="repeat")
            rows = compare(baseline, read_jsonl(answers), tolerance=args.tolerance)
            text = render_compare(rows, args.tolerance)
            (args.out / "noise.md").write_text(text, encoding="utf-8")
            print(text, end="")
            return 0
    except QuestionError as e:
        print(f"aeb: {e}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"aeb: {e}", file=sys.stderr)
        return 2
    return 2
