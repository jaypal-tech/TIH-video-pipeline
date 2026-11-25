"""Stage: plates. One empty view of every place in the episode, to lock.

Paid. Each place in the shot plan that has no locked plate gets a few takes of
an empty establishing still. The owner locks one with `tih lock place`; every
keyframe at that place then carries it as a labelled reference, so the room,
light and weather hold from shot to shot.
"""

from tih.assets import ShotRef
from tih.bible import load_bible, load_locks
from tih.briefs import plate_brief
from tih.gates import require_approved
from tih.production import TakeSet, call_step, gateway_for, load_pipeline, load_resolved, take_from, write_takes
from tih.stages.registry import RunContext, Stage, register

PLATE_SEED = 1865


def run_plates(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    series = ctx.paths.series
    bible = load_bible(series)
    locks = load_locks(series)
    plan = load_resolved(ctx)
    config = load_pipeline(series).steps["plate"]
    gateway = gateway_for(ctx)

    sets: dict[str, TakeSet] = {}
    for place in dict.fromkeys(shot.place for shot in plan.shots):
        if place in locks.places or not bible.places[place].plate:
            continue
        takes = []
        for take in range(config.takes):
            result = call_step(
                gateway,
                config,
                "plate",
                {**plate_brief(bible.places[place], bible), "seed": PLATE_SEED + take},
                ShotRef(episode=None, shot=place, step="plate"),
                take=take,
            )
            takes.append(take_from(result, take))
        sets[f"plate:{place}"] = TakeSet(takes=takes)
    write_takes(ctx.paths.stage_dir("plates"), sets)


register(Stage(name="plates", requires=("shotplan",), gate="script", run=run_plates))
