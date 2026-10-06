# lasto.dev

The project website. A small Python build turns Markdown and data files into a static site, and a GitHub Actions workflow (`.github/workflows/site.yml`) deploys it to GitHub Pages from `main`. The plan and the decisions behind it are in `docs/website.md`.

## Build and preview

The site's dependencies are pinned with hashes in `site/requirements.txt` and `site/requirements-test.txt`, apart from the app's `uv.lock`. They get their own environment, `.venv-site`: `uv sync` would remove them from `.venv`, and `uv run --with-requirements` doesn't check the hashes. Install and run them like this (on Linux, `bin` in place of `Scripts`):

```bash
uv venv .venv-site
uv pip install --python .venv-site/Scripts/python.exe --require-hashes --only-binary :all: -r site/requirements.txt -r site/requirements-test.txt
.venv-site/Scripts/python site/build.py build
.venv-site/Scripts/python site/build.py serve --watch
.venv-site/Scripts/python site/build.py links
.venv-site/Scripts/python -m pytest -c site/pytest.ini site/tests
```

`links` checks every link and anchor in the built site with lychee, offline, as CI does. Add `--online` to check links to other sites too.

`serve` builds, then serves `site/dist` at http://127.0.0.1:8000/ and rebuilds when content, data, templates, static files, the safety core source, the other app files the site reads (in `sitegen/appfacts.py`), `pyproject.toml`, or any page's `sources:` change. Every build runs the checks in `sitegen/checks.py`, and any problem fails it. CI installs the same files with `pip --require-hashes` and adds `--strict`, which also needs full git history and committed sources.

The tests use their own config, so the app's pytest settings (the simulator plugin and the 100 percent coverage gate on `lasto.safety`) never apply to the site, and never get loosened for it.

The tests also run actionlint on every workflow in `.github/workflows`, so a workflow GitHub would reject fails here first.

The tests read a frozen copy of what the site reads from the app, `tests/fixtures/app/` (its README says what's in it), with the expected values written in the tests, so a change to the app can't fail them. The live source is the strict build's to check against the live pages. No test takes an expected value from it: only CLAUDE.md's rules (the allowlist stays within CLAUDE.md's, the never-list contains it, the rate and voltage limits) and the `services.toml` cross-check read it, since only a real break can fail them, and the tests of the built site check what doesn't depend on a value. Tests that change source change a copy of the fixture, and find what they change by name or structure with `tests/source_edit.py`, never by its text.

actionlint and lychee are pinned in `sitegen/tools.py`: each is its project's official release, pinned by version and by the sha256 the release publishes. The first use downloads it into `site/.cache`, so it needs network access; every use checks the archive's sha256 and compares the binary with the archive's copy, so only a checked copy ever runs. Dependabot can't see these pins. To update one, change its version and copy the two hashes (Windows and Linux) from the new release.

## Where things are

| Path | What it is |
|---|---|
| `content/*.md` | Pages. Front matter, then Markdown, rendered through Jinja first so pages can use the data below. |
| `content/docs.md`, `content/docs/*.md` | The docs index at `/docs/`, and one file per doc. A doc's URL must be a path `roadmap.toml` reserves (the build refuses any other), and it publishes only once its phase is done. A doc that belongs to no phase, like an FAQ, is reserved without one. |
| `data/site.toml` | Site name, URLs, navigation, credit, the name note, and the trademark list. |
| `data/roadmap.toml` | Phase status, truck test dates, and the reserved docs paths. The roadmap, the status lines, page stamps, the docs index, the 404 page, and the summaries in JSON-LD and the llms files read it. A phase's commands aren't listed here: the roadmap takes them from `cli.py`'s `COMMANDS`, and `extra_commands` adds longer command lines like `lasto drive --profile`. Its text renders like page content, so it quotes the app's numbers as `{{ capture.silence|num }}` and the like. |
| `data/services.toml` | Plain words for each service byte. Which services are allowed comes from the safety core source, never from here. It must describe every service the source lists; a spare description, like one left over after the allowlist is trimmed, is fine. |
| `data/hardware.toml` | The hardware chain, for Fig. 2 (its pin labels included) and the quick reference table. |
| `templates/` | Jinja templates: `base.html`, `home.html`, `page.html`, the chain figure, and the social image. |
| `static/` | CSS, the favicon, and the font sources with their licenses. |
| `sitegen/` | The build. `safety.py` reads the safety core with `ast` and never imports it, and `appfacts.py` reads the capture's timings and limits the same way. `tools.py` pins actionlint and lychee, and `links.py` runs the link check. |

## Content conventions

- Every `##` and `###` heading ends with a fixed id, like `## Two modes {#modes}`. The build refuses a heading without one, so links never break when a heading is reworded.
- `Table: Caption` on the line before a table becomes its numbered caption.
- `::: side` and `:::`, each on its own line, put blocks in the side column.
- ```` ```figure service-map ````, ```` ```figure chain ````, and ```` ```figure ack-slot FRAME ```` insert generated figures. A ```` ```frames ```` block holds example CAN frames, with a `caption:` first line.
- Jinja comments are `{%# ... #%}`, because `{#` marks heading ids.
- No em or en dashes, no hype words. The build checks both.
- Numbers come from the source: `facts.*` from the safety core, `capture.*` from the capture code. Every `lasto` command and `--option` shown in code must exist in `cli.py`, and an option shown with a command must be one that command takes, or the build fails.
- Hyphenated identifiers (OBD-II, K-line, 2026-09-26) and numbers with their units stay on one line in Markdown text. Template text, like Fig. 2's caption, gets the same rules through the `nobreak` filter.
- A page's date follows the data it uses inside `{{ }}` or `{% %}`, so the same word in prose doesn't count. Docs pages get a breadcrumb and a matching BreadcrumbList on their own. `crumb:` in the front matter shortens the last crumb, and `sources:` lists other repo files a page's date should follow.

## When an app phase ships

1. In `data/roadmap.toml`, add `live_tested = YYYY-MM-DD` once its test at the truck is done: what the site says has run at the truck follows that date, since the truck test comes before approval. Then set the phase's `status = "done"` and `approved = YYYY-MM-DD`, and `public = true` once its commits are on `main`. Update its `live_test`, `open`, and `delivers` to what actually happened. Its commands follow `cli.py` on their own.
2. Update anything the build flags as stale (it knows which phrases stop being true once a phase is done or has run at the truck, and checks that "built in N phases" matches the roadmap). Stamps, the status sentence, and the summaries in the llms files and JSON-LD follow Phases 2 and 4 on their own; anything else needs reading.
3. Add the docs the phase brings as `content/docs/<slug>.md`, at the path the roadmap reserves. The docs index, the roadmap's links, and the 404 page's list pick them up; the nav's Docs link is fixed.
4. Update the Safety pages for whatever the phase changed in the safety core, and check each changed claim against the code.

Phase 1 is public, so the Safety page's "Enforced in" notes are permalinks to the exact lines in the safety core, at the commit that last changed each file.

## Fonts

Archivo and Fragment Mono, both under the SIL Open Font License 1.1, with no Reserved Font Names. The sources and their license files are in `static/fonts/src/`; the build subsets them on every build and copies the licenses next to the served fonts. See `static/fonts/src/README.md`.
