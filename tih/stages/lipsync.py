"""Stage: lipsync. Sync the mouth in every on-face line to its chosen audio.

Paid. Only shots whose line is delivered on the speaker's face go here;
off-screen lines and reaction shots are left as they are. A line split across
shots is voiced once; each shot gets only its own words, cut from the take by
Scribe's word timings. Cost is logged
from the clip's real length in minutes.
"""

import json
from pathlib import Path

from tih.assets import ShotRef
from tih.gates import require_approved
from tih.media import ffmpeg
from tih.media.ffmpeg import FfmpegError, duration
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
from tih.stages.registry import RunContext, Stage, register
from tih.stages.voice import line_shots, slice_for_shot, word_timings_path


def _minutes(path: Path) -> float | None:
    try:
        return duration(path) / 60
    except (FfmpegError, KeyError, ValueError):
        return None


def shot_audio(line: Path, pieces: list[str], index: int, out: Path) -> Path:
    """This shot's words only, cut from the whole-line take."""
    timings = json.loads(word_timings_path(line).read_text(encoding="utf-8"))
    start, end = slice_for_shot(timings, pieces, index)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(["ffmpeg", "-y", "-v", "error", "-i", str(line), "-ss", f"{start:.3f}", "-to", f"{end:.3f}", str(out)])
    return out


def run_lipsync(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    require_approved(ctx.paths, "look")
    plan = load_resolved(ctx)
    motion = read_takes(ctx, "motion")
    voice = read_takes(ctx, "voice")
    config = load_pipeline(ctx.paths.series).steps["lipsync"]
    gateway = gateway_for(ctx)
    by_beat = line_shots(plan)
    audio_dir = ctx.paths.stage_dir("lipsync") / "audio"

    sets: dict[str, TakeSet] = {}
    for shot in plan.shots:
        if not shot.needs_lipsync:
            continue
        video = Path(motion[shot.id].chosen)
        shots = by_beat[shot.beat]
        pieces = [s.line for s in shots]
        index = [s.id for s in shots].index(shot.id)
        audio = shot_audio(Path(voice[shot.beat].chosen), pieces, index, audio_dir / f"{shot.id}.wav")
        result = call_step(
            gateway,
            config,
            "lipsync",
            {"video_url": upload_local(gateway, video), "audio_url": upload_local(gateway, audio)},
            ShotRef(plan.episode, shot.clip, shot.id),
            quantity=_minutes(video),
        )
        sets[shot.id] = choose(ctx, gateway.ledger, "lipsync", shot.id, [take_from(result, 0)])
    write_takes(ctx.paths.stage_dir("lipsync"), sets)


register(Stage(name="lipsync", requires=("motion", "voice"), gate="look", run=run_lipsync))
