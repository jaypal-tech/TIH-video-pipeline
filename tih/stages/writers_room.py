from pathlib import Path

import yaml

from tih.gates import require_approved
from tih.llm import LlmContext, ask_json
from tih.paths import RunPaths
from tih.series import SeriesConfig, load_series
from tih.stages.registry import RunContext, Stage, register
from tih.stages.story_room import PROMPTS, _tag, _yaml
from tih.story.models import Canon, Episode
from tih.story.script import PROTECTED, WriterResult, check_script, render_script, render_script_changes
from tih.story.season import SeasonPlan


class WritersRoomFailed(RuntimeError):
    """The Writer could not produce a script that passes the hard checks."""


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _drafts(season: RunPaths) -> list[Episode]:
    return [Episode(**e) for e in _load(season.stage_dir("record") / "script.yaml") or []]


def _speakers(drafts: list[Episode]) -> dict[str, str]:
    """Who said each real or reported line, as the source's drafts record it."""
    return {
        beat.line.text: beat.line.speaker
        for draft in drafts
        for clip in draft.clips
        for beat in clip.beats
        if beat.line and beat.line.evidence in PROTECTED
    }


def _brief(config: SeriesConfig, source: str, plan: SeasonPlan, number: int, canon: Canon, draft: Episode | None) -> str:
    by_number = {episode.number: episode for episode in plan.episodes}
    text = _tag("series_config", _yaml(config.model_dump(mode="json")))
    text += _tag("source", source)
    text += _tag("episode_plan", _yaml(by_number[number].model_dump(mode="json")))
    if number - 1 in by_number:
        text += _tag("previous_episode", _yaml(by_number[number - 1].model_dump(mode="json")))
    if number + 1 in by_number:
        text += _tag("next_episode", _yaml(by_number[number + 1].model_dump(mode="json")))
    ledger = {
        "setups": [
            s.model_dump(mode="json") for s in plan.setups if number in (s.planted_ep, s.payoff_ep)
        ],
        "knowledge": [k.model_dump(mode="json") for k in plan.knowledge if k.episode == number],
        "held_back": [h.model_dump(mode="json") for h in plan.held_back],
    }
    text += _tag("season_ledger", _yaml(ledger))
    text += _tag(
        "canon",
        _yaml({"real_words": canon.real_words, "reported": canon.reported, "true_cards": canon.true_cards}),
    )
    if config.script_notes.get(number):
        text += _tag("script_notes", _yaml(config.script_notes[number]))
    if draft is not None:
        text += _tag("draft_script", _yaml(draft.model_dump(mode="json")))
    return text


def run_writers_room(ctx: RunContext) -> None:
    llm = LlmContext.for_run(ctx.paths)
    try:
        _run(ctx, llm)
    finally:
        llm.ledger.export_csv(llm.ledger.path.with_name("calls.csv"))


def _run(ctx: RunContext, llm: LlmContext) -> None:
    number = ctx.paths.episode
    season = RunPaths(ctx.paths.series, None, root=ctx.paths.root)
    require_approved(season, "story")
    plan = SeasonPlan(**_load(season.stage_dir("story_room") / "season_plan.yaml"))
    if number not in {episode.number for episode in plan.episodes}:
        raise WritersRoomFailed(f"Episode {number} is not in the season plan")
    config = load_series(ctx.paths.series)
    source = (Path("series") / ctx.paths.series / "source" / "brief.txt").read_text("utf-8")
    canon = Canon(**_load(season.stage_dir("record") / "canon.yaml"))
    drafts = _drafts(season)
    draft = next((d for d in drafts if d.number == number), None)
    speakers = _speakers(drafts)

    base = _brief(config, source, plan, number, canon, draft)
    task = (
        f"Sharpen the draft script for episode {number}."
        if draft is not None
        else f"Write the script for episode {number} from the plan."
    )
    system = (PROMPTS / "writer.md").read_text(encoding="utf-8")
    result = ask_json(llm, "writer", system, base + task, WriterResult)
    violations = check_script(result.script, number, canon, config, draft, speakers)
    if violations:
        problems = "\n".join(f"- {v.rule}: {v.detail}" for v in violations)
        result = ask_json(
            llm,
            "writer_repair",
            system,
            base
            + _tag("script", _yaml(result.script.model_dump(mode="json")))
            + _tag("violations", problems)
            + f"{task} Fix these violations in the script and change nothing else. "
            "Return the full script and the full change list.",
            WriterResult,
        )
        violations = check_script(result.script, number, canon, config, draft, speakers)
        if violations:
            problems = "\n".join(f"- {v.rule}: {v.detail}" for v in violations)
            raise WritersRoomFailed(f"Script still breaks the rules:\n{problems}")

    stage_dir = ctx.paths.stage_dir("writers_room")
    (stage_dir / "script.yaml").write_text(
        _yaml(result.script.model_dump(mode="json")), encoding="utf-8"
    )
    (stage_dir / "script.md").write_text(render_script(result.script), encoding="utf-8")
    (stage_dir / "changes.md").write_text(
        render_script_changes(result.script.title, result.changes, draft is not None),
        encoding="utf-8",
    )


register(Stage(name="writers_room", requires=(), gate=None, run=run_writers_room))
