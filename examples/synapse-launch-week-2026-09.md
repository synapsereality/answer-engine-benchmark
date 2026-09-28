# Example: one company's launch-week run, September 2026

**This is an example, not part of the question set.** It shows what a run
produces. Every figure here was already published on
[our AEO vs SEO page](https://synapsereality.io/aeo-vs-seo/). The company is
Synapse, the authors of this tool. It measured its own site the day after moving it to a new build. The
questions were Synapse's own, written before this template existed. Only the
published aggregates are here. Raw answers and the domains of other companies
are not.

## The setup

- 30 questions in the four groups this template uses.
- Four engines, OpenAI, Gemini, Perplexity and Claude, each with web search on.
- Every question asked 3 times on every engine: 360 answers.
- Run on 24 September 2026.

The Claude answers in this run came from the Claude Code CLI on a subscription,
not from the API adapter in this package. So they cost nothing per call and
aren't in the dollar figure below.

## The result

| question type | example | answers citing the site |
|---|---|---|
| names the company | "What does synapsereality.io do?" | 105 of 156 |
| asks for a provider or advice | "Which agency does answer engine optimization for SaaS companies?" | 1 of 204 |

Found when asked by name, close to invisible otherwise. The one citation in the
second row was an answer about MAP monitoring.

## What the engines cited instead

For the 204 answers that didn't name the company:

| source | answers citing it |
|---|---|
| a forum site | 25 |
| Google's developer documentation | 20 |
| LinkedIn | 15 |
| the most-cited agency | 15 |
| two freelance marketplaces | 15 and 9 |

No single competitor owned these answers. The `--anonymise` flag on `aeb run`
and `aeb report` produces this kind of table, with other companies' domains
replaced by labels.

## What the answers said

41 of the 156 brand and trust answers mentioned NFT, metaverse or Web3 work from
the company's old site. About 15 presented it as current. One gave a founding
year of 2015. The company was incorporated in 2018. `stale_markers` in the
question file exists to count this kind of error.

## Noise

Five questions were asked again the same day, three times each on three
engines. All 15 question and engine pairs landed within one citation of the
first run. `aeb noise` repeats that check.

## Cost

$1.39 in API fees for the 360 baseline answers. The Claude answers used a
subscription and are not in that figure.
