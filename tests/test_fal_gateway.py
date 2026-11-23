import json
from pathlib import Path

import pytest

from tih.assets import ShotRef, save_asset
from tih.budget import BudgetExceeded, BudgetGuard
from tih.ledger import Ledger
from tih.paths import RunPaths
from tih.providers.fal import (
    GatewayContext,
    ProviderError,
    ProviderRefused,
    generate,
    is_refusal,
)
from tih.providers.pricing import read_cost

URL = "https://v3.fal.media/files/out/video.mp4"
SHOT = ShotRef(episode=1, clip=2, shot=3)
MODELS = """
endpoints:
  fal/video: {unit: seconds, rate: 0.14}
  fal/backup: {unit: seconds, rate: 0.14}
  fal/strict: {unit: seconds, rate: 0.14}
  fal/usage: {unit: usage}
  wan:
    unit: seconds
    tier_key: resolution
    rates: {"480p": 0.05, "720p": 0.10, "1080p": 0.20}
  kling:
    unit: seconds
    tier_key: generate_audio
    rates: {"false": 0.112, "true": 0.168}
  h3: {unit: seconds, rate: 0.08, padding_seconds: 0.7}
  sync: {unit: minutes, rate: 8.0}
  music: {unit: minutes, rate: 0.6, round_up: true}
  tts: {unit: 1000 characters, rate: 0.1}
  voice: {unit: cloned voices, rate: 3.0}
  seedvr: {unit: megapixels, rate: 0.001}
"""


