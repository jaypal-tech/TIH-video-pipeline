"""Stage: lipsync. Sync the mouth in every on-face line to its chosen audio.

Paid. Only shots whose line is delivered on the speaker's face go here;
off-screen lines and reaction shots are left as they are. A line split across
shots is voiced once; each shot gets only its own words, cut from the take by
Scribe's word timings. Cost is logged
from the clip's real length in minutes.
"""

from pathlib import Path

from tih.assets import ShotRef
from tih.gates import require_approved
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
from tih.stages.voice import beat_leads, line_shots, piece_audio


def _minutes(path: Path) -> float | None:
    try:
        return duration(path) / 60
    except (FfmpegError, KeyError, ValueError):
        return None


def run_lipsync(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    require_approved(ctx.paths, "look")
    plan = load_resolved(ctx)
    motion = read_takes(ctx, "motion")
    voice = read_takes(ctx, "voice")
    config = load_pipeline(ctx.paths.series).steps["lipsync"]
    gateway = gateway_for(ctx)
    by_beat = line_shots(plan)
    leads = beat_leads(ctx, plan)
    audio_dir = ctx.paths.stage_dir("lipsync") / "audio"

    sets: dict[str, TakeSet] = {}
    for shot in plan.shots:
        if not shot.needs_lipsync:
            continue
        video = Path(motion[shot.id].chosen)
        shots = by_beat[shot.beat]
        pieces = [s.line for s in shots]
        index = [s.id for s in shots].index(shot.id)
        # a J-cut line is already `lead` seconds in when its first shot appears
        skip = leads[shot.beat] if index == 0 else 0.0
        audio = piece_audio(Path(voice[shot.beat].chosen), pieces, index, audio_dir / f"{shot.id}.wav", skip)
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
