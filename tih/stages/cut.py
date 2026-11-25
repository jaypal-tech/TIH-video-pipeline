"""Stage: cut_<flow>. Edit, mix, title and check the episode. Free (local ffmpeg).

1. Picture: the opening take, darkened and blurred, for OPENING_SECONDS, then
   each scene take trimmed to its planned length (the model returns whole
   seconds), scaled to 1080x1920, with slow motion and freezes applied to
   single-shot takes, joined in order.
2. Dialogue: each take's own sound starts with its take, is mixed into one
   stem and normalised to -16 LUFS in two passes.
3. Music and effects: each cue at its time and level, ducked under speech.
4. Text: the title block over the opening; caption pages timed to the words
   the model spoke; tags, name cards, supers and the TRUE card drawn over the
   clean picture; the white flash at the end.
5. Check: validate_master must return no problems, or the stage fails.
"""

import json
from functools import partial
from pathlib import Path

import yaml

from tih.bible import Bible, Grade, load_bible
from tih.media import ffmpeg
from tih.media.overlays import Text, drawtext_filters, opening_title
from tih.media.overlays import filters as overlay_filters
from tih.production import load_resolved, read_takes
from tih.scenes import (
    OPENING_ID, OPENING_SECONDS, Caption, Flow, SceneTake, opening_take, retimed, scene_takes, shifted,
)
from tih.series import load_series
from tih.shots import ResolvedPlan
from tih.stages.performance import source_seconds
from tih.stages.registry import RunContext, Stage, register
from tih.stages.scenes import read_timing
from tih.story.models import Episode

SAMPLE_RATE = 48000
FLASH_SECONDS = 0.5
OPENING_LOOK = "eq=brightness=-0.25:saturation=0.6,gblur=sigma=6"
TAKE_FADE = 0.05  # seconds faded in and out at each take's edges; a take's last word may end 0.1 s before its end


def record_in(take: SceneTake) -> float:
    """Where a take starts in the finished episode: the opening first, every scene take after it."""
    return 0.0 if take.id == OPENING_ID else take.start_s + OPENING_SECONDS


def played_seconds(take: SceneTake) -> float:
    """Seconds of a take's own sound that play: as much of its clip as the picture shows moving.

    A slowed or frozen take (always a single shot) keeps its sound at normal
    speed for the source seconds on screen; silence fills the rest.
    """
    # limitation: a slowed shot's sound is not stretched, so it runs ahead of its slowed picture
    if len(take.shots) != 1:
        return take.duration
    shot = take.shots[0]
    return source_seconds(take.duration, shot.speed, shot.freeze_at)


def _picture(ctx: RunContext, plan: ResolvedPlan, flow: Flow, work: Path) -> tuple[Path, list[dict]]:
    chosen = read_takes(ctx, f"scenes_{flow}")
    opening = opening_take(plan)
    raw = work / "opening_raw.mp4"
    ffmpeg.render_shot(Path(chosen[OPENING_ID].chosen), raw, OPENING_SECONDS, plan.fps)
    first = work / "takes" / f"{OPENING_ID}.mp4"
    first.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(raw), "-an", "-vf", f"{OPENING_LOOK},format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-r", str(plan.fps), str(first),
    ])
    parts = [first]
    edl = [{"take": OPENING_ID, "shots": [], "source": chosen[OPENING_ID].chosen,
            "record_in": record_in(opening), "duration": OPENING_SECONDS}]
    for take in scene_takes(plan, flow):
        source = chosen[take.id].chosen
        speed, freeze_at = (take.shots[0].speed, take.shots[0].freeze_at) if len(take.shots) == 1 else (1.0, None)
        part = work / "takes" / f"{take.id}.mp4"
        ffmpeg.render_shot(Path(source), part, take.duration, plan.fps, speed, freeze_at)
        parts.append(part)
        edl.append({"take": take.id, "shots": [shot.id for shot in take.shots], "source": source,
                    "record_in": record_in(take), "duration": take.duration})
    picture = work / "picture.mp4"
    ffmpeg.concat(parts, picture)
    return picture, edl


MIN_LOOP_SECONDS = 0.1
AMBIENCE_FADE = 0.5  # seconds; each scene's bed fades in and runs this far into the next


def grade_filters(grade: Grade) -> list[str]:
    filters = [f"eq={grade.eq}"] if grade.eq else []
    if grade.balance:
        filters.append(f"colorbalance={grade.balance}")
    if grade.grain > 0:
        filters.append(f"noise=alls={grade.grain}:allf=t")
    return filters


