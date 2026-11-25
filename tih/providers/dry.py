"""A stand-in for fal that makes placeholder media locally, for $0.

`tih rehearse` runs a whole episode through every stage with this transport:
stills are flat colour frames labelled with their prompt, videos hold the
still (a scene take is a flat colour per shot, cut at its planned times), speech
is a tone as long as the line, transcripts echo the line. The
ledger still prices every call from models.yaml, so a rehearsal tells you
what the real run should cost, and the final cut shows timing, captions and
cards exactly as they will be.
"""

import hashlib
import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from tih.briefs import SHOT_HEADER, SPOKEN
from tih.media.ffmpeg import extract_audio

PAUSE_TAG = re.compile(r"<#[0-9.]+#>")
SECONDS_PER_WORD = 0.38
SHOT_COLOURS = ("0x303a4a", "0xd0d0c0", "0x101820", "0x909aa0")  # far apart, so every planned cut is a real one


def _ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed making placeholder media: {result.stderr[-500:]}")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _kind(endpoint: str) -> str:
    name = endpoint.lower()
    if "speech-to-text" in name or "scribe" in name:
        return "transcript"
    if "voice-design" in name:
        return "voice_design"
    if "lipsync" in name:
        return "lipsync"
    if "speech" in name or "tts" in name:
        return "speech"
    if "music" in name or "sound-effects" in name:
        return "sound"
    if "video" in name:
        return "video"
    return "image"


