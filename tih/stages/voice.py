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
from tih.media import ffmpeg
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
    """One entry per script word: a Scribe token like "twenty-five" counts as the script counts it."""
    expanded = []
    for token in timings:
        if token.get("type", "word") == "word":
            expanded += [token] * len(words(str(token.get("text", ""))))
    return expanded


def slice_for_shot(timings: list[dict], pieces: list[str], index: int) -> tuple[float, float | None]:
    """Start and end seconds of piece `index` of a line, from the word timings.

    A piece runs from its first word to the next piece's first word, so the
    breath between pieces stays with the earlier shot. The first piece starts at
    0 and the last runs to the end of the take (None), so every piece is placed
    at its own shot's start and lips and soundtrack share one clock.
    """
    timed = spoken_words(timings)
    first = sum(len(words(piece)) for piece in pieces[:index])
    count = len(words(pieces[index]))
    if first + count > len(timed):
        raise ValueError(f"word timings cover {len(timed)} words; piece {index + 1} needs {first + count}")
    start = 0.0 if index == 0 else float(timed[first]["start"])
    if first + count < len(timed):
        return start, float(timed[first + count]["start"])
    return start, None


def piece_audio(line: Path, pieces: list[str], index: int, out: Path) -> Path:
    """One shot's words, cut from the whole-line take."""
    timings = json.loads(word_timings_path(line).read_text(encoding="utf-8"))
    start, end = slice_for_shot(timings, pieces, index)
    out.parent.mkdir(parents=True, exist_ok=True)
    until = [] if end is None else ["-to", f"{end:.3f}"]
    ffmpeg.run(["ffmpeg", "-y", "-v", "error", "-i", str(line), "-ss", f"{start:.3f}", *until, str(out)])
    return out


def line_shots(plan: ResolvedPlan) -> dict[str, list[ResolvedShot]]:
    """Every beat with a line, and its shots that carry a piece of it, in order."""
    by_beat: dict[str, list[ResolvedShot]] = {}
    for shot in plan.shots:
        if shot.line is not None:
            by_beat.setdefault(shot.beat, []).append(shot)
    return by_beat


def line_pieces(ctx: RunContext, plan: ResolvedPlan, out_dir: Path) -> list[tuple[Path, float]]:
    """Every shot's piece of its line, and where it starts: the shot's own start."""
    voice = read_takes(ctx, "voice")
    placed = []
    for beat, shots in line_shots(plan).items():
        pieces = [shot.line for shot in shots]
        for index, shot in enumerate(shots):
            out = piece_audio(Path(voice[beat].chosen), pieces, index, out_dir / f"{shot.id}.wav")
            placed.append((out, shot.start_s))
    return placed


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