def video_chain(
    plan, bible: Bible, text_dir: Path, captions: list[Caption] | None = None,
    title: list[Text] | None = None, picture: ResolvedPlan | None = None,
) -> list[str]:
    """Grade first, so captions and cards stay clean, then text, flashes, pixel format.

    `picture` is the timeline the shots are cut on, for the flashes; it defaults
    to `plan`, whose shots may be re-timed to the spoken words for the text.
    """
    chain = grade_filters(bible.style.grade) + overlay_filters(plan, bible.overlays, text_dir, captions=captions)
    chain += drawtext_filters(title or [], bible.overlays, text_dir / "title")
    for shot in (picture or plan).shots:
        if shot.flash_at is not None:
            chain.append(f"fade=t=out:st={shot.start_s + shot.flash_at:.3f}:d={FLASH_SECONDS}:color=white")
    chain.append("format=yuv420p")
    return chain


def ambience_track(source: Path, length: float, target: Path) -> Path:
    """A scene's bed: the generated loop repeated to `length`, faded in and out."""
    try:
        loop_seconds = ffmpeg.duration(source)
    except (ffmpeg.FfmpegError, KeyError, ValueError):
        loop_seconds = 0.0
    if loop_seconds < MIN_LOOP_SECONDS:
        # looping an empty file never ends; stop with a reason instead
        raise ffmpeg.FfmpegError(f"ambience {source.name} is too short to loop ({loop_seconds:.2f} s)")
    fade = min(AMBIENCE_FADE, length / 4)
    target.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run([
        "ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(source), "-t", f"{length:.3f}",
        "-af", f"afade=t=in:d={fade:.3f},afade=t=out:st={length - fade:.3f}:d={fade:.3f}",
        "-ar", str(SAMPLE_RATE), str(target),
    ])
    return target


def _mix_inputs(
    inputs: list[tuple[Path, float, float | None, float]], runtime: float, target: Path, fade: float | None = None,
) -> None:
    """Place (file, start, length or None, gain dB) clips on one stereo track.

    A clip with a length fades out over its end (up to 0.3 s); given `fade`, it
    fades in and out over exactly that many seconds instead.
    """
    if not inputs:
        ffmpeg.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"anullsrc=r={SAMPLE_RATE}:cl=stereo", "-t", f"{runtime:.3f}", str(target)])
        return
    args = ["ffmpeg", "-y", "-v", "error"]
    chains, labels = [], []
    for index, (path, start, length, gain) in enumerate(inputs):
        args += ["-i", str(path)]
        delay = int(round(start * 1000))
        steps = [f"aresample={SAMPLE_RATE}", "aformat=channel_layouts=stereo"]
        if length is not None:
            out = min(0.3, length / 4) if fade is None else fade
            steps.append(f"atrim=0:{length:.3f}")
            if fade is not None:
                steps.append(f"afade=t=in:d={fade:.3f}")
            steps.append(f"afade=t=out:st={length - out:.3f}:d={out:.3f}")
        steps += [f"volume={gain}dB", f"adelay={delay}|{delay}"]
        chains.append(f"[{index}:a]{','.join(steps)}[a{index}]")
        labels.append(f"[a{index}]")
    mix = (f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,"
           f"apad=whole_dur={runtime:.3f},atrim=0:{runtime:.3f}[out]")
    args += ["-filter_complex", ";".join(chains + [mix]), "-map", "[out]",
             "-ar", str(SAMPLE_RATE), str(target)]
    ffmpeg.run(args)


