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
from tih.story.critique import (
    Change,
    ChangeList,
    Comparison,
    EndingLabel,
    EpisodeCritique,
    Finding,
    SeasonCritique,
    TruthCritique,
)
from tih.story.season import EpisodeBatch, SeasonPlan

from test_season import details_of, spine_of, valid_plan

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

    def sent(self, index):
        return self.calls[index]["messages"][0]["content"]


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
    assert client.calls[0]["max_tokens"] == 64000


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


def weak_ending(plan, episode=3):
    return Finding(
        check="E5",
        episode=episode,
        quote=plan.episodes[episode - 1].cliff,
        problem="No threat is left hanging.",
        verdict="no",
    )


def run_replies(draft, revised, batch=10, true_story=True, findings=None, changes=None):
    """Replies for one full Story Room run, in call order."""
    details = details_of(draft)
    replies = [reply(spine_of(draft))]
    for start in range(0, len(details), batch):
        replies.append(reply(EpisodeBatch(episodes=details[start : start + batch])))
    findings = [weak_ending(draft)] if findings is None else findings
    labels = [EndingLabel(number=e.number, label="opens_question") for e in draft.episodes]
    replies.append(reply(EpisodeCritique(findings=findings, ending_labels=labels)))
    replies.append(
        reply(SeasonCritique(findings=[], top_fixes=["Fix episode 3."], verdict="consider"))
    )
    if true_story:
        replies.append(reply(TruthCritique(findings=[])))
    if changes is None:
        changes = [Change(finding="F1", action="fixed", note="New ending.")]
    replies.append(reply(ChangeList(changes=changes)))
    replies.append(reply(revised))
    return replies


def compare_replies():
    return [
        reply(Comparison(why="B is sharper.", better="B")),
        reply(Comparison(why="A is sharper.", better="A")),
    ]


def revised_plan():
    plan = valid_plan()
    plan.episodes[2].cliff = "The stranger says her name, and she runs."
    return plan


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


def read(season, name):
    return (season.paths.stage_dir("story_room") / name).read_text("utf-8")


def test_story_room_runs_every_step_and_writes_files(season, tmp_path, monkeypatch):
    client = FakeClient(*run_replies(valid_plan(), revised_plan()), *compare_replies())
    llm = use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)

    roles = [r.settings["role"] for r in llm.ledger.current()]
    assert roles == [
        "showrunner_spine",
        "showrunner_episodes",
        "showrunner_episodes",
        "showrunner_episodes",
        "critic_episodes",
        "critic_season",
        "critic_truth",
        "showrunner_changes",
        "showrunner_revise",
        "compare",
        "compare",
    ]
    final = yaml.safe_load(read(season, "season_plan.yaml"))
    draft = yaml.safe_load(read(season, "draft_plan.yaml"))
    assert final["episodes"][2]["cliff"] == "The stranger says her name, and she runs."
    assert draft["episodes"][2]["cliff"] == valid_plan().episodes[2].cliff
    assert "The Shot and the Knife" in read(season, "season_plan.md")
    assert "## Unresolved" not in read(season, "season_plan.md")
    assert yaml.safe_load(read(season, "season_ledger.yaml"))["setups"][0]["id"] == "card"
    assert yaml.safe_load(read(season, "bible.yaml"))["open_questions"] == [
        "Who was the card for?"
    ]
    assert yaml.safe_load(read(season, "risks.yaml")) == ["Confirm the names before use."]
    assert "Blind comparison: **revision**" in read(season, "critique.md")
    assert "F1" in read(season, "changes.md") and "New ending." in read(season, "changes.md")


def test_each_step_sees_the_work_before_it(season, tmp_path, monkeypatch):
    client = FakeClient(*run_replies(valid_plan(), revised_plan()), *compare_replies())
    use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)

    assert "Write episodes 11 to 20." in client.sent(2)
    assert "<season_spine>" in client.sent(2)
    assert "A hand closes on a letter in episode 10." in client.sent(2)
    assert "E1: The hook is a moment" in client.sent(4)
    assert "<source>" not in client.sent(4)
    assert "T1: Nothing stated" in client.sent(6) and "<source>" in client.sent(6)
    assert "F1" in client.sent(7) and "No threat is left hanging." in client.sent(7)
    assert "New ending." in client.sent(8) and "<source>" in client.sent(8)
    assert "<plan_a>" in client.sent(9) and "<plan_b>" in client.sent(9)


