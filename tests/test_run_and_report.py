import json

from answer_engine_benchmark.cli import main
from answer_engine_benchmark.engines import Bound
from answer_engine_benchmark.runner import read_jsonl, rescore, run
from answer_engine_benchmark.summary import compare, pick_questions, render, render_compare, summarise

Q = {"ours": ["acme.test"], "brand_terms": ["Acme"], "stale_markers": ["since 1999"],
     "groups": {"discovery": ["Best widgets?", "Cheap widgets?"], "brand": ["What is Acme?"]}}


def fake_engine(name, answers):
    it = iter(answers)

    def fn(question, *, key, model):
        a = next(it)
        if isinstance(a, Exception):
            raise a
        return {"text": a[0], "urls": a[1], "model": model, "usage": {}, "cost_usd": 0.001}

    return Bound(name, "m-1", "sk-supersecret-123", fn)


def test_run_writes_jsonl_scores_and_redacts(tmp_path):
    eng = fake_engine("openai", [
        ("Try other.test", ["https://other.test/"]),
        ("Acme, since 1999", ["https://acme.test/"]),
        RuntimeError("boom sk-supersecret-123"),
        ("Acme is here", ["https://acme.test/about"]),
        ("x", []),
        ("y", []),
    ])
    out = tmp_path / "answers.jsonl"
    recs = run(Q, {"openai": eng}, runs=2, out=out, sleep=0)
    assert len(recs) == 6
    stored = read_jsonl(out)
    assert [r["cited"] for r in stored] == [False, True, False, True, False, False]
    assert stored[1]["stale"] == ["since 1999"]
    assert stored[2]["error"] and "supersecret" not in stored[2]["error"]
    assert "sk-supersecret" not in out.read_text()
    s = summarise(stored)
    assert s["all"]["errors"] == 1 and s["all"]["answered"] == 5
    assert s["all"]["citation_rate"] == 0.4
    assert s["group:discovery"]["cited"] == 2 and s["group:brand"]["cited"] == 0


def test_render_anonymises_other_domains():
    recs = [{"engine": "openai", "group": "discovery", "question": "q", "error": "", "cited": True,
             "mentioned": True, "position": 2, "domains_cited": ["rival.test", "acme.test"], "stale": [],
             "cost_usd": 0.0}]
    md = render(recs, ["acme.test"], anonymise=True)
    assert "rival.test" not in md and "Source A" in md and "**acme.test** (yours)" in md
    assert "rival.test" in render(recs, ["acme.test"])


def test_rescore_uses_new_ours():
    recs = [{"answer": "see https://new.test/", "urls": []}]
    assert rescore(recs, {**Q, "ours": ["new.test"]})[0]["cited"]


def test_pick_questions_round_robin():
    assert pick_questions(Q, 2) == [("discovery", "Best widgets?"), ("brand", "What is Acme?")]
    assert len(pick_questions(Q, 10)) == 3


def test_compare_within_tolerance():
    def rec(q, e, cited, err=""):
        return {"question": q, "engine": e, "cited": cited, "error": err}
    base = [rec("a", "x", True)] * 3 + [rec("b", "x", False)] * 3
    rep = [rec("a", "x", True)] * 2 + [rec("a", "x", False)] + [rec("b", "x", True)] * 2 + [rec("b", "x", False, "429")]
    rows = compare(base, rep)
    by = {r["question"]: r for r in rows}
    assert by["a"]["within"] and by["a"]["delta"] == -1
    assert not by["b"]["within"] and by["b"]["repeat"] == "2 of 2"
    assert "1 of 2" in render_compare(rows, 1)


def test_cli_check_and_no_keys(tmp_path, monkeypatch, capsys):
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    tpl = "questions/template.yaml"
    assert main(["check", tpl]) == 2
    assert main(["check", tpl, "--allow-unfilled"]) == 0
    assert "24 questions" in capsys.readouterr().out
    sets = ["--set", "brand=Acme", "--set", "domain=acme.test", "--set", "category=widgets", "--set", "audience=shops"]
    assert main(["run", tpl, "--out", str(tmp_path), *sets]) == 2
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert main(["run", tpl, "--out", str(tmp_path), *sets, "--max-calls", "10"]) == 2
    assert "over --max-calls" in capsys.readouterr().err


def test_cli_run_end_to_end_with_a_stubbed_engine(tmp_path, monkeypatch):
    import answer_engine_benchmark.engines as E

    def fake(question, *, key, model):
        return {"text": "Acme at https://acme.test/", "urls": [], "model": model, "usage": {}, "cost_usd": 0.0}

    monkeypatch.setitem(E.ENGINES, "openai", E.Engine("openai", "OPENAI_API_KEY", fake, "m"))
    for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sets = ["--set", "brand=Acme", "--set", "domain=acme.test", "--set", "category=widgets", "--set", "audience=shops"]
    assert main(["run", "questions/template.yaml", "--out", str(tmp_path), *sets, "--dry-run", "--sleep", "0"]) == 0
    rows = [json.loads(x) for x in (tmp_path / "answers.jsonl").read_text().splitlines()]
    assert len(rows) == 4 and all(r["cited"] for r in rows)
    assert "4 of 4" in (tmp_path / "report.md").read_text()
    assert main(["noise", "questions/template.yaml", *sets, "--baseline", str(tmp_path / "answers.jsonl"),
                 "--out", str(tmp_path / "rep"), "--sample", "2", "--runs", "1", "--sleep", "0"]) == 0
    assert "2 of 2 question-engine pairs" in (tmp_path / "rep" / "noise.md").read_text()
    assert main(["report", str(tmp_path / "answers.jsonl"), "--anonymise", "--out", str(tmp_path / "r.md")]) == 0
