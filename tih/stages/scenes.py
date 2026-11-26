"""Stage: scenes. The video model stages each take itself, sound and voices included.

Paid. Each take (one or more shots of a clip, or the silent opening) is one
text-to-video call. Its sound is transcribed and compared word for word with
the script, as the voice stage does: a take that adds, drops or changes a word,
speaks where nobody should, or runs its line past its end is rejected with the
reason and taken again, once. When the second attempt fails too, the attempt closest to
its script is kept (the share of wrong words, a line cut off by the clip, shots that do not cut where
planned; the earlier one on a tie), noted "accepted with fault" in the ledger and takes.yaml, and
listed in timing.yaml under `faults` and on stderr, with real-words lines marked as to fix before
release. A take with no line is never kept this way: speech there would be captioned. The chosen takes' word timings give each lined shot its
spoken span and the caption pages for the edit. Each chosen clip's real cuts
are found too: where they match the plan, timing.yaml gives every shot its real
start and end in the clip (`shots`), and the edit follows them; where they do
not, the take is marked `cuts: unmatched` and keeps its planned times.

Only the Seedance flow is built: words only, no image of any kind is sent.

Takes are independent, so up to the step's `parallel` of them run at once; the
attempts within one take stay in order. A take that ends with no usable attempt
lets the takes already running finish (they are paid for) and starts no more.
"""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from typing import Any

import yaml

from tih.assets import ShotRef
from tih.bible import load_bible
from tih.briefs import opening_brief, scene_brief
from tih.gates import require_approved
from tih.media import ffmpeg
from tih.production import (
    StepConfig,
    Take,
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
    MAX_TAKE_SECONDS, MAX_TAKE_SHOTS, OPENING_ID, Caption, Flow, SceneTake, as_shot, caption_pages, kept_cuts, line_spans, opening_take,
    real_shots, request_seconds, scene_takes,
)
from tih.shots import PlanError
from tih.stages.registry import RunContext, Stage, register
from tih.stages.voice import spoken_words, word_difference, word_timings_path, words as script_words
from tih.story.models import Evidence

TIMING_FILE = "timing.yaml"
MAX_TAIL = 1.5  # seconds a take may play past its plan: the clip plays as generated, without a long tail
FAULT_NOTE = "accepted with fault: "  # a take kept although it failed the check, because no attempt passed
RUNS_PAST_PENALTY = 0.5  # the clip ends before the line does
CUTS_PENALTY = 0.25  # the clip's shots do not cut where planned, so name labels may land on the wrong face

Timing = tuple[dict[str, tuple[float, float]], list[dict], float]  # spans, word timings, clip seconds


class Interrupted(RuntimeError):
    """The run was stopped, so this take starts no further paid attempt."""


def heard_text(timings: list[dict]) -> str:
    """The transcript's spoken words only: sound events such as "(wind blowing)" are not speech."""
    return " ".join(str(t.get("text", "")) for t in timings if t.get("type", "word") == "word")


def check_take(take: SceneTake, transcript: str, timings: list[dict], clip_seconds: float) -> str | None:
    """Why a take's sound does not match its script, or None when it does.

    The model does not keep to the prompt's time ranges, so a line may run past
    the planned length as long as it ends inside the clip: the edit follows the clip.
    """
    if not take.line and transcript.split():
        return f"unexpected speech: {transcript}"
    difference = word_difference(take.line, transcript)
    if difference is not None:
        return difference
    heard = spoken_words(timings)
    if heard and float(heard[-1]["end"]) > clip_seconds - 0.1:
        return "line runs past the take"
    return None


def fault_score(scene: SceneTake, transcript: str, timings: list[dict], clip_seconds: float) -> float:
    """How far a rejected attempt is from its script: 0 is perfect, higher is worse.

    The share of the line's words that are wrong or missing (0 to 1), plus a penalty when the clip ends
    before the line does. The clip's cuts are scored separately, from the video itself.
    """
    want, got = script_words(scene.line), script_words(transcript)
    score = 1.0 - SequenceMatcher(None, want, got).ratio() if want else 0.0
    heard = spoken_words(timings)
    if heard and float(heard[-1]["end"]) > clip_seconds - 0.1:
        score += RUNS_PAST_PENALTY
    return score


