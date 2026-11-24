from pathlib import Path

import pytest
import yaml

from tih.gates import ArtifactChanged, GateNotApproved, approve, require_approved
from tih.paths import RunPaths
from tih.series import load_series
from tih.stages import writers_room
from tih.stages.record import run_record
from tih.stages.registry import RunContext
from tih.story.models import Episode
from tih.story.script import ScriptChange, WriterResult

from test_season import valid_plan
from test_story_stages import FakeClient, make_llm, reply

ROOT = Path(__file__).parents[1]
HOOK = "You could hit him from here."
LAST_SPEECH = "That is the last speech he will ever make."


@pytest.fixture
def season(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    paths = RunPaths("lincoln", None, root=tmp_path)
    run_record(RunContext(paths=paths))
    stage = paths.stage_dir("story_room")
    (stage / "season_plan.yaml").write_text(yaml.safe_dump(valid_plan().model_dump(mode="json")))
    (stage / "season_ledger.yaml").write_text("setups: []\n")
    (stage / "bible.yaml").write_text("theme: demo\n")
    return paths


@pytest.fixture
def locked(season):
    approve(season, "story")
    return season


def draft(season, number=1) -> Episode:
    episodes = yaml.safe_load((season.stage_dir("record") / "script.yaml").read_text("utf-8"))
    return Episode(**episodes[number - 1])


def sharpened(season, number=1) -> WriterResult:
    script = draft(season).model_copy(deep=True)
    script.number = number
    beat = next(b for c in script.clips for b in c.beats if b.line and b.line.text == HOOK)
    beat.line.text = "One shot. From here."
    change = ScriptChange(beat=beat.id, before=HOOK, after="One shot. From here.", reason="Sharper hook.")
    return WriterResult(changes=[change], script=script)


def dropped(season) -> WriterResult:
    script = draft(season).model_copy(deep=True)
    for clip in script.clips:
        clip.beats = [b for b in clip.beats if not (b.line and b.line.text == LAST_SPEECH)]
    return WriterResult(changes=[], script=script)


def run(locked, tmp_path, monkeypatch, *results, number=1):
    client = FakeClient(*(reply(result, number=i) for i, result in enumerate(results)))
    llm = make_llm(tmp_path, client)
    monkeypatch.setattr(writers_room.LlmContext, "for_run", lambda paths: llm)
    ctx = RunContext(paths=RunPaths("lincoln", number, root=tmp_path))
    writers_room.run_writers_room(ctx)
    return client, llm, ctx.paths.stage_dir("writers_room")


def roles(llm):
    return [r.settings["role"] for r in llm.ledger.current()]


def test_writer_refuses_before_story_lock(season, tmp_path, monkeypatch):
    with pytest.raises(GateNotApproved):
        run(season, tmp_path, monkeypatch)


def test_writer_refuses_when_plan_changed_after_lock(locked, tmp_path, monkeypatch):
    plan = locked.stage_dir("story_room") / "season_plan.yaml"
    plan.write_text(plan.read_text() + "\n")
    with pytest.raises(ArtifactChanged):
        run(locked, tmp_path, monkeypatch)


def test_episode_missing_from_plan_fails_without_calls(locked, tmp_path, monkeypatch):
    with pytest.raises(writers_room.WritersRoomFailed, match="not in the season plan"):
        run(locked, tmp_path, monkeypatch, number=31)


def test_writer_with_draft_sends_draft_and_sharpen_instruction(locked, tmp_path, monkeypatch):
    result = sharpened(locked)
    client, llm, stage = run(locked, tmp_path, monkeypatch, result)
    sent = client.sent(0)
    assert "<draft_script>" in sent and "Sharpen the draft script for episode 1." in sent
    assert "We meet this evening, not in sorrow, but in gladness of heart." in sent
    saved = Episode(**yaml.safe_load((stage / "script.yaml").read_text("utf-8")))
    assert saved == result.script
    assert "Sharper hook." in (stage / "changes.md").read_text("utf-8")
    assert "One shot. From here." in (stage / "script.md").read_text("utf-8")
    assert roles(llm) == ["writer"]
    assert (tmp_path / "lincoln" / "calls.csv").exists()


def test_writer_without_draft_writes_from_plan(locked, tmp_path, monkeypatch):
    result = WriterResult(changes=[], script=sharpened(locked, number=6).script)
    client, _, stage = run(locked, tmp_path, monkeypatch, result, number=6)
    sent = client.sent(0)
    assert "<draft_script>" not in sent
    assert "Write the script for episode 6 from the plan." in sent
    assert "Written from the season plan; there was no draft." in (stage / "changes.md").read_text("utf-8")


def test_prompt_includes_only_this_episodes_ledger_entries(locked, tmp_path, monkeypatch):
    client, _, _ = run(locked, tmp_path, monkeypatch, sharpened(locked))
    sent = client.sent(0)
    assert "Keep it" not in sent
    assert "Booth expects him to kill Johnson." not in sent
    assert "A door opens on a stranger in episode 1." in sent
    assert "A hand closes on a letter in episode 2." in sent
    assert "the shot" in sent


def test_ledger_entries_for_the_episode_are_sent(locked, tmp_path, monkeypatch):
    result = WriterResult(changes=[], script=draft(locked, 4))
    client, _, _ = run(locked, tmp_path, monkeypatch, result, number=4)
    assert "Keep it" in client.sent(0)


def test_script_notes_are_sent(locked, tmp_path, monkeypatch):
    config = load_series("lincoln").model_copy(update={"script_notes": {1: ["Check the window light."]}})
    monkeypatch.setattr(writers_room, "load_series", lambda name: config)
    client, _, _ = run(locked, tmp_path, monkeypatch, sharpened(locked))
    assert "<script_notes>" in client.sent(0) and "Check the window light." in client.sent(0)


def test_violation_triggers_one_repair(locked, tmp_path, monkeypatch):
    client, llm, stage = run(locked, tmp_path, monkeypatch, dropped(locked), sharpened(locked))
    assert roles(llm) == ["writer", "writer_repair"]
    assert "protected_dropped" in client.sent(1)
    assert (stage / "script.yaml").exists()


def test_repair_still_failing_stops_without_script(locked, tmp_path, monkeypatch):
    with pytest.raises(writers_room.WritersRoomFailed, match="protected_dropped"):
        run(locked, tmp_path, monkeypatch, dropped(locked), dropped(locked))
    stage = RunPaths("lincoln", 1, root=tmp_path).stage_dir("writers_room")
    assert not (stage / "script.yaml").exists()
    assert (tmp_path / "lincoln" / "calls.csv").exists()


def test_script_gate_detects_edit_after_approval(locked, tmp_path, monkeypatch):
    _, _, stage = run(locked, tmp_path, monkeypatch, sharpened(locked))
    paths = RunPaths("lincoln", 1, root=tmp_path)
    approve(paths, "script")
    require_approved(paths, "script")
    (stage / "script.yaml").write_text("edited\n")
    with pytest.raises(ArtifactChanged, match="script.yaml"):
        require_approved(paths, "script")


def test_no_story_names_in_writer_engine():
    for path in ("tih/stages/writers_room.py", "tih/story/script.py", "prompts/writer.md"):
        text = (ROOT / path).read_text("utf-8")
        assert not any(name in text for name in ("Lincoln", "Booth", "Surratt")), path
