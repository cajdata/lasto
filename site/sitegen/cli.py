"""python site/build.py build | check | serve | links"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sitegen import checks, gitinfo, paths
from sitegen.data import BuildError


def _build_and_check(out: Path, strict: bool) -> int:
    from sitegen.pages import build

    gitinfo.clear_caches()
    b = build(out, strict=strict)
    problems = checks.run(out, b.pages, b.site, b.roadmap, b.webfonts.text_cmap, b.webfonts.mono_cmap)
    for p in problems:
        print(f"problem: {p}", file=sys.stderr)
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"Built {len(b.pages)} pages into {out} ({total:,} bytes). {len(problems)} problem(s).")
    return 1 if problems else 0


def watch_roots() -> list[Path]:
    """What serve --watch watches: the site's own sources, every app file the site reads, pyproject.toml
    (the home page's "Runs on" line), and every file a page's date follows (its sources:)."""
    import yaml

    from sitegen import appfacts
    from sitegen.pages import FRONT_MATTER

    sources: set[Path] = set()
    for page in paths.CONTENT.rglob("*.md"):
        m = FRONT_MATTER.match(page.read_text(encoding="utf-8").replace("\r\n", "\n"))
        meta = (yaml.safe_load(m.group(1)) or {}) if m else {}
        sources.update(paths.ROOT / s for s in meta.get("sources", []))
    return [paths.CONTENT, paths.DATA, paths.TEMPLATES, paths.STATIC, paths.SAFETY, paths.ROOT / "pyproject.toml",
            *(paths.APP / rel for rel in appfacts.FILES), *sorted(sources)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build.py", description="Build and preview lasto.dev.", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build the site and check it", allow_abbrev=False)
    b.add_argument("--strict", action="store_true", help="also require full git history and committed sources (CI)")
    b.add_argument("--out", type=Path, default=paths.DIST, help="output folder (default site/dist)")
    c = sub.add_parser("check", help="build into a throwaway folder and report problems", allow_abbrev=False)
    c.add_argument("--strict", action="store_true")
    s = sub.add_parser("serve", help="build, then serve on 127.0.0.1", allow_abbrev=False)
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--watch", action="store_true",
                   help="rebuild and recheck when anything the site is built or dated from changes")
    lk = sub.add_parser("links", help="check the built site's links with the pinned lychee", allow_abbrev=False)
    lk.add_argument("--online", action="store_true", help="also check links to other sites (needs network)")
    lk.add_argument("--dist", type=Path, default=paths.DIST, help="built site to check (default site/dist)")
    args = parser.parse_args(argv)
    try:
        if args.command == "links":
            from sitegen import links, tools

            try:
                return links.check(args.dist, online=args.online)
            except tools.ToolError as exc:
                print(f"link check failed: {exc}", file=sys.stderr)
                return 2
        if args.command == "build":
            return _build_and_check(args.out, args.strict)
        if args.command == "check":
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                return _build_and_check(Path(tmp) / "dist", args.strict)
        if args.command == "serve":
            from sitegen.serve import serve

            code = _build_and_check(paths.DIST, strict=False)
            serve(paths.DIST, args.port, watch_roots() if args.watch else None, lambda: _build_and_check(paths.DIST, strict=False))
            return code
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 2
    return 0
