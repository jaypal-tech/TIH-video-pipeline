from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from tih.series import SeriesConfig
from tih.story.models import OutlineEntry


class Phase(BaseModel):
    name: str
    first_ep: int
    last_ep: int
    purpose: str


class EpisodePlan(BaseModel):
    number: int
    title: str
    job: str
    hook: str
    cliff: str
    reveals: list[str]
    wall: Literal["none", "early", "late"]


class Arc(BaseModel):
    character: str
    starts_as: str
    turns_when: str
    ends_as: str


class Setup(BaseModel):
    id: str
    planted_ep: int
    payoff_ep: int
    description: str
    rule: str


class KnowledgeEvent(BaseModel):
    episode: int
    who: str
    topic: str
    learns: str
    evidence: str


class HeldMoment(BaseModel):
    name: str
    earliest_ep: int


class Motif(BaseModel):
    name: str
    meaning: str
    episodes: list[int]


class SeasonPlan(BaseModel):
    promise: str
    phases: list[Phase]
    episodes: list[EpisodePlan]
    arcs: list[Arc]
    setups: list[Setup]
    knowledge: list[KnowledgeEvent]
    held_back: list[HeldMoment]
    motifs: list[Motif]
    open_questions: list[str]


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str


def check_season(
    plan: SeasonPlan, outline: list[OutlineEntry], config: SeriesConfig
) -> list[Violation]:
    violations = []
    if outline:
        planned = [(e.number, e.title) for e in plan.episodes]
        expected = [(e.number, e.title) for e in outline]
        if planned != expected:
            different = sorted(set(planned) ^ set(expected)) or ["episode order"]
            violations.append(Violation("outline_mismatch", f"Differs at: {different}"))
    elif [e.number for e in plan.episodes] != list(range(1, config.episodes + 1)):
        violations.append(
            Violation("outline_mismatch", f"Plan must number episodes 1 to {config.episodes}")
        )

    for episode in plan.episodes:
        wall = config.paywalls.get(episode.number, "none")
        if episode.wall != wall:
            violations.append(
                Violation("wall", f"Ep {episode.number} must have wall '{wall}'")
            )

    for setup in plan.setups:
        if setup.payoff_ep < setup.planted_ep:
            violations.append(
                Violation(
                    "payoff_before_setup",
                    f"{setup.id}: paid in ep {setup.payoff_ep}, planted in ep {setup.planted_ep}",
                )
            )

    floors = {moment.name: moment.earliest_ep for moment in plan.held_back}
    for name, earliest in config.held_back.items():
        if floors.get(name) != earliest:
            violations.append(
                Violation("held_back_missing", f"'{name}' must be held until ep {earliest}")
            )
    floors.update(config.held_back)
    for episode in plan.episodes:
        for moment in episode.reveals:
            if moment in floors and episode.number < floors[moment]:
                violations.append(
                    Violation(
                        "held_back_early",
                        f"'{moment}' appears in ep {episode.number}, before ep {floors[moment]}",
                    )
                )

    for event in plan.knowledge:
        for rule in config.knowledge_rules:
            if (
                rule.who in event.who
                and event.topic == rule.topic
                and event.episode < rule.not_before
            ):
                violations.append(
                    Violation(
                        "knowledge_too_early",
                        f"{rule.who} learns '{rule.topic}' in ep {event.episode}, "
                        f"before ep {rule.not_before}",
                    )
                )
    return violations


def render_season_plan(plan: SeasonPlan, title: str) -> str:
    lines = [f"# {title}, season plan", "", plan.promise, "", "## Shape"]
    lines += [
        f"- **Eps {p.first_ep}–{p.last_ep}, {p.name}:** {p.purpose}" for p in plan.phases
    ]
    lines += ["", "## Episodes", "", "| Ep | Title | Job |", "|---|---|---|"]
    markers = {"early": " **← EARLY WALL**", "late": " **← LATE WALL**", "none": ""}
    lines += [
        f"| {e.number} | {e.title} | {e.job}{markers[e.wall]} |" for e in plan.episodes
    ]
    held = ", ".join(f"{m.name} (ep {m.earliest_ep})" for m in plan.held_back)
    lines += ["", f"**Held back:** {held}"]
    lines += [f"**Stays open:** {'; '.join(plan.open_questions)}"]
    return "\n".join(lines) + "\n"
