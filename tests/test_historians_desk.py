from pathlib import Path

import pytest
import yaml

from tih.paths import RunPaths
from tih.series import load_series
from tih.stages import historians_desk
from tih.stages.record import run_record
from tih.stages.registry import MissingStage, RunContext, run_stage
from tih.story.models import Episode
from tih.story.truth import NoteAnswer, TruthFinding, TruthReport, render_truth_report, review_report

from test_story_stages import FakeClient, make_llm, reply

ROOT = Path(__file__).parents[1]
NOTE = "Check the window light."


@pytest.fixture
def written(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    season = RunPaths("lincoln", None, root=tmp_path)
    run_record(RunContext(paths=season))
    episodes = yaml.safe_load((season.stage_dir("record") / "script.yaml").read_text("utf-8"))
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    stage = ctx.paths.stage_dir("writers_room")
    (stage / "script.yaml").write_text(yaml.safe_dump(episodes[0], allow_unicode=True))
    (stage / "DONE.json").write_text('{"outputs": {}}')
    config = load_series("lincoln").model_copy(update={"script_notes": {1: [NOTE]}})
    monkeypatch.setattr(historians_desk, "load_series", lambda name: config)
    return ctx, Episode(**episodes[0])


def finding(quote, kind="false_implication"):
    return TruthFinding(beat="e1c1b1", quote=quote, kind=kind, problem="Implies a plan.", proposal="Cut the line.")


def answer(note=NOTE):
    return NoteAnswer(note=note, status="unverified", answer="No source names the light.", source=None)


def run(ctx, tmp_path, monkeypatch, report):
    client = FakeClient(reply(report))
    llm = make_llm(tmp_path, client)
    monkeypatch.setattr(historians_desk.LlmContext, "for_run", lambda paths: llm)
    historians_desk.run_historians_desk(ctx)
    return client, ctx.paths.stage_dir("historians_desk")


def test_historian_drops_quote_not_in_script(written):
    _, script = written
    report = TruthReport(findings=[finding("You could hit him from here."), finding("Not in the script.")], notes=[])
    kept, dropped, _ = review_report(report, script, [])
    assert [f.quote for f in kept] == ["You could hit him from here."]
    assert dropped == 1


def test_unanswered_note_is_listed(written):
    _, script = written
    kept, dropped, unanswered = review_report(TruthReport(findings=[], notes=[]), script, [NOTE])
    page = render_truth_report("The Last Speech", kept, dropped, [], unanswered)
    assert "Not answered" in page and NOTE in page


def test_report_shows_kind_problem_and_proposal():
    page = render_truth_report("The Last Speech", [finding("You could hit him from here.")], 0, [answer()], [])
    for text in ("false implication", "Implies a plan.", "Cut the line.", "unverified", "No source names the light."):
        assert text in page


def test_historian_does_not_modify_script(written, tmp_path, monkeypatch):
    ctx, _ = written
    script_file = ctx.paths.stage_dir("writers_room") / "script.yaml"
    before = script_file.read_bytes()
    _, stage = run(ctx, tmp_path, monkeypatch, TruthReport(findings=[], notes=[answer()]))
    assert script_file.read_bytes() == before
    assert (stage / "truth_report.md").exists() and (stage / "truth_report.yaml").exists()
    assert (tmp_path / "lincoln" / "calls.csv").exists()


def test_historian_prompt_has_source_script_and_notes(written, tmp_path, monkeypatch):
    ctx, _ = written
    client, _ = run(ctx, tmp_path, monkeypatch, TruthReport(findings=[], notes=[answer()]))
    sent = client.sent(0)
    assert "<source>" in sent and "<script>" in sent and "You could hit him from here." in sent
    assert "<script_notes>" in sent and NOTE in sent


def test_historian_requires_writers_room(tmp_path):
    ctx = RunContext(paths=RunPaths("lincoln", 2, root=tmp_path))
    with pytest.raises(MissingStage, match="writers_room"):
        run_stage(ctx, "historians_desk")


def test_no_story_names_in_historian_engine():
    for path in ("tih/stages/historians_desk.py", "tih/story/truth.py", "prompts/historian.md"):
        text = (ROOT / path).read_text("utf-8")
        assert not any(name in text for name in ("Lincoln", "Booth", "Surratt")), path


def test_quote_must_be_in_the_named_beat(written):
    _, script = written
    elsewhere = TruthFinding(beat="e1c2b2", quote="You could hit him from here.", kind="unsupported", problem="x", proposal="y")
    missing = TruthFinding(beat="nope", quote="You could hit him from here.", kind="unsupported", problem="x", proposal="y")
    kept, dropped, _ = review_report(TruthReport(findings=[elsewhere, missing, finding("You could hit him from here.")], notes=[]), script, [])
    assert [f.beat for f in kept] == ["e1c1b1"] and dropped == 2


@pytest.mark.parametrize("quote", [" ", "e", "  hi "])
def test_too_short_quote_is_dropped(written, quote):
    _, script = written
    kept, dropped, _ = review_report(TruthReport(findings=[finding(quote)], notes=[]), script, [])
    assert kept == [] and dropped == 1
