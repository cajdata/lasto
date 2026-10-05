"""GitHub rejects an invalid workflow file before it runs anything, so the same checks run here first.

actionlint is the official release binary, pinned in sitegen/tools.py: downloaded once into
site/.cache and checked against the sha256 from its release before every run. shellcheck comes from shellcheck-py, hash-pinned
in requirements-test.txt, and is handed to actionlint by path, so every machine checks run:
scripts with the same shellcheck. None of these checks skip: a skipped check would pass a
broken workflow.
"""

import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
import yaml

from sitegen import paths, tools

GITHUB = paths.ROOT / ".github"
WORKFLOWS = sorted([*(GITHUB / "workflows").glob("*.yml"), *(GITHUB / "workflows").glob("*.yaml")])

# Dependabot's schedule intervals, and what each ecosystem in dependabot.yml reads in its directory.
INTERVALS = {"daily", "weekly", "monthly", "quarterly", "semiannually", "yearly", "cron"}
READS = {"github-actions": ".github/workflows/*.yml", "pip": "requirements*.txt"}


@pytest.fixture(scope="session")
def actionlint() -> Path:
    """The pinned actionlint for this machine, extracted from an archive whose sha256 was checked this run."""
    try:
        return tools.fetch("actionlint")
    except tools.ToolError as exc:
        pytest.fail(str(exc))


@pytest.fixture(scope="session")
def shellcheck() -> Path:
    """shellcheck-py's own shellcheck, found through the package's installed files, wherever the environment put it."""
    name = "shellcheck.exe" if sys.platform == "win32" else "shellcheck"
    try:
        dist = metadata.distribution("shellcheck-py")
    except metadata.PackageNotFoundError:
        pytest.fail("shellcheck-py isn't installed; install site/requirements-test.txt")
    for f in dist.files or ():
        exe = Path(dist.locate_file(f)).resolve()
        if f.name == name and exe.is_file():
            return exe
    pytest.fail(f"shellcheck-py {dist.version} is installed but has no {name}")


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    """UTF-8 both ways: text=True alone would use the locale's code page on Windows."""
    return subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=120, **kwargs)


def _lint(actionlint: Path, shellcheck: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    command = [str(actionlint), "-no-color", "-oneline", f"-shellcheck={shellcheck}", "-pyflakes=", *args]
    return _run(command, cwd=paths.ROOT, input=stdin)


def test_there_are_workflows_to_check():
    assert WORKFLOWS


@pytest.mark.parametrize("path", [*WORKFLOWS, GITHUB / "dependabot.yml"], ids=lambda p: p.name)
def test_file_is_valid_yaml(path):
    assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)


def test_actionlint_is_the_pinned_version(actionlint):
    out = _run([str(actionlint), "-version"], check=True).stdout
    assert out.splitlines()[0] == tools.TOOLS["actionlint"].version


def test_shellcheck_is_the_pinned_version(shellcheck):
    pin = re.search(r"^shellcheck-py==(\S+)", (paths.SITE / "requirements-test.txt").read_text(encoding="utf-8"), re.M)
    assert pin, "requirements-test.txt no longer pins shellcheck-py"
    assert metadata.version("shellcheck-py") == pin.group(1)
    out = _run([str(shellcheck), "--version"], check=True).stdout
    # shellcheck-py 0.11.0.1 is shellcheck 0.11.0, plus a packaging number.
    assert f"version: {pin.group(1).rsplit('.', 1)[0]}" in out.splitlines()


def test_actionlint_output_is_read_as_utf8(actionlint, shellcheck):
    # actionlint quotes the offending text, and Windows would decode it as cp1252 unless told otherwise.
    workflow = "on: push\njobs:\n  check:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: echo \"${{ github.ref == ”x” }}\"\n"
    result = _lint(actionlint, shellcheck, "-stdin-filename", "broken.yml", "-", stdin=workflow)
    assert result.returncode == 1 and "”" in result.stdout, result


def test_actionlint_finds_no_problems(actionlint, shellcheck):
    result = _lint(actionlint, shellcheck, *(p.relative_to(paths.ROOT).as_posix() for p in WORKFLOWS))
    assert result.returncode == 0, result.stdout + result.stderr


# Each one must fail, so a check that quietly stopped working can't pass the real workflows.
BROKEN = {
    "syntax-check": "      - run: python -m pip install --only-binary=:all: -r requirements.txt\n",
    "shellcheck": "      - run: echo $UNQUOTED\n",
    "expression": "      - run: echo \"${{ github.no_such_property }}\"\n",
}


@pytest.mark.parametrize("rule", BROKEN)
def test_actionlint_catches_a_broken_workflow(rule, actionlint, shellcheck):
    workflow = "on: push\njobs:\n  check:\n    runs-on: ubuntu-24.04\n    steps:\n" + BROKEN[rule]
    result = _lint(actionlint, shellcheck, "-stdin-filename", "broken.yml", "-", stdin=workflow)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"[{rule}]" in result.stdout, result.stdout


def test_dependabot_watches_files_that_exist():
    config = yaml.safe_load((GITHUB / "dependabot.yml").read_text(encoding="utf-8"))
    assert config["version"] == 2
    for update in config["updates"]:
        ecosystem = update["package-ecosystem"]
        assert ecosystem in READS, f"test_workflows.py has no check for {ecosystem} yet"
        assert update["schedule"]["interval"] in INTERVALS
        directory = paths.ROOT / update["directory"].strip("/")
        assert list(directory.glob(READS[ecosystem])), f"{ecosystem} in {update['directory']} would find nothing"
