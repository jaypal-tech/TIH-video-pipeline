"""Stage: sound. Generate the music and effects cues listed in the shot plan.

Paid (small). Each cue says when it plays, for how long, how loud, and what
it should sound like; `kind` picks the music or sfx model in pipeline.yaml.
"""

import math

from tih.assets import ShotRef
from tih.bible import load_bible
from tih.briefs import cue_brief
from tih.gates import require_approved
from tih.production import (
    TakeSet,
    call_step,
    choose,
    gateway_for,
    load_pipeline,
    load_resolved,
    take_from,
    write_takes,
)
from tih.stages.registry import RunContext, Stage, register

SFX_MAX_SECONDS = 22.0


def run_sound(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    require_approved(ctx.paths, "look")
    plan = load_resolved(ctx)
    steps = load_pipeline(ctx.paths.series).steps
    bible = load_bible(ctx.paths.series)
    gateway = gateway_for(ctx)

    sets: dict[str, TakeSet] = {}
    for cue in plan.cues:
        config = steps[cue.kind]
        values = {
            "prompt": cue_brief(cue, bible),
            "length_ms": int(math.ceil(cue.duration * 1000)),
            "duration_seconds": min(max(cue.duration, 0.5), SFX_MAX_SECONDS),
        }
        result = call_step(
            gateway,
            config,
            cue.kind,
            values,
            ShotRef(plan.episode, None, cue.id),
            # music is billed per minute, effects per second
            quantity=cue.duration / 60 if cue.kind == "music" else values["duration_seconds"],
        )
        sets[cue.id] = choose(ctx, gateway.ledger, "sound", cue.id, [take_from(result, 0)])
    write_takes(ctx.paths.stage_dir("sound"), sets)


register(Stage(name="sound", requires=("shotplan",), gate="look", run=run_sound))
