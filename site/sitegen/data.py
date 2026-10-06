"""Site data: site.toml, roadmap.toml, services.toml, hardware.toml. Loaded and validated."""

from __future__ import annotations

import dataclasses
import datetime as dt
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sitegen import paths


class BuildError(Exception):
    """Something in the content, data, or source makes the site wrong. The build stops."""


def load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


STATUS_LABELS = {
    "done": "Done",
    "awaiting-approval": "Built, waiting on my sign-off",
    "in-progress": "In progress",
    "planned": "Planned",
}


@dataclass(frozen=True)
class Phase:
    number: int
    slug: str
    name: str
    status: str
    public: bool
    transmits: str
    summary: str
    delivers: tuple[str, ...]
    # Command lines beyond the phase's own commands, like "lasto drive --profile". Its own come from
    # cli.py's COMMANDS (safety.phase_commands), so a new command reaches the roadmap with no edit here.
    extra_commands: tuple[str, ...]
    live_test: str
    open: tuple[str, ...]
    built: dt.date | None = None
    approved: dt.date | None = None
    # The day its test at the truck finished. Truck tests come before approval, so what the site says
    # has run at the truck follows this, never `done`.
    live_tested: dt.date | None = None
    site_note: str = ""

    @property
    def done(self) -> bool:
        return self.status == "done"

    @property
    def tested_at_truck(self) -> bool:
        return self.live_tested is not None

    @property
    def anchor(self) -> str:
        return f"phase-{self.number}"

    @property
    def status_label(self) -> str:
        if self.done and self.approved:
            return f"Done, approved {self.approved.isoformat()}"
        if self.status == "awaiting-approval" and self.built:
            return f"Built {self.built.isoformat()}, waiting on my sign-off"
        return STATUS_LABELS[self.status]


@dataclass(frozen=True)
class Reserved:
    path: str
    title: str
    phase: int | None  # None for a doc that belongs to no phase, like an FAQ: it publishes whenever it's written


@dataclass
class Roadmap:
    project_status: str
    phases: list[Phase]
    reserved: list[Reserved] = field(default_factory=list)

    def phase(self, number: int) -> Phase:
        for p in self.phases:
            if p.number == number:
                return p
        raise BuildError(f"roadmap.toml has no phase {number}")

    def status_sentence(self) -> str:
        """One sentence for the home page and page stamps, generated so it can't go stale."""
        waiting = [p for p in self.phases if p.status == "awaiting-approval"]
        working = [p for p in self.phases if p.status == "in-progress"]
        done = [p for p in self.phases if p.done]
        if waiting:
            where = f"Phase {waiting[-1].number} is built and waiting on my sign-off"
        elif working:
            where = f"Phase {working[-1].number} is in progress"
        else:
            where = f"Phase {done[-1].number} is done" if done else "nothing is built yet"
        if not self.phase(2).tested_at_truck:
            tail = ", and nothing runs at the truck yet."
        elif not self.phase(4).tested_at_truck:
            tail = ", and at the truck Lasto only listens so far."
        else:
            tail = "."
        return f"{self.project_status.capitalize()}: {where}{tail}"


def load_roadmap(path: Path | None = None) -> Roadmap:
    raw = load_toml(path or paths.DATA / "roadmap.toml")
    phases: list[Phase] = []
    known = {f.name for f in dataclasses.fields(Phase)}
    for p in raw.get("phase", []):
        if "commands" in p:
            raise BuildError(f"roadmap.toml phase {p.get('number')}: a phase's commands come from cli.py's COMMANDS now; "
                             "list only longer command lines, like `lasto drive --profile`, in extra_commands")
        unknown = sorted(set(p) - known)
        if unknown:  # a misspelled optional key would otherwise drop out of the site without a word
            raise BuildError(f"roadmap.toml phase {p.get('number')} has keys the build doesn't know: {', '.join(unknown)}")
        try:
            phase = Phase(
                number=p["number"],
                slug=p["slug"],
                name=p["name"],
                status=p["status"],
                public=p["public"],
                transmits=p["transmits"],
                summary=p["summary"],
                delivers=tuple(p["delivers"]),
                extra_commands=tuple(p.get("extra_commands", [])),
                live_test=p["live_test"],
                open=tuple(p["open"]),
                built=p.get("built"),
                approved=p.get("approved"),
                live_tested=p.get("live_tested"),
                site_note=p.get("site_note", ""),
            )
        except KeyError as exc:
            raise BuildError(f"roadmap.toml phase {p.get('number')} is missing {exc}") from None
        phases.append(phase)
    try:
        reserved = [Reserved(r["path"], r["title"], r.get("phase")) for r in raw.get("reserved", [])]
    except KeyError as exc:
        raise BuildError(f"a [[reserved]] entry in roadmap.toml is missing {exc}") from None
    roadmap = Roadmap(raw["project_status"], phases, reserved)
    validate_roadmap(roadmap)
    return roadmap