def _sound(ctx: RunContext, plan: ResolvedPlan, flow: Flow, work: Path) -> tuple[Path, ffmpeg.Loudness | None]:
    """`plan` is the episode's own plan; the mix runs on it shifted past the opening."""
    chosen = read_takes(ctx, f"scenes_{flow}")
    sound = read_takes(ctx, "sound")
    timeline = shifted(plan, OPENING_SECONDS)
    # each take's own sound at its take's start, cut where its picture stops moving
    lines = [
        (ffmpeg.extract_audio(Path(chosen[take.id].chosen), work / "takes" / f"{take.id}.wav"),
         record_in(take), played_seconds(take), 0.0)
        for take in [opening_take(plan), *scene_takes(plan, flow)]
    ]
    raw_dialogue = work / "dialogue_raw.wav"
    _mix_inputs(lines, timeline.runtime_s, raw_dialogue, fade=TAKE_FADE)
    dialogue = work / "dialogue.wav"
    loudness = None
    if lines:
        loudness = ffmpeg.loudnorm_two_pass(raw_dialogue, dialogue)
    else:
        raw_dialogue.replace(dialogue)

    cues = []
    for c in timeline.cues:
        if c.kind == "ambience":
            length = min(c.duration + AMBIENCE_FADE, timeline.runtime_s - c.at)
            bed_file = ambience_track(Path(sound[c.id].chosen), length, work / "ambience" / f"{c.id}.wav")
            cues.append((bed_file, c.at, None, c.gain_db))
        else:
            cues.append((Path(sound[c.id].chosen), c.at, c.duration, c.gain_db))
    bed = work / "bed.wav"
    _mix_inputs(cues, timeline.runtime_s, bed)

    mix = work / "mix.wav"
    ffmpeg.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(dialogue), "-i", str(bed), "-filter_complex",
        "[0:a]asplit=2[d][key];"
        "[1:a][key]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=350[ducked];"
        "[d][ducked]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.89[out]",
        "-map", "[out]", "-ar", str(SAMPLE_RATE), str(mix),
    ])
    return mix, loudness


def without_title_super(plan: ResolvedPlan) -> ResolvedPlan:
    """The plan without its first time super, which the opening title already shows."""
    for shot in plan.shots:
        for index, overlay in enumerate(shot.overlays):
            if overlay.kind == "time_super":
                kept = shot.overlays[:index] + shot.overlays[index + 1:]
                shots = [s.model_copy(update={"overlays": kept}) if s is shot else s for s in plan.shots]
                return plan.model_copy(update={"shots": shots})
    return plan


def title_lines(ctx: RunContext, plan: ResolvedPlan) -> list[str]:
    """Series title, episode number, episode title and the plan's first time super, when it has one."""
    script = Episode.model_validate(
        yaml.safe_load((ctx.paths.scope_dir / "writers_room" / "script.yaml").read_text(encoding="utf-8"))
    )
    lines = [load_series(ctx.paths.series).title, f"EPISODE {plan.episode}", script.title]
    supers = [o.text for shot in plan.shots for o in shot.overlays if o.kind == "time_super"]
    return lines + supers[:1]


def run_cut(ctx: RunContext, flow: Flow) -> None:
    plan = load_resolved(ctx)
    bible = load_bible(ctx.paths.series)
    low, high = load_series(ctx.paths.series).runtime_s
    stage_dir = ctx.paths.stage_dir(f"cut_{flow}")
    work = stage_dir / "work"
    timeline = shifted(plan, OPENING_SECONDS)
    picture, edl = _picture(ctx, plan, flow, work)
    mix, loudness = _sound(ctx, plan, flow, work)

    spans, captions = read_timing(ctx, flow)
    shown = without_title_super(shifted(retimed(plan, scene_takes(plan, flow), spans), OPENING_SECONDS))
    pages = [c.model_copy(update={"start": c.start + OPENING_SECONDS, "end": c.end + OPENING_SECONDS})
             for c in captions]
    title = opening_title(title_lines(ctx, plan), OPENING_SECONDS, bible.overlays)
    chain = video_chain(shown, bible, work / "text", captions=pages, title=title, picture=timeline)
    filter_script = work / "video_filters.txt"
    filter_script.write_text(",\n".join(chain), encoding="utf-8")

    final = stage_dir / "final.mp4"
    ffmpeg.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(picture), "-i", str(mix),
        "-/filter:v", str(filter_script),
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(plan.fps),
        "-c:a", "aac", "-b:a", "192k", "-t", f"{timeline.runtime_s:.3f}",
        "-movflags", "+faststart", str(final),
    ])

    problems = ffmpeg.validate_master(final, min_s=low, max_s=high + OPENING_SECONDS)
    (stage_dir / "edl.yaml").write_text(yaml.safe_dump(edl, sort_keys=False), encoding="utf-8")
    report = {
        "runtime_s": ffmpeg.duration(final),
        "dialogue_lufs": None if loudness is None else loudness.i,
        "dialogue_true_peak": None if loudness is None else loudness.tp,
        "problems": problems,
    }
    (stage_dir / "master_check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if problems:
        raise RuntimeError("Master check failed:\n- " + "\n- ".join(problems))


register(Stage(name="cut_seedance", requires=("scenes_seedance", "sound"), gate=None,
               run=partial(run_cut, flow="seedance")))
