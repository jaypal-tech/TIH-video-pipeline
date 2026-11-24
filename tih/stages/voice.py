"""Stage: voice. Speak every line with the character's locked voice.

Paid. Each take is transcribed and compared word for word with the locked
script. A take that adds, drops or changes a word is rejected with the
difference as its reason, and the next take is tried. This is what protects
REAL WORDS lines: the caption comes from the script, and the audio must match
it. An ellipsis is sent as a pause, never as a word.
"""

import re
from pathlib import Path

from tih.assets import ShotRef
from tih.bible import load_locks, require_voice
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
from tih.providers.fal import upload_local
from tih.stages.registry import RunContext, Stage, register

WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")


def words(text: str) -> list[str]:
    text = text.lower().replace("’", "'").replace("‘", "'")
    return WORD.findall(text)


def word_difference(expected: str, heard: str) -> str | None:
    """None if the same words were spoken in order, else a short description."""
    want, got = words(expected), words(heard)
    if want == got:
        return None
    return f"expected {' '.join(want)!r}, heard {' '.join(got)!r}"


def speech_text(line: str, pause_token: str | None) -> str:
    if pause_token is None:
        return line
    return re.sub(r"\s*(…|\.\.\.)\s*", f" {pause_token} ", line).strip()


def run_voice(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    require_approved(ctx.paths, "look")
    locks = load_locks(ctx.paths.series)
    plan = load_resolved(ctx)
    steps = load_pipeline(ctx.paths.series).steps
    speech, transcribe = steps["speech"], steps["transcribe"]
    gateway = gateway_for(ctx)

    sets: dict[str, TakeSet] = {}
    for shot in plan.shots:
        if shot.line is None:
            continue
        lock = require_voice(locks, shot.speaker)
        ref = ShotRef(plan.episode, shot.clip, shot.id)
        takes = []
        for take in range(speech.takes):
            result = call_step(
                gateway,
                speech,
                "voice",
                {"text": speech_text(shot.line, speech.pause_token), "voice_id": lock.voice_id},
                ref,
                take=take,
            )
            reason = None
            if result.status == "ok" and result.output_paths:
                heard = call_step(
                    gateway,
                    transcribe,
                    "transcribe",
                    {"audio_url": upload_local(gateway, Path(result.output_paths[0]))},
                    ref,
                    take=take,
                    expects_files=False,
                )
                transcript = str(heard.payload.get("text", "")) if heard.status == "ok" else ""
                reason = word_difference(shot.line, transcript)
            takes.append(take_from(result, take, reason=reason))
            if takes[-1].status == "ok" and takes[-1].reason is None:
                break
        sets[shot.id] = choose(ctx, gateway.ledger, "voice", shot.id, takes)
    write_takes(ctx.paths.stage_dir("voice"), sets)


register(Stage(name="voice", requires=("shotplan",), gate="look", run=run_voice))