def validate_roadmap(roadmap: Roadmap) -> None:
    numbers = [p.number for p in roadmap.phases]
    if numbers != list(range(len(numbers))):
        raise BuildError(f"roadmap.toml phases must be numbered 0, 1, 2 ... in order, got {numbers}")
    slugs = [p.slug for p in roadmap.phases]
    if len(set(slugs)) != len(slugs):
        raise BuildError("roadmap.toml has duplicate phase slugs")
    for p in roadmap.phases:
        if p.status not in STATUS_LABELS:
            raise BuildError(f"phase {p.number}: status {p.status!r} isn't one of {sorted(STATUS_LABELS)}")
        if p.done != (p.approved is not None):
            raise BuildError(f"phase {p.number}: 'approved' must be set if and only if status is done")
        if p.public and p.status not in ("done", "awaiting-approval"):
            raise BuildError(f"phase {p.number}: public = true needs a built phase")
        if p.approved and p.built and p.approved < p.built:
            raise BuildError(f"phase {p.number}: approved before it was built")
        if p.live_tested is not None:
            if type(p.live_tested) is not dt.date:
                raise BuildError(f"phase {p.number}: live_tested must be a date, like 2026-10-01")
            if p.status == "planned":
                raise BuildError(f"phase {p.number}: live_tested is set, but the phase hasn't started")
            if p.built and p.live_tested < p.built:
                raise BuildError(f"phase {p.number}: live_tested comes before it was built")
            if p.approved and p.live_tested > p.approved:
                raise BuildError(f"phase {p.number}: live_tested comes after its approval; the truck test comes first")
        has_truck_test = bool(p.live_test.strip()) and not p.live_test.startswith("None")
        if p.done and has_truck_test and p.live_tested is None:
            raise BuildError(f"phase {p.number}: done with a test at the truck, so it needs live_tested")
        if p.live_test.startswith("Done") and p.live_tested is None:
            raise BuildError(f"phase {p.number}: its live_test says it's done, so it needs live_tested")
    numbers_tested = {p.number for p in roadmap.phases if p.live_tested is not None}
    if 4 in numbers_tested and 2 not in numbers_tested:
        raise BuildError("roadmap.toml: Phase 4 has a live_tested date but Phase 2 doesn't; passive capture runs at the truck first")
    if sum(p.status == "in-progress" for p in roadmap.phases) > 1:
        raise BuildError("roadmap.toml: at most one phase can be in progress")
    seen = set()
    for r in roadmap.reserved:
        if r.path in seen:
            raise BuildError(f"roadmap.toml reserves {r.path} twice")
        seen.add(r.path)
        if not (r.path.startswith("/docs/") and r.path.endswith("/")):
            raise BuildError(f"reserved path {r.path} must look like /docs/name/")
        if r.phase is not None:
            roadmap.phase(r.phase)


def load_site() -> dict:
    site = load_toml(paths.DATA / "site.toml")
    site["base_url"] = site["base_url"].rstrip("/")
    return site


def load_services() -> dict[str, dict[int, str]]:
    raw = load_toml(paths.DATA / "services.toml")
    return {group: {int(k, 16): v for k, v in raw[group].items()} for group in ("allowed", "never")}


def load_hardware() -> dict:
    return load_toml(paths.DATA / "hardware.toml")
