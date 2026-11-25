"""Stage: scenes. The video model stages each take itself, sound and voices included.

Paid. Each take (one or more shots of a clip, or the silent opening) is one
text-to-video call. Its sound is transcribed and compared word for word with
the script, as the voice stage does: a take that adds, drops or changes a word,
speaks where nobody should, or runs its line past its end is rejected with the
reason and taken again. The chosen takes' word timings give each lined shot its
spoken span and the caption pages for the edit.

Only the Seedance flow is built: words only, no image of any kind is sent.
"""

import json
from functools import partial
from pathlib import Path

import yaml

from tih.assets import ShotRef
from tih.bible import load_bible
from tih.briefs import opening_brief, scene_brief
from tih.gates import require_approved
from tih.media import ffmpeg
from tih.production import (
    StepConfig,
    TakeSet,
    call_step,
    choose,
    gateway_for,
    load_pipeline,
    load_resolved,
    take_from,
    write_takes,
)
from tih.providers.fal import GatewayContext, upload_local
from tih.scenes import (
    OPENING_ID, Caption, Flow, SceneTake, caption_pages, line_spans, opening_take, request_seconds, scene_takes,
)
from tih.stages.registry import RunContext, Stage, register
from tih.stages.voice import spoken_words, word_difference, word_timings_path

TIMING_FILE = "timing.yaml"


def heard_text(timings: list[dict]) -> str:
    """The transcript's spoken words only: sound events such as "(wind blowing)" are not speech."""
    return " ".join(str(t.get("text", "")) for t in timings if t.get("type", "word") == "word")


def check_take(take: SceneTake, transcript: str, timings: list[dict]) -> str | None:
    """Why a take's sound does not match its script, or None when it does."""
    if not take.line and transcript.split():
        return f"unexpected speech: {transcript}"
    difference = word_difference(take.line, transcript)
    if difference is not None:
        return difference
    heard = spoken_words(timings)
    if heard and float(heard[-1]["end"]) > take.duration - 0.1:
        return "line runs past the take"
    return None


def run_scenes(ctx: RunContext, flow: Flow) -> None:
    if flow != "seedance":
        raise NotImplementedError("the veo flow is not built yet")
    require_approved(ctx.paths, "script")
    require_approved(ctx.paths, "look")
    plan = load_resolved(ctx)
    bible = load_bible(ctx.paths.series)
    steps = load_pipeline(ctx.paths.series).steps
    config, transcribe = steps["scene_seedance"], steps["transcribe"]
    gateway = gateway_for(ctx)
    stage = f"scenes_{flow}"
    scenes = [opening_take(plan), *scene_takes(plan, flow)]

    sets: dict[str, TakeSet] = {}
    timed: dict[str, tuple[dict[str, tuple[float, float]], list[Caption]]] = {}
    for scene in scenes:
        if scene.id == OPENING_ID:
            prompts = opening_brief(bible.places[scene.place], bible, False, opening=plan.opening)
        else:
            prompts = scene_brief(scene, bible, False)
        ref = ShotRef(plan.episode, scene.clip, f"{flow}-take{scene.id}")
        takes = []
        for take in range(config.takes):
            result = call_step(
                gateway,
                config,
                "scene",
                {
                    **prompts,
                    "image_urls": None,
                    "duration": str(request_seconds(scene, config.duration_range)),
                },
                ref,
                take=take,
            )
            reason = None
            if result.status == "ok" and result.output_paths:
                video = Path(result.output_paths[0])
                if ffmpeg.duration(video) < scene.duration - 0.05:
                    reason = "clip is shorter than the take"
                else:
                    reason = _check_sound(gateway, transcribe, scene, video, ref, take, result.call_id, timed)
            takes.append(take_from(result, take, reason=reason))
            if takes[-1].status == "ok" and takes[-1].reason is None:
                break
        sets[scene.id] = choose(ctx, gateway.ledger, stage, scene.id, takes)

    stage_dir = ctx.paths.stage_dir(stage)
    write_takes(stage_dir, sets)
    spans: dict[str, list[float]] = {}
    captions: list[dict] = []
    for scene in scenes:
        chosen_spans, chosen_captions = timed[sets[scene.id].chosen_call_id]
        spans.update({shot: list(span) for shot, span in chosen_spans.items()})
        captions += [page.model_dump(mode="json") for page in chosen_captions]
    (stage_dir / TIMING_FILE).write_text(
        yaml.safe_dump({"spans": spans, "captions": captions}, allow_unicode=True, sort_keys=False, width=10000),
        encoding="utf-8",
    )


def _check_sound(
    gateway: GatewayContext,
    transcribe: StepConfig,
    scene: SceneTake,
    video: Path,
    ref: ShotRef,
    take: int,
    call_id: str,
    timed: dict[str, tuple[dict[str, tuple[float, float]], list[Caption]]],
) -> str | None:
    """Transcribe one take's sound and check it; a usable take's spans and captions go into `timed`."""
    audio = ffmpeg.extract_audio(video, video.with_suffix(".wav"))
    heard = call_step(
        gateway,
        transcribe,
        "transcribe",
        {"audio_url": upload_local(gateway, audio)},
        ref,
        take=take,
        expects_files=False,
    )
    words = heard.payload.get("words", []) if heard.status == "ok" else []
    word_timings_path(audio).write_text(json.dumps(words, indent=1), encoding="utf-8")
    if heard.status != "ok":
        return f"transcription returned {heard.status}"
    text = str(heard.payload.get("text", ""))
    if text.strip() and not words:
        return "transcript has no word timings"
    reason = check_take(scene, heard_text(words), words)
    if reason is not None:
        return reason
    try:
        timed[call_id] = (line_spans(scene, words), caption_pages(scene, words))
    except ValueError as error:
        return f"words could not be timed: {error}"
    return None


def read_timing(ctx: RunContext, flow: Flow) -> tuple[dict[str, tuple[float, float]], list[Caption]]:
    """Each lined shot's spoken span (seconds from its take's start) and the caption pages of the chosen takes."""
    path = ctx.paths.scope_dir / f"scenes_{flow}" / TIMING_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    spans = {shot: (float(span[0]), float(span[1])) for shot, span in (data.get("spans") or {}).items()}
    return spans, [Caption.model_validate(page) for page in data.get("captions") or []]


register(Stage(name="scenes_seedance", requires=("shotplan",), gate="look", run=partial(run_scenes, flow="seedance")))
