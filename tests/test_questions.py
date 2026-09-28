from pathlib import Path

import pytest
import yaml

from answer_engine_benchmark.questions import QuestionError, count, load

TEMPLATE = Path(__file__).resolve().parents[1] / "questions" / "template.yaml"
FILLED = {"brand": "Acme Ltd", "domain": "acme.test", "category": "invoice software", "audience": "small firms"}


def test_template_refuses_example_values():
    with pytest.raises(QuestionError, match="example values"):
        load(TEMPLATE)


def test_template_fills():
    q = load(TEMPLATE, FILLED)
    assert q["ours"] == ["acme.test"]
    assert q["brand_terms"] == ["Acme Ltd", "acme.test"]
    assert count(q) == 24
    assert "What is Acme Ltd?" in q["groups"]["brand"]
    assert all("{" not in x for qs in q["groups"].values() for x in qs)


def test_template_is_neutral():
    raw = yaml.safe_load(TEMPLATE.read_text())
    for g in ("discovery", "comparison"):
        for x in raw["groups"][g]:
            assert "{brand}" not in x and "{domain}" not in x, x
    for g in ("brand", "trust"):
        for x in raw["groups"][g]:
            assert "{brand}" in x or "{domain}" in x, x


def test_unknown_placeholder(tmp_path):
    f = tmp_path / "q.yaml"
    f.write_text("ours: [a.test]\nbrand_terms: [A]\ngroups:\n  g:\n    - Is {thing} good?\n")
    with pytest.raises(QuestionError, match="thing"):
        load(f)


@pytest.mark.parametrize("body,msg", [
    ("brand_terms: [A]\ngroups: {g: [q]}\n", "ours"),
    ("ours: [a.test]\ngroups: {g: [q]}\n", "brand_terms"),
    ("ours: [a.test]\nbrand_terms: [A]\ngroups: {}\n", "no questions"),
    ("ours: [a.test]\nbrand_terms: [A]\ngroups: {g: [1, '']}\n", "list of question strings"),
])
def test_validation(tmp_path, body, msg):
    f = tmp_path / "q.yaml"
    f.write_text(body)
    with pytest.raises(QuestionError, match=msg):
        load(f)
