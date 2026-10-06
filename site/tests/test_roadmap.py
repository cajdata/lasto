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
        summary="s", delivers=(), extra_commands=(), live_test="", open=(),
    )
    if status == "done":
        # Phases 2 and 4 are the ones tested at the truck.
        fields.update(built=D, approved=D, public=True, live_tested=D if n in (2, 4) else None)
    elif status == "awaiting-approval":
        fields.update(built=D)
    fields.update(kw)
    return Phase(**fields)


def roadmap(*statuses: str) -> Roadmap:
    return Roadmap("pre-alpha", [phase(i, s) for i, s in enumerate(statuses)])


NINE = ["planned"] * 9


def test_real_roadmap_loads_and_validates():
    r = data.load_roadmap()
    assert [p.number for p in r.phases] == list(range(10))
    assert r.phase(0).done


def test_phase_1_is_approved_and_public_and_the_gui_is_planned():
    r = data.load_roadmap()
    one, gui = r.phase(1), r.phase(9)
    assert one.done and one.public and one.approved == dt.date(2026, 9, 28)
    assert gui.slug == "gui" and gui.status == "planned" and not gui.public
    assert gui.transmits == "No"


def _one_phase_roadmap(tmp_path, extra: str):
    p = tmp_path / "roadmap.toml"
    p.write_text('project_status = "pre-alpha"\n\n[[phase]]\nnumber = 0\nslug = "plan"\nname = "Plan"\nstatus = "planned"\n'
                 f'public = false\ntransmits = "No"\nsummary = "s"\ndelivers = []\n{extra}\nlive_test = ""\nopen = []\n',
                 encoding="utf-8")
    return p


def test_commands_come_from_cli_py_not_roadmap_toml(tmp_path):
    # A phase's own commands are read from cli.py's COMMANDS; roadmap.toml lists only longer command lines.
    with pytest.raises(BuildError, match="extra_commands"):
        data.load_roadmap(_one_phase_roadmap(tmp_path, 'commands = ["lasto drive"]'))


def test_a_misspelled_phase_key_is_refused(tmp_path):
    # Optional keys would otherwise drop out of the site without a word.
    with pytest.raises(BuildError, match="extra_command"):
        data.load_roadmap(_one_phase_roadmap(tmp_path, 'extra_command = ["lasto drive --profile"]'))
    data.load_roadmap(_one_phase_roadmap(tmp_path, 'extra_commands = ["lasto drive --profile"]'))


def test_phase_4_shows_polled_logging_on_drive():
    assert data.load_roadmap().phase(4).extra_commands == ("lasto drive --profile",)


def test_phase_2_is_approved_and_public():
    two = data.load_roadmap().phase(2)
    assert two.done and two.public and two.approved == dt.date(2026, 10, 1)
    assert two.live_tested == dt.date(2026, 10, 1)


def test_a_truck_test_date_needs_a_phase_that_has_started():
    r = Roadmap("pre-alpha", [phase(0, "done"), phase(1, live_tested=D)] + [phase(i) for i in range(2, 9)])
    with pytest.raises(BuildError, match="live_tested"):
        data.validate_roadmap(r)


def test_a_done_phase_with_a_truck_test_needs_its_date():
    r = Roadmap("pre-alpha", [phase(0, "done", live_test="A short drive.")] + [phase(i) for i in range(1, 9)])
    with pytest.raises(BuildError, match="live_tested"):
        data.validate_roadmap(r)
    r = Roadmap("pre-alpha", [phase(0, "done", live_test="None. Nothing touches the truck.")] + [phase(i) for i in range(1, 9)])
    data.validate_roadmap(r)


def test_a_truck_test_comes_before_approval():
    late = D + dt.timedelta(days=1)
    r = Roadmap("pre-alpha", [phase(0, "done", live_test="A drive.", live_tested=late)] + [phase(i) for i in range(1, 9)])
    with pytest.raises(BuildError, match="live_tested"):
        data.validate_roadmap(r)


def test_truck_claims_go_stale_when_polled_mode_runs_at_the_truck():
    from sitegen import checks

    text = "Only passive capture has run at the truck."
    assert checks._stale("page", text, roadmap("done", "done", "done", "done", *NINE[4:])) == []
    tested = Roadmap("pre-alpha", [phase(i, "done") for i in range(4)] + [phase(4, "awaiting-approval", live_tested=D)]
                     + [phase(i) for i in range(5, 10)])
    assert checks._stale("page", text, tested) == [
        'page: says "only passive capture has run at the truck", but Phase 4 has run at the truck'
    ]
    assert checks._stale("page", "Lasto hasn’t asked the truck anything yet.", tested) == [
        'page: says "hasn\'t asked the truck anything yet", but Phase 4 has run at the truck'
    ]


def test_truck_claims_go_stale_at_each_phase_s_truck_test_not_its_approval():
    from sitegen import checks

    two = Roadmap("pre-alpha", [phase(0, "done"), phase(1, "done"), phase(2, "awaiting-approval", live_tested=D)]
                  + [phase(i) for i in range(3, 10)])
    assert checks._stale("page", "Nothing runs at the truck yet.", two) == [
        'page: says "nothing runs at the truck", but Phase 2 has run at the truck'
    ]
    three = Roadmap("pre-alpha", [phase(i, "done") for i in range(3)] + [phase(3, "awaiting-approval", live_tested=D)]
                    + [phase(i) for i in range(4, 10)])
    assert checks._stale("page", "Polled mode and the MX+ haven’t.", three) == [
        'page: says "polled mode and the mx+ haven\'t", but Phase 3 has run at the truck'
    ]


def test_a_truck_test_date_is_a_date_after_the_build():
    for bad in ["yes", False, D - dt.timedelta(days=1)]:
        r = Roadmap("pre-alpha", [phase(0, "done"), phase(1, "awaiting-approval", live_tested=bad)]
                    + [phase(i) for i in range(2, 9)])
        with pytest.raises(BuildError, match="live_tested"):
            data.validate_roadmap(r)


def test_polled_mode_cant_be_tested_at_the_truck_before_passive_mode():
    r = Roadmap("pre-alpha", [phase(i, "done") for i in range(2)] + [phase(2, "in-progress"), phase(3),
                phase(4, "awaiting-approval", live_tested=D)] + [phase(i) for i in range(5, 10)])
    with pytest.raises(BuildError, match="Phase 2"):
        data.validate_roadmap(r)


def test_a_finished_truck_test_needs_its_date_before_approval_too():
    r = Roadmap("pre-alpha", [phase(0, "done"), phase(1, "awaiting-approval", live_test="Done 2026-09-26: a drive.")]
                + [phase(i) for i in range(2, 9)])
    with pytest.raises(BuildError, match="live_tested"):
        data.validate_roadmap(r)


def test_stale_phrases_are_caught_with_curly_apostrophes():
    from sitegen import checks

    r = roadmap("done", "done", "done", *NINE[3:])
    assert checks._stale("page", "The bench test hasn’t been done yet.", r) == [
        "page: says \"hasn't been done yet\", but Phase 2 is done"
    ]


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
