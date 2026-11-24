"""Stage: shotplan. Check the Director's shot plan and write the resolved shots.

Free (no API calls). Fails with a list of every mismatch between the shot
plan, the locked Writer script, and the bible, so nothing is generated from a
plan that would put wrong words on screen or an unknown face in a shot.
"""

import yaml

from tih.bible import load_bible
from tih.series import load_series
from tih.shots import load_plan, plan_path, render_plan, resolve_plan
from tih.stages.registry import RunContext, Stage, register
from tih.story.models import Episode


def run_shotplan(ctx: RunContext) -> None:
    script_file = ctx.paths.scope_dir / "writers_room" / "script.yaml"
    episode = Episode.model_validate(yaml.safe_load(script_file.read_text(encoding="utf-8")))
    bible = load_bible(ctx.paths.series)
    runtime = load_series(ctx.paths.series).runtime_s
    plan = resolve_plan(load_plan(plan_path(ctx.paths)), episode, bible, runtime)
    stage_dir = ctx.paths.stage_dir("shotplan")
    (stage_dir / "shots.yaml").write_text(
        yaml.safe_dump(plan.model_dump(mode="json"), allow_unicode=True, sort_keys=False, width=10000),
        encoding="utf-8",
    )
    (stage_dir / "plan.md").write_text(render_plan(plan), encoding="utf-8")


register(Stage(name="shotplan", requires=("director",), gate=None, run=run_shotplan))
