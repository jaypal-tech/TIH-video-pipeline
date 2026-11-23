from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import yaml

from tih.budget import BudgetExceeded, BudgetGuard
from tih.ledger import Ledger
from tih.llm import LlmContext, LlmError, ask_json
from tih.paths import RunPaths
from tih.providers.pricing import token_cost
from tih.stages import story_room
from tih.stages.record import run_record
from tih.stages.registry import RunContext
from tih.story.season import SeasonPlan

from test_season import valid_plan

ROOT = Path(__file__).parents[1]
MODELS = ROOT / "series" / "lincoln" / "models.yaml"


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        if isinstance(self.message, Exception):
            raise self.message
        return self.message


class FakeClient:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.messages = SimpleNamespace(stream=self.stream)

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.outputs.pop(0))


def reply(parsed, stop_reason="end_turn", number=1):
    return SimpleNamespace(
        id=f"msg_{number}",
        stop_reason=stop_reason,
        parsed_output=parsed,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=2000),
    )


def make_llm(tmp_path, client, stop=3000.0):
    paths = RunPaths("lincoln", None, root=tmp_path)
    ledger = Ledger(tmp_path / "lincoln" / "calls.jsonl")
    return LlmContext(paths, ledger, BudgetGuard(ledger, stop=stop), client, MODELS)


def ask(llm, take=0):
    return ask_json(llm, "showrunner", "system", "user", SeasonPlan, take=take)


def test_token_cost_uses_models_file():
    assert token_cost(MODELS, "claude-opus-5-5", 1000, 2000) == pytest.approx(0.044)


def test_call_is_logged_with_real_token_cost(tmp_path):
    client = FakeClient(reply(valid_plan()))
    llm = make_llm(tmp_path, client)
    result = ask(llm)
    record = llm.ledger.current()[0]
    assert result.promise == valid_plan().promise
    assert (record.provider, record.model, record.status) == (
        "anthropic",
        "claude-opus-5-5",
        "ok",
    )
    assert record.cost_usd == pytest.approx(0.044)
    assert record.cost_source == "usage"
    assert record.settings["role"] == "showrunner"
    assert client.calls[0]["output_format"] is SeasonPlan


def test_identical_call_is_not_paid_twice(tmp_path):
    client = FakeClient(reply(valid_plan()))
    llm = make_llm(tmp_path, client)
    first = ask(llm)
    second = ask(llm)
    assert first == second
    assert len(client.calls) == 1


def test_new_take_is_a_new_call(tmp_path):
    client = FakeClient(reply(valid_plan()), reply(valid_plan(), number=2))
    llm = make_llm(tmp_path, client)
    ask(llm, take=0)
    ask(llm, take=1)
    assert len(client.calls) == 2


def test_budget_blocks_before_calling_claude(tmp_path):
    client = FakeClient(reply(valid_plan()))
    llm = make_llm(tmp_path, client, stop=0.01)
    with pytest.raises(BudgetExceeded):
        ask(llm)
    assert client.calls == []


@pytest.mark.parametrize(
    "stop_reason,status", [("refusal", "blocked"), ("max_tokens", "error")]
)
def test_unfinished_reply_is_logged_and_raised(tmp_path, stop_reason, status):
    llm = make_llm(tmp_path, FakeClient(reply(None, stop_reason=stop_reason)))
    with pytest.raises(LlmError, match=stop_reason):
        ask(llm)
    record = llm.ledger.current()[0]
    assert record.status == status
    assert record.cost_usd == pytest.approx(0.044)


def test_api_status_error_is_logged_free(tmp_path):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.InternalServerError(
        "server error", response=httpx.Response(500, request=request), body=None
    )
    llm = make_llm(tmp_path, FakeClient(error))
    with pytest.raises(LlmError):
        ask(llm)
    record = llm.ledger.current()[0]
    assert (record.status, record.cost_usd) == ("error", 0.0)


