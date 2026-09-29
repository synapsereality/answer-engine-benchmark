"""claude-cli engine tests. A fake `claude` script stands in for the real CLI, so nothing
here needs a login or makes a network call."""

import json
import os
import stat
import sys
import textwrap
import time

import pytest

from answer_engine_benchmark import engines as E
from answer_engine_benchmark.cli import main
from answer_engine_benchmark.runner import run

SEARCH_RESULT = ('Web search results for query: "acme"\n\nLinks: [{"title": "A [1]", "url": "https://s.test/1"},'
                 '{"title": "B", "url": "https://acme.test/about"}]\n\nMore text')

STREAM = [
    {"type": "system", "subtype": "init", "tools": ["WebFetch", "WebSearch"]},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "WebSearch", "input": {"query": "acme"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": SEARCH_RESULT}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "WebFetch", "input": {"url": "https://down.test/"}},
        {"type": "tool_use", "id": "t3", "name": "WebFetch", "input": {"url": "https://f.test/page"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": "ENOTFOUND", "is_error": True},
        {"type": "tool_result", "tool_use_id": "t3", "content": [{"type": "text", "text": "page"}]}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "duration_ms": 1200,
     "result": "Acme makes widgets ([About](https://acme.test/about)). See https://t.test/x.",
     "total_cost_usd": 0.0421,
     "modelUsage": {"claude-haiku-5": {"outputTokens": 40}, "claude-opus-5": {"outputTokens": 900}}},
]


def make_fake(tmp_path, *, stream=None, exit_code=0, stderr="", sleep=0.0):
    """Put a fake `claude` on a fresh PATH. It records what it was given, then replies."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    record = tmp_path / "record.json"
    out = "\n".join(json.dumps(e) for e in (STREAM if stream is None else stream))
    script = bindir / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
        cred = os.path.join(cfg, ".credentials.json")
        json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
                   "cwd_files": os.listdir("."), "env": dict(os.environ),
                   "cred": open(cred).read() if os.path.exists(cred) else None}},
                  open({str(record)!r}, "w"))
        time.sleep({sleep})
        sys.stdout.write({out!r})
        sys.stderr.write({stderr!r})
        sys.exit({exit_code})
        """))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bindir, record