def test_single_pass_plans_in_one_call_then_uses_the_same_critic(season, tmp_path, monkeypatch):
    replies = run_replies(valid_plan(), revised_plan())
    client = FakeClient(reply(valid_plan()), *replies[4:], *compare_replies())
    llm = use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room_single(season)

    roles = [r.settings["role"] for r in llm.ledger.current()]
    assert roles == [
        "showrunner_single",
        "critic_episodes",
        "critic_season",
        "critic_truth",
        "showrunner_changes",
        "showrunner_revise",
        "compare",
        "compare",
    ]
    assert client.calls[0]["output_format"] is SeasonPlan
    assert "in one pass" in client.sent(0)
    stage_dir = season.paths.stage_dir("story_room_single")
    final = yaml.safe_load((stage_dir / "season_plan.yaml").read_text("utf-8"))
    assert final["episodes"][2]["cliff"] == "The stranger says her name, and she runs."
    assert (stage_dir / "critique.md").exists()
    assert not (season.paths.stage_dir("story_room") / "season_plan.yaml").exists()


def test_single_pass_overwrites_model_written_walls(season, tmp_path, monkeypatch):
    draft = valid_plan()
    draft.episodes[0].wall = "late"
    replies = run_replies(valid_plan(), revised_plan(), findings=[])
    client = FakeClient(reply(draft), *replies[4:], *compare_replies())
    use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room_single(season)
    assert "must have wall" not in client.sent(4)


def test_declined_hard_finding_is_shown_as_unresolved(season, tmp_path, monkeypatch):
    changes = [Change(finding="F1", action="declined", note="The ending works.")]
    client = FakeClient(
        *run_replies(valid_plan(), valid_plan(), changes=changes), *compare_replies()
    )
    use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)
    page = read(season, "season_plan.md")
    assert "## Unresolved" in page and "F1 (E5, ep 3)" in page


def test_rule_violation_in_draft_reaches_the_showrunner(season, tmp_path, monkeypatch):
    draft = valid_plan()
    draft.episodes[9].reveals = ["the shot"]
    client = FakeClient(
        *run_replies(draft, revised_plan(), findings=[]), *compare_replies()
    )
    use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)
    assert "held_back_early" in client.sent(7)


def test_broken_revision_gets_one_repair_that_sees_the_plan(season, tmp_path, monkeypatch):
    broken = revised_plan()
    broken.episodes[9].reveals = ["the shot"]
    client = FakeClient(
        *run_replies(valid_plan(), broken), reply(revised_plan()), *compare_replies()
    )
    use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)
    assert "held_back_early" in client.sent(9)
    assert "The stranger says her name, and she runs." in client.sent(9)
    assert yaml.safe_load(read(season, "season_plan.yaml"))["episodes"][9]["reveals"] == []


def test_story_room_stops_when_repair_also_fails(season, tmp_path, monkeypatch):
    broken = revised_plan()
    broken.episodes[9].reveals = ["the shot"]
    client = FakeClient(*run_replies(valid_plan(), broken), reply(broken))
    use_client(monkeypatch, tmp_path, client)
    with pytest.raises(story_room.StoryRoomFailed, match="held_back_early"):
        story_room.run_story_room(season)
    assert not (season.paths.stage_dir("story_room") / "season_plan.md").exists()


def test_batch_with_wrong_episodes_stops_the_stage(season, tmp_path, monkeypatch):
    replies = run_replies(valid_plan(), revised_plan())
    replies[2] = replies[1]
    use_client(monkeypatch, tmp_path, FakeClient(*replies))
    with pytest.raises(story_room.StoryRoomFailed, match="11"):
        story_room.run_story_room(season)


def test_any_series_runs_from_its_own_folder(tmp_path, monkeypatch):
    series = tmp_path / "series" / "harbor"
    (series / "source").mkdir(parents=True)
    (series / "series.yaml").write_text(
        "title: Harbor Lights\nepisodes: 2\nsource_format: freeform\n"
        "tone: Quiet dread.\nstory_notes: [The keeper hides a letter.]\n"
        "ending_convention: End on the lamp going dark.\n"
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
        episode.title = f"Working title {episode.number}"
    plan.held_back = []
    plan.knowledge = []
    client = FakeClient(
        *run_replies(plan, plan, true_story=False, findings=[], changes=[]),
        *compare_replies(),
    )
    ledger = Ledger(tmp_path / "runs" / "harbor" / "calls.jsonl")
    llm = LlmContext(ctx.paths, ledger, BudgetGuard(ledger), client, series / "models.yaml")
    monkeypatch.setattr(story_room.LlmContext, "for_run", lambda paths: llm)
    story_room.run_story_room(ctx)

    page = (ctx.paths.stage_dir("story_room") / "season_plan.md").read_text("utf-8")
    assert page.startswith("# Harbor Lights, season plan")
    assert "Working title 2" in page
    assert len(client.calls) == 8
    assert "critic_truth" not in [r.settings["role"] for r in ledger.current()]
    for text in ("The keeper hides a letter.", "Quiet dread.", "End on the lamp going dark."):
        assert text in client.sent(0)


@pytest.mark.parametrize("name", ["lincoln", "booth", "atzerodt", "seward", "surratt"])
def test_engine_contains_no_story_names(name):
    files = list((ROOT / "tih").rglob("*.py")) + list((ROOT / "prompts").glob("*.md"))
    assert not [f.name for f in files if name in f.read_text("utf-8").lower()]
