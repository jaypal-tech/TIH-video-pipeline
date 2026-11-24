"""Thin wrappers around ffmpeg and ffprobe. Nothing here calls a paid API."""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

WIDTH = 1080
HEIGHT = 1920


class FfmpegError(RuntimeError):
    """ffmpeg or ffprobe failed."""


@dataclass(frozen=True)
class Loudness:
    i: float
    tp: float
    lra: float
    thresh: float
    offset: float


def run(args: list[str]) -> str:
    """Run ffmpeg/ffprobe and return stderr (where ffmpeg writes its reports)."""
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        raise FfmpegError(f"{args[0]} failed:\n{tail}")
    return result.stdout + result.stderr


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise FfmpegError(f"ffprobe could not read {path}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def _rate(text: str) -> float:
    num, _, den = text.partition("/")
    return float(num) / float(den or 1)


def render_shot(
    source: Path,
    target: Path,
    seconds: float,
    fps: int,
    speed: float = 1.0,
    freeze_at: float | None = None,
) -> None:
    """Make one shot exactly `seconds` long, 1080x1920, no audio.

    The source is scaled to fill the vertical frame and centre-cropped. Slow
    motion divides the speed. If the source runs short, or the shot freezes,
    the last frame is held.
    """
    moving = seconds if freeze_at is None else min(freeze_at, seconds)
    hold = seconds - moving
    filters = [
        f"setpts=PTS/{speed}",
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase",
        f"crop={WIDTH}:{HEIGHT}",
        "setsar=1",
        f"fps={fps}",
        f"trim=duration={moving:.3f}",
        "setpts=PTS-STARTPTS",
        f"tpad=stop_mode=clone:stop_duration={hold + seconds:.3f}",
        f"trim=duration={seconds:.3f}",
        "setpts=PTS-STARTPTS",
        "format=yuv420p",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(source), "-an",
            "-vf", ",".join(filters),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-r", str(fps),
            str(target),
        ]
    )


def concat(parts: list[Path], target: Path) -> None:
    listing = target.with_suffix(".txt")
    listing.write_text(
        "".join(f"file '{part.resolve()}'\n" for part in parts), encoding="utf-8"
    )
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", str(target)])


def measure_loudness(path: Path) -> Loudness:
    report = run(["ffmpeg", "-hide_banner", "-i", str(path), "-af",
                  "loudnorm=print_format=json", "-f", "null", "-"])
    data = json.loads(report[report.rindex("{"): report.rindex("}") + 1])
    return Loudness(
        i=float(data["input_i"]),
        tp=float(data["input_tp"]),
        lra=float(data["input_lra"]),
        thresh=float(data["input_thresh"]),
        offset=float(data["target_offset"]),
    )


def loudnorm_two_pass(source: Path, target: Path, i: float = -16.0) -> Loudness:
    """Normalise to `i` LUFS with measured values (second pass, linear mode)."""
    first = measure_loudness(source)
    run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(source), "-af",
            f"loudnorm=I={i}:TP=-1.5:LRA=11:measured_I={first.i}:measured_TP={first.tp}:"
            f"measured_LRA={first.lra}:measured_thresh={first.thresh}:"
            f"offset={first.offset}:linear=true,aresample=48000",
            "-ar", "48000", str(target),
        ]
    )
    return measure_loudness(target)


def validate_master(path: Path, min_s: float = 60.0, max_s: float = 90.0) -> list[str]:
    """Return every way the file breaks the brief's delivery spec ([] = passes)."""
    info = probe(path)
    problems = []
    video = [s for s in info["streams"] if s["codec_type"] == "video"]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not video:
        return ["no video stream"]
    v = video[0]
    if v.get("codec_name") != "h264":
        problems.append(f"codec is {v.get('codec_name')}, needs h264")
    if (v.get("width"), v.get("height")) != (WIDTH, HEIGHT):
        problems.append(f"size is {v.get('width')}x{v.get('height')}, needs 1080x1920")
    fps = round(_rate(v.get("avg_frame_rate", "0/1")), 2)
    if fps not in (24.0, 30.0):
        problems.append(f"frame rate is {fps}, needs 24 or 30")
    if v.get("pix_fmt") != "yuv420p":
        problems.append(f"pixel format is {v.get('pix_fmt')}, needs yuv420p")
    seconds = float(info["format"]["duration"])
    if not min_s <= seconds <= max_s:
        problems.append(f"runtime is {seconds:.2f} s, needs {min_s:.0f}-{max_s:.0f} s")
    if not audio:
        problems.append("no audio stream")
    return problems


def escape_path(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    return re.sub(r"([\\:'\[\],;])", r"\\\1", str(path.resolve()))
