import csv
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
    EndingLabel,
    EpisodeCritique,
    EpisodeRewrite,
    Finding,
    Recheck,
    RecheckResult,
    SeasonCritique,
    SeasonFields,
    SeasonRewrite,
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


def weak_ending(plan, episode=3, check="E5"):
    return Finding(
        check=check,
        episode=episode,
        quote=plan.episodes[episode - 1].cliff,
        problem="No threat is left hanging.",
        verdict="no",
    )


NEW_CLIFF = "The stranger says her name, and she runs."


def fixed_episode(number=3, cliff=NEW_CLIFF):
    episode = valid_plan().episodes[number - 1].model_copy()
    episode.cliff = cliff
    return episode


def episode_fix(action="fixed", episodes=None):
    episodes = [fixed_episode()] if episodes is None else episodes
    return EpisodeRewrite(
        changes=[Change(finding="F1", action=action, note="New ending.")], episodes=episodes
    )


def recheck(verdict="pass", finding="F1"):
    return Recheck(
        results=[
            RecheckResult(finding=finding, quote=NEW_CLIFF, reason="It opens a threat.", verdict=verdict)
        ]
    )


def run_replies(draft, *after, batch=10, true_story=True, findings=None, season_findings=()):
    """Replies for one Story Room run, in call order: planning, critique, then *after."""
    details = details_of(draft)
    replies = [reply(spine_of(draft))]
    for start in range(0, len(details), batch):
        replies.append(reply(EpisodeBatch(episodes=details[start : start + batch])))
    findings = [weak_ending(draft)] if findings is None else findings
    labels = [EndingLabel(number=e.number, label="opens_question") for e in draft.episodes]
    replies.append(reply(EpisodeCritique(findings=findings, ending_labels=labels)))
    replies.append(
        reply(
            SeasonCritique(
                findings=list(season_findings), top_fixes=["Fix episode 3."], verdict="consider"
            )
        )
    )
    if true_story:
        replies.append(reply(TruthCritique(findings=[])))
    return replies + [reply(item) for item in after]


def default_run():
    return run_replies(valid_plan(), episode_fix(), recheck())


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


def roles(llm):
    return [r.settings["role"] for r in llm.ledger.current()]


PLANNING = ["showrunner_spine"] + ["showrunner_episodes"] * 3
CRITIQUE = ["critic_episodes", "critic_season", "critic_truth"]


def test_story_room_fixes_must_fix_findings_and_writes_files(season, tmp_path, monkeypatch):
    llm = use_client(monkeypatch, tmp_path, FakeClient(*default_run()))
    story_room.run_story_room(season)

    assert roles(llm) == PLANNING + CRITIQUE + ["showrunner_fix_episodes", "critic_recheck"]
    final = yaml.safe_load(read(season, "season_plan.yaml"))
    draft = yaml.safe_load(read(season, "draft_plan.yaml"))
    assert final["episodes"][2]["cliff"] == NEW_CLIFF
    assert draft["episodes"][2]["cliff"] == valid_plan().episodes[2].cliff
    assert "The Shot and the Knife" in read(season, "season_plan.md")
    assert "## Unresolved" not in read(season, "season_plan.md")
    assert yaml.safe_load(read(season, "season_ledger.yaml"))["setups"][0]["id"] == "card"
    assert yaml.safe_load(read(season, "bible.yaml"))["open_questions"] == [
        "Who was the card for?"
    ]
    assert yaml.safe_load(read(season, "risks.yaml")) == ["Confirm the names before use."]
    changes = read(season, "changes.md")
    assert "**fixed**: New ending." in changes and "re-check: **pass**" in changes
    assert "Blind comparison" not in read(season, "critique.md")


