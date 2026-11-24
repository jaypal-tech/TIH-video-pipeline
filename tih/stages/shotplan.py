"""Stage: shotplan. Check the episode's shot plan and write the resolved shots.

Free (no API calls). Fails with a list of every mismatch between the shot
plan, the script from `record`, and the bible, so nothing is generated from a
plan that would put wrong words on screen or an unknown face in a shot.
"""

import yaml

from tih.bible import load_bible
from tih.shots import load_plan, resolve_plan
from tih.stages.registry import RunContext, Stage, register
from tih.story.models import Episode


def _summary(plan) -> str:
    rows = ["| Shot | Start | Dur | Size | Cast | Line | Lip-sync |", "|---|---|---|---|---|---|---|"]
    for shot in plan.shots:
        line = f"{shot.speaker}: {shot.line}" if shot.line else ""
        rows.append(
            f"| {shot.id} | {shot.start_s:.1f} | {shot.duration:.1f} | {shot.size} "
            f"{shot.lens_mm}mm | {', '.join(shot.cast)} | {line} | "
            f"{'yes' if shot.needs_lipsync else ''} |"
        )
    lipsync = sum(shot.needs_lipsync for shot in plan.shots)
    head = (
        f"# Episode {plan.episode} shot plan\n\n"
        f"{len(plan.shots)} shots, {plan.runtime_s:.1f} s, {lipsync} lip-sync shots, "
        f"{len(plan.cues)} sound cues.\n\n"
    )
    return head + "\n".join(rows) + "\n"


def run_shotplan(ctx: RunContext) -> None:
    script_file = ctx.paths.scope_dir / "record" / "script.yaml"
    episode = Episode.model_validate(yaml.safe_load(script_file.read_text(encoding="utf-8")))
    bible = load_bible(ctx.paths.series)
    plan = resolve_plan(load_plan(ctx.paths.series, ctx.paths.episode), episode, bible)
    stage_dir = ctx.paths.stage_dir("shotplan")
    (stage_dir / "shots.yaml").write_text(
        yaml.safe_dump(plan.model_dump(mode="json"), allow_unicode=True, sort_keys=False, width=10000),
        encoding="utf-8",
    )
    (stage_dir / "plan.md").write_text(_summary(plan), encoding="utf-8")


register(Stage(name="shotplan", requires=("record",), gate=None, run=run_shotplan))
