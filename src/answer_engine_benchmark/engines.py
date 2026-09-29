"""One adapter per answer engine, over plain HTTPS so no vendor SDK is needed.

Every adapter takes a question and returns:

    {"text": str, "urls": [str], "model": str, "usage": dict, "cost_usd": float | None}

`urls` holds every source the engine attached to the answer, in the order it gave
them, because the position of your first citation is scored. Keys are read from
the environment only and are never written to a log or a results file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from .scoring import URL_RX

TIMEOUT = 180
SYSTEM_HINT = "Answer as you would for any user. Cite your sources with links."

# Public list prices in USD per 1M tokens (input, output), checked 2026-09. Vendors change
# these, so a run's cost is an estimate unless the API reports it (Perplexity does).
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5-mini": (0.25, 2.00),
    "gemini-3.5-flash": (0.30, 2.50),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "perplexity/sonar": (1.00, 1.00),
}
# Claude's web search tool is billed per search on top of tokens: $10 per 1,000.
ANTHROPIC_SEARCH_USD = 0.01


def token_cost(model: str, inp: int, out: int) -> float | None:
    p = PRICES.get(model)
    if p is None:
        # Dated snapshots such as gpt-5-mini-2025-08-07 price like their base name.
        p = next((v for k, v in PRICES.items() if model.startswith(k + "-")), None)
    return round((inp * p[0] + out * p[1]) / 1e6, 6) if p else None


# ----------------------------------------------------------------------------- OpenAI ----
def openai_answer(question: str, *, key: str, model: str = "gpt-5-mini", timeout: float = TIMEOUT) -> dict:
    body = {
        "model": model,
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
        "input": question,
        "instructions": SYSTEM_HINT,
    }
    r = requests.post("https://api.openai.com/v1/responses",
                      headers={"Authorization": f"Bearer {key}"}, json=body, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    text, urls = [], []
    for item in d.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text":
                text.append(c.get("text", ""))
                urls += [a["url"] for a in c.get("annotations", []) or []
                         if a.get("type") == "url_citation" and a.get("url")]
    u = d.get("usage", {}) or {}
    return {
        "text": "\n".join(text),
        "urls": urls,
        "model": d.get("model", model),
        "usage": u,
        "cost_usd": token_cost(d.get("model", model), u.get("input_tokens", 0), u.get("output_tokens", 0)),
    }


# ----------------------------------------------------------------------------- Gemini ----
def gemini_answer(question: str, *, key: str, model: str = "gemini-3.5-flash", timeout: float = TIMEOUT) -> dict:
    body = {"contents": [{"parts": [{"text": question}]}], "tools": [{"google_search": {}}]}
    # The key goes in a header, not the URL. In the query string it ends up in the text of
    # any HTTPError, and from there in the results file.
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key}, json=body, timeout=timeout,
    )
    r.raise_for_status()
    d = r.json()
    cand = (d.get("candidates") or [{}])[0]
    text = "\n".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) or [])
    gm = cand.get("groundingMetadata") or {}
    urls = [(ch.get("web") or {}).get("uri") for ch in gm.get("groundingChunks", []) or []]
    u = d.get("usageMetadata", {}) or {}
    return {
        "text": text,
        # These are vertexaisearch.cloud.google.com redirect URLs; resolve.py turns them
        # into the real sources before scoring.
        "urls": [x for x in urls if x],
        "model": model,
        "usage": u,
        "cost_usd": token_cost(model, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)),
        "grounding_queries": gm.get("webSearchQueries", []),
    }


# ----------------------------------------------------------------------------- Claude ----
def anthropic_answer(question: str, *, key: str, model: str = "claude-sonnet-5", timeout: float = TIMEOUT) -> dict:
    """Claude through the Messages API with the server-side web search tool.

    No model fallback is configured on purpose: an answer from a different model would be
    scored as this one. A refusal is recorded as an error instead.
    """
    messages: list[dict] = [{"role": "user", "content": question}]
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    text: list[str] = []
    urls: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0}
    served = model
    # A long server-tool turn can stop with `pause_turn`. Sending the partial turn back
    # lets it continue. Three rounds is plenty for a single question.
    for _ in range(3):
        body = {
            "model": model,
            "max_tokens": 16000,
            "system": SYSTEM_HINT,
            "messages": messages,
            "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
        }
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body,
                          timeout=timeout)
        r.raise_for_status()
        d = r.json()
        served = d.get("model", served)
        u = d.get("usage", {}) or {}
        usage["input_tokens"] += u.get("input_tokens", 0)
        usage["output_tokens"] += u.get("output_tokens", 0)
        usage["web_search_requests"] += (u.get("server_tool_use") or {}).get("web_search_requests", 0)
        for b in d.get("content", []) or []:
            if b.get("type") == "text":
                text.append(b.get("text", ""))
                urls += [c["url"] for c in b.get("citations", []) or [] if c.get("url")]
            elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
                urls += [x["url"] for x in b["content"] if isinstance(x, dict) and x.get("url")]
        stop = d.get("stop_reason")
        if stop == "refusal":
            raise RuntimeError(f"refusal: {(d.get('stop_details') or {}).get('category')}")
        if stop != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": d.get("content", [])}]
    cost = token_cost(served, usage["input_tokens"], usage["output_tokens"])
    if cost is not None:
        cost = round(cost + usage["web_search_requests"] * ANTHROPIC_SEARCH_USD, 6)
    return {"text": "".join(text), "urls": urls, "model": served, "usage": usage, "cost_usd": cost}


# ------------------------------------------------------------------------- Perplexity ----
def perplexity_answer(question: str, *, key: str, model: str = "perplexity/sonar", timeout: float = TIMEOUT) -> dict:
    """Perplexity's Responses API. Web search is OFF unless the tool is passed.

    Without `tools=[{"type": "web_search"}]` the call still succeeds and returns a fluent
    answer naming real companies, with zero citations. For a benchmark scored on
    citations that is the worst failure there is: no error, and no data.
    """
    body = {"model": model, "input": question, "tools": [{"type": "web_search"}]}
    r = requests.post("https://api.perplexity.ai/v1/responses",
                      headers={"Authorization": f"Bearer {key}"}, json=body, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    text, urls = "", []
    for item in d.get("output", []) or []:
        if item.get("type") == "search_results":
            urls += [x.get("url") for x in item.get("results") or [] if x.get("url")]
        for c in item.get("content", []) or []:
            text += c.get("text", "")
            urls += [a.get("url") for a in c.get("annotations") or [] if a.get("url")]
    u = d.get("usage", {}) or {}
    reported = (u.get("cost") or {}).get("total_cost")
    return {
        "text": text,
        "urls": list(dict.fromkeys(urls)),
        "model": model,
        "usage": u,
        "cost_usd": round(float(reported), 6) if reported is not None
        else token_cost(model, u.get("input_tokens", 0), u.get("output_tokens", 0)),
    }


# ------------------------------------------------------------------------- Claude CLI ----
# Claude through the locally logged-in Claude Code CLI (`claude -p`), for people with a Claude
# subscription and no API key. The CLI is a coding agent that normally reads a lot about the
# person running it: git identity, CLAUDE.md files, memory, settings, hooks, MCP servers. In an
# earlier run it passed the operator's git username to the model, and brand answers then told
# the reader the company was probably their own. So every call runs as a stranger: HOME, the
# config dir and the working directory are fresh empty temp dirs, git reads no config, and the
# environment is an allow-list. Only the login credentials are copied in.
CLAUDE_CLI_TIMEOUT = 600
# Everything else is dropped: USER, EMAIL, every GIT_* variable, the calling session's CLAUDE*
# and ANTHROPIC* variables, SSH agents and so on.
_CLI_ENV_KEEP = ("PATH", "LANG", "LANGUAGE", "LC_ALL", "TZ", "TMPDIR",
                 "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
                 "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS")
# A long-lived token from `claude setup-token`. When set, no credentials file is copied.
_CLI_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
_CLI_TOOLS = "WebSearch,WebFetch"


class EngineUnavailable(ValueError):
    """An engine was asked for but cannot run on this machine."""


def claude_exe(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    exe = shutil.which("claude", path=env.get("PATH"))
    if not exe:
        raise EngineUnavailable("claude-cli needs the Claude Code CLI: no `claude` on PATH. "
                                "Install it and run `claude` once to log in.")
    return exe


def _cli_config_dir(env: dict) -> Path:
    return Path(env.get("CLAUDE_CONFIG_DIR") or Path(env.get("HOME") or Path.home()) / ".claude")


def cli_env(home: str, src: dict | None = None) -> dict:
    """The environment of someone who has never met the operator."""
    src = os.environ if src is None else src
    env = {k: src[k] for k in (*_CLI_ENV_KEEP, _CLI_TOKEN_VAR) if src.get(k)}
    env.update({
        "HOME": home,
        "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"),
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "DISABLE_AUTOUPDATER": "1",
        "USER": "user",
        "LOGNAME": "user",
    })
    return env


def _stage_auth(cfg_dir: str, src: dict) -> Path | None:
    """Copy only the login credentials into the empty config dir. Returns the original's path,
    or None when the token variable carries the login instead."""
    os.makedirs(cfg_dir, mode=0o700, exist_ok=True)
    if src.get(_CLI_TOKEN_VAR):
        return None
    real = _cli_config_dir(src) / ".credentials.json"
    if not real.is_file():
        # macOS keeps the login in the Keychain, where it cannot be copied.
        raise EngineUnavailable(f"claude-cli: no login found at {real}. Run `claude setup-token` and "
                                f"export {_CLI_TOKEN_VAR}, or run `claude` once to log in.")
    fd = os.open(os.path.join(cfg_dir, ".credentials.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(real.read_bytes())
    return real


def _expires_at(raw: bytes) -> int:
    try:
        return int(json.loads(raw)["claudeAiOauth"]["expiresAt"])
    except (ValueError, KeyError, TypeError):
        return 0


def _write_back_auth(real: Path, staged: str) -> None:
    """If the CLI refreshed its login inside the temp dir, hand the new one back. The refresh
    token rotates, so keeping the old one could log the operator's own sessions out."""
    try:
        new, old = Path(staged).read_bytes(), real.read_bytes()
    except OSError:
        return
    if new == old or _expires_at(new) <= _expires_at(old):
        return
    tmp = real.with_name(real.name + ".aeb.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(new)
    os.replace(tmp, real)


def _run_cli(cmd: list[str], prompt: str, *, cwd: str, env: dict, timeout: float) -> subprocess.CompletedProcess:
    # The prompt goes in on stdin, so a question that starts with "-" is never read as a flag.
    # Own process group, so a timeout kills the CLI and anything it started.
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()
        p.communicate()
        raise TimeoutError(f"claude-cli: no answer after {timeout:g}s") from None
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def _tool_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _search_links(text: str) -> list[str]:
    """WebSearch results arrive as text with a `Links: [{"title": ..., "url": ...}]` list."""
    i = text.find("Links: [")
    if i < 0:
        return []
    try:
        links, _ = json.JSONDecoder().raw_decode(text, i + len("Links: "))
    except ValueError:
        return []
    return [x["url"] for x in links if isinstance(x, dict) and isinstance(x.get("url"), str)]


def parse_cli_stream(stdout: str) -> dict:
    """Read `--output-format stream-json`. Sources come from the tool stream: the CLI's web
    tools run client-side, so the usage counter for server-side searches stays at 0."""
    calls: dict[str, tuple[str, dict]] = {}
    tools: list[str] = []
    urls: list[str] = []
    final: dict = {}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        blocks = (ev.get("message") or {}).get("content") or []
        if ev.get("type") == "assistant":
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                    tools.append(b["name"])
                    calls[b.get("id", "")] = (b["name"], b.get("input") or {})
        elif ev.get("type") == "user":
            for b in blocks:
                if not isinstance(b, dict) or b.get("type") != "tool_result" or b.get("is_error"):
                    continue
                name, inp = calls.get(b.get("tool_use_id", ""), ("", {}))
                if name == "WebSearch":
                    urls += _search_links(_tool_text(b.get("content")))
                elif name == "WebFetch" and isinstance(inp.get("url"), str):
                    urls.append(inp["url"])
        elif ev.get("type") == "result":
            final = ev
    if not final:
        raise RuntimeError("claude-cli: no result in the CLI output")
    if final.get("is_error") or final.get("subtype") != "success":
        raise RuntimeError(f"claude-cli: {final.get('subtype')}: {str(final.get('result', ''))[:200]}")
    text = final.get("result") or ""
    mu = final.get("modelUsage") or {}
    # The CLI may use a small model for side jobs; the answer comes from the one that wrote most.
    model = max(mu, key=lambda m: (mu[m] or {}).get("outputTokens", 0)) if mu else ""
    return {
        "text": text,
        "urls": list(dict.fromkeys(urls + URL_RX.findall(text))),
        "model": model,
        "usage": {
            "turns": final.get("num_turns"),
            "tools": tools,
            "web_search_requests": tools.count("WebSearch"),
            "web_fetch_requests": tools.count("WebFetch"),
            "duration_ms": final.get("duration_ms"),
            # What the CLI says the call would cost at API prices. Not charged on a subscription.
            "api_equivalent_usd": final.get("total_cost_usd"),
        },
    }


def claude_cli_answer(question: str, *, key: str = "", model: str = "default",
                      timeout: float = CLAUDE_CLI_TIMEOUT, env: dict | None = None) -> dict:
    """Claude through the local Claude Code CLI, with only WebSearch and WebFetch available.

    Uses the operator's Claude subscription, not API credit, so `cost_usd` is 0 and the row
    is flagged `subscription`. `key` is unused. `model="default"` lets the CLI pick.
    """
    src = os.environ if env is None else env
    exe = claude_exe(src)  # from the caller's PATH, before the environment is replaced
    with tempfile.TemporaryDirectory(prefix="aeb-home-") as home, \
            tempfile.TemporaryDirectory(prefix="aeb-cwd-") as cwd:
        cenv = cli_env(home, src)
        cenv["GIT_CEILING_DIRECTORIES"] = os.path.dirname(cwd)  # never find a repo above the temp dir
        real = _stage_auth(cenv["CLAUDE_CONFIG_DIR"], src)
        mcp = os.path.join(home, "mcp.json")
        with open(mcp, "w", encoding="utf-8") as f:
            f.write('{"mcpServers": {}}')
        cmd = [exe, "-p", "--output-format", "stream-json", "--verbose",
               "--append-system-prompt", SYSTEM_HINT, "--max-turns", "12",
               "--tools", _CLI_TOOLS, "--allowedTools", _CLI_TOOLS,
               "--strict-mcp-config", "--mcp-config", mcp, "--setting-sources", "user"]
        if model and model != "default":
            cmd += ["--model", model]
        try:
            r = _run_cli(cmd, question, cwd=cwd, env=cenv, timeout=timeout)
        finally:
            if real is not None:
                _write_back_auth(real, os.path.join(cenv["CLAUDE_CONFIG_DIR"], ".credentials.json"))
    try:
        out = parse_cli_stream(r.stdout or "")
    except RuntimeError as e:
        detail = (r.stderr or "").strip()[:200]
        raise RuntimeError(f"{e} (exit {r.returncode}){': ' + detail if detail else ''}") from None
    if r.returncode != 0:
        raise RuntimeError(f"claude-cli exit {r.returncode}: {(r.stderr or '').strip()[:200]}")
    out["model"] = out["model"] or model
    out["cost_usd"] = 0.0
    out["subscription"] = True
    return out


@dataclass(frozen=True)
class Engine:
    name: str
    env_key: str  # empty: the engine needs no key and runs only when asked for by name
    fn: Callable[..., dict]
    default_model: str


ENGINES: dict[str, Engine] = {
    "openai": Engine("openai", "OPENAI_API_KEY", openai_answer, "gpt-5-mini"),
    "gemini": Engine("gemini", "GEMINI_API_KEY", gemini_answer, "gemini-3.5-flash"),
    "claude": Engine("claude", "ANTHROPIC_API_KEY", anthropic_answer, "claude-sonnet-5"),
    "perplexity": Engine("perplexity", "PERPLEXITY_API_KEY", perplexity_answer, "perplexity/sonar"),
    "claude-cli": Engine("claude-cli", "", claude_cli_answer, "default"),
}


@dataclass(frozen=True)
class Bound:
    """An engine with its key and model resolved. `key` is never printed or stored."""

    name: str
    model: str
    key: str
    fn: Callable[..., dict]
    timeout: float | None = None  # None: the adapter's own default (180 s, claude-cli 600 s)

    def ask(self, question: str) -> dict:
        if self.timeout is None:
            return self.fn(question, key=self.key, model=self.model)
        return self.fn(question, key=self.key, model=self.model, timeout=self.timeout)

    def __repr__(self) -> str:  # keep the key out of tracebacks and debug output
        return f"Bound(name={self.name!r}, model={self.model!r})"


def available(env: dict | None = None, *, only: list[str] | None = None,
              models: dict[str, str] | None = None, timeout: float | None = None) -> dict[str, Bound]:
    """Engines whose key is set, plus keyless engines named in `only`. `models` overrides a
    default model per engine and `timeout` the per-call limit in seconds."""
    env = os.environ if env is None else env
    models = models or {}
    out = {}
    for name, e in ENGINES.items():
        if only and name not in only:
            continue
        if e.env_key:
            key = env.get(e.env_key, "")
            if not key:
                continue
        elif only:
            claude_exe(env)  # fail now, with a clear message, rather than on every call
            key = ""
        else:
            continue
        out[name] = Bound(name, models.get(name, e.default_model), key, e.fn, timeout)
    return out


def redact(message: str, secrets: list[str]) -> str:
    """Remove every key from an error message before it is logged."""
    for s in secrets:
        if s and len(s) >= 6:
            message = message.replace(s, "[redacted]")
    # Belt and braces for key-shaped strings that were not in the list.
    return re.sub(r"(key=)[^&\s]+", r"\1[redacted]", message)
