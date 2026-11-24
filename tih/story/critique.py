from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from tih.story.season import EpisodePlan, SeasonFields, SeasonPlan, Violation


@dataclass(frozen=True)
class Check:
    text: str
    hard: bool


EPISODE_CHECKS = {
    "E1": Check("The hook is a moment: a face, a line or an action, not a date or a setting.", True),
    "E2": Check("Something is different by the end of the episode.", True),
    "E3": Check("That change comes from a character's choice.", False),
    "E5": Check("The ending leaves a specific threat or expectation hanging.", True),
    "E6": Check("The ending could not move to another series unchanged.", False),
    "E7": Check("The opening picks up the previous episode's ending.", True),
    "E8": Check(
        "The episode has its own unknown and does not lean on an outcome the audience "
        "already knows.",
        True,
    ),
    "E9": Check("The viewer gets a payoff: an answer, a win or a reveal.", False),
    "E10": Check("The episode is as specific as the first three episodes.", True),
}
SEASON_CHECKS = {
    "S1": Check("The promise states a clear season question and what is at stake.", True),
    "S2": Check("There is an engine: a source of conflict that keeps producing episodes.", False),
    "S3": Check(
        "Each main character has an active goal, and the opposing force has a motive.", True
    ),
    "S4": Check("Stakes rise from phase to phase.", False),
    "S5": Check(
        "In episodes 1 to 3, the lead's goal is clear in episode 1 and a major event "
        "lands within three episodes.",
        True,
    ),
    "S6": Check("Each paywall ending introduces a threat that was not present before it.", False),
    "S7": Check("Characters act consistently, especially late in the season.", True),
    "S8": Check("Arcs change through choices and are not plot summaries.", False),
    "S9": Check("The tone matches the series config.", False),
    "S10": Check("The plan adds story thinking beyond the source outline.", True),
}
TRUTH_CHECKS = {
    "T1": Check("Nothing stated that the source does not support.", True),
    "T2": Check("Disputed accounts keep their labels.", True),
    "T3": Check("Nothing false is implied by arrangement or omission.", True),
    "T4": Check("No character acts on hindsight.", True),
    "T5": Check("Named people are not shown as guiltier than the source supports.", True),
    "T6": Check("Invented material is marked as dramatized.", True),
    "T7": Check("A perpetrator's acts are never presented as heroic.", True),
}
LABEL_CHECK = "E4"
LABEL_TEXT = "The ending opens a question, and does not state an outcome or resolve the tension."
RULE_CHECK = "RULE"
RULE_TEXT = "A series rule from the config."
CHECKS = {**EPISODE_CHECKS, **SEASON_CHECKS, **TRUTH_CHECKS}


class Finding(BaseModel):
    check: str = Field(description="The ID of the check that failed, such as E5.")
    episode: int = Field(description="The episode number, or 0 for the whole season.")
    quote: str = Field(description="Words copied exactly from the plan that show the problem.")
    problem: str = Field(description="What is wrong, in one or two sentences.")
    verdict: Literal["no", "unclear"]


class EndingLabel(BaseModel):
    number: int
    label: Literal["opens_question", "states_outcome", "resolves"]


class EpisodeCritique(BaseModel):
    findings: list[Finding]
    ending_labels: list[EndingLabel] = Field(description="One label for every episode.")


class SeasonCritique(BaseModel):
    findings: list[Finding]
    top_fixes: list[str] = Field(description="The three most important fixes, in order.")
    verdict: Literal["pass", "consider", "recommend"]


class TruthCritique(BaseModel):
    findings: list[Finding]


class ReviewedFinding(Finding):
    id: str
    hard: bool


class Change(BaseModel):
    finding: str = Field(description="The finding ID, such as F3.")
    action: Literal["fixed", "declined"]
    note: str = Field(description="What changed, or why the plan stands.")


class EpisodeRewrite(BaseModel):
    changes: list[Change] = Field(description="One entry for every finding.")
    episodes: list[EpisodePlan] = Field(
        description="Only the episodes you were asked to rewrite, in full."
    )


class SeasonRewrite(BaseModel):
    changes: list[Change] = Field(description="One entry for every finding.")
    season: SeasonFields


class RecheckResult(BaseModel):
    finding: str = Field(description="The finding ID, such as F3.")
    quote: str = Field(description="Words copied exactly from the revised plan.")
    reason: str = Field(description="Why the revised plan now passes or still fails the check.")
    verdict: Literal["pass", "fail", "unclear"]


class Recheck(BaseModel):
    results: list[RecheckResult] = Field(description="One entry for every finding given.")


def check_text(check_id: str) -> str:
    if check_id == LABEL_CHECK:
        return LABEL_TEXT
    if check_id == RULE_CHECK:
        return RULE_TEXT
    return CHECKS[check_id].text


def render_checks(checks: dict[str, Check]) -> str:
    return "\n".join(f"{check_id}: {check.text}" for check_id, check in checks.items())


