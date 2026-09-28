"""One adapter per answer engine, over plain HTTPS so no vendor SDK is needed.

Every adapter takes a question and returns:

    {"text": str, "urls": [str], "model": str, "usage": dict, "cost_usd": float | None}

`urls` holds every source the engine attached to the answer, in the order it gave
them, because the position of your first citation is scored. Keys are read from
the environment only and are never written to a log or a results file.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass

import requests

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
def openai_answer(question: str, *, key: str, model: str = "gpt-5-mini") -> dict:
    body = {
        "model": model,
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
        "input": question,
        "instructions": SYSTEM_HINT,
    }
    r = requests.post("https://api.openai.com/v1/responses",
                      headers={"Authorization": f"Bearer {key}"}, json=body, timeout=TIMEOUT)
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
def gemini_answer(question: str, *, key: str, model: str = "gemini-3.5-flash") -> dict:
    body = {"contents": [{"parts": [{"text": question}]}], "tools": [{"google_search": {}}]}
    # The key goes in a header, not the URL. In the query string it ends up in the text of
    # any HTTPError, and from there in the results file.
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key}, json=body, timeout=TIMEOUT,
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
def anthropic_answer(question: str, *, key: str, model: str = "claude-opus-5") -> dict:
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
                          timeout=TIMEOUT)
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
def perplexity_answer(question: str, *, key: str, model: str = "perplexity/sonar") -> dict:
    """Perplexity's Responses API. Web search is OFF unless the tool is passed.

    Without `tools=[{"type": "web_search"}]` the call still succeeds and returns a fluent
    answer naming real companies, with zero citations. For a benchmark scored on
    citations that is the worst failure there is: no error, and no data.
    """
    body = {"model": model, "input": question, "tools": [{"type": "web_search"}]}
    r = requests.post("https://api.perplexity.ai/v1/responses",
                      headers={"Authorization": f"Bearer {key}"}, json=body, timeout=TIMEOUT)
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


@dataclass(frozen=True)
class Engine:
    name: str
    env_key: str
    fn: Callable[..., dict]
    default_model: str


ENGINES: dict[str, Engine] = {
    "openai": Engine("openai", "OPENAI_API_KEY", openai_answer, "gpt-5-mini"),
    "gemini": Engine("gemini", "GEMINI_API_KEY", gemini_answer, "gemini-3.5-flash"),
    "claude": Engine("claude", "ANTHROPIC_API_KEY", anthropic_answer, "claude-opus-5"),
    "perplexity": Engine("perplexity", "PERPLEXITY_API_KEY", perplexity_answer, "perplexity/sonar"),
}


@dataclass(frozen=True)
class Bound:
    """An engine with its key and model resolved. `key` is never printed or stored."""

    name: str
    model: str
    key: str
    fn: Callable[..., dict]

    def ask(self, question: str) -> dict:
        return self.fn(question, key=self.key, model=self.model)

    def __repr__(self) -> str:  # keep the key out of tracebacks and debug output
        return f"Bound(name={self.name!r}, model={self.model!r})"


def available(env: dict | None = None, *, only: list[str] | None = None,
              models: dict[str, str] | None = None) -> dict[str, Bound]:
    """Engines whose key is set. `models` overrides a default model per engine."""
    env = os.environ if env is None else env
    models = models or {}
    out = {}
    for name, e in ENGINES.items():
        if only and name not in only:
            continue
        key = env.get(e.env_key, "")
        if key:
            out[name] = Bound(name, models.get(name, e.default_model), key, e.fn)
    return out


def redact(message: str, secrets: list[str]) -> str:
    """Remove every key from an error message before it is logged."""
    for s in secrets:
        if s and len(s) >= 6:
            message = message.replace(s, "[redacted]")
    # Belt and braces for key-shaped strings that were not in the list.
    return re.sub(r"(key=)[^&\s]+", r"\1[redacted]", message)
