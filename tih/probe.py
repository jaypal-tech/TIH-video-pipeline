"""One real call per generation step, to prove every model accepts our arguments.

Runs one shot of the series' example plan through every paid step once, with
the exact prompts and settings an episode would send: look sheet, keyframe from
that look, a 3 s motion clip, voice design, the shot's line, its transcript,
lip-sync, 3 s of music and 1 s of sound effect. A step whose input failed is
skipped. The report lists each endpoint's status, time and cost.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from tih.assets import ShotRef
from tih.bible import character_text, load_bible
from tih.briefs import cue_brief, keyframe_brief, motion_brief, speech_settings, voice_design_brief
from tih.ledger import CallRecord
from tih.production import StepConfig, call_step, gateway_for, load_pipeline
from tih.providers.fal import GenerationResult, upload_local
from tih.series import load_series
from tih.shots import example_plan_path, load_plan, resolve_plan
from tih.stages import record  # noqa: F401  registers the record stage the probe reads its script from
from tih.stages.casting import BASE_SEED, _voice_id
from tih.stages.registry import RunContext, run_stage
from tih.stages.voice import speech_text
from tih.story.models import Episode

PROBE_STEPS = ("look", "keyframe", "motion", "voice_design", "speech", "transcribe", "lipsync", "music", "sfx")
MOTION_SECONDS = 3
MUSIC_SECONDS = 3.0
SFX_SECONDS = 1.0


@dataclass
class ProbeResult:
    step: str
    endpoint: str
    status: str
    seconds: float | None = None
    cost_usd: float | None = None
    error: str = ""


def probe_estimate(series: str) -> float:
    """What the budget guard reserves for one probe: each step's estimate once."""
    steps = load_pipeline(series).steps
    return round(sum(steps[name].estimate_usd for name in PROBE_STEPS), 2)


def run_probe(ctx: RunContext, shot_id: str) -> list[ProbeResult]:
    series = ctx.paths.series
    run_stage(ctx, "record")
    script = Episode.model_validate(
        yaml.safe_load((ctx.paths.scope_dir / "record" / "script.yaml").read_text(encoding="utf-8"))
    )
    bible = load_bible(series)
    plan = resolve_plan(load_plan(example_plan_path(series)), script, bible, load_series(series).runtime_s)
    shot = next(s for s in plan.shots if s.id == shot_id)
    if not shot.needs_lipsync or len(shot.cast) != 1:
        raise ValueError(f"shot {shot_id} must be a lip-sync shot with one face")
    character = shot.cast[0]
    steps = load_pipeline(series).steps
    gateway = gateway_for(ctx)
    ref = ShotRef(plan.episode, shot.clip, f"probe-{shot.id}")
    results: list[ProbeResult] = []
    outputs: dict[str, GenerationResult] = {}

    def attempt(name: str, kind: str, config: StepConfig, values: dict, needs: tuple[str, ...] = (), **kwargs) -> None:
        missing = [n for n in needs if n not in outputs or outputs[n].status != "ok"]
        if missing:
            results.append(ProbeResult(name, config.endpoint, "skipped", error=f"needs {', '.join(missing)}"))
            return
        try:
            result = call_step(gateway, config, kind, values() if callable(values) else values, ref, **kwargs)
        except Exception as error:  # the probe exists to report every failure, then carry on
            results.append(ProbeResult(name, config.endpoint, "error", error=str(error)[:300]))
            return
        outputs[name] = result
        record = _record(gateway.ledger.current(), result.call_id)
        results.append(ProbeResult(
            name, result.endpoint or config.endpoint, result.status,
            record.time_taken_s if record else None, record.cost_usd if record else None,
            "" if result.status == "ok" else str(result.payload)[:300],
        ))

    look_prompt = bible.style.reference_sheet.format(look=character_text(bible, character))
    if bible.style.clean:
        look_prompt = f"{look_prompt}. {bible.style.clean}"
    attempt("look", "look", steps["look"], {"prompt": look_prompt, "seed": BASE_SEED})
    attempt("keyframe", "keyframe", steps["keyframe"], lambda: {
        **keyframe_brief(shot, bible),
        "image_urls": [upload_local(gateway, outputs["look"].output_paths[0])],
        "seed": BASE_SEED,
    }, needs=("look",))
    attempt("motion", "video", steps["motion"], lambda: {
        **motion_brief(shot, bible),
        "image_url": upload_local(gateway, outputs["keyframe"].output_paths[0]),
        "duration": str(MOTION_SECONDS),
    }, needs=("keyframe",))
    attempt("voice_design", "voice_design", steps["voice_design"], {
        "voice_description": voice_design_brief(bible.characters[character]),
        "preview_text": shot.line,
        "voice_name": f"{series} {character} probe",
    })
    attempt("speech", "voice", steps["speech"], lambda: {
        "text": speech_text(shot.line, steps["speech"].pause_token),
        "voice_id": _voice_id(outputs["voice_design"].payload),
        **speech_settings(shot.direction),
    }, needs=("voice_design",))
    attempt("transcribe", "transcribe", steps["transcribe"], lambda: {
        "audio_url": upload_local(gateway, outputs["speech"].output_paths[0]),
    }, needs=("speech",), expects_files=False)
    attempt("lipsync", "lipsync", steps["lipsync"], lambda: {
        "video_url": upload_local(gateway, outputs["motion"].output_paths[0]),
        "audio_url": upload_local(gateway, outputs["speech"].output_paths[0]),
    }, needs=("motion", "speech"), quantity=MOTION_SECONDS / 60)
    music = next(c for c in plan.cues if c.kind == "music")
    attempt("music", "music", steps["music"], {
        "prompt": cue_brief(music, bible), "length_ms": int(MUSIC_SECONDS * 1000),
    }, quantity=MUSIC_SECONDS / 60)
    sfx = next(c for c in plan.cues if c.kind == "sfx")
    attempt("sfx", "sfx", steps["sfx"], {
        "prompt": cue_brief(sfx, bible), "duration_seconds": SFX_SECONDS,
    }, quantity=SFX_SECONDS)

    write_report(ctx.paths.root / series / "probe" / "report.md", shot.id, results)
    return results


def _record(records: list[CallRecord], call_id: str) -> CallRecord | None:
    return next((r for r in records if r.call_id == call_id), None)


def write_report(path: Path, shot_id: str, results: list[ProbeResult]) -> None:
    rows = [
        f"| {r.step} | `{r.endpoint}` | {r.status} | "
        f"{'' if r.seconds is None else f'{r.seconds:.1f}'} | "
        f"{'' if r.cost_usd is None else f'${r.cost_usd:.3f}'} | {r.error.replace('|', '/')} |"
        for r in results
    ]
    total = sum(r.cost_usd or 0.0 for r in results)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Probe: one call per step (shot {shot_id})\n\n"
        "| Step | Endpoint | Status | Seconds | Cost | Error |\n|---|---|---|---|---|---|\n"
        + "\n".join(rows) + f"\n\nTotal: ${total:.2f}\n",
        encoding="utf-8",
    )
