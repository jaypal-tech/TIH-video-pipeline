"""Stage: motion. Turn each chosen keyframe into a video clip.

Paid. Asks the model for the shortest duration it supports that covers the
shot (slow-motion shots need less source; frozen tails need none). The cut
stage trims each clip to its exact length.
"""

import math
from pathlib import Path

from tih.assets import ShotRef
from tih.bible import Locks, load_bible, load_locks, require_look
from tih.briefs import motion_brief
from tih.gates import require_approved
from tih.production import (
    TakeSet,
    call_step,
    choose,
    gateway_for,
    load_pipeline,
    load_resolved,
    read_takes,
    take_from,
    write_takes,
)
from tih.providers.fal import GatewayContext, upload_local
from tih.shots import ResolvedShot
from tih.stages.registry import RunContext, Stage, register


def source_seconds(duration: float, speed: float, freeze_at: float | None) -> float:
    """How many seconds of generated motion a shot actually shows."""
    moving = duration if freeze_at is None else min(freeze_at, duration)
    return moving * speed


def model_duration(needed: float, allowed: tuple[int, int] | None) -> int:
    """Whole seconds to request: the shot's own length, inside the model's range."""
    seconds = math.ceil(needed)
    if allowed is None:
        return seconds
    low, high = allowed
    return min(max(seconds, low), high)


def motion_elements(gateway: GatewayContext, locks: Locks, shot: ResolvedShot) -> list[dict] | None:
    """Each cast member's locked look as a Kling element, in cast order; None with no cast."""
    if not shot.cast:
        return None
    elements = []
    for character in shot.cast:
        url = upload_local(gateway, Path(require_look(locks, character).reference))
        elements.append({"frontal_image_url": url, "reference_image_urls": [url]})
    return elements


def run_motion(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    require_approved(ctx.paths, "look")
    plan = load_resolved(ctx)
    keyframes = read_takes(ctx, "keyframes")
    bible = load_bible(ctx.paths.series)
    locks = load_locks(ctx.paths.series)
    config = load_pipeline(ctx.paths.series).steps["motion"]
    gateway = gateway_for(ctx)

    sets: dict[str, TakeSet] = {}
    for shot in plan.shots:
        image_url = upload_local(gateway, Path(keyframes[shot.id].chosen))
        seconds = model_duration(
            source_seconds(shot.duration, shot.speed, shot.freeze_at), config.duration_range
        )
        takes = []
        for take in range(config.takes):
            result = call_step(
                gateway,
                config,
                "video",
                {
                    **motion_brief(shot, bible),
                    "image_url": image_url,
                    "elements": motion_elements(gateway, locks, shot),
                    "duration": str(seconds),
                },
                ShotRef(plan.episode, shot.clip, shot.id),
                take=take,
            )
            takes.append(take_from(result, take))
        sets[shot.id] = choose(ctx, gateway.ledger, "motion", shot.id, takes)
    write_takes(ctx.paths.stage_dir("motion"), sets)


register(Stage(name="motion", requires=("keyframes",), gate="look", run=run_motion))
