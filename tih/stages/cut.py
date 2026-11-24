"""Stage: cut. Edit, mix, title and check the episode. Free (local ffmpeg).

1. Picture: each shot is trimmed to its planned length from its lip-synced
   clip (on-face lines) or its motion clip, scaled to 1080x1920, with slow
   motion and freezes applied, then joined in order.
2. Dialogue: each line starts with its shot, is mixed into one stem and
   normalised to -16 LUFS in two passes.
3. Music and effects: each cue at its time and level, ducked under speech.
4. Text: captions, tags, name cards, supers and the TRUE card drawn over the
   clean picture; the white flash at the end.
5. Check: validate_master must return no problems, or the stage fails.
"""

import json
from pathlib import Path

import yaml

from tih.bible import Bible, Grade, load_bible
from tih.media import ffmpeg
from tih.media.overlays import filters as overlay_filters
from tih.production import load_resolved, read_takes
from tih.stages.registry import RunContext, Stage, register
from tih.stages.voice import line_pieces

SAMPLE_RATE = 48000
FLASH_SECONDS = 0.5


def _picture(ctx: RunContext, plan, work: Path) -> tuple[Path, list[dict]]:
    motion = read_takes(ctx, "motion")
    lipsync = read_takes(ctx, "lipsync")
    parts, edl = [], []
    for shot in plan.shots:
        source = lipsync[shot.id].chosen if shot.id in lipsync else motion[shot.id].chosen
        part = work / "shots" / f"{shot.id}.mp4"
        ffmpeg.render_shot(Path(source), part, shot.duration, plan.fps, shot.speed, shot.freeze_at)
        parts.append(part)
        edl.append({
            "shot": shot.id, "source": source, "record_in": shot.start_s,
            "duration": shot.duration, "speed": shot.speed, "freeze_at": shot.freeze_at,
        })
    picture = work / "picture.mp4"
    ffmpeg.concat(parts, picture)
    return picture, edl


AMBIENCE_FADE = 0.5  # seconds; each scene's bed fades in and runs this far into the next


def grade_filters(grade: Grade) -> list[str]:
    filters = [f"eq={grade.eq}"] if grade.eq else []
    if grade.grain > 0:
        filters.append(f"noise=alls={grade.grain}:allf=t")
    return filters


def video_chain(plan, bible: Bible, text_dir: Path) -> list[str]:
    """Grade first, so captions and cards stay clean, then text, flashes, pixel format."""
    chain = grade_filters(bible.style.grade) + overlay_filters(plan, bible.overlays, text_dir)
    for shot in plan.shots:
        if shot.flash_at is not None:
            chain.append(f"fade=t=out:st={shot.start_s + shot.flash_at:.3f}:d={FLASH_SECONDS}:color=white")
    chain.append("format=yuv420p")
    return chain


def ambience_track(source: Path, length: float, target: Path) -> Path:
    """A scene's bed: the generated loop repeated to `length`, faded in and out."""
    fade = min(AMBIENCE_FADE, length / 4)
    target.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run([
        "ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(source), "-t", f"{length:.3f}",
        "-af", f"afade=t=in:d={fade:.3f},afade=t=out:st={length - fade:.3f}:d={fade:.3f}",
        "-ar", str(SAMPLE_RATE), str(target),
    ])
    return target


def _mix_inputs(inputs: list[tuple[Path, float, float | None, float]], runtime: float, target: Path) -> None:
    """Place (file, start, length or None, gain dB) clips on one stereo track."""
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
            fade = min(0.3, length / 4)
            steps += [f"atrim=0:{length:.3f}", f"afade=t=out:st={length - fade:.3f}:d={fade:.3f}"]
        steps += [f"volume={gain}dB", f"adelay={delay}|{delay}"]
        chains.append(f"[{index}:a]{','.join(steps)}[a{index}]")
        labels.append(f"[a{index}]")
    mix = (f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,"
           f"apad,atrim=0:{runtime:.3f}[out]")
    args += ["-filter_complex", ";".join(chains + [mix]), "-map", "[out]",
             "-ar", str(SAMPLE_RATE), str(target)]
    ffmpeg.run(args)


def _sound(ctx: RunContext, plan, work: Path) -> tuple[Path, ffmpeg.Loudness | None]:
    voice = read_takes(ctx, "voice")
    sound = read_takes(ctx, "sound")
    # each shot's piece of its line at that shot's start: the same slice its lips were synced to
    lines = [(path, at, None, 0.0) for path, at in line_pieces(ctx, plan, work / "lines")]
    raw_dialogue = work / "dialogue_raw.wav"
    _mix_inputs(lines, plan.runtime_s, raw_dialogue)
    dialogue = work / "dialogue.wav"
    loudness = None
    if lines:
        loudness = ffmpeg.loudnorm_two_pass(raw_dialogue, dialogue)
    else:
        raw_dialogue.replace(dialogue)

    cues = []
    for c in plan.cues:
        if c.kind == "ambience":
            length = min(c.duration + AMBIENCE_FADE, plan.runtime_s - c.at)
            bed_file = ambience_track(Path(sound[c.id].chosen), length, work / "ambience" / f"{c.id}.wav")
            cues.append((bed_file, c.at, None, c.gain_db))
        else:
            cues.append((Path(sound[c.id].chosen), c.at, c.duration, c.gain_db))
    bed = work / "bed.wav"
    _mix_inputs(cues, plan.runtime_s, bed)

    mix = work / "mix.wav"
    ffmpeg.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(dialogue), "-i", str(bed), "-filter_complex",
        "[0:a]asplit=2[d][key];"
        "[1:a][key]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=350[ducked];"
        "[d][ducked]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.89[out]",
        "-map", "[out]", "-ar", str(SAMPLE_RATE), str(mix),
    ])
    return mix, loudness


def run_cut(ctx: RunContext) -> None:
    plan = load_resolved(ctx)
    bible = load_bible(ctx.paths.series)
    stage_dir = ctx.paths.stage_dir("cut")
    work = stage_dir / "work"
    picture, edl = _picture(ctx, plan, work)
    mix, loudness = _sound(ctx, plan, work)

    chain = video_chain(plan, bible, work / "text")
    filter_script = work / "video_filters.txt"
    filter_script.write_text(",\n".join(chain), encoding="utf-8")

    final = stage_dir / "final.mp4"
    ffmpeg.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(picture), "-i", str(mix),
        "-/filter:v", str(filter_script),
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(plan.fps),
        "-c:a", "aac", "-b:a", "192k", "-t", f"{plan.runtime_s:.3f}",
        "-movflags", "+faststart", str(final),
    ])

    problems = ffmpeg.validate_master(final)
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


register(Stage(name="cut", requires=("motion", "voice", "lipsync", "sound"), gate=None, run=run_cut))
