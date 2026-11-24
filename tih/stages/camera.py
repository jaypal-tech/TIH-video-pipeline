"""Stage: keyframes. One still per shot, built only from locked looks.

Paid. Each shot's prompt is the shot plan's keyframe text with bible tokens
expanded, plus the style block; the locked reference image of every
character in the shot is attached, and the first character's locked seed is
used. Shots with nobody in them use the text-only step.
"""

from pathlib import Path

from tih.assets import ShotRef
from tih.bible import load_locks, require_look
from tih.gates import require_approved
from tih.production import (
    TakeSet,
    call_step,
    choose,
    gateway_for,
    load_pipeline,
    load_resolved,
    seed_for,
    take_from,
    write_takes,
)
from tih.providers.fal import upload_local
from tih.stages.registry import RunContext, Stage, register


def run_keyframes(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    require_approved(ctx.paths, "look")
    locks = load_locks(ctx.paths.series)
    plan = load_resolved(ctx)
    steps = load_pipeline(ctx.paths.series).steps
    gateway = gateway_for(ctx)

    sets: dict[str, TakeSet] = {}
    for shot in plan.shots:
        character_locks = [require_look(locks, c) for c in shot.cast]
        references = [upload_local(gateway, Path(lock.reference)) for lock in character_locks]
        step = "keyframe" if references else "keyframe_no_refs"
        config = steps[step]
        base_seed = next((lock.seed for lock in character_locks if lock.seed is not None), None)
        takes = []
        for take in range(config.takes):
            result = call_step(
                gateway,
                config,
                "keyframe",
                {
                    "prompt": shot.keyframe_prompt,
                    "negative_prompt": shot.negative_prompt,
                    "image_urls": references or None,
                    "seed": seed_for(base_seed, take),
                },
                ShotRef(plan.episode, shot.clip, shot.id),
                take=take,
            )
            takes.append(take_from(result, take))
        sets[shot.id] = choose(ctx, gateway.ledger, "keyframes", shot.id, takes)
    write_takes(ctx.paths.stage_dir("keyframes"), sets)


register(Stage(name="keyframes", requires=("shotplan",), gate="look", run=run_keyframes))
