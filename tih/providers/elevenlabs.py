"""Direct ElevenLabs calls, for the steps fal's text filter refuses.

fal blocks real script lines (a murder plot) on every speech model it hosts,
so voice design and speech go straight to ElevenLabs. Steps whose endpoint
starts with `elevenlabs-api/` come here; everything else stays on fal. The
gateway logs, prices and budget-checks these calls exactly like fal ones.

ElevenLabs answers at once with the audio, so `wait` makes the call and keeps
the audio in memory under a placeholder https address until `download` saves it.
"""

import base64
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from tih.providers.fal import ELEVENLABS_PREFIX, ProviderError, ProviderRefused, Transport, is_refusal

API = "https://api.elevenlabs.io/v1"
LOCAL = "https://elevenlabs.invalid/"  # https, so save_asset accepts it; never fetched
OUTPUT_FORMAT = "mp3_44100_128"
DESIGN_TEXT_MIN, DESIGN_TEXT_MAX = 100, 1000  # the design endpoint's sample text limits
TIMEOUT_S = 300


def elevenlabs_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY") or dotenv_values(Path.cwd() / ".env").get("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    return key


def _design_text(text: str) -> str:
    """The character's own words, repeated until the design endpoint accepts them."""
    sample = text.strip()
    while sample and len(sample) < DESIGN_TEXT_MIN:
        sample = f"{sample} {text.strip()}"
    return sample[:DESIGN_TEXT_MAX]


def _failure(response: httpx.Response) -> Exception:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        kind, message = detail.get("status"), str(detail.get("message", detail))
    else:
        kind, message = None, str(detail or response.text)
    message = message[:500]
    if is_refusal(response.status_code, kind, message):
        return ProviderRefused(message, at_submit=True)
    return ProviderError(f"ElevenLabs {response.status_code}: {message}", status_code=response.status_code)


class ElevenLabsTransport:
    """The key is read on the first real call, so reused takes need none."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self.requests: dict[str, dict[str, Any]] = {}
        self.audio: dict[str, bytes] = {}

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=API, headers={"xi-api-key": elevenlabs_key()}, timeout=TIMEOUT_S)
        return self._client

    def submit(self, endpoint: str, arguments: dict[str, Any]) -> str:
        request_id = uuid.uuid4().hex
        self.requests[request_id] = dict(arguments)
        return request_id

    def wait(self, endpoint: str, request_id: str) -> dict[str, Any]:
        if request_id not in self.requests:
            raise ProviderError("ElevenLabs request was lost when the program stopped; run the step again")
        arguments = self.requests.pop(request_id)
        name = endpoint.removeprefix(ELEVENLABS_PREFIX)
        if name == "text-to-speech":
            return self._speech(request_id, arguments)
        if name == "voice-design":
            return self._design(request_id, arguments)
        raise ProviderError(f"Unknown ElevenLabs endpoint {endpoint}")

    def _post(self, path: str, body: dict[str, Any], params: dict[str, str] | None = None) -> httpx.Response:
        response = self.client.post(path, json=body, params=params)
        if response.status_code >= 400:
            raise _failure(response)
        return response

    def _keep(self, request_id: str, data: bytes) -> dict[str, str]:
        url = f"{LOCAL}{request_id}.mp3"
        self.audio[url] = data
        return {"url": url}

    def _speech(self, request_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = dict(arguments)
        voice_id = body.pop("voice_id")
        output_format = body.pop("output_format", OUTPUT_FORMAT)
        response = self._post(f"/text-to-speech/{voice_id}", body, {"output_format": output_format})
        return {"audio": self._keep(request_id, response.content), "request_id": response.headers.get("request-id")}

    def _design(self, request_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = dict(arguments)
        voice_name = body.pop("voice_name")
        body["text"] = _design_text(str(body.get("text", "")))
        designed = self._post("/text-to-voice/design", body, {"output_format": OUTPUT_FORMAT}).json()
        preview = designed["previews"][0]
        saved = self._post("/text-to-voice", {
            "voice_name": voice_name,
            "voice_description": body["voice_description"],
            "generated_voice_id": preview["generated_voice_id"],
        }).json()
        return {
            "custom_voice_id": saved["voice_id"],
            "generated_voice_id": preview["generated_voice_id"],
            "text": designed.get("text", body["text"]),
            "audio": self._keep(request_id, base64.b64decode(preview["audio_base_64"])),
        }

    def download(self, url: str, destination: Path) -> None:
        destination.write_bytes(self.audio.pop(url))

    def upload(self, path: Path) -> str:
        raise ProviderError("ElevenLabs steps take no uploaded files")


class RoutedTransport:
    """`elevenlabs-api/` steps to ElevenLabs; every other step, and all uploads, to fal."""

    def __init__(self, fal: Transport, elevenlabs: Transport) -> None:
        self.fal = fal
        self.elevenlabs = elevenlabs

    def _for(self, endpoint: str) -> Transport:
        return self.elevenlabs if endpoint.startswith(ELEVENLABS_PREFIX) else self.fal

    def submit(self, endpoint: str, arguments: dict[str, Any]) -> str:
        return self._for(endpoint).submit(endpoint, arguments)

    def wait(self, endpoint: str, request_id: str) -> dict[str, Any]:
        return self._for(endpoint).wait(endpoint, request_id)

    def download(self, url: str, destination: Path) -> None:
        (self.elevenlabs if url.startswith(LOCAL) else self.fal).download(url, destination)

    def upload(self, path: Path) -> str:
        return self.fal.upload(path)