def test_cost_log_is_written_as_csv(season, tmp_path, monkeypatch):
    use_client(monkeypatch, tmp_path, FakeClient(*default_run()))
    story_room.run_story_room(season)
    with (tmp_path / "lincoln" / "calls.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 9
    assert {row["cost_usd"] for row in rows} == {"0.044"}
    assert rows[-1]["settings"].count("critic_recheck") == 1


def test_only_must_fix_findings_reach_the_showrunner(season, tmp_path, monkeypatch):
    findings = [weak_ending(valid_plan()), weak_ending(valid_plan(), 4, check="E6")]
    client = FakeClient(*run_replies(valid_plan(), episode_fix(), recheck(), findings=findings))
    use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)

    assert "F1" in client.sent(7) and "No threat is left hanging." in client.sent(7)
    assert "F2" not in client.sent(7)
    assert "Rewrite episodes 3." in client.sent(7)
    assert "F2 (E6, ep 4)" in read(season, "critique.md")


def test_rewrite_changes_only_the_episodes_it_was_asked_for(season, tmp_path, monkeypatch):
    extra = fixed_episode(5, "An episode nobody asked to change.")
    client = FakeClient(*run_replies(valid_plan(), episode_fix(episodes=[fixed_episode(), extra]), recheck()))
    use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)
    final = yaml.safe_load(read(season, "season_plan.yaml"))
    assert final["episodes"][2]["cliff"] == NEW_CLIFF
    assert final["episodes"][4] == valid_plan().episodes[4].model_dump(mode="json")


def test_recheck_sees_the_fix_and_the_source(season, tmp_path, monkeypatch):
    client = FakeClient(*default_run())
    use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)
    sent = client.sent(8)
    assert "F1 (E5, ep 3): The ending leaves a specific threat" in sent
    assert NEW_CLIFF in sent and "<source>" in sent and "New ending." in sent


def test_no_must_fix_findings_means_no_revision(season, tmp_path, monkeypatch):
    llm = use_client(monkeypatch, tmp_path, FakeClient(*run_replies(valid_plan(), findings=[])))
    story_room.run_story_room(season)
    assert roles(llm) == PLANNING + CRITIQUE
    assert read(season, "season_plan.yaml") == read(season, "draft_plan.yaml")


@pytest.mark.parametrize("verdict", ["fail", "unclear"])
def test_fix_that_fails_recheck_is_flagged(season, tmp_path, monkeypatch, verdict):
    client = FakeClient(*run_replies(valid_plan(), episode_fix(), recheck(verdict)))
    use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)
    page = read(season, "season_plan.md")
    assert "## Unresolved" in page and "F1 (E5, ep 3)" in page and f"re-check {verdict}" in page


def test_declined_finding_is_flagged_and_not_rechecked(season, tmp_path, monkeypatch):
    client = FakeClient(*run_replies(valid_plan(), episode_fix("declined", episodes=[])))
    llm = use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)
    assert roles(llm)[-1] == "showrunner_fix_episodes"
    page = read(season, "season_plan.md")
    assert "F1 (E5, ep 3)" in page and "declined: New ending." in page


def season_finding(plan):
    return Finding(check="S1", episode=0, quote=plan.promise, problem="No stakes.", verdict="no")


def season_fix(promise="The night three men were marked to die."):
    fields = SeasonFields(**valid_plan().model_dump(exclude={"episodes"}))
    fields.promise = promise
    return SeasonRewrite(changes=[Change(finding="F1", action="fixed", note="New promise.")], season=fields)


def test_season_finding_uses_the_season_call_only(season, tmp_path, monkeypatch):
    replies = run_replies(
        valid_plan(), season_fix(), recheck(), findings=[], season_findings=[season_finding(valid_plan())]
    )
    llm = use_client(monkeypatch, tmp_path, FakeClient(*replies))
    story_room.run_story_room(season)
    assert roles(llm) == PLANNING + CRITIQUE + ["showrunner_fix_season", "critic_recheck"]
    final = yaml.safe_load(read(season, "season_plan.yaml"))
    assert final["promise"] == "The night three men were marked to die."
    assert final["episodes"] == yaml.safe_load(read(season, "draft_plan.yaml"))["episodes"]


