"""GitHub rejects an invalid workflow file before it runs anything, so the same checks run here first.

actionlint is the official release binary, downloaded once into site/.cache and checked against
the sha256 from its release before every run. shellcheck comes from shellcheck-py, hash-pinned
in requirements-test.txt, and is handed to actionlint by path, so every machine checks run:
scripts with the same shellcheck. None of these checks skip: a skipped check would pass a
broken workflow.
"""

import hashlib
import io
import platform
import re
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from importlib import metadata
from pathlib import Path

import pytest
import yaml

from sitegen import paths

GITHUB = paths.ROOT / ".github"
WORKFLOWS = sorted([*(GITHUB / "workflows").glob("*.yml"), *(GITHUB / "workflows").glob("*.yaml")])

# From actionlint_1.7.12_checksums.txt on https://github.com/rhysd/actionlint/releases/tag/v1.7.12.
# To update, change the version and copy both lines from the new release's checksums file.
ACTIONLINT_VERSION = "1.7.12"
ACTIONLINT = {
    ("win32", "AMD64"): ("windows_amd64.zip", "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9", "actionlint.exe"),
    ("linux", "x86_64"): ("linux_amd64.tar.gz", "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8", "actionlint"),
}

# Dependabot's schedule intervals, and what each ecosystem in dependabot.yml reads in its directory.
INTERVALS = {"daily", "weekly", "monthly", "quarterly", "semiannually", "yearly", "cron"}
READS = {"github-actions": ".github/workflows/*.yml", "pip": "requirements*.txt"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _member(archive: bytes, name: str, member: str) -> bytes:
    """One named file out of the release archive."""
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            return z.read(member)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as t:
        f = t.extractfile(member)
        if f is None:
            pytest.fail(f"{name} has no file {member}")
        return f.read()


@pytest.fixture(scope="session")
def actionlint() -> Path:
    """The pinned actionlint for this machine, extracted from an archive whose sha256 was checked this run."""
    key = (sys.platform, platform.machine())
    if key not in ACTIONLINT:
        pytest.fail(f"no pinned actionlint for {key}; add it from the release's checksums file")
    suffix, sha256, member = ACTIONLINT[key]
    name = f"actionlint_{ACTIONLINT_VERSION}_{suffix}"
    cache = paths.SITE / ".cache" / "actionlint" / ACTIONLINT_VERSION
    archive = cache / name
    data = archive.read_bytes() if archive.exists() else b""
    if _sha256(data) != sha256:
        url = f"https://github.com/rhysd/actionlint/releases/download/v{ACTIONLINT_VERSION}/{name}"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        if _sha256(data) != sha256:
            pytest.fail(f"{url} doesn't match its pinned sha256")
        cache.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(data)
    exe = cache / member
    exe.write_bytes(_member(data, name, member))  # every run, so only the checked archive's copy ever runs
    exe.chmod(0o755)
    return exe


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
    assert out.splitlines()[0] == ACTIONLINT_VERSION


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
