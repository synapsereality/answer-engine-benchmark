# Releasing answer-engine-benchmark

Nothing here has happened yet. Every step below is yours, from your own
accounts. The org name `synapsereality` is a placeholder. If you pick another
name, replace it everywhere with
`grep -rl --exclude-dir=.git synapsereality/ . | xargs sed -i 's#synapsereality/#NEWNAME/#g'`
(that leaves the synapsereality.io URLs alone), then commit.

## Once, for all three repos: the GitHub org

1. Create the org at https://github.com/account/organizations/new (Free plan),
   named `synapsereality`.
2. Org settings, Authentication security: tick "Require two-factor
   authentication". Marketplace publishing needs 2FA anyway.

## Once, for this repo

3. Create an empty public repo `synapsereality/answer-engine-benchmark`, with no README, licence
   or .gitignore, then push:

   ```bash
   cd ~/Documents/ben-is-a-dev/oss/answer-engine-benchmark
   git remote add origin git@github.com:synapsereality/answer-engine-benchmark.git
   git push -u origin main
   ```

4. Set the website field. It is the link people copy into tutorials, so it has
   to be our docs page and not GitHub:

   ```bash
   gh repo edit synapsereality/answer-engine-benchmark \
     --homepage https://synapsereality.io/open-source/answer-engine-benchmark/ \
     --description "Ask AI answer engines your buyers' questions, several times each, and count who they cite." \
     --add-topic aeo --add-topic geo --add-topic llm --add-topic seo --add-topic benchmark
   ```

5. Repo Settings, Environments, New environment, named `pypi`. Add yourself
   under "Required reviewers" if you want to click Approve before each upload.
   The release workflow waits for that click.

6. Publish the docs page first (`SITE-PAGE.md` in this folder, not in git) at
   https://synapsereality.io/open-source/answer-engine-benchmark/. The README and the package
   metadata already link to it.

## Live check before the first release

A `--dry-run` against the real APIs passed on 2026-09-29 for OpenAI
(`gpt-5-mini`), Gemini (`gemini-3.5-flash`) and Perplexity (`perplexity/sonar`),
with a neutral test brand. Claude adapter verified against mocks only: there
was no Anthropic API key to test with. Before or soon after the first release,
run one `aeb run ... --dry-run --engine claude` with an Anthropic API key.

## Once: PyPI trusted publisher (no token)

7. Log in at https://pypi.org, then Account settings, Publishing, "Add a new
   pending publisher", GitHub tab:

   | field | value |
   |---|---|
   | PyPI Project Name | `answer-engine-benchmark` |
   | Owner | `synapsereality` |
   | Repository name | `answer-engine-benchmark` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   A pending publisher does not reserve the name. If someone else registers
   `answer-engine-benchmark` before step 9, it is void, so do steps 7 to 9 on the same day. The
   name was free on 2026-09-29.

## Every release

8. Bump `version` in `pyproject.toml` and `CITATION.cff` (not needed for
   0.1.0), commit and push.
9. On GitHub: Releases, Draft a new release. Tag `v0.1.0` (create it on
   publish), target `main`, title `v0.1.0`. Click Publish release.
10. The `release` workflow checks that the tag matches the version, runs the
    tests, builds, and uploads to PyPI. Check it under Actions, then:

    ```bash
    pip install answer-engine-benchmark==0.1.0
    aeb --version
    ```

## Zenodo DOI (before the first release)

Zenodo archives releases published after the switch is on. So flip it before
you publish the first release, or make a second release afterwards.

1. Log in at https://zenodo.org with GitHub. When GitHub asks, grant access to
   the `synapsereality` org.
2. Zenodo, account menu, GitHub: find `synapsereality/answer-engine-benchmark` and switch it on.
   Zenodo reads `.zenodo.json` for the title, creators, licence and links.
3. After the release, Zenodo shows two DOIs. Use the concept DOI, which always
   points to the latest version. Uncomment the `doi:` line in `CITATION.cff`,
   put it in, add a DOI line to the README, and commit.

## What is not automated, on purpose

No token for PyPI, npm or GitHub is stored in the repo or its secrets. The
workflows get a short-lived OIDC token from GitHub, valid for that one job, for
this repo, this workflow file and this environment.
