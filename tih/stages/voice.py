"""Stage: voice. Speak every line with the character's locked voice.

Paid. Each script line is spoken once, whole, even when the Director splits it
across shots, so the read keeps one breath and one tone. Scribe's word timings
are saved beside the take; lip-sync cuts each shot's own words from them. Each take is transcribed and compared word for word with the locked
script. A take that adds, drops or changes a word is rejected with the
difference as its reason, and the next take is tried. This is what protects
REAL WORDS lines: the caption comes from the script, and the audio must match
it. An ellipsis is sent as a pause, never as a word.
"""

import json
import re
from pathlib import Path

from tih.assets import ShotRef
from tih.bible import load_locks, require_voice
from tih.briefs import speech_settings
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
from tih.shots import ResolvedPlan, ResolvedShot
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


def word_timings_path(audio: Path) -> Path:
    """Scribe's word timings for one voice take, kept beside its audio."""
    return audio.with_suffix(".words.json")


def spoken_words(timings: list[dict]) -> list[dict]:
    return [w for w in timings if w.get("type", "word") == "word" and words(str(w.get("text", "")))]


def slice_for_shot(timings: list[dict], pieces: list[str], index: int) -> tuple[float, float]:
    """Start and end seconds of piece `index` of a line, from the word timings.

    A piece runs from its first word to the next piece's first word, so the
    breath between pieces stays with the earlier shot; the last piece ends on
    its last word.
    """
    timed = spoken_words(timings)
    first = sum(len(words(piece)) for piece in pieces[:index])
    count = len(words(pieces[index]))
    if first + count > len(timed):
        raise ValueError(f"word timings cover {len(timed)} words; piece {index + 1} needs {first + count}")
    start = float(timed[first]["start"])
    if first + count < len(timed):
        return start, float(timed[first + count]["start"])
    return start, float(timed[-1]["end"])


def line_shots(plan: ResolvedPlan) -> dict[str, list[ResolvedShot]]:
    """Every beat with a line, and its shots that carry a piece of it, in order."""
    by_beat: dict[str, list[ResolvedShot]] = {}
    for shot in plan.shots:
        if shot.line is not None:
            by_beat.setdefault(shot.beat, []).append(shot)
    return by_beat


def run_voice(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    require_approved(ctx.paths, "look")
    locks = load_locks(ctx.paths.series)
    plan = load_resolved(ctx)
    steps = load_pipeline(ctx.paths.series).steps
    speech, transcribe = steps["speech"], steps["transcribe"]
    gateway = gateway_for(ctx)

    sets: dict[str, TakeSet] = {}
    for beat, shots in line_shots(plan).items():
        first = shots[0]
        line = " ".join(shot.line for shot in shots)
        lock = require_voice(locks, first.speaker)
        ref = ShotRef(plan.episode, first.clip, beat)
        takes = []
        for take in range(speech.takes):
            result = call_step(
                gateway,
                speech,
                "voice",
                {
                    "text": speech_text(line, speech.pause_token),
                    "voice_id": lock.voice_id,
                    **speech_settings(first.direction),
                },
                ref,
                take=take,
            )
            reason = None
            if result.status == "ok" and result.output_paths:
                audio = Path(result.output_paths[0])
                heard = call_step(
                    gateway,
                    transcribe,
                    "transcribe",
                    {"audio_url": upload_local(gateway, audio)},
                    ref,
                    take=take,
                    expects_files=False,
                )
                transcript = str(heard.payload.get("text", "")) if heard.status == "ok" else ""
                reason = word_difference(line, transcript)
                word_timings_path(audio).write_text(
                    json.dumps(heard.payload.get("words", []), indent=1), encoding="utf-8"
                )
            takes.append(take_from(result, take, reason=reason))
            if takes[-1].status == "ok" and takes[-1].reason is None:
                break
        sets[beat] = choose(ctx, gateway.ledger, "voice", beat, takes)
    write_takes(ctx.paths.stage_dir("voice"), sets)


register(Stage(name="voice", requires=("shotplan",), gate="look", run=run_voice))
