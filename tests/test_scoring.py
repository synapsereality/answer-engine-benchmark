from answer_engine_benchmark.scoring import Resolver, domain, is_ours, score


def test_domain_strips_www_and_case():
    assert domain("https://WWW.Example.com/a") == "example.com"


def test_bare_domain_matches_host_and_subdomains_only():
    assert is_ours("https://example.com/x", ["example.com"])
    assert is_ours("https://docs.example.com/x", ["example.com"])
    assert not is_ours("https://notexample.com/x", ["example.com"])
    assert not is_ours("https://example.com.evil.net/", ["example.com"])


def test_path_entry_matches_at_a_boundary():
    ours = ["github.com/acme"]
    assert is_ours("https://github.com/acme", ours)
    assert is_ours("https://github.com/acme/repo", ours)
    assert is_ours("https://www.github.com/acme?tab=repos", ours)
    assert not is_ours("https://github.com/acme-fan/repo", ours)
    assert not is_ours("https://github.com/other", ours)


def test_entries_with_scheme_or_www_still_match():
    assert is_ours("https://example.com/", ["https://www.example.com/"])


def test_cited_and_position():
    ans = {"text": "See https://other.org/a and https://example.com/p.", "urls": ["https://first.net/x"]}
    s = score(ans, ["example.com"], ["Acme"])
    assert s["cited"] and s["position"] == 3
    assert s["our_urls"] == ["https://example.com/p"]
    assert s["domains_cited"] == ["first.net", "other.org", "example.com"]
    assert not s["mentioned"]


def test_shared_host_position_counts_our_path_separately():
    ans = {"urls": ["https://github.com/someone/x", "https://github.com/acme/y"], "text": ""}
    s = score(ans, ["github.com/acme"], ["acme"])
    assert s["position"] == 2


def test_not_cited_when_only_mentioned():
    s = score({"text": "Acme is a vendor.", "urls": ["https://review.site/acme"]}, ["acme.com"], ["Acme"])
    assert s["mentioned"] and not s["cited"] and s["position"] is None


def test_stale_marker_needs_proximity_to_the_brand():
    near = "Acme, founded in 2015, sells widgets."
    far = "Acme sells widgets. " + "x" * 300 + " Another firm was founded in 2015."
    assert score({"text": near}, ["acme.com"], ["Acme"], stale_markers=["founded in 2015"])["stale"]
    assert not score({"text": far}, ["acme.com"], ["Acme"], stale_markers=["founded in 2015"])["stale"]
    # no mention, no stale flag
    assert not score({"text": "founded in 2015"}, ["acme.com"], ["Acme"], stale_markers=["founded in 2015"])["stale"]


def test_trailing_punctuation_is_trimmed():
    s = score({"text": "Read https://example.com/a."}, ["example.com"], ["x"])
    assert s["our_urls"] == ["https://example.com/a"]


def test_resolver_follows_proxy_and_caches(tmp_path, monkeypatch):
    calls = []

    class R:
        url = "https://real.site/page"

    def fake_head(url, **kw):
        calls.append(url)
        return R()

    monkeypatch.setattr("answer_engine_benchmark.scoring.requests.head", fake_head)
    cache = tmp_path / "c.json"
    r = Resolver(cache)
    proxy = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
    assert r("https://plain.site/") == "https://plain.site/"
    assert r(proxy) == "https://real.site/page"
    assert r(proxy) == "https://real.site/page"
    assert len(calls) == 1
    r.save()
    assert Resolver(cache)(proxy) == "https://real.site/page"
    assert len(calls) == 1
