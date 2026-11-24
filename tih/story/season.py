from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from tih.series import SeriesConfig
from tih.story.models import OutlineEntry

EndingType = Literal[
    "revelation", "reversal", "deadline", "intrusion", "threat", "decision", "question"
]
Wall = Literal["none", "early", "late"]


class Phase(BaseModel):
    name: str
    first_ep: int
    last_ep: int
    purpose: str = Field(description="What this movement of the season must achieve.")


class Arc(BaseModel):
    character: str
    goal: str = Field(description="What the character is actively trying to get.")
    opposition: str = Field(description="Who or what stands in the way, and why.")
    starts_as: str
    turns_when: str = Field(description="The choice that changes the character.")
    ends_as: str


class Ending(BaseModel):
    number: int
    title: str
    turn: str = Field(description="The one thing that changes in this episode.")
    ending: str = Field(description="The final moment, which must open a question.")
    ending_type: EndingType


class Setup(BaseModel):
    id: str
    planted_ep: int
    payoff_ep: int
    description: str
    rule: str = Field(description="What later writers must do to protect this setup.")


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


class SeasonSpine(BaseModel):
    season_question: str = Field(
        description="The question a viewer keeps watching to answer. Never 'what happens' "
        "when the audience already knows the outcome."
    )
    theme: str
    promise: str
    phases: list[Phase]
    arcs: list[Arc]
    tentpole_endings: list[Ending] = Field(
        description="The ending of each paywall episode and of the finale, written first."
    )
    endings: list[Ending] = Field(description="Every episode in order, as one chain.")
    setups: list[Setup]
    knowledge: list[KnowledgeEvent]
    held_back: list[HeldMoment]
    motifs: list[Motif]
    open_questions: list[str] = Field(
        description="What the story itself deliberately leaves unresolved."
    )
    risks: list[str] = Field(
        description="Production and sourcing flags, such as a fact that needs a source."
    )


class EpisodeDetail(BaseModel):
    number: int
    opens_with: str = Field(description="How the first seconds pick up the previous ending.")
    hook: str = Field(description="The first 3 seconds: a face, a line or an action.")
    viewer_knows: str = Field(description="What the viewer knows that a character does not.")
    unknown: str = Field(description="The small thing the viewer does not know yet.")
    turn: str = Field(description="The one change, and the choice that causes it.")
    payoff: str = Field(description="What the viewer is given: an answer, a win or a reveal.")
    cliff: str = Field(description="The final moment and the specific threat it leaves.")
    cliff_type: EndingType
    reveals: list[str]
    job: str = Field(description="What this episode changes, in 15 words or fewer.")


class EpisodeBatch(BaseModel):
    episodes: list[EpisodeDetail]


class EpisodePlan(EpisodeDetail):
    title: str
    wall: Wall


class SeasonFields(BaseModel):
    """Every season-level field of a plan, without the episodes."""

    season_question: str
    theme: str
    promise: str
    phases: list[Phase]
    arcs: list[Arc]
    setups: list[Setup]
    knowledge: list[KnowledgeEvent]
    held_back: list[HeldMoment]
    motifs: list[Motif]
    open_questions: list[str]
    risks: list[str]


class SeasonPlan(BaseModel):
    season_question: str
    theme: str
    promise: str
    phases: list[Phase]
    arcs: list[Arc]
    episodes: list[EpisodePlan]
    setups: list[Setup]
    knowledge: list[KnowledgeEvent]
    held_back: list[HeldMoment]
    motifs: list[Motif]
    open_questions: list[str]
    risks: list[str]


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str


def fix_fields(plan: SeasonPlan, titles: dict[int, str], config: SeriesConfig) -> SeasonPlan:
    """Overwrite the fields that code decides: paywalls, and titles when they are fixed."""
    for episode in plan.episodes:
        episode.wall = config.paywalls.get(episode.number, "none")
        episode.title = titles.get(episode.number, episode.title)
    return plan


def assemble_plan(
    spine: SeasonSpine,
    details: list[EpisodeDetail],
    outline: list[OutlineEntry],
    config: SeriesConfig,
) -> SeasonPlan:
    spine_titles = {ending.number: ending.title for ending in spine.endings}
    episodes = [
        EpisodePlan(
            **detail.model_dump(), title=spine_titles.get(detail.number, ""), wall="none"
        )
        for detail in details
    ]
    data = spine.model_dump(exclude={"tentpole_endings", "endings"})
    plan = SeasonPlan(**data, episodes=episodes)
    return fix_fields(plan, {entry.number: entry.title for entry in outline}, config)


def merge_rewrite(
    plan: SeasonPlan,
    episodes: list[EpisodePlan],
    touched: set[int],
    season: dict[str, Any] | None,
) -> SeasonPlan:
    """Return a copy of the plan with only the touched episodes, and the season fields if given, replaced."""
    rewritten = {episode.number: episode for episode in episodes if episode.number in touched}
    data = plan.model_dump(mode="json")
    if season is not None:
        data.update(season)
    data["episodes"] = [
        rewritten[episode.number].model_dump(mode="json")
        if episode.number in rewritten
        else episode.model_dump(mode="json")
        for episode in plan.episodes
    ]
    return SeasonPlan.model_validate(data)


def ending_repeats(plan: SeasonPlan) -> int:
    types = [episode.cliff_type for episode in plan.episodes]
    return sum(1 for first, second in zip(types, types[1:]) if first == second)


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


def render_season_plan(
    plan: SeasonPlan, title: str, unresolved: Sequence[str] = ()
) -> str:
    lines = [f"# {title}, season plan", ""]
    if unresolved:
        lines += ["## Unresolved", *[f"- {item}" for item in unresolved], ""]
    lines += [f"**{plan.season_question}** {plan.promise}", "", "## Shape"]
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
