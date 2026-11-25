import base64
import json

import httpx
import pytest

from tih.paths import RunPaths
from tih.production import gateway_for
from tih.providers.elevenlabs import ElevenLabsTransport, RoutedTransport
from tih.providers.fal import ProviderError, ProviderRefused, provider_name
from tih.stages.registry import RunContext


def _transport(handler):
    client = httpx.Client(base_url="https://api.elevenlabs.io/v1", transport=httpx.MockTransport(handler))
    return ElevenLabsTransport(client=client)


def test_speech_posts_the_line_to_the_voice_and_keeps_the_audio(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"mp3-bytes")

    eleven = _transport(handler)
    request_id = eleven.submit(
        "elevenlabs-api/text-to-speech",
        {"text": "You could hit him from here.", "voice_id": "v1", "model_id": "eleven_v3",
         "voice_settings": {"speed": 0.95}, "output_format": "mp3_44100_128"},
    )
    payload = eleven.wait("elevenlabs-api/text-to-speech", request_id)
    eleven.download(payload["audio"]["url"], tmp_path / "line.mp3")

    assert seen[0].url.path == "/v1/text-to-speech/v1"
    assert seen[0].url.params["output_format"] == "mp3_44100_128"
    assert json.loads(seen[0].content) == {
        "text": "You could hit him from here.", "model_id": "eleven_v3", "voice_settings": {"speed": 0.95},
    }
    assert payload["audio"]["url"].startswith("https://")
    assert (tmp_path / "line.mp3").read_bytes() == b"mp3-bytes"


def test_voice_design_lengthens_short_samples_and_saves_the_first_voice(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/design"):
            previews = [{"audio_base_64": base64.b64encode(b"preview-1").decode(), "generated_voice_id": "g1",
                         "media_type": "audio/mpeg", "duration_secs": 3.0},
                        {"audio_base_64": base64.b64encode(b"preview-2").decode(), "generated_voice_id": "g2",
                         "media_type": "audio/mpeg", "duration_secs": 3.0}]
            return httpx.Response(200, json={"previews": previews, "text": "sample"})
        return httpx.Response(200, json={"voice_id": "saved-voice"})

    eleven = _transport(handler)
    line = "Not in this crowd. And a thousand men on top of me."
    request_id = eleven.submit(
        "elevenlabs-api/voice-design",
        {"voice_description": "a young man, low voice", "text": line, "model_id": "eleven_ttv_v3",
         "voice_name": "powell"},
    )
    payload = eleven.wait("elevenlabs-api/voice-design", request_id)
    eleven.download(payload["audio"]["url"], tmp_path / "preview.mp3")

    design = json.loads(seen[0].content)
    assert len(design["text"]) >= 100 and design["text"].startswith(line) and "voice_name" not in design
    assert json.loads(seen[1].content) == {
        "voice_name": "powell", "voice_description": "a young man, low voice", "generated_voice_id": "g1",
    }
    assert payload["custom_voice_id"] == "saved-voice"
    assert (tmp_path / "preview.mp3").read_bytes() == b"preview-1"


def test_refusals_and_errors_are_told_apart():
    def refuse(request):
        return httpx.Response(400, json={"detail": {"status": "content_policy_violation", "message": "flagged"}})

    def broken(request):
        return httpx.Response(401, json={"detail": {"status": "invalid_api_key", "message": "bad key"}})

    for handler, error in ((refuse, ProviderRefused), (broken, ProviderError)):
        eleven = _transport(handler)
        request_id = eleven.submit("elevenlabs-api/text-to-speech", {"text": "x", "voice_id": "v"})
        with pytest.raises(error):
            eleven.wait("elevenlabs-api/text-to-speech", request_id)


def test_a_request_lost_when_the_program_stopped_is_an_error():
    with pytest.raises(ProviderError):
        _transport(lambda request: httpx.Response(200)).wait("elevenlabs-api/text-to-speech", "unknown")


class _Recorder:
    def __init__(self, name):
        self.name, self.calls = name, []

    def submit(self, endpoint, arguments):
        self.calls.append("submit")
        return self.name

    def wait(self, endpoint, request_id):
        self.calls.append("wait")
        return {}

    def download(self, url, destination):
        self.calls.append("download")

    def upload(self, path):
        self.calls.append("upload")
        return "https://fal.media/x"


def test_routing_sends_only_elevenlabs_api_steps_to_elevenlabs(tmp_path):
    fal, eleven = _Recorder("fal"), _Recorder("eleven")
    routed = RoutedTransport(fal, eleven)
    assert routed.submit("elevenlabs-api/text-to-speech", {}) == "eleven"
    assert routed.submit("elevenlabs/music/v2.5", {}) == "fal"  # fal's own ElevenLabs endpoints stay on fal
    routed.wait("elevenlabs-api/voice-design", "eleven")
    routed.upload(tmp_path / "a.mp3")
    routed.download("https://elevenlabs.invalid/abc.mp3", tmp_path / "a.mp3")
    routed.download("https://v3.fal.media/abc.mp3", tmp_path / "b.mp3")
    assert eleven.calls == ["submit", "wait", "download"]
    assert fal.calls == ["submit", "upload", "download"]


def test_ledger_names_the_provider_each_call_went_to():
    assert provider_name("elevenlabs-api/text-to-speech") == "elevenlabs"
    assert provider_name("elevenlabs/music/v2.5") == "fal"
    assert provider_name("fal-ai/kling-video/v3/pro/image-to-video") == "fal"


def test_real_runs_route_through_both_providers(tmp_path):
    gateway = gateway_for(RunContext(paths=RunPaths("lincoln", 1, root=tmp_path)))
    assert isinstance(gateway.transport, RoutedTransport)