class FakeTransport:
    def __init__(self):
        self.submitted = []
        self.waited = []
        self.outcomes = {}
        self.wait_error = None

    def submit(self, endpoint, arguments):
        self.submitted.append((endpoint, arguments))
        outcome = self.outcomes.get(endpoint)
        if isinstance(outcome, ProviderRefused) and outcome.at_submit:
            raise outcome
        return f"req-{len(self.submitted)}"

    def wait(self, endpoint, request_id):
        self.waited.append((endpoint, request_id))
        if self.wait_error:
            error, self.wait_error = self.wait_error, None
            raise error
        outcome = self.outcomes.get(endpoint, {"video": {"url": URL}})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def download(self, url, destination):
        destination.write_bytes(f"bytes of {url}".encode())


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def models_file(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(MODELS)
    return path


@pytest.fixture
def ctx(tmp_path, transport, models_file):
    ledger = Ledger(tmp_path / "lincoln" / "calls.jsonl")
    return GatewayContext(
        paths=RunPaths("lincoln", 1, root=tmp_path),
        ledger=ledger,
        budget=BudgetGuard(ledger),
        transport=transport,
        models_file=models_file,
    )


ARGS = {
    "prompt": "MCU Booth in the rain",
    "duration": "5",
    "image_url": "https://example.com/key.png",
}


def run(ctx, endpoint="fal/video", **kwargs):
    return generate(ctx, "video", endpoint, ARGS, SHOT, estimate_usd=1.0, **kwargs)


def test_success_logs_cost(ctx):
    result = run(ctx)
    record = ctx.ledger.current()[0]
    assert result.status == "ok"
    assert result.output_paths[0].read_bytes() == f"bytes of {URL}".encode()
    assert (record.status, record.cost_usd, record.cost_source) == (
        "ok",
        0.7,
        "unit_price",
    )
    assert record.request_id == "req-1"
    assert record.prompt == ARGS["prompt"]
    assert record.inputs == [ARGS["image_url"]]
    assert record.settings == {"duration": "5", "take": 0}
    assert (record.episode, record.clip, record.shot, record.step) == (1, 2, 3, "video")
    assert record.time_taken_s is not None
    assert record.output_file == str(result.output_paths[0])


def test_request_id_saved_before_waiting(ctx, transport):
    transport.wait_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run(ctx)
    record = ctx.ledger.current()[0]
    assert (record.status, record.request_id) == ("submitted", "req-1")


def test_reuses_previous_request_after_crash(ctx, transport):
    transport.wait_error = ConnectionError("network lost")
    with pytest.raises(ConnectionError):
        run(ctx)
    result = run(ctx)
    assert len(transport.submitted) == 1
    assert transport.waited[-1] == ("fal/video", "req-1")
    assert result.status == "ok"
    assert len(ctx.ledger.current()) == 1


def test_completed_call_is_not_paid_for_twice(ctx, transport):
    first = run(ctx)
    second = run(ctx)
    assert len(transport.submitted) == 1
    assert second.output_paths == first.output_paths
    assert second.payload == first.payload


def test_new_take_is_a_new_paid_call(ctx, transport):
    run(ctx, take=0)
    run(ctx, take=1)
    assert len(transport.submitted) == 2


def test_refusal_is_blocked_and_uses_fallback(ctx, transport):
    transport.outcomes["fal/strict"] = ProviderRefused("content policy")
    result = run(ctx, endpoint="fal/strict", fallbacks=("fal/backup",))
    records = ctx.ledger.current()
    assert result.status == "ok"
    assert [r.status for r in records] == ["blocked", "ok"]
    assert records[0].reject_reason == "content policy"
    assert records[0].kept is False
    assert records[1].model == "fal/backup"


def test_refusal_is_not_retried_on_same_endpoint(ctx, transport):
    transport.outcomes["fal/strict"] = ProviderRefused("content policy")
    assert run(ctx, endpoint="fal/strict").status == "blocked"
    assert run(ctx, endpoint="fal/strict").status == "blocked"
    assert len(transport.submitted) == 1


def test_refusal_at_submit_is_blocked(ctx, transport):
    transport.outcomes["fal/strict"] = ProviderRefused("policy", at_submit=True)
    assert run(ctx, endpoint="fal/strict").status == "blocked"
    assert ctx.ledger.current()[0].status == "blocked"


def test_refusal_with_unknown_charge_still_counts(ctx, transport):
    transport.outcomes["fal/strict"] = ProviderRefused("content policy")
    run(ctx, endpoint="fal/strict")
    assert ctx.budget.spent() == 1.0


def test_all_nsfw_flagged_output_is_blocked(ctx, transport):
    transport.outcomes["fal/video"] = {
        "images": [{"url": URL}],
        "has_nsfw_concepts": [True],
    }
    assert run(ctx).status == "blocked"


def test_empty_output_status(ctx, transport):
    transport.outcomes["fal/video"] = {"video": None}
    result = run(ctx)
    assert result.status == "empty"
    assert ctx.ledger.current()[0].status == "empty"


def test_empty_output_uses_fallback(ctx, transport):
    transport.outcomes["fal/video"] = {}
    result = run(ctx, fallbacks=("fal/backup",))
    assert result.status == "ok"


def test_text_result_allowed_when_no_files_expected(ctx, transport):
    transport.outcomes["fal/video"] = {"output": "score: 8"}
    result = run(ctx, expects_files=False)
    assert (result.status, result.output_paths) == ("ok", [])


def test_server_error_is_logged_free_and_not_fallen_back(ctx, transport):
    transport.outcomes["fal/video"] = ProviderError("down", status_code=503)
    result = run(ctx, fallbacks=("fal/backup",))
    record = ctx.ledger.current()[0]
    assert result.status == "error"
    assert (record.status, record.cost_usd) == ("error", 0.0)
    assert len(transport.submitted) == 1


def test_client_error_keeps_estimate(ctx, transport):
    transport.outcomes["fal/video"] = ProviderError("bad request", status_code=400)
    run(ctx)
    assert ctx.budget.spent() == 1.0


def test_budget_blocks_before_submit(tmp_path, transport, models_file):
    ledger = Ledger(tmp_path / "lincoln" / "calls.jsonl")
    ctx = GatewayContext(
        paths=RunPaths("lincoln", 1, root=tmp_path),
        ledger=ledger,
        budget=BudgetGuard(ledger, stop=0.5),
        transport=transport,
        models_file=models_file,
    )
    with pytest.raises(BudgetExceeded):
        run(ctx)
    assert transport.submitted == []
    assert ledger.current() == []


def test_usage_billed_model_uses_conservative_estimate(ctx):
    run(ctx, endpoint="fal/usage")
    record = ctx.ledger.current()[0]
    assert (record.cost_usd, record.cost_source) == (1.0, "estimated")


def test_endpoint_missing_from_models_uses_estimate(ctx):
    run(ctx, endpoint="fal/unlisted")
    assert ctx.ledger.current()[0].cost_source == "estimated"


@pytest.mark.parametrize(
    "endpoint,arguments,quantity,expected",
    [
        ("wan", {"resolution": "1080p", "duration": 5}, None, 1.0),
        ("wan", {"resolution": "480p", "duration": "5"}, None, 0.25),
        ("kling", {"generate_audio": True, "duration": "5"}, None, 0.84),
        ("kling", {"generate_audio": False, "duration": "10s"}, None, 1.12),
        ("h3", {"duration": 5}, None, 0.456),
        ("sync", {}, 0.5, 4.0),
        ("sync", {"duration": 30}, None, 4.0),
        ("music", {}, 0.5, 0.6),
        ("tts", {"text": "x" * 2000}, None, 0.2),
        ("voice", {}, None, 3.0),
        ("seedvr", {}, 311.0, 0.311),
    ],
)
def test_read_cost_from_models(models_file, endpoint, arguments, quantity, expected):
    cost, source = read_cost(models_file, endpoint, arguments, quantity, 9.0)
    assert cost == pytest.approx(expected)
    assert source == "unit_price"


@pytest.mark.parametrize(
    "endpoint,arguments",
    [
        ("wan", {"duration": 5}),
        ("wan", {"resolution": "4K", "duration": 5}),
        ("wan", {"resolution": "1080p"}),
        ("fal/usage", {"duration": 5}),
        ("seedvr", {}),
        ("unlisted", {"duration": 5}),
    ],
)
def test_read_cost_falls_back_to_estimate(models_file, endpoint, arguments):
    assert read_cost(models_file, endpoint, arguments, None, 9.0) == (9.0, "estimated")


def test_lincoln_models_file_prices():
    path = Path(__file__).parents[1] / "series" / "lincoln" / "models.yaml"
    cases = [
        ("alibaba/wan-3.0/image-to-video", {"resolution": "1080p", "duration": 5}, 1.0),
        (
            "fal-ai/kling-video/v3/pro/image-to-video",
            {"generate_audio": True, "duration": "5"},
            0.84,
        ),
        ("minimax/h3-max/image-to-video", {"resolution": "768P", "duration": 5}, 0.456),
    ]
    for endpoint, arguments, expected in cases:
        assert read_cost(path, endpoint, arguments, None, 9.0)[0] == pytest.approx(expected)
    assert read_cost(path, "google/nano-banana-2.1/edit", {}, None, 9.0) == (
        9.0,
        "estimated",
    )


@pytest.mark.parametrize(
    "status,error_type,message,expected",
    [
        (422, "content_policy_violation", "nope", True),
        (422, None, "The content could not be processed by the safety checker", True),
        (400, None, "Sensitive content detected", True),
        (422, None, "duration must be 5 or 10", False),
        (500, None, "safety system crashed", False),
    ],
)
def test_is_refusal(status, error_type, message, expected):
    assert is_refusal(status, error_type, message) is expected


def test_save_asset_is_content_addressed(ctx):
    first = save_asset(URL, ctx, SHOT)
    second = save_asset(URL, ctx, SHOT)
    assert first == second
    assert first.suffix == ".mp4"
    assert len(first.stem) == 64
    assert first.parent == ctx.paths.root / "lincoln" / "assets" / "ep01" / "clip02" / "shot3"
    assert not list(first.parent.glob("*.tmp"))


def test_save_asset_rejects_non_https(ctx):
    with pytest.raises(ValueError, match="https"):
        save_asset("file:///etc/passwd", ctx, SHOT)


def test_save_asset_sanitises_suffix(ctx):
    path = save_asset("https://x.test/a.mp4%2F..%2Fevil", ctx, SHOT)
    assert path.parent == ctx.paths.root / "lincoln" / "assets" / "ep01" / "clip02" / "shot3"
    assert path.suffix == ""


def test_payload_is_stored_for_reuse(ctx):
    result = run(ctx)
    stored = ctx.paths.root / "lincoln" / "payloads" / f"{result.call_id}.json"
    assert json.loads(stored.read_text()) == result.payload
