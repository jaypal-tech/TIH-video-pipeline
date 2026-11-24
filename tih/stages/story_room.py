from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from tih.llm import LlmContext, ask_json
from tih.series import SeriesConfig, load_series
from tih.stages.registry import RunContext, Stage, register
from tih.story.critique import (
    EPISODE_CHECKS,
    RULE_CHECK,
    SEASON_CHECKS,
    TRUTH_CHECKS,
    Change,
    EpisodeCritique,
    EpisodeRewrite,
    Recheck,
    ReviewedFinding,
    SeasonCritique,
    SeasonRewrite,
    TruthCritique,
    check_text,
    describe,
    flagged,
    render_changes,
    render_checks,
    render_critique,
    render_findings,
    review_findings,
)
from tih.story.models import OutlineEntry
from tih.story.season import (
    EpisodeBatch,
    EpisodePlan,
    SeasonPlan,
    SeasonSpine,
    assemble_plan,
    check_season,
    ending_repeats,
    fix_fields,
    merge_rewrite,
    render_season_plan,
)

PROMPTS = Path(__file__).parents[2] / "prompts"
SchemaT = TypeVar("SchemaT", bound=BaseModel)


class StoryRoomFailed(RuntimeError):
    """The Story Room could not produce a plan that passes the series rules."""


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def _yaml(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=10000)


def _tag(name: str, text: str) -> str:
    return f"<{name}>\n{text.rstrip()}\n</{name}>\n\n"


def _plan_tag(name: str, plan: SeasonPlan) -> str:
    return _tag(name, _yaml(plan.model_dump(mode="json")))


def _ask_numbered(
    llm: LlmContext,
    role: str,
    system: str,
    user: str,
    schema: type[SchemaT],
    numbers: Callable[[SchemaT], list[int]],
    expected: list[int],
    what: str,
) -> SchemaT:
    """Ask once, and once more if the reply covers the wrong episodes."""
    for take in range(2):
        result = ask_json(llm, role, system, user, schema, take=take)
        if numbers(result) == expected:
            return result
    raise StoryRoomFailed(f"{what} came back twice with the wrong episodes")


def _draft(
    llm: LlmContext, config: SeriesConfig, base: str, outline: list[OutlineEntry]
) -> SeasonPlan:
    spine = _ask_numbered(
        llm,
        "showrunner_spine",
        _prompt("showrunner_spine"),
        base + f"Plan the spine of the full {config.episodes}-episode season.",
        SeasonSpine,
        lambda result: [ending.number for ending in result.endings],
        list(range(1, config.episodes + 1)),
        f"The spine for episodes 1 to {config.episodes}",
    )
    spine_block = _tag("season_spine", _yaml(spine.model_dump(mode="json")))

    details = []
    for first in range(1, config.episodes + 1, config.batch_size):
        last = min(first + config.batch_size - 1, config.episodes)
        written = ""
        if details:
            written = _tag(
                "episodes_written_so_far", _yaml([d.model_dump(mode="json") for d in details])
            )
        batch = _ask_numbered(
            llm,
            "showrunner_episodes",
            _prompt("showrunner_episodes"),
            base + spine_block + written + f"Write episodes {first} to {last}.",
            EpisodeBatch,
            lambda result: [episode.number for episode in result.episodes],
            list(range(first, last + 1)),
            f"The batch for episodes {first} to {last}",
        )
        details += batch.episodes
    return assemble_plan(spine, details, outline, config)


def _where(finding: ReviewedFinding) -> str:
    return f"ep {finding.episode}" if finding.episode else "season"


def run_story_room(ctx: RunContext) -> None:
    llm = LlmContext.for_run(ctx.paths)
    try:
        _run(ctx, llm)
    finally:
        llm.ledger.export_csv(llm.ledger.path.with_name("calls.csv"))


