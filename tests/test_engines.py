import pytest

from answer_engine_benchmark import engines as E


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise E.requests.HTTPError(f"{self.status_code} error")


@pytest.fixture
def post(monkeypatch):
    calls = []
    replies = []

    def fake(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        return replies.pop(0)

    monkeypatch.setattr(E.requests, "post", fake)
    return calls, replies


def test_openai_reads_url_citations(post):
    calls, replies = post
    replies.append(Resp({"model": "gpt-5-mini-2025-08-07", "usage": {"input_tokens": 1000, "output_tokens": 500},
                         "output": [{"type": "web_search_call"}, {"type": "message", "content": [
                             {"type": "output_text", "text": "Answer",
                              "annotations": [{"type": "url_citation", "url": "https://a.test/"}]}]}]}))
    out = E.openai_answer("q?", key="k-openai")
    assert out["urls"] == ["https://a.test/"] and out["text"] == "Answer"
    assert calls[0]["json"]["tools"] == [{"type": "web_search"}]
    assert out["cost_usd"] == pytest.approx((1000 * 0.25 + 500 * 2.0) / 1e6)


def test_gemini_key_goes_in_a_header_not_the_url(post):
    calls, replies = post
    replies.append(Resp({"candidates": [{"content": {"parts": [{"text": "A"}]},
                                         "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://p/1"}}],
                                                               "webSearchQueries": ["x"]}}],
                         "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20}}))
    out = E.gemini_answer("q?", key="AIza-secret")
    assert "AIza-secret" not in calls[0]["url"]
    assert calls[0]["headers"]["x-goog-api-key"] == "AIza-secret"
    assert calls[0]["json"]["tools"] == [{"google_search": {}}]
    assert out["urls"] == ["https://p/1"] and out["grounding_queries"] == ["x"]


def test_claude_collects_citations_and_search_results_and_continues_paused_turns(post):
    calls, replies = post
    replies.append(Resp({"model": "claude-sonnet-5", "stop_reason": "pause_turn",
                         "usage": {"input_tokens": 100, "output_tokens": 10,
                                   "server_tool_use": {"web_search_requests": 1}},
                         "content": [{"type": "server_tool_use", "name": "web_search"},
                                     {"type": "web_search_tool_result",
                                      "content": [{"type": "web_search_result", "url": "https://r.test/"}]}]}))
    replies.append(Resp({"model": "claude-sonnet-5", "stop_reason": "end_turn",
                         "usage": {"input_tokens": 200, "output_tokens": 50},
                         "content": [{"type": "text", "text": "Use R",
                                      "citations": [{"type": "web_search_result_location", "url": "https://r.test/"}]},
                                     {"type": "text", "text": "."}]}))
    out = E.anthropic_answer("q?", key="test-key")
    assert out["text"] == "Use R."
    assert out["urls"] == ["https://r.test/", "https://r.test/"]
    assert len(calls) == 2 and calls[1]["json"]["messages"][1]["role"] == "assistant"
    assert calls[0]["json"]["tools"][0]["type"] == "web_search_20260209"
    assert out["usage"]["web_search_requests"] == 1
    assert out["cost_usd"] == pytest.approx((300 * 2 + 60 * 10) / 1e6 + 0.01)


def test_claude_refusal_is_an_error(post):
    _, replies = post
    replies.append(Resp({"stop_reason": "refusal", "stop_details": {"category": "cyber"}, "content": [], "usage": {}}))
    with pytest.raises(RuntimeError, match="refusal"):
        E.anthropic_answer("q?", key="k")


def test_perplexity_enables_search_and_uses_reported_cost(post):
    calls, replies = post
    replies.append(Resp({"output": [{"type": "search_results", "results": [{"url": "https://s/1"}, {"url": "https://s/2"}]},
                                    {"type": "message", "content": [{"text": "A",
                                                                     "annotations": [{"url": "https://s/1"}]}]}],
                         "usage": {"cost": {"total_cost": 0.0061}}}))
    out = E.perplexity_answer("q?", key="pplx-k")
    assert calls[0]["json"]["tools"] == [{"type": "web_search"}]
    assert out["urls"] == ["https://s/1", "https://s/2"]
    assert out["cost_usd"] == 0.0061


def test_available_reads_env_only_and_hides_keys():
    env = {"OPENAI_API_KEY": "sk-abc", "PERPLEXITY_API_KEY": ""}
    got = E.available(env, models={"openai": "gpt-x"})
    assert list(got) == ["openai"] and got["openai"].model == "gpt-x"
    assert "sk-abc" not in repr(got["openai"])
    assert E.available(env, only=["gemini"]) == {}


def test_redact():
    assert E.redact("HTTPError for url ...?key=AIzaXYZ&alt=json", []) == "HTTPError for url ...?key=[redacted]&alt=json"
    assert "secret-value" not in E.redact("bad secret-value here", ["secret-value"])


def test_token_cost_matches_dated_snapshots():
    assert E.token_cost("gpt-5-mini-2025-08-07", 1_000_000, 0) == 0.25
    assert E.token_cost("unknown-model", 1, 1) is None