def test_rule_violation_in_draft_is_fixed_and_checked_by_code(season, tmp_path, monkeypatch):
    draft = valid_plan()
    draft.episodes[9].reveals = ["the shot"]
    rule_fix = season_fix(valid_plan().promise)
    client = FakeClient(*run_replies(draft, rule_fix, valid_plan(), findings=[]))
    llm = use_client(monkeypatch, tmp_path, client)
    story_room.run_story_room(season)
    assert "held_back_early" in client.sent(7)
    assert roles(llm)[-2:] == ["showrunner_fix_season", "showrunner_repair"]
    assert yaml.safe_load(read(season, "season_plan.yaml"))["episodes"][9]["reveals"] == []
    assert "## Unresolved" not in read(season, "season_plan.md")


def test_story_room_stops_when_repair_also_fails(season, tmp_path, monkeypatch):
    broken = valid_plan()
    broken.episodes[9].reveals = ["the shot"]
    client = FakeClient(*run_replies(broken, season_fix(valid_plan().promise), broken, findings=[]))
    use_client(monkeypatch, tmp_path, client)
    with pytest.raises(story_room.StoryRoomFailed, match="held_back_early"):
        story_room.run_story_room(season)
    assert not (season.paths.stage_dir("story_room") / "season_plan.md").exists()


def test_batch_with_wrong_episodes_is_retried_once(season, tmp_path, monkeypatch):
    replies = default_run()
    replies.insert(2, replies[1])
    llm = use_client(monkeypatch, tmp_path, FakeClient(*replies))
    story_room.run_story_room(season)
    assert roles(llm).count("showrunner_episodes") == 4
    assert yaml.safe_load(read(season, "season_plan.yaml"))["episodes"][10]["number"] == 11


def test_batch_wrong_twice_stops_the_stage_and_still_logs_costs(season, tmp_path, monkeypatch):
    replies = default_run()
    replies[2] = replies[1]
    replies.insert(2, replies[1])
    use_client(monkeypatch, tmp_path, FakeClient(*replies))
    with pytest.raises(story_room.StoryRoomFailed, match="11"):
        story_room.run_story_room(season)
    assert (tmp_path / "lincoln" / "calls.csv").exists()


def test_finding_about_a_fixed_title_is_dropped(season, tmp_path, monkeypatch):
    title = Finding(check="E10", episode=2, quote="The Box", problem="Vague title.", verdict="no")
    llm = use_client(monkeypatch, tmp_path, FakeClient(*run_replies(valid_plan(), findings=[title])))
    story_room.run_story_room(season)
    assert roles(llm) == PLANNING + CRITIQUE
    assert "1 finding dropped" in read(season, "critique.md")


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
    client = FakeClient(*run_replies(plan, true_story=False, findings=[]))
    ledger = Ledger(tmp_path / "runs" / "harbor" / "calls.jsonl")
    llm = LlmContext(ctx.paths, ledger, BudgetGuard(ledger), client, series / "models.yaml")
    monkeypatch.setattr(story_room.LlmContext, "for_run", lambda paths: llm)
    story_room.run_story_room(ctx)

    page = (ctx.paths.stage_dir("story_room") / "season_plan.md").read_text("utf-8")
    assert page.startswith("# Harbor Lights, season plan")
    assert "Working title 2" in page
    assert len(client.calls) == 4
    assert "critic_truth" not in roles(llm)
    for text in ("The keeper hides a letter.", "Quiet dread.", "End on the lamp going dark."):
        assert text in client.sent(0)


@pytest.mark.parametrize("name", ["lincoln", "booth", "atzerodt", "seward", "surratt"])
def test_engine_contains_no_story_names(name):
    files = list((ROOT / "tih").rglob("*.py")) + list((ROOT / "prompts").glob("*.md"))
    assert not [f.name for f in files if name in f.read_text("utf-8").lower()]