class DryRunTransport:
    def __init__(self, workdir: Path) -> None:
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.requests: dict[str, tuple[str, dict[str, Any]]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.uploads_file = self.workdir / "uploads.json"
        saved = json.loads(self.uploads_file.read_text()) if self.uploads_file.exists() else {}
        self.uploads: dict[str, Path] = {url: Path(path) for url, path in saved.items()}
        self.spoken: dict[str, str] = {}
        self.lengths: dict[str, float] = {}  # seconds of each scene take's sound, by hash
        self._lock = threading.Lock()  # stages may call from several threads at once

    def submit(self, endpoint: str, arguments: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps([endpoint, arguments], sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        request_id = f"dry-{digest}"
        self.requests[request_id] = (endpoint, arguments)
        return request_id

    def _url(self, request_id: str, suffix: str, spec: dict[str, Any]) -> str:
        url = f"https://dry.run/{request_id}{suffix}"
        self.pending[url] = spec
        return url

    def wait(self, endpoint: str, request_id: str) -> dict[str, Any]:
        _, arguments = self.requests[request_id]
        kind = _kind(endpoint)
        if kind == "transcript":
            source = self.uploads.get(str(arguments.get("audio_url")))
            text = self.spoken.get(_sha(source), "") if source else ""
            length = self.lengths.get(_sha(source)) if source else None
            step = SECONDS_PER_WORD
            if length and len(text.split()) * step > length:
                step = length / len(text.split())  # spread the words evenly inside the take
            timed = [
                {"text": word, "start": round(i * step, 3),
                 "end": round((i + 1) * step - 0.05, 3), "type": "word"}
                for i, word in enumerate(text.split())
            ]
            return {"text": text, "words": timed}
        if kind == "voice_design":
            url = self._url(request_id, ".mp3", {"kind": "tone", "seconds": 2.0, "text": ""})
            return {"custom_voice_id": f"dry-voice-{request_id[4:12]}", "audio": {"url": url}}
        if kind == "speech":
            text = PAUSE_TAG.sub(" ", str(arguments.get("prompt", arguments.get("text", ""))))
            seconds = max(1.0, len(text.split()) * SECONDS_PER_WORD)
            url = self._url(request_id, ".mp3", {"kind": "tone", "seconds": seconds, "text": text})
            return {"audio": {"url": url}}
        if kind == "sound":
            ms = arguments.get("music_length_ms")
            seconds = float(ms) / 1000 if ms else float(arguments.get("duration_seconds", 2.0))
            url = self._url(request_id, ".mp3", {"kind": "noise", "seconds": seconds, "text": ""})
            return {"audio": {"url": url}}
        if kind == "lipsync":
            video = self.uploads[str(arguments["video_url"])]
            url = self._url(request_id, ".mp4", {"kind": "copy", "source": str(video)})
            return {"video": {"url": url}}
        if kind == "video" and not ({"image_url", "start_image_url"} & arguments.keys()):
            seconds = float(str(arguments.get("duration", "5")).rstrip("s"))
            text = " ".join(SPOKEN.findall(str(arguments.get("prompt", ""))))
            cuts = [float(start) for start, _ in SHOT_HEADER.findall(str(arguments.get("prompt", "")))][1:]
            url = self._url(request_id, ".mp4", {"kind": "scene", "seconds": seconds, "text": text, "cuts": cuts})
            return {"video": {"url": url}}
        if kind == "video":
            image = self.uploads[str(arguments.get("start_image_url", arguments.get("image_url")))]
            seconds = float(str(arguments.get("duration", "5")).rstrip("s"))
            url = self._url(request_id, ".mp4", {"kind": "hold", "source": str(image), "seconds": seconds})
            return {"video": {"url": url}}
        label = str(arguments.get("prompt", ""))[:48]
        url = self._url(request_id, ".png", {"kind": "still", "label": label})
        return {"images": [{"url": url}]}

    def download(self, url: str, destination: Path) -> None:
        spec = self.pending[url]
        kind = spec["kind"]
        out = str(destination)
        if kind == "still":
            colour = "0x" + hashlib.sha256(spec["label"].encode()).hexdigest()[:6]
            label_file = destination.with_suffix(".label.txt")  # one per download, never shared
            label_file.write_text(spec["label"], encoding="utf-8")
            _ffmpeg(["-f", "lavfi", "-i", f"color=c={colour}:s=720x1280", "-frames:v", "1",
                     "-vf", f"drawtext=textfile={label_file}:fontcolor=white:fontsize=28:x=20:y=60",
                     "-f", "image2", "-c:v", "png", out])
            label_file.unlink()
        elif kind == "hold":
            _ffmpeg(["-loop", "1", "-i", spec["source"], "-t", f"{spec['seconds']}", "-r", "24",
                     "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                     "-f", "mp4", out])
        elif kind == "scene":
            # A pitch per spoken text keeps scenes of equal length from sharing one transcript.
            pitch = 180 + int(hashlib.sha256(spec["text"].encode()).hexdigest()[:4], 16) % 400
            # a new flat colour at each planned cut, so the stage finds the cuts it would find in a real clip
            shots = ["null", *(
                f"drawbox=w=iw:h=ih:t=fill:color={SHOT_COLOURS[number % len(SHOT_COLOURS)]}:enable='gte(t,{at})'"
                for number, at in enumerate(spec["cuts"], start=1))]
            _ffmpeg(["-f", "lavfi", "-i", f"color=c={SHOT_COLOURS[0]}:s=720x1280:r=24:d={spec['seconds']}",
                     "-f", "lavfi", "-i", f"sine=frequency={pitch}:duration={spec['seconds']}",
                     "-vf", ",".join(shots), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-shortest", "-f", "mp4", out])
            sound = _sha(extract_audio(Path(out), Path(out).with_suffix(".dry.wav")))
            Path(out).with_suffix(".dry.wav").unlink()
            self.spoken[sound] = spec["text"]
            self.lengths[sound] = spec["seconds"]
        elif kind == "copy":
            Path(out).write_bytes(Path(spec["source"]).read_bytes())
        elif kind == "tone":
            # A different pitch per line keeps every placeholder file distinct.
            pitch = 180 + int(hashlib.sha256(spec["text"].encode()).hexdigest()[:4], 16) % 400
            _ffmpeg(["-f", "lavfi", "-i", f"sine=frequency={pitch}:duration={spec['seconds']}",
                     "-ac", "2", "-f", "mp3", out])
            self.spoken[_sha(Path(out))] = spec["text"]
        else:
            _ffmpeg(["-f", "lavfi", "-i", f"anoisesrc=d={spec['seconds']}:c=pink:a=0.1",
                     "-ac", "2", "-f", "mp3", out])

    def upload(self, path: Path) -> str:
        url = f"https://dry.run/upload/{_sha(Path(path))}"
        with self._lock:
            self.uploads[url] = Path(path)
            self.uploads_file.write_text(
                json.dumps({key: str(value) for key, value in self.uploads.items()}, indent=2)
            )
        return url
