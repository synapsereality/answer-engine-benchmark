# answer-engine-benchmark

Asks ChatGPT, Gemini, Perplexity and Claude the questions your buyers ask, with
web search on, several times each. Then it counts how often each answer cites
your site, what it cites instead, and what it says about you. Every answer is
kept in a JSONL file, so you can check any number in the report against the
text behind it.

Docs: https://synapsereality.io/open-source/answer-engine-benchmark/

```bash
pip install answer-engine-benchmark
export OPENAI_API_KEY=... GEMINI_API_KEY=... PERPLEXITY_API_KEY=... ANTHROPIC_API_KEY=...
aeb run questions/template.yaml --out runs/first \
  --set brand="Acme Analytics" --set domain=acme.example \
  --set category="invoice software" --set audience="small accounting firms"
```

## Why repeat every question

The same question can come back with different sources a minute later. One
answer is one sample. The default is 3 runs per question per engine, and every
rate in the report is over those runs. `aeb noise` re-asks a sample of questions
later and tells you whether a change you see is bigger than the noise.

## The question set

`questions/template.yaml` holds 24 questions in four groups:

| group | the buyer is | your name in the question |
|---|---|---|
| discovery | looking for a provider | no |
| comparison | learning how to choose | no |
| brand | asking about you | yes |
| trust | checking you are safe to buy from | yes |

Discovery and comparison tell you whether engines find you when nobody asked
for you. Brand and trust tell you what they say when somebody does.

Fill the four `vars` in the file, or pass them with `--set`. A run won't start
while any of them still holds its example value. Add your own questions under
any group, or new groups. Write them the way a buyer types, and never make a
competitor the subject of a question.

The file also sets:

- `ours`: domains that count as your citation. `acme.example` covers its
  subdomains. `github.com/acme` covers only paths under it, so a citation of
  someone else's GitHub repo is not yours.
- `brand_terms`: names that count as a mention.
- `stale_markers`: phrases that describe you wrongly or out of date, such as an
  old product or a wrong founding year. One is flagged only within 200
  characters of a brand term, so another company "founded in 2015" doesn't count
  against you.

## How an answer is scored

**Cited** means one of the answer's URLs is yours. The URLs are the sources the
engine attached plus any link in the text. **Mentioned** means a brand term
appears in the text. An answer can mention you without citing you, and that
difference is worth watching. **Position** is the rank of your first source
among the distinct sites the answer cited.

Failed calls are recorded as errors and left out of every rate. A rate limit
never shows up as "not cited".

Gemini returns its sources as Google redirect links. They are resolved to the
real pages before scoring, with a cache next to the results. Otherwise every
Gemini answer would look like it cited Google.

## Engines

| engine | key | default model | how it searches |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | `gpt-5-mini` | Responses API `web_search` tool |
| `gemini` | `GEMINI_API_KEY` | `gemini-3.5-flash` | Google Search grounding |
| `perplexity` | `PERPLEXITY_API_KEY` | `perplexity/sonar` | Responses API `web_search` tool |
| `claude` | `ANTHROPIC_API_KEY` | `claude-sonnet-5` | Messages API web search tool |

Keys are read from the environment only. They are sent in request headers and
removed from any error message before it is written, so they never reach the
results file. An engine with no key is skipped.

Change a model with `--model claude=claude-opus-5`. Pick the models your
buyers actually use in the apps, or say in the report which ones you used.

Perplexity's API doesn't search unless you ask it to. Without the search tool
it still answers, fluently, with no citations. This adapter always turns search
on.

## Commands

```bash
aeb check QUESTIONS [--set ...]           # validate, show which keys are set and how many calls a run needs
aeb run QUESTIONS --out DIR [--set ...]   # ask everything, write DIR/answers.jsonl and DIR/report.md
aeb run QUESTIONS --out DIR --dry-run     # first question of each group, once: a cheap smoke test
aeb report DIR/answers.jsonl [QUESTIONS]  # rebuild the report, re-scoring if you changed the question file
aeb noise QUESTIONS --baseline DIR/answers.jsonl --out DIR2 --sample 5
```

`--anonymise` on `run` and `report` replaces every domain that isn't yours with
"Source A", "Source B" and so on. Use it before you share a report outside your
company. `--max-calls` (default 500) stops a run that would make more calls
than you expected.

`answers.jsonl` gets one line per answer, appended as it arrives, so a stopped
run keeps what it already paid for. Each line holds the question, engine, model,
run number, the full answer text, every URL, token usage, cost, and the score
fields.

## What it costs

A full run of the template is 24 questions x 4 engines x 3 runs = 288 calls.
Our own run of 360 answers cost $1.39 in API fees without Claude (details in
`examples/`). Adding Claude costs more, because web search on the Claude API is
billed per search on top of tokens. Each
row carries its cost. Perplexity reports the real cost. The others are
estimated from list prices in `engines.py`, so check them against your bills.

## Measure as a stranger

Run the benchmark from an account and machine that has never been told who you
are. We learned this from a run through a coding assistant's CLI instead of an
API. The CLI passed the operator's own git identity to the model, and many brand
answers then told the reader the company was probably their own. The API
adapters here send only the question and a one-line instruction to cite sources.

## Example

`examples/synapse-launch-week-2026-09.md` is a real run on one company's own
site, with the published figures only. 105 of 156 answers that named the
company cited its site. Of the 204 that didn't name it, 1 did. It is an example
of the output, not part of the question set.

## Install from source

```bash
git clone https://github.com/synapsereality/answer-engine-benchmark
cd answer-engine-benchmark
pip install .
aeb --help
```

Python 3.10 or later. Needs `requests` and `PyYAML`.

## Tests

```bash
pip install -e ".[test]"
pytest
```

33 tests. They mock every API, so they spend nothing and need no keys.

## Licence

MIT. Made by [Synapse](https://synapsereality.io/open-source/answer-engine-benchmark/).
