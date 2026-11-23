from pathlib import Path
from typing import Any

import yaml

from tih.llm import LlmContext, ask_json
from tih.series import load_series
from tih.stages.registry import RunContext, Stage, register
from tih.story.models import OutlineEntry
from tih.story.season import SeasonPlan, check_season, render_season_plan

ATTEMPTS = 2
PROMPTS = Path(__file__).parents[2] / "prompts"


class StoryRoomFailed(RuntimeError):
    """The season plan still breaks a story rule after one revision."""


def _write_yaml(path: Path, data: Any) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=10000),
        encoding="utf-8",
    )


def run_story_room(ctx: RunContext) -> None:
    series_dir = Path("series") / ctx.paths.series
    config = load_series(ctx.paths.series)
    source = (series_dir / "source" / "brief.txt").read_text(encoding="utf-8")
    system = (PROMPTS / "showrunner.md").read_text(encoding="utf-8")
    record_dir = ctx.paths.stage_dir("record")
    entries = yaml.safe_load((record_dir / "season_outline.yaml").read_text("utf-8"))
    outline = [OutlineEntry(**entry) for entry in entries]

    llm = LlmContext.for_run(ctx.paths)
    config_text = yaml.safe_dump(
        config.model_dump(mode="json"), allow_unicode=True, sort_keys=False, width=10000
    )
    user = (
        f"<series_config>\n{config_text}</series_config>\n\n"
        f"<source>\n{source}\n</source>\n\n"
        f"Plan the full {config.episodes}-episode season."
    )
    feedback = ""
    for take in range(ATTEMPTS):
        plan = ask_json(llm, "showrunner", system, user + feedback, SeasonPlan, take=take)
        violations = check_season(plan, outline, config)
        if not violations:
            break
        problems = "\n".join(f"- {v.rule}: {v.detail}" for v in violations)
        feedback = f"\n\nYour previous plan broke these rules. Fix them:\n{problems}"
    else:
        raise StoryRoomFailed(f"Season plan still has problems:\n{problems}")

    data = plan.model_dump(mode="json")
    stage_dir = ctx.paths.stage_dir("story_room")
    (stage_dir / "season_plan.md").write_text(
        render_season_plan(plan, config.title), encoding="utf-8"
    )
    _write_yaml(stage_dir / "season_plan.yaml", data)
    _write_yaml(
        stage_dir / "season_ledger.yaml",
        {key: data[key] for key in ("setups", "knowledge", "held_back")},
    )
    _write_yaml(
        stage_dir / "bible.yaml",
        {key: data[key] for key in ("promise", "arcs", "motifs", "open_questions")},
    )


register(Stage(name="story_room", requires=("record",), gate=None, run=run_story_room))
