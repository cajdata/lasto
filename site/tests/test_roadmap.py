"""roadmap.toml validation and the generated status sentence.

These build their own roadmaps, so marking a real phase done never breaks them.
"""

import datetime as dt

import pytest

from sitegen import data
from sitegen.data import BuildError, Phase, Roadmap

D = dt.date(2026, 9, 26)


def phase(n: int, status: str = "planned", **kw) -> Phase:
    fields = dict(
        number=n, slug=f"p{n}", name=f"Phase {n} name", status=status, public=False, transmits="No",
        summary="s", delivers=(), commands=(), live_test="", open=(),
    )
    if status == "done":
        fields.update(built=D, approved=D, public=True)
    elif status == "awaiting-approval":
        fields.update(built=D)
    fields.update(kw)
    return Phase(**fields)


def roadmap(*statuses: str) -> Roadmap:
    return Roadmap("pre-alpha", [phase(i, s) for i, s in enumerate(statuses)])


NINE = ["planned"] * 9


def test_real_roadmap_loads_and_validates():
    r = data.load_roadmap()
    assert [p.number for p in r.phases] == list(range(9))
    assert r.phase(0).done


def test_copy_that_counts_the_phases_must_match_the_roadmap():
    from sitegen import checks

    r = roadmap(*["planned"] * 10)
    assert checks._stale("page", "Lasto is built in ten phases, and each one stops.", r) == []
    problems = checks._stale("page", "It's built in nine phases.", r)
    assert problems == ["page: says it's built in nine phases, but the roadmap has 10"]
    assert checks._stale("page", "Lasto gets built in 10 phases.", r) == []


def test_status_sentence_follows_the_data():
    r = roadmap("done", "awaiting-approval", *NINE[2:])
    assert r.status_sentence() == "Pre-alpha: Phase 1 is built and waiting on my sign-off, and nothing runs at the truck yet."
    r = roadmap("done", "done", *NINE[2:])
    assert r.status_sentence() == "Pre-alpha: Phase 1 is done, and nothing runs at the truck yet."
    r = roadmap("done", "done", "in-progress", *NINE[3:])
    assert r.status_sentence() == "Pre-alpha: Phase 2 is in progress, and nothing runs at the truck yet."
    r = roadmap("done", "done", "done", "in-progress", *NINE[4:])
    assert r.status_sentence() == "Pre-alpha: Phase 3 is in progress, and at the truck Lasto only listens so far."


def test_status_labels():
    assert phase(1, "awaiting-approval").status_label == "Built 2026-09-26, waiting on my sign-off"
    assert phase(0, "done").status_label == "Done, approved 2026-09-26"
    assert phase(5).status_label == "Planned"


def test_done_needs_an_approval_date():
    r = Roadmap("pre-alpha", [phase(0, "done", approved=None)] + [phase(i) for i in range(1, 9)])
    with pytest.raises(BuildError, match="approved"):
        data.validate_roadmap(r)


def test_public_needs_a_built_phase():
    r = Roadmap("pre-alpha", [phase(0, "done"), phase(1, public=True)] + [phase(i) for i in range(2, 9)])
    with pytest.raises(BuildError, match="public"):
        data.validate_roadmap(r)


def test_one_phase_in_progress_at_most():
    r = roadmap("done", "in-progress", "in-progress", *NINE[3:])
    with pytest.raises(BuildError, match="at most one"):
        data.validate_roadmap(r)


def test_phases_are_numbered_in_order():
    r = Roadmap("pre-alpha", [phase(0), phase(2)])
    with pytest.raises(BuildError, match="numbered"):
        data.validate_roadmap(r)