def played_length(take: SceneTake, clip_seconds: float) -> float:
    """How long a take plays: its whole clip, as generated, up to MAX_TAIL past its plan.

    The opening keeps its plan (the title is timed to it), and so does a slowed or
    frozen shot, whose plan is screen seconds while its clip is source seconds.
    """
    if not take.shots or any(shot.speed != 1.0 or shot.freeze_at is not None for shot in take.shots):
        return round(take.duration, 3)
    return round(max(take.duration, min(clip_seconds, take.duration + MAX_TAIL, MAX_TAKE_SECONDS)), 3)


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
    _one_take_per_clip(scenes[1:])

    briefs = [
        opening_brief(bible.places[scene.place], bible, False, opening=plan.opening)
        if scene.id == OPENING_ID else scene_brief(scene, bible, False)
        for scene in scenes
    ]
    failed = threading.Event()  # a take failed: no take starts after it
    interrupted = threading.Event()  # Ctrl-C: no take starts another paid attempt either

    def shoot(scene: SceneTake, prompts: dict[str, Any]) -> tuple[TakeSet, Timing] | None:
        if failed.is_set() or interrupted.is_set():
            return None
        try:
            return _shoot(ctx, gateway, config, transcribe, stage, flow, plan.episode, scene, prompts, interrupted)
        except Exception:
            failed.set()
            raise

    pool = ThreadPoolExecutor(max_workers=config.parallel, thread_name_prefix=stage)
    futures = [pool.submit(shoot, scene, prompts) for scene, prompts in zip(scenes, briefs)]
    try:
        wait(futures)
    except BaseException:
        interrupted.set()
        pool.shutdown(wait=False, cancel_futures=True)
        print("Stopping: calls already at the provider finish and are logged first; "
              "a re-run reuses them.", file=sys.stderr)
        raise
    pool.shutdown()
    results = [future.result() for future in futures]  # the first error in plan order, if any
    sets = {scene.id: result[0] for scene, result in zip(scenes, results)}

    stage_dir = ctx.paths.stage_dir(stage)
    write_takes(stage_dir, sets)
    spans: dict[str, list[float]] = {}
    captions: list[dict] = []
    played = {scene.id: played_length(scene, clip) for scene, (_, (_, _, clip)) in zip(scenes, results)}
    # the model does not cut at the prompt's times: where a clip's real cuts match its plan, its shots follow them
    real: dict[str, tuple[float, float]] = {}
    for scene in scenes[1:]:
        real.update(real_shots(scene, ffmpeg.cut_times(Path(sets[scene.id].chosen)), played[scene.id]) or {})
    real = kept_cuts(plan, scenes, played, real, flow)
    cuts = {scene.id: "matched" if scene.shots[0].id in real else "unmatched" for scene in scenes[1:]}
    # captions are timed on the as-shot timeline: each take starts where the longer takes before it end
    shot_plan = as_shot(plan, scenes, played, real)
    shot_takes = {take.id: take for take in [opening_take(shot_plan), *scene_takes(shot_plan, flow)]}
    for scene, (_, (chosen_spans, words, _)) in zip(scenes, results):
        spans.update({shot: list(span) for shot, span in chosen_spans.items()})
        captions += [page.model_dump(mode="json") for page in caption_pages(shot_takes[scene.id], words)]
    faults = _faults(scenes, sets)
    timing = {"spans": spans, "captions": captions, "played": played,
              "shots": {shot: list(span) for shot, span in real.items()}, "cuts": cuts}
    if faults:  # a clean run's file is unchanged
        timing["faults"] = faults
    (stage_dir / TIMING_FILE).write_text(
        yaml.safe_dump(timing, allow_unicode=True, sort_keys=False, width=10000), encoding="utf-8")
    for scene_id, fault in faults.items():
        print(f"{'MUST FIX before release (real words)' if fault['real_words'] else 'Kept with a fault'}: "
              f"take {scene_id}, {fault['fault']}", file=sys.stderr)


def _faults(scenes: list[SceneTake], sets: dict[str, TakeSet]) -> dict[str, dict]:
    """Each chosen take that failed the check and was kept as the best attempt, with whether it holds real words."""
    faults = {}
    for scene in scenes:
        chosen = sets[scene.id]
        note = next((take.note for take in chosen.takes if take.call_id == chosen.chosen_call_id), None) or ""
        if note.startswith(FAULT_NOTE):
            real_words = any(shot.spoken and shot.evidence == Evidence.real_words for shot in scene.shots)
            faults[scene.id] = {"fault": note[len(FAULT_NOTE):], "real_words": real_words}
    return faults


MIN_SPLIT_SECONDS = 5.0  # a long scene may be two or three generations, never a short leftover


def _alone(take: SceneTake) -> bool:
    return len(take.shots) == 1 and (take.shots[0].speed != 1.0 or take.shots[0].freeze_at is not None)


def _one_take_per_clip(takes: list[SceneTake]) -> None:
    """Each clip is one generation; a plan that splits a clip is stopped before anything is paid."""
    for clip in dict.fromkeys(take.clip for take in takes):
        mine = [take for take in takes if take.clip == clip]
        split = [take for take in mine if not _alone(take)]  # a slowed or frozen shot is always its own take
        if len(mine) == 1 or all(take.duration >= MIN_SPLIT_SECONDS for take in split):
            continue
        shots = [shot for take in mine for shot in take.shots]
        seconds = sum(shot.duration for shot in shots)
        alone = [shot.id for shot in shots if shot.speed != 1.0 or shot.freeze_at is not None]
        why = f"; slowed or frozen shots are taken alone: {', '.join(alone)}" if alone else ""
        raise PlanError(
            f"clip {clip} does not fit one generation: {len(shots)} shots, {seconds:g} s "
            f"(at most {MAX_TAKE_SHOTS} shots and {MAX_TAKE_SECONDS:g} s){why} — re-run the director"
        )