def _run(ctx: RunContext, llm: LlmContext) -> None:
    series_dir = Path("series") / ctx.paths.series
    config = load_series(ctx.paths.series)
    source = (series_dir / "source" / "brief.txt").read_text(encoding="utf-8")
    record_dir = ctx.paths.stage_dir("record")
    entries = yaml.safe_load((record_dir / "season_outline.yaml").read_text("utf-8"))
    outline = [OutlineEntry(**entry) for entry in entries]

    config_block = _tag("series_config", _yaml(config.model_dump(mode="json")))
    base = config_block + _tag("source", source)

    draft = _draft(llm, config, base, outline)
    titles = {episode.number: episode.title for episode in draft.episodes}
    draft_block = _plan_tag("season_plan", draft)

    critic = _prompt("critic")
    episode_review = ask_json(
        llm,
        "critic_episodes",
        critic,
        config_block
        + draft_block
        + _tag("checks", render_checks(EPISODE_CHECKS))
        + "Review every episode against every check, and label every episode's ending.",
        EpisodeCritique,
    )
    season_review = ask_json(
        llm,
        "critic_season",
        critic,
        base
        + draft_block
        + _tag("checks", render_checks(SEASON_CHECKS))
        + "Review the season as a whole against every check, then give your overall verdict.",
        SeasonCritique,
    )
    raw = episode_review.findings + season_review.findings
    if config.true_story:
        truth_review = ask_json(
            llm,
            "critic_truth",
            critic,
            base
            + draft_block
            + _tag("checks", render_checks(TRUTH_CHECKS))
            + "Review the plan against the source for every check.",
            TruthCritique,
        )
        raw += truth_review.findings
    findings, dropped = review_findings(
        raw,
        episode_review.ending_labels,
        draft,
        check_season(draft, outline, config),
        titles={entry.title for entry in outline},
    )

    hard = [finding for finding in findings if finding.hard]
    episode_hard = [finding for finding in hard if finding.episode]
    season_hard = [finding for finding in hard if not finding.episode]
    touched = sorted({finding.episode for finding in episode_hard})
    revise = _prompt("showrunner_revise")
    changes: list[Change] = []
    rewritten: list[EpisodePlan] = []
    season_fields = None
    if episode_hard:
        listed = ", ".join(str(number) for number in touched)
        episode_fix = ask_json(
            llm,
            "showrunner_fix_episodes",
            revise,
            base
            + draft_block
            + _tag("findings", render_findings(episode_hard))
            + f"Rewrite episodes {listed}. Return a change for every finding, "
            "then only those episodes, in full.",
            EpisodeRewrite,
        )
        changes += episode_fix.changes
        rewritten = episode_fix.episodes
    if season_hard:
        season_fix = ask_json(
            llm,
            "showrunner_fix_season",
            revise,
            base
            + draft_block
            + _tag("findings", render_findings(season_hard))
            + "Fix the season-level fields. Return a change for every finding, "
            "then every season-level field in full.",
            SeasonRewrite,
        )
        changes += season_fix.changes
        season_fields = season_fix.season.model_dump(mode="json")

    plan = fix_fields(merge_rewrite(draft, rewritten, set(touched), season_fields), titles, config)
    violations = check_season(plan, outline, config)
    if violations:
        problems = "\n".join(f"- {v.rule}: {v.detail}" for v in violations)
        repaired = ask_json(
            llm,
            "showrunner_repair",
            revise,
            base
            + _plan_tag("season_plan", plan)
            + _tag("rule_violations", problems)
            + "Return the full plan with these rule violations fixed and nothing else changed.",
            SeasonPlan,
        )
        plan = fix_fields(repaired, titles, config)
        violations = check_season(plan, outline, config)
        if violations:
            problems = "\n".join(f"- {v.rule}: {v.detail}" for v in violations)
            raise StoryRoomFailed(f"Season plan still breaks series rules:\n{problems}")

    decided = {change.finding: change for change in changes}
    to_check = [
        finding
        for finding in hard
        if finding.check != RULE_CHECK
        and finding.id in decided
        and decided[finding.id].action == "fixed"
    ]
    recheck = Recheck(results=[])
    if to_check:
        listing = "\n".join(
            f"{finding.id} ({finding.check}, {_where(finding)}): {check_text(finding.check)}\n"
            f"  Problem found: {finding.problem}\n"
            f"  Fix made: {decided[finding.id].note}"
            for finding in to_check
        )
        recheck = ask_json(
            llm,
            "critic_recheck",
            critic,
            base
            + _plan_tag("season_plan", plan)
            + _tag("recheck", listing)
            + "Re-check each finding against the revised plan.",
            Recheck,
        )
    open_items = [
        f"{describe(finding)} ({why})" for finding, why in flagged(findings, changes, recheck)
    ]

    data = plan.model_dump(mode="json")
    stage_dir = ctx.paths.stage_dir("story_room")
    (stage_dir / "season_plan.md").write_text(
        render_season_plan(plan, config.title, open_items), encoding="utf-8"
    )
    (stage_dir / "season_plan.yaml").write_text(_yaml(data), encoding="utf-8")
    (stage_dir / "draft_plan.yaml").write_text(
        _yaml(draft.model_dump(mode="json")), encoding="utf-8"
    )
    (stage_dir / "season_ledger.yaml").write_text(
        _yaml({key: data[key] for key in ("setups", "knowledge", "held_back")}),
        encoding="utf-8",
    )
    bible_keys = ("season_question", "theme", "promise", "arcs", "motifs", "open_questions")
    (stage_dir / "bible.yaml").write_text(
        _yaml({key: data[key] for key in bible_keys}), encoding="utf-8"
    )
    (stage_dir / "risks.yaml").write_text(_yaml(data["risks"]), encoding="utf-8")
    (stage_dir / "critique.md").write_text(
        render_critique(
            draft,
            findings,
            dropped,
            season_review.verdict,
            season_review.top_fixes,
            ending_repeats(draft),
        ),
        encoding="utf-8",
    )
    (stage_dir / "changes.md").write_text(
        render_changes(findings, changes, recheck), encoding="utf-8"
    )


register(Stage(name="story_room", requires=("record",), gate=None, run=run_story_room))
