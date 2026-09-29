"""The citation rule and the rest of the scoring.

An answer CITES you when one of the URLs it gives, in the text or in its source
list, is on one of your properties (see `is_ours`). It MENTIONS you when one of
your brand terms appears in the text. Only aggregates over repeated runs mean
anything: the same question asked twice can come back with different sources.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

URL_RX = re.compile(r"https?://[^\s\)\]\"'>]+")
# Only matches within this many characters of a brand term count as being about you.
PROXIMITY = 200

# Gemini's grounding sources are proxy URLs on this host that redirect to the real page.
# Scored as they are, every Gemini citation looks like a citation of Google.
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)


class Resolver:
    """Follows citation-proxy redirects to the real URL, with a JSON cache on disk."""

    def __init__(self, cache_file: Path | None = None, timeout: int = 20) -> None:
        self.cache_file = cache_file
        self.timeout = timeout
        self.cache: dict[str, str] = {}
        if cache_file and cache_file.exists():
            try:
                self.cache = json.loads(cache_file.read_text())
            except (OSError, ValueError):
                self.cache = {}

    def __call__(self, url: str) -> str:
        if not any(h in url for h in REDIRECT_HOSTS):
            return url
        if url not in self.cache:
            try:
                self.cache[url] = requests.head(url, allow_redirects=True, timeout=self.timeout).url or url
            except requests.RequestException:
                return url  # not cached, so a later run tries again
        return self.cache[url]

    def warm(self, urls: list[str], workers: int = 16) -> None:
        from concurrent.futures import ThreadPoolExecutor

        todo = [u for u in dict.fromkeys(urls) if any(h in u for h in REDIRECT_HOSTS) and u not in self.cache]
        if todo:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(self, todo))

    def save(self) -> None:
        if self.cache_file:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self.cache))


def domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def is_ours(url: str, ours: list[str]) -> bool:
    """Is this URL one of your properties?

    A bare domain (`example.com`) matches that host and its subdomains. A
    path-qualified entry (`github.com/example`) matches only up to a path
    boundary, so `github.com/example/repo` is yours and `github.com/example-fan`
    is not. Reducing entries to their host would make every GitHub citation yours.
    """
    host = domain(url)
    rest = url.split("://", 1)[-1].lower()
    rest = rest.removeprefix("www.")
    for o in ours:
        o = o.lower().strip().rstrip("/")
        if o.startswith(("http://", "https://")):
            o = o.split("://", 1)[1]
        o = o.removeprefix("www.")
        if "/" in o:
            if rest.startswith(o) and (len(rest) == len(o) or rest[len(o)] in "/?#"):
                return True
        elif host == o or host.endswith("." + o):
            return True
    return False


def _near(text: str, terms: list[str], needles: list[str]) -> list[str]:
    """Needles that appear within PROXIMITY characters of any term."""
    low = text.lower()
    spans = [m.start() for t in terms if t for m in re.finditer(re.escape(t.lower()), low)]
    hits = []
    for n in needles:
        for m in re.finditer(re.escape(n.lower()), low):
            if any(abs(m.start() - s) <= PROXIMITY for s in spans):
                hits.append(n)
                break
    return hits


def score(answer: dict, ours: list[str], brand_terms: list[str], *,
          stale_markers: list[str] | None = None, resolve=None) -> dict:
    """Score one answer. `resolve` maps a URL to its real destination (see Resolver)."""
    resolve = resolve or (lambda u: u)
    text = answer.get("text", "") or ""
    cited = list(dict.fromkeys((answer.get("urls") or []) + URL_RX.findall(text)))
    cited = [resolve(u.rstrip(".,;")) for u in cited]
    domains = list(dict.fromkeys(d for d in (domain(u) for u in cited) if d))
    our_urls = [u for u in cited if is_ours(u, ours)]
    # Position: rank of your first source among the distinct sources cited. A source is
    # a domain, except that your URLs on a shared host (github.com/you) count as a
    # source of their own, so someone else's repo cited first still ranks ahead of you.
    sources = list(dict.fromkeys(
        ("ours:" if is_ours(u, ours) else "") + domain(u) for u in cited if domain(u)))
    position = next((i + 1 for i, s in enumerate(sources) if s.startswith("ours:")), None)
    mentioned = any(t and t.lower() in text.lower() for t in brand_terms)
    stale = _near(text, brand_terms, stale_markers or []) if mentioned else []
    return {
        "mentioned": mentioned,
        "cited": bool(our_urls),
        "position": position,
        "our_urls": our_urls[:5],
        "domains_cited": domains,
        "n_domains": len(domains),
        "stale": stale,
    }
