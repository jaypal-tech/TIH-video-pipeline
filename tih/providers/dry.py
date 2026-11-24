"""A stand-in for fal that makes placeholder media locally, for $0.

`tih rehearse` runs a whole episode through every stage with this transport:
stills are flat colour frames labelled with their prompt, videos hold the
still, speech is a tone as long as the line, transcripts echo the line. The
ledger still prices every call from models.yaml, so a rehearsal tells you
what the real run should cost, and the final cut shows timing, captions and
cards exactly as they will be.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

PAUSE_TAG = re.compile(r"<#[0-9.]+#>")
SECONDS_PER_WORD = 0.38


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
            return {"text": text}
        if kind == "voice_design":
            url = self._url(request_id, ".mp3", {"kind": "tone", "seconds": 2.0, "text": ""})
            return {"custom_voice_id": f"dry-voice-{request_id[4:12]}", "audio": {"url": url}}
        if kind == "speech":
            text = PAUSE_TAG.sub(" ", str(arguments.get("text", "")))
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
        if kind == "video":
            image = self.uploads[str(arguments["image_url"])]
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
            label_file = self.workdir / f"{hashlib.sha256(spec['label'].encode()).hexdigest()[:12]}.txt"
            label_file.write_text(spec["label"], encoding="utf-8")
            _ffmpeg(["-f", "lavfi", "-i", f"color=c={colour}:s=720x1280", "-frames:v", "1",
                     "-vf", f"drawtext=textfile={label_file}:fontcolor=white:fontsize=28:x=20:y=60",
                     "-f", "image2", "-c:v", "png", out])
        elif kind == "hold":
            _ffmpeg(["-loop", "1", "-i", spec["source"], "-t", f"{spec['seconds']}", "-r", "24",
                     "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                     "-f", "mp4", out])
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
        self.uploads[url] = Path(path)
        self.uploads_file.write_text(
            json.dumps({key: str(value) for key, value in self.uploads.items()}, indent=2)
        )
        return url
