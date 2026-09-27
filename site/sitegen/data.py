"""Site data: site.toml, roadmap.toml, services.toml, hardware.toml. Loaded and validated."""

from __future__ import annotations

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
    commands: tuple[str, ...]
    live_test: str
    open: tuple[str, ...]
    built: dt.date | None = None
    approved: dt.date | None = None
    site_note: str = ""

    @property
    def done(self) -> bool:
        return self.status == "done"

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
    phase: int


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
        if not self.phase(2).done:
            tail = ", and nothing runs at the truck yet."
        elif not self.phase(4).done:
            tail = ", and at the truck Lasto only listens so far."
        else:
            tail = "."
        return f"{self.project_status.capitalize()}: {where}{tail}"


def load_roadmap(path: Path | None = None) -> Roadmap:
    raw = load_toml(path or paths.DATA / "roadmap.toml")
    phases: list[Phase] = []
    for p in raw.get("phase", []):
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
                commands=tuple(p["commands"]),
                live_test=p["live_test"],
                open=tuple(p["open"]),
                built=p.get("built"),
                approved=p.get("approved"),
                site_note=p.get("site_note", ""),
            )
        except KeyError as exc:
            raise BuildError(f"roadmap.toml phase {p.get('number')} is missing {exc}") from None
        phases.append(phase)
    reserved = [Reserved(r["path"], r["title"], r["phase"]) for r in raw.get("reserved", [])]
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
    if sum(p.status == "in-progress" for p in roadmap.phases) > 1:
        raise BuildError("roadmap.toml: at most one phase can be in progress")
    seen = set()
    for r in roadmap.reserved:
        if r.path in seen:
            raise BuildError(f"roadmap.toml reserves {r.path} twice")
        seen.add(r.path)
        if not (r.path.startswith("/docs/") and r.path.endswith("/")):
            raise BuildError(f"reserved path {r.path} must look like /docs/name/")
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
