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

`serve` builds, then serves `site/dist` at http://127.0.0.1:8000/ and rebuilds when content, data, templates, static files, or the safety core source change. Every build runs the checks in `sitegen/checks.py`, and any problem fails it. CI installs the same files with `pip --require-hashes` and adds `--strict`, which also needs full git history and committed sources.

The tests use their own config, so the app's pytest settings (the simulator plugin and the 100 percent coverage gate on `lasto.safety`) never apply to the site, and never get loosened for it.

The tests also run actionlint on every workflow in `.github/workflows`, so a workflow GitHub would reject fails here first.

actionlint and lychee are pinned in `sitegen/tools.py`: each is its project's official release, pinned by version and by the sha256 the release publishes. The first use downloads it into `site/.cache`, so it needs network access; every use checks the archive's sha256 and compares the binary with the archive's copy, so only a checked copy ever runs. Dependabot can't see these pins. To update one, change its version and copy the two hashes (Windows and Linux) from the new release.

## Where things are

| Path | What it is |
|---|---|
| `content/*.md` | Pages. Front matter, then Markdown, rendered through Jinja first so pages can use the data below. |
| `data/site.toml` | Site name, URLs, navigation, credit, the name note, and the trademark list. |
| `data/roadmap.toml` | Phase status. The roadmap, the status lines, page stamps, and the 404 page read it. |
| `data/services.toml` | Plain words for each service byte. Which services are allowed comes from the safety core source, never from here. |
| `data/hardware.toml` | The planned hardware chain, for Fig. 2 and the quick reference table. |
| `templates/` | Jinja templates: `base.html`, `home.html`, `page.html`, the chain figure, and the social image. |
| `static/` | CSS, the favicon, and the font sources with their licenses. |
| `sitegen/` | The build. `safety.py` reads the safety core with `ast` and never imports it. `tools.py` pins actionlint and lychee, and `links.py` runs the link check. |

## Content conventions

- Every `##` and `###` heading ends with a fixed id, like `## Two modes {#modes}`. The build refuses a heading without one, so links never break when a heading is reworded.
- `Table: Caption` on the line before a table becomes its numbered caption.
- `::: side` and `:::`, each on its own line, put blocks in the side column.
- ```` ```figure service-map ````, ```` ```figure chain ````, and ```` ```figure ack-slot FRAME ```` insert generated figures. A ```` ```frames ```` block holds example CAN frames, with a `caption:` first line.
- Jinja comments are `{%# ... #%}`, because `{#` marks heading ids.
- No em or en dashes, no hype words. The build checks both.

## When an app phase ships

1. In `data/roadmap.toml`, set the phase's `status = "done"` and `approved = YYYY-MM-DD`, and `public = true` once its commits are on `main`.
2. Update anything the build flags as stale (it knows which phrases stop being true after Phase 2, and checks that "built in N phases" matches the roadmap).
3. Write or publish the docs the phase adds (W2 adds the docs section and its gating).

Phase 1 is public, so the Safety page's "Enforced in" notes are permalinks to the exact lines in the safety core, at the commit that last changed each file.

## Fonts

Archivo and Fragment Mono, both under the SIL Open Font License 1.1, with no Reserved Font Names. The sources and their license files are in `static/fonts/src/`; the build subsets them on every build and copies the licenses next to the served fonts. See `static/fonts/src/README.md`.
