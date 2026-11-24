"""Stage: lipsync. Sync the mouth in every on-face line to its chosen audio.

Paid. Only shots whose line is delivered on the speaker's face go here;
off-screen lines and reaction shots are left as they are. Cost is logged
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

    sets: dict[str, TakeSet] = {}
    for shot in plan.shots:
        if not shot.needs_lipsync:
            continue
        video = Path(motion[shot.id].chosen)
        audio = Path(voice[shot.id].chosen)
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
