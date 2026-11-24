"""Stage: director. Write the episode's shot plan from the locked script.

Claude writes the plan; `resolve_plan` checks it against the script, the bible
and the series runtime. One repair call on failure, then the stage stops with
the list of problems, so no picture is ever generated from a wrong plan.
"""

from pathlib import Path

import yaml

from tih.bible import load_bible, series_dir
from tih.gates import require_approved
from tih.llm import LlmContext, ask_json
from tih.series import SeriesConfig, load_series
from tih.shots import PlanError, ShotPlan, example_plan_path, plan_path, render_plan, resolve_plan
from tih.stages.registry import RunContext, Stage, register
from tih.stages.story_room import PROMPTS, _tag, _yaml
from tih.story.models import Episode


class DirectorFailed(RuntimeError):
    """The Director could not produce a shot plan that passes the checks."""


def _brief(config: SeriesConfig, bible_text: str, example: str | None, script: Episode) -> str:
    text = _tag("series_config", _yaml(config.model_dump(mode="json")))
    text += _tag("bible", bible_text)
    if example is not None:
        text += _tag("example_plan", example)
    text += _tag("script", _yaml(script.model_dump(mode="json")))
    return text


def _problems(plan: ShotPlan, number: int, script: Episode, bible, runtime: tuple[int, int]) -> str | None:
    """The checker's problem list for this plan, or None when it passes."""
    problems = []
    if plan.episode != number:
        problems.append(f"- the plan is for episode {plan.episode}; it must be episode {number}")
    try:
        resolve_plan(plan, script, bible, runtime)
    except PlanError as error:
        problems.append(str(error))
    return "\n".join(problems) or None


def run_director(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    llm = LlmContext.for_run(ctx.paths)
    try:
        _run(ctx, llm)
    finally:
        llm.ledger.export_csv(llm.ledger.path.with_name("calls.csv"))


def _run(ctx: RunContext, llm: LlmContext) -> None:
    number = ctx.paths.episode
    series = ctx.paths.series
    script_file = ctx.paths.scope_dir / "writers_room" / "script.yaml"
    script = Episode.model_validate(yaml.safe_load(script_file.read_text(encoding="utf-8")))
    config = load_series(series)
    bible = load_bible(series)
    example_file = example_plan_path(series)
    example = example_file.read_text(encoding="utf-8") if example_file.exists() else None
    bible_text = (series_dir(series) / "bible.yaml").read_text(encoding="utf-8")

    base = _brief(config, bible_text, example, script)
    task = f"Write the shot plan for episode {number}."
    system = (PROMPTS / "director.md").read_text(encoding="utf-8")
    plan = ask_json(llm, "director", system, base + task, ShotPlan)
    problems = _problems(plan, number, script, bible, config.runtime_s)
    if problems:
        plan = ask_json(
            llm,
            "director_repair",
            system,
            base
            + _tag("shot_plan", _yaml(plan.model_dump(mode="json")))
            + _tag("problems", problems)
            + f"{task} Fix these problems and change nothing else. Return the full plan.",
            ShotPlan,
        )
        problems = _problems(plan, number, script, bible, config.runtime_s)
        if problems:
            raise DirectorFailed(f"Shot plan still has problems:\n{problems}")

    resolved = resolve_plan(plan, script, bible, config.runtime_s)
    target = plan_path(ctx.paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_yaml(plan.model_dump(mode="json")), encoding="utf-8")
    (target.parent / "plan.md").write_text(render_plan(resolved), encoding="utf-8")


register(Stage(name="director", requires=("writers_room",), gate="script", run=run_director))
