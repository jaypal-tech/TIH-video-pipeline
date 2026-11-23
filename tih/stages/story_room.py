from pathlib import Path
from typing import Any

import yaml

from tih.llm import LlmContext, ask_json
from tih.series import load_series
from tih.stages.registry import RunContext, Stage, register
from tih.story.critique import (
    EPISODE_CHECKS,
    SEASON_CHECKS,
    TRUTH_CHECKS,
    Comparison,
    EpisodeCritique,
    Revision,
    SeasonCritique,
    TruthCritique,
    describe,
    render_changes,
    render_checks,
    render_critique,
    render_findings,
    review_findings,
    unresolved,
)
from tih.story.models import OutlineEntry
from tih.story.season import (
    EpisodeBatch,
    SeasonPlan,
    SeasonSpine,
    assemble_plan,
    check_season,
    ending_repeats,
    fix_fields,
    render_season_plan,
)

PROMPTS = Path(__file__).parents[2] / "prompts"


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


def _compare(llm: LlmContext, config_block: str, draft: SeasonPlan, revised: SeasonPlan) -> str:
    votes = []
    for take, (first, second, names) in enumerate(
        (
            (draft, revised, {"A": "draft", "B": "revision"}),
            (revised, draft, {"A": "revision", "B": "draft"}),
        )
    ):
        user = (
            config_block
            + _plan_tag("plan_a", first)
            + _plan_tag("plan_b", second)
            + "Which plan is the stronger season?"
        )
        result = ask_json(llm, "compare", _prompt("compare"), user, Comparison, take=take)
        votes.append(names.get(result.better, "tie"))
    return votes[0] if votes[0] == votes[1] else "neither clearly"


def run_story_room(ctx: RunContext) -> None:
    series_dir = Path("series") / ctx.paths.series
    config = load_series(ctx.paths.series)
    source = (series_dir / "source" / "brief.txt").read_text(encoding="utf-8")
    record_dir = ctx.paths.stage_dir("record")
    entries = yaml.safe_load((record_dir / "season_outline.yaml").read_text("utf-8"))
    outline = [OutlineEntry(**entry) for entry in entries]
    llm = LlmContext.for_run(ctx.paths)

    config_block = _tag("series_config", _yaml(config.model_dump(mode="json")))
    source_block = _tag("source", source)
    base = config_block + source_block

    spine = ask_json(
        llm,
        "showrunner_spine",
        _prompt("showrunner_spine"),
        base + f"Plan the spine of the full {config.episodes}-episode season.",
        SeasonSpine,
    )
    expected = list(range(1, config.episodes + 1))
    if [ending.number for ending in spine.endings] != expected:
        raise StoryRoomFailed(f"The spine must list endings for episodes 1 to {config.episodes}")
    spine_block = _tag("season_spine", _yaml(spine.model_dump(mode="json")))

    details = []
    for first in range(1, config.episodes + 1, config.batch_size):
        last = min(first + config.batch_size - 1, config.episodes)
        written = ""
        if details:
            written = _tag(
                "episodes_written_so_far", _yaml([d.model_dump(mode="json") for d in details])
            )
        batch = ask_json(
            llm,
            "showrunner_episodes",
            _prompt("showrunner_episodes"),
            base + spine_block + written + f"Write episodes {first} to {last}.",
            EpisodeBatch,
        )
        if [episode.number for episode in batch.episodes] != list(range(first, last + 1)):
            raise StoryRoomFailed(
                f"The batch for episodes {first} to {last} came back with the wrong episodes"
            )
        details += batch.episodes
    draft = assemble_plan(spine, details, outline, config)
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
        raw, episode_review.ending_labels, draft, check_season(draft, outline, config)
    )

    revision = ask_json(
        llm,
        "showrunner_revise",
        _prompt("showrunner_revise"),
        base + draft_block + _tag("findings", render_findings(findings)) + "Revise the plan.",
        Revision,
    )
    plan = fix_fields(revision.plan, titles, config)
    violations = check_season(plan, outline, config)
    if violations:
        problems = "\n".join(f"- {v.rule}: {v.detail}" for v in violations)
        repaired = ask_json(
            llm,
            "showrunner_repair",
            _prompt("showrunner_revise"),
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

    comparison = _compare(llm, config_block, draft, plan)
    open_items = [describe(finding) for finding in unresolved(findings, revision.changes)]

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
            comparison,
            ending_repeats(draft),
        ),
        encoding="utf-8",
    )
    (stage_dir / "changes.md").write_text(
        render_changes(findings, revision.changes), encoding="utf-8"
    )


register(Stage(name="story_room", requires=("record",), gate=None, run=run_story_room))