@pytest.fixture
def user_home(tmp_path):
    """A pretend operator: logged in, with a git identity and a CLAUDE.md."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {"expiresAt": 100}}')
    (home / ".claude" / "CLAUDE.md").write_text("The user owns Acme.")
    (home / ".gitconfig").write_text("[user]\n  name = operator-handle\n")
    return home


def env_for(bindir, home, **extra):
    return {"PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home), "USER": "operator-handle",
            "GIT_AUTHOR_NAME": "operator-handle", "GIT_DIR": "/somewhere/.git", "ANTHROPIC_API_KEY": "sk-ant-x",
            "CLAUDECODE": "1", "LANG": "C.UTF-8", **extra}


def test_answer_text_sources_and_zero_cost(tmp_path, user_home):
    bindir, _ = make_fake(tmp_path)
    out = E.claude_cli_answer("Who makes widgets?", env=env_for(bindir, user_home))
    assert out["text"].startswith("Acme makes widgets")
    # Search results, then pages fetched without error, then links in the text; no repeats.
    assert out["urls"] == ["https://s.test/1", "https://acme.test/about", "https://f.test/page", "https://t.test/x."]
    assert out["model"] == "claude-opus-5"
    assert out["cost_usd"] == 0.0 and out["subscription"] is True
    assert out["usage"]["web_search_requests"] == 1 and out["usage"]["web_fetch_requests"] == 2
    assert out["usage"]["api_equivalent_usd"] == 0.0421


def test_runs_as_a_stranger(tmp_path, user_home):
    bindir, record = make_fake(tmp_path)
    E.claude_cli_answer("-rf what is acme?", env=env_for(bindir, user_home))
    rec = json.loads(record.read_text())
    env = rec["env"]
    # Fresh empty working dir and HOME, outside any repo, and gone afterwards.
    assert rec["cwd_files"] == [] and not os.path.exists(rec["cwd"])
    assert env["HOME"] != str(user_home) and not os.path.exists(env["HOME"])
    assert env["CLAUDE_CONFIG_DIR"].startswith(env["HOME"])
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CEILING_DIRECTORIES"] == os.path.dirname(rec["cwd"])
    assert not [k for k in env if k.startswith("GIT_") and k not in
                ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_CEILING_DIRECTORIES")]
    assert "operator-handle" not in json.dumps(env)
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDECODE" not in env
    # Only the login was copied in; the operator's CLAUDE.md was not.
    assert json.loads(rec["cred"]) == {"claudeAiOauth": {"expiresAt": 100}}
    # The question goes in on stdin, so a leading "-" cannot become a flag.
    assert rec["stdin"] == "-rf what is acme?" and "-rf what is acme?" not in rec["argv"]
    argv = rec["argv"]
    assert argv[:3] == ["-p", "--output-format", "stream-json"]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert "--strict-mcp-config" in argv and "--model" not in argv


def test_model_override_is_passed(tmp_path, user_home):
    bindir, record = make_fake(tmp_path)
    E.claude_cli_answer("q", model="sonnet", env=env_for(bindir, user_home))
    argv = json.loads(record.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_token_variable_replaces_the_credentials_file(tmp_path):
    bindir, record = make_fake(tmp_path)
    E.claude_cli_answer("q", env=env_for(bindir, tmp_path / "nobody", CLAUDE_CODE_OAUTH_TOKEN="tok"))
    rec = json.loads(record.read_text())
    assert rec["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok" and rec["cred"] is None


def test_no_login_is_a_clear_error(tmp_path):
    bindir, _ = make_fake(tmp_path)
    with pytest.raises(E.EngineUnavailable, match="setup-token"):
        E.claude_cli_answer("q", env=env_for(bindir, tmp_path / "nobody"))


def test_refreshed_login_is_written_back(tmp_path, user_home):
    bindir, _ = make_fake(tmp_path)
    script = bindir / "claude"
    # The fake refreshes its login the way the real CLI does when the token is near expiry.
    script.write_text(script.read_text().replace(
        "time.sleep(", 'open(cred, "w").write(\'{"claudeAiOauth": {"expiresAt": 200}}\'); time.sleep('))
    E.claude_cli_answer("q", env=env_for(bindir, user_home))
    assert json.loads((user_home / ".claude" / ".credentials.json").read_text())["claudeAiOauth"]["expiresAt"] == 200


def test_timeout(tmp_path, user_home):
    bindir, _ = make_fake(tmp_path, sleep=30)
    t0 = time.time()
    with pytest.raises(TimeoutError, match="after 1s"):
        E.claude_cli_answer("q", timeout=1, env=env_for(bindir, user_home))
    assert time.time() - t0 < 10


def test_cli_error_result_and_bad_exit(tmp_path, user_home):
    bindir, _ = make_fake(tmp_path, stream=[{"type": "result", "subtype": "error_max_turns", "is_error": True}],
                          exit_code=1)
    with pytest.raises(RuntimeError, match="error_max_turns"):
        E.claude_cli_answer("q", env=env_for(bindir, user_home))
    bindir, _ = make_fake(tmp_path, stream=[], exit_code=1, stderr="Invalid API key")
    with pytest.raises(RuntimeError, match="Invalid API key"):
        E.claude_cli_answer("q", env=env_for(bindir, user_home))


def test_missing_cli_is_a_clear_error(tmp_path):
    env = {"PATH": str(tmp_path)}
    with pytest.raises(E.EngineUnavailable, match="no `claude` on PATH"):
        E.available(env, only=["claude-cli"])
    with pytest.raises(E.EngineUnavailable):
        E.claude_cli_answer("q", env=env)


def test_selected_only_by_name(tmp_path):
    bindir, _ = make_fake(tmp_path)
    env = {"PATH": str(bindir), "OPENAI_API_KEY": "sk-1"}
    assert list(E.available(env)) == ["openai"]  # never joins a run on its own
    got = E.available(env, only=["claude-cli"], timeout=42)
    assert list(got) == ["claude-cli"] and got["claude-cli"].timeout == 42
    assert E.available(env, only=["claude-cli"])["claude-cli"].timeout is None  # the adapter's default


def test_bound_passes_timeout_only_when_set():
    seen = []

    def old_style(question, *, key, model):  # an adapter written before --timeout existed
        seen.append(model)
        return {}

    E.Bound("x", "m", "", old_style).ask("q")
    assert seen == ["m"]
    got = {}
    E.Bound("x", "m", "", lambda q, **kw: got.update(kw) or {}, timeout=5).ask("q")
    assert got["timeout"] == 5


def test_run_records_subscription_flag(tmp_path, user_home, monkeypatch):
    bindir, _ = make_fake(tmp_path)
    for k, v in env_for(bindir, user_home).items():
        monkeypatch.setenv(k, v)
    q = {"ours": ["acme.test"], "brand_terms": ["Acme"], "stale_markers": [], "groups": {"brand": ["What is Acme?"]}}
    eng = E.available(only=["claude-cli"])
    recs = run(q, eng, runs=1, out=tmp_path / "out" / "answers.jsonl", sleep=0)
    assert recs[0]["subscription"] is True and recs[0]["cost_usd"] == 0.0
    assert recs[0]["cited"] and recs[0]["engine"] == "claude-cli"


def test_cli_reports_a_missing_claude(tmp_path, monkeypatch, capsys):
    qf = tmp_path / "q.yaml"
    qf.write_text("ours: [acme.test]\nbrand_terms: [Acme]\ngroups:\n  brand:\n    - What is Acme?\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert main(["run", str(qf), "--engine", "claude-cli", "--out", str(tmp_path / "o")]) == 2
    assert "no `claude` on PATH" in capsys.readouterr().err