def _normal(text: str) -> str:
    return " ".join(text.split()).casefold()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_normal(value)]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def review_findings(
    findings: list[Finding],
    labels: list[EndingLabel],
    plan: SeasonPlan,
    violations: Sequence[Violation] = (),
    titles: Collection[str] = frozenset(),
) -> tuple[list[ReviewedFinding], int]:
    """Keep findings whose check exists and whose quote is in the plan, unless it only quotes a title."""
    plan_text = _strings(plan.model_dump(mode="json"))
    fixed_titles = {_normal(title) for title in titles}
    cliffs = {episode.number: episode.cliff for episode in plan.episodes}
    graded: list[tuple[Finding, bool]] = []
    dropped = 0
    for finding in findings:
        quote = _normal(finding.quote)
        if (
            finding.check in CHECKS
            and quote
            and quote not in fixed_titles
            and any(quote in text for text in plan_text)
        ):
            hard = CHECKS[finding.check].hard and finding.verdict == "no"
            graded.append((finding, hard))
        else:
            dropped += 1
    for label in labels:
        if label.label == "opens_question" or label.number not in cliffs:
            continue
        resolves = label.label == "resolves"
        problem = (
            "The ending resolves the tension instead of opening a question."
            if resolves
            else "The ending states an outcome instead of opening a question."
        )
        graded.append(
            (
                Finding(
                    check=LABEL_CHECK,
                    episode=label.number,
                    quote=cliffs[label.number],
                    problem=problem,
                    verdict="no",
                ),
                resolves,
            )
        )
    for violation in violations:
        graded.append(
            (
                Finding(
                    check=RULE_CHECK,
                    episode=0,
                    quote="",
                    problem=f"{violation.rule}: {violation.detail}",
                    verdict="no",
                ),
                True,
            )
        )
    reviewed = [
        ReviewedFinding(**finding.model_dump(), id=f"F{index}", hard=hard)
        for index, (finding, hard) in enumerate(graded, start=1)
    ]
    return reviewed, dropped


def flagged(
    findings: list[ReviewedFinding], changes: list[Change], recheck: Recheck
) -> list[tuple[ReviewedFinding, str]]:
    """Must-fix findings the owner has to look at: declined, not addressed, or not passed on re-check."""
    by_id = {change.finding: change for change in changes}
    results = {result.finding: result for result in recheck.results}
    out = []
    for finding in findings:
        if not finding.hard:
            continue
        change = by_id.get(finding.id)
        if change is None:
            out.append((finding, "not addressed"))
        elif change.action == "declined":
            out.append((finding, f"declined: {change.note}"))
        elif finding.check == RULE_CHECK:
            continue
        elif finding.id not in results:
            out.append((finding, "not re-checked"))
        elif results[finding.id].verdict != "pass":
            result = results[finding.id]
            out.append((finding, f"re-check {result.verdict}: {result.reason}"))
    return out


def describe(finding: ReviewedFinding) -> str:
    where = f"ep {finding.episode}" if finding.episode else "season"
    return f"{finding.id} ({finding.check}, {where}): {finding.problem}"


def render_findings(findings: list[ReviewedFinding]) -> str:
    lines = []
    for finding in findings:
        kind = "MUST FIX" if finding.hard else "advice"
        lines.append(f"{describe(finding)} [{kind}]")
        if finding.quote:
            lines.append(f'  Quote: "{finding.quote}"')
    return "\n".join(lines)


def render_critique(
    plan: SeasonPlan,
    findings: list[ReviewedFinding],
    dropped: int,
    verdict: str,
    top_fixes: list[str],
    repeats: int,
) -> str:
    hard = sum(1 for finding in findings if finding.hard)
    lines = [
        "# Critique of the draft plan",
        "",
        f"Overall verdict: **{verdict}**",
        f"{hard} must-fix, {len(findings) - hard} advice. "
        f"{dropped} finding{'' if dropped == 1 else 's'} dropped for an unknown check "
        "or a quote not found in the plan.",
        f"The same ending type appears twice in a row {repeats} time{'' if repeats == 1 else 's'}.",
        "",
        "## Top fixes",
        *[f"{index}. {fix}" for index, fix in enumerate(top_fixes, start=1)],
        "",
        "## By episode",
        "",
        "| Ep | Must fix | Advice |",
        "|---|---|---|",
    ]
    for episode in plan.episodes:
        own = [finding for finding in findings if finding.episode == episode.number]
        must = ", ".join(finding.check for finding in own if finding.hard)
        advice = ", ".join(finding.check for finding in own if not finding.hard)
        lines.append(f"| {episode.number} | {must} | {advice} |")
    lines += ["", "## Findings", "", render_findings(findings)]
    return "\n".join(lines) + "\n"


def render_changes(
    findings: list[ReviewedFinding], changes: list[Change], recheck: Recheck
) -> str:
    by_id = {change.finding: change for change in changes}
    results = {result.finding: result for result in recheck.results}
    lines = ["# Must-fix findings: what changed and whether it held", ""]
    for finding in findings:
        if not finding.hard:
            continue
        change = by_id.get(finding.id)
        lines.append(f"- {describe(finding)}")
        lines.append(f"  - **{change.action}**: {change.note}" if change else "  - **not addressed**")
        if change and change.action == "fixed":
            if finding.check == RULE_CHECK:
                lines.append("  - re-check: **checked by code**")
            elif finding.id in results:
                result = results[finding.id]
                lines.append(f"  - re-check: **{result.verdict}**: {result.reason}")
            else:
                lines.append("  - re-check: **missing**")
    return "\n".join(lines) + "\n"
