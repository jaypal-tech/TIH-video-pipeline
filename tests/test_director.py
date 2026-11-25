import shutil
from pathlib import Path

import anthropic
import pytest

from tih.gates import GateNotApproved, approve
from tih.paths import RunPaths
from tih.shots import Opening, SceneRule, ShotPlan, example_plan_path, load_plan, plan_path
from tih.stages import director, registry
from tih.stages.registry import RunContext, run_stage

from test_story_stages import FakeClient, make_llm, reply

ROOT = Path(__file__).parents[1]


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """Episode 1 with the draft standing in for the locked Writer script."""
    monkeypatch.chdir(ROOT)
    context = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_stage(context, "record")
    scope = context.paths.scope_dir
    (scope / "writers_room").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(scope / "record" / "script.yaml", scope / "writers_room" / "script.yaml")
    registry._write_done(context, "writers_room")
    return context


def good() -> ShotPlan:
    return load_plan(example_plan_path("lincoln"))


def bad_line() -> ShotPlan:
    plan = good()
    plan.shots[0].line = "You could hit him."
    return plan


def wrong_episode() -> ShotPlan:
    plan = good()
    plan.episode = 2
    return plan


def run(ctx, tmp_path, monkeypatch, *plans):
    client = FakeClient(*[reply(p, number=i) for i, p in enumerate(plans)])
    llm = make_llm(tmp_path, client)
    monkeypatch.setattr(director.LlmContext, "for_run", lambda paths: llm)
    director.run_director(ctx)
    return client


def test_director_refuses_without_script_lock(ctx, tmp_path, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(director.LlmContext, "for_run", lambda paths: make_llm(tmp_path, client))
    with pytest.raises(GateNotApproved):
        director.run_director(ctx)
    assert client.calls == []


def test_director_writes_checked_plan(ctx, tmp_path, monkeypatch):
    approve(ctx.paths, "script")
    client = run(ctx, tmp_path, monkeypatch, good())
    assert len(client.calls) == 1
    assert load_plan(plan_path(ctx.paths)) == good()
    assert "29 shots" in (ctx.paths.stage_dir("director") / "plan.md").read_text(encoding="utf-8")


def test_director_brief_has_script_bible_and_example(ctx, tmp_path, monkeypatch):
    approve(ctx.paths, "script")
    client = run(ctx, tmp_path, monkeypatch, good())
    sent = client.sent(0)
    for tag in ("<script>", "<bible>", "<example_plan>", "<series_config>"):
        assert tag in sent
    assert "You could hit him from here." in sent
    assert "Write the shot plan for episode 1." in sent


def test_director_repairs_once(ctx, tmp_path, monkeypatch):
    approve(ctx.paths, "script")
    client = run(ctx, tmp_path, monkeypatch, bad_line(), good())
    assert len(client.calls) == 2
    assert "line is not an exact part" in client.sent(1)
    assert "<shot_plan>" in client.sent(1)
    assert load_plan(plan_path(ctx.paths)) == good()


def test_director_fails_after_repair(ctx, tmp_path, monkeypatch):
    approve(ctx.paths, "script")
    with pytest.raises(director.DirectorFailed, match="exact part"):
        run(ctx, tmp_path, monkeypatch, bad_line(), bad_line())
    assert not plan_path(ctx.paths).exists()


def test_director_rejects_wrong_episode(ctx, tmp_path, monkeypatch):
    approve(ctx.paths, "script")
    with pytest.raises(director.DirectorFailed, match="episode 2"):
        run(ctx, tmp_path, monkeypatch, wrong_episode(), wrong_episode())


def _empty_boxes(node, path="$"):
    """Objects Claude's answer format leaves no room in: no properties and no extra keys allowed."""
    found = []
    if isinstance(node, dict):
        if node.get("type") == "object" and not node.get("properties") and node.get("additionalProperties") is False:
            found.append(path)
        for key, value in node.items():
            found += _empty_boxes(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _empty_boxes(value, f"{path}[{index}]")
    return found


def test_shot_plan_answer_format_has_room_for_every_field():
    assert _empty_boxes(anthropic.transform_schema(ShotPlan)) == []


def test_shot_plan_answer_format_asks_for_the_opening():
    schema = anthropic.transform_schema(ShotPlan)
    assert "opening" in schema["properties"]
    assert "opening" not in schema.get("required", [])
    opening = Opening.model_json_schema()
    assert set(opening["properties"]) == {"place", "keyframe", "motion", "sound"}


def test_scene_sides_accept_claude_rows_and_plan_maps():
    rows = SceneRule.model_validate({"clip": 1, "light": "l", "weather": "w",
                                     "sides": [{"who": "booth", "side": "left"}, {"who": "powell", "side": "right"}]})
    plan = SceneRule.model_validate({"clip": 1, "light": "l", "weather": "w",
                                     "sides": {"booth": "left", "powell": "right"}})
    assert rows.sides == plan.sides == {"booth": "left", "powell": "right"}


def test_director_prompt_carries_scene_staging_rules():
    text = (ROOT / "prompts" / "director.md").read_text(encoding="utf-8")
    assert "`place` is where the camera is looking" in text
    assert "face_reference: false" in text
    assert "not a posed picture" in text
    assert "Write `opening`: one silent 5-second wide establishing shot that the title sits over." in text
    assert "It is not one of the numbered shots." in text
    assert "added for you. Do not write a shot for it." not in text
    assert "about 6 words" in text
    assert "On a shot where a face speaks, the camera holds still: no push in, no pan." in text
    assert "`opening` is required." in text
    assert "No shot is shorter than 1.5 seconds." in text
    assert "Every shot runs 1.5 to 8.0 seconds." in text
    for phrase in (
        "`keyframe` states where each person is in the frame (left, centre, right; near or far) and where they stand relative to each other. Keep those positions the same in every shot of the scene.",
        "`motion` gives the expression and the intent behind the line in a few words (for example: jaw tight, he says it like a dare). When a shot has a line, the person starts speaking as the shot begins.",
        "`sound` lists only what is heard in that shot, two or three specific sounds, no music and no repeats of the word for the same sound.",
        "`keyframe` names any object or clothing state that carries from the shot before (glove off, page in hand, hat wet), so it does not change between shots.",
    ):
        assert phrase in text