@pytest.fixture
def season(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    ctx = RunContext(paths=RunPaths("lincoln", None, root=tmp_path))
    run_record(ctx)
    return ctx


def use_client(monkeypatch, tmp_path, client):
    llm = make_llm(tmp_path, client)
    monkeypatch.setattr(story_room.LlmContext, "for_run", lambda paths: llm)
    return llm


def test_story_room_writes_season_files(season, tmp_path, monkeypatch):
    use_client(monkeypatch, tmp_path, FakeClient(reply(valid_plan())))
    story_room.run_story_room(season)
    stage = season.paths.stage_dir("story_room")
    assert "The Shot and the Knife" in (stage / "season_plan.md").read_text("utf-8")
    assert len(yaml.safe_load((stage / "season_plan.yaml").read_text("utf-8"))["episodes"]) == 30
    ledger = yaml.safe_load((stage / "season_ledger.yaml").read_text("utf-8"))
    assert ledger["setups"][0]["id"] == "card"
    bible = yaml.safe_load((stage / "bible.yaml").read_text("utf-8"))
    assert bible["open_questions"] == ["Who was the card for?"]


def test_story_room_sends_problems_back_once(season, tmp_path, monkeypatch):
    broken = valid_plan()
    broken.episodes[9].reveals = ["the shot"]
    client = FakeClient(reply(broken), reply(valid_plan(), number=2))
    use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)
    assert len(client.calls) == 2
    assert "held_back_early" in client.calls[1]["messages"][0]["content"]


def test_story_room_stops_after_second_failure(season, tmp_path, monkeypatch):
    broken = valid_plan()
    broken.episodes[9].reveals = ["the shot"]
    client = FakeClient(reply(broken), reply(broken, number=2))
    use_client(monkeypatch, tmp_path, client)
    with pytest.raises(story_room.StoryRoomFailed, match="held_back_early"):
        story_room.run_story_room(season)
    stage = season.paths.stage_dir("story_room")
    assert not (stage / "season_plan.md").exists()


def test_any_series_runs_from_its_own_folder(tmp_path, monkeypatch):
    series = tmp_path / "series" / "harbor"
    (series / "source").mkdir(parents=True)
    (series / "series.yaml").write_text(
        "title: Harbor Lights\nepisodes: 2\nsource_format: freeform\n"
        "tone: Quiet dread.\nstory_notes: [The keeper hides a letter.]\n"
    )
    (series / "source" / "brief.txt").write_text("A lighthouse keeper hides a letter.")
    (series / "models.yaml").write_text(MODELS.read_text("utf-8"))
    monkeypatch.chdir(tmp_path)
    ctx = RunContext(paths=RunPaths("harbor", None, root=tmp_path / "runs"))
    run_record(ctx)

    plan = valid_plan()
    plan.episodes = plan.episodes[:2]
    for episode in plan.episodes:
        episode.wall = "none"
        episode.reveals = []
    plan.held_back = []
    plan.knowledge = []
    client = FakeClient(reply(plan))
    ledger = Ledger(tmp_path / "runs" / "harbor" / "calls.jsonl")
    llm = LlmContext(ctx.paths, ledger, BudgetGuard(ledger), client, series / "models.yaml")
    monkeypatch.setattr(story_room.LlmContext, "for_run", lambda paths: llm)
    story_room.run_story_room(ctx)

    page = (ctx.paths.stage_dir("story_room") / "season_plan.md").read_text("utf-8")
    assert page.startswith("# Harbor Lights, season plan")
    sent = client.calls[0]["messages"][0]["content"]
    assert "The keeper hides a letter." in sent and "Quiet dread." in sent


@pytest.mark.parametrize("name", ["lincoln", "booth", "atzerodt", "seward", "surratt"])
def test_engine_contains_no_story_names(name):
    files = list((ROOT / "tih").rglob("*.py")) + list((ROOT / "prompts").glob("*.md"))
    assert not [f.name for f in files if name in f.read_text("utf-8").lower()]