def _shoot(
    ctx: RunContext,
    gateway: GatewayContext,
    config: StepConfig,
    transcribe: StepConfig,
    stage: str,
    flow: Flow,
    episode: int,
    scene: SceneTake,
    prompts: dict[str, Any],
    interrupted: threading.Event,
) -> tuple[TakeSet, Timing]:
    """Take one scene until an attempt says its words; its chosen take and that take's timing."""
    ref = ShotRef(episode, scene.clip, f"{flow}-take{scene.id}")
    takes = []
    timed: dict[str, Timing] = {}
    faulty: dict[str, tuple[float, Timing]] = {}
    for take in range(config.takes):
        if interrupted.is_set():
            raise Interrupted(f"{stage} {scene.id}: stopped before take {take}")
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
            clip = ffmpeg.duration(video)
            if clip < scene.duration - 0.05:
                reason = "clip is shorter than the take"
            else:
                reason = _check_sound(gateway, transcribe, scene, video, clip, ref, take, result.call_id, timed, faulty)
        takes.append(take_from(result, take, reason=reason))
        if takes[-1].status == "ok" and takes[-1].reason is None:
            break
    fault = None
    if not any(t.status == "ok" and t.reason is None for t in takes):
        takes, fault = _keep_best(takes, faulty, scene)
    chosen = choose(ctx, gateway.ledger, stage, scene.id, takes)
    if fault:
        gateway.ledger.update(chosen.chosen_call_id, kept=True, reject_reason=fault)
    return chosen, timed.get(chosen.chosen_call_id) or faulty[chosen.chosen_call_id][1]


def _keep_best(takes: list[Take], faulty: dict[str, tuple[float, Timing]], scene: SceneTake) -> tuple[list[Take], str | None]:
    """With no attempt passing, the rejected attempt closest to its script (earliest on a tie), marked as kept
    with its fault; the same takes and None when none of them can be kept."""
    candidates = [take for take in takes if take.call_id in faulty]
    if not candidates:
        return takes, None

    def distance(take: Take) -> tuple[float, int]:
        score, (_, _, clip) = faulty[take.call_id]
        cuts = len(scene.shots) > 1 and real_shots(scene, ffmpeg.cut_times(Path(take.path)), played_length(scene, clip)) is None
        return score + (CUTS_PENALTY if cuts else 0.0), take.take

    best = min(candidates, key=distance)
    note = FAULT_NOTE + (best.reason or "")
    return [take.model_copy(update={"reason": None, "note": note}) if take is best else take for take in takes], note


def _check_sound(
    gateway: GatewayContext,
    transcribe: StepConfig,
    scene: SceneTake,
    video: Path,
    clip_seconds: float,
    ref: ShotRef,
    take: int,
    call_id: str,
    timed: dict[str, Timing],
    faulty: dict[str, tuple[float, Timing]],
) -> str | None:
    """Transcribe one take's sound and check it; a usable take's spans, words and length go into `timed`.

    A rejected take of a scene with a line, whose words can still be timed, goes into `faulty` with its
    score: it is the fallback if no attempt of the scene passes.
    """
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
    reason = check_take(scene, heard_text(words), words, clip_seconds)
    if reason is not None and not scene.line:
        return reason  # speech where nobody speaks would be captioned: never kept
    try:
        caption_pages(scene, words)  # the words must page; the pages are placed once every take is in
        timing = (line_spans(scene, words), words, clip_seconds)
    except ValueError as error:
        return reason or f"words could not be timed: {error}"
    if reason is None:
        timed[call_id] = timing
    else:
        faulty[call_id] = (fault_score(scene, heard_text(words), words, clip_seconds), timing)
    return reason


def read_timing(
    ctx: RunContext, flow: Flow
) -> tuple[dict[str, tuple[float, float]], list[Caption], dict[str, float]]:
    """Each lined shot's spoken span (seconds from its take's start), the chosen takes' caption pages
    (on the as-shot timeline, before the opening) and each take's played length in seconds."""
    path = ctx.paths.scope_dir / f"scenes_{flow}" / TIMING_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    spans = {shot: (float(span[0]), float(span[1])) for shot, span in (data.get("spans") or {}).items()}
    played = {str(take): float(seconds) for take, seconds in (data.get("played") or {}).items()}
    return spans, [Caption.model_validate(page) for page in data.get("captions") or []], played


def read_shots(ctx: RunContext, flow: Flow) -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
    """Each shot's real (start, end) in its take's clip, where the clip's cuts matched its plan, and
    each take's `matched` or `unmatched`."""
    path = ctx.paths.scope_dir / f"scenes_{flow}" / TIMING_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    shots = {shot: (float(span[0]), float(span[1])) for shot, span in (data.get("shots") or {}).items()}
    return shots, {str(take): str(state) for take, state in (data.get("cuts") or {}).items()}


register(Stage(name="scenes_seedance", requires=("shotplan",), gate="look", run=partial(run_scenes, flow="seedance")))
