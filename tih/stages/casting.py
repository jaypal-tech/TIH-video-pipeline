"""Stage: casting. Candidate reference sheets and voices for this episode's cast.

Paid. Only characters that are not locked yet are cast, so a character cast
in episode 1 costs nothing in episode 2. The owner listens and looks, then
locks one of each with `tih lock look` and `tih lock voice`; that choice is
what every later shot uses.
"""

from tih.assets import ShotRef
from tih.bible import character_text, load_bible, load_locks
from tih.gates import require_approved
from tih.production import (
    TakeSet,
    call_step,
    gateway_for,
    load_pipeline,
    load_resolved,
    take_from,
    write_takes,
)
from tih.stages.registry import RunContext, Stage, register

VOICE_ID_KEYS = ("custom_voice_id", "voice_id", "voice")
BASE_SEED = 1865


def _voice_id(payload: dict) -> str | None:
    for key in VOICE_ID_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def run_casting(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    series = ctx.paths.series
    bible = load_bible(series)
    locks = load_locks(series)
    plan = load_resolved(ctx)
    steps = load_pipeline(series).steps
    gateway = gateway_for(ctx)

    on_screen = sorted({c for shot in plan.shots for c in shot.cast})
    speakers = sorted({shot.speaker for shot in plan.shots if shot.speaker})
    first_line = {}
    for shot in plan.shots:
        if shot.speaker and shot.speaker not in first_line:
            first_line[shot.speaker] = shot.line

    sets: dict[str, TakeSet] = {}
    for character_id in on_screen:
        if locks.characters.get(character_id, None) and locks.characters[character_id].reference:
            continue
        config = steps["look"]
        prompt = bible.style.reference_sheet.format(look=character_text(bible, character_id))
        takes = []
        for take in range(config.takes):
            result = call_step(
                gateway,
                config,
                "look",
                {
                    "prompt": prompt,
                    "negative_prompt": bible.style.negative,
                    "seed": BASE_SEED + take,
                },
                ShotRef(episode=None, shot=character_id, step="look"),
                take=take,
            )
            takes.append(take_from(result, take, note=f"seed {BASE_SEED + take}"))
        sets[f"look:{character_id}"] = TakeSet(takes=takes)

    for character_id in speakers:
        lock = locks.characters.get(character_id)
        if lock and lock.voice_id:
            continue
        config = steps["voice_design"]
        takes = []
        for take in range(config.takes):
            result = call_step(
                gateway,
                config,
                "voice_design",
                {
                    "voice_description": bible.characters[character_id].voice,
                    "preview_text": first_line[character_id],
                },
                ShotRef(episode=None, shot=character_id, step="voice_design"),
                take=take,
            )
            voice_id = _voice_id(result.payload)
            takes.append(take_from(result, take, note=f"voice id {voice_id}" if voice_id else None))
        sets[f"voice:{character_id}"] = TakeSet(takes=takes)

    write_takes(ctx.paths.stage_dir("casting"), sets)


register(Stage(name="casting", requires=("shotplan",), gate="script", run=run_casting))
