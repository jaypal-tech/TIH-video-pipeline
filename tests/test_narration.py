"""The narrator: script and plan rules, the narration stage, and how the cut lays it over the film.

No paid calls: everything runs through DryRunTransport.
"""

import hashlib
import re
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tih.bible import NARRATOR_ID, NotLocked, load_bible, load_locks, save_locks
from tih.briefs import opening_brief, scene_brief
from tih.cli import FLOW_STAGES, app, prepare_rehearsal
from tih.gates import approve
from tih.media import ffmpeg
from tih.paths import RunPaths
from tih.production import NoUsableTake, load_resolved, read_takes
from tih.providers.dry import DryRunTransport
from tih.scenes import (
    NARRATION_LEAD, OPENING_ID, OPENING_NARRATION_LEAD, OPENING_SECONDS, Caption, Narration, NarrationTooLong,
    as_shot, narration_extras, narration_lines, narration_pages, narration_starts, scene_takes, without_overlap,
)
from tih.series import SeriesConfig
from tih.shots import PlanError, ShotPlan, load_plan, resolve_plan
from tih.stages import record as _record  # noqa: F401  registers the stage
from tih.stages import registry
from tih.stages.narration import read_narration
from tih.stages.registry import MissingStage, RunContext, run_stage
from tih.stages.scenes import check_take, read_timing
from tih.story.models import NARRATOR, Canon, Delivery, Episode, Evidence
from tih.story.script import check_script

ROOT = Path(__file__).parents[1]
FILM_PLAN = ROOT / "tests" / "fixtures" / "film_plan.yaml"
OPENING_LINE = "April 1865. The war is over. Washington is celebrating."
MIDDLE_LINE = "He decides tonight. No more waiting."
CLOSING_LINE = "Three days later, he chose three targets."
OPENING = {"place": "L1R", "keyframe": "The crowd seen from behind, torches held high.",
           "motion": "Men cheer and wave hats; the camera pushes slowly forward.",
           "sound": "crowd cheering, rain on umbrellas", "line": OPENING_LINE}


@pytest.fixture(autouse=True)
def in_repo(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("TIH_LOCKS_FILE", raising=False)


def _narration(text):
    return {"speaker": NARRATOR, "text": text, "delivery": "narration", "evidence": "narration"}


def narrate_script(script: dict) -> dict:
    """The record script with an opening, a middle and a closing narration line."""
    beats = {beat["id"]: beat for clip in script["clips"] for beat in clip["beats"]}
    script["clips"][0]["beats"].insert(0, {"id": "e1c1b0", "start_s": 0.0, "picture": "The crowd on the lawn.",
                                           "line": _narration(OPENING_LINE)})
    beats["e1c3b3"]["line"] = _narration(MIDDLE_LINE)
    beats["e1c5b4"]["line"] = _narration(CLOSING_LINE)
    return script


def narrate_plan(plan: dict) -> dict:
    shots = {shot["id"]: shot for shot in plan["shots"]}
    plan["opening"] = dict(OPENING)
    shots["3.4"].update(line=MIDDLE_LINE, delivery="narration")
    shots["5.4"].update(line=CLOSING_LINE, delivery="narration")
    return plan


def _source(tmp_path) -> RunContext:
    source = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path / "src"))
    run_stage(source, "record")
    return source


def _texts(tmp_path, script_edit=None, plan_edit=None) -> tuple[Episode, ShotPlan]:
    scope = _source(tmp_path).paths.scope_dir
    script = narrate_script(yaml.safe_load((scope / "record" / "script.yaml").read_text(encoding="utf-8")))
    plan = narrate_plan(yaml.safe_load(FILM_PLAN.read_text(encoding="utf-8")))
    if script_edit:
        script_edit(script)
    if plan_edit:
        plan_edit(plan)
    return Episode.model_validate(script), ShotPlan.model_validate(plan)


def _resolved(tmp_path, script_edit=None, plan_edit=None):
    episode, plan = _texts(tmp_path, script_edit, plan_edit)
    return resolve_plan(plan, episode, load_bible("lincoln"))


def _staged(tmp_path, monkeypatch, narrated=True) -> RunContext:
    """What `tih rehearse` builds before its paid stages, from a script and plan with narration."""
    source = _source(tmp_path)
    scope = source.paths.scope_dir
    script = yaml.safe_load((scope / "record" / "script.yaml").read_text(encoding="utf-8"))
    plan = yaml.safe_load(FILM_PLAN.read_text(encoding="utf-8"))
    if narrated:
        script, plan = narrate_script(script), narrate_plan(plan)
    for folder, name, data in (("writers_room", "script.yaml", script), ("director", "shots.yaml", plan)):
        (scope / folder).mkdir(parents=True, exist_ok=True)
        (scope / folder / name).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        registry._write_done(source, folder)
    monkeypatch.setenv("TIH_LOCKS_FILE", str(tmp_path / "dry" / "lincoln" / "locks.yaml"))
    return prepare_rehearsal(1, "lincoln", tmp_path / "dry", source=tmp_path / "src")


# --- the script ---

def _checked(tmp_path, edit):
    season = RunContext(paths=RunPaths("lincoln", None, root=tmp_path / "season"))
    _record.run_record(season)
    canon = Canon(**yaml.safe_load((season.paths.stage_dir("record") / "canon.yaml").read_text("utf-8")))
    episode, _ = _texts(tmp_path, script_edit=edit)
    return [v.rule for v in check_script(episode, 1, canon, SeriesConfig(title="Demo", episodes=30), None)]


def test_a_narration_line_is_the_narrators_and_labelled_narration(tmp_path):
    assert _checked(tmp_path, None) == []


@pytest.mark.parametrize("change", [
    {"evidence": "dramatized"}, {"speaker": "BOOTH"}, {"delivery": "off"},
])
def test_a_mislabelled_narration_line_is_a_violation(tmp_path, change):
    def edit(script):
        script["clips"][0]["beats"][0]["line"].update(change)

    assert _checked(tmp_path, edit) == ["narration"]


def test_the_final_beat_may_carry_narration(tmp_path):
    assert "dramatized_close" not in _checked(tmp_path, None)


# --- the plan ---

def test_a_narration_shot_is_the_narrators_and_needs_no_lipsync(tmp_path):
    plan = _resolved(tmp_path)
    shot = next(s for s in plan.shots if s.id == "3.4")
    assert (shot.speaker, shot.delivery, shot.needs_lipsync) == (NARRATOR_ID, Delivery.narration, False)
    assert (shot.line, shot.spoken, shot.evidence) == (MIDDLE_LINE, None, Evidence.narration)
    assert plan.opening.line == OPENING_LINE
    assert [(n.beat, n.text, n.shots) for n in narration_lines(plan)] == [
        ("e1c1b0", OPENING_LINE, [OPENING_ID]), ("e1c3b3", MIDDLE_LINE, ["3.4"]), ("e1c5b4", CLOSING_LINE, ["5.4"])]


def test_a_narration_shot_must_last_its_words(tmp_path):
    def edit(plan):  # 6 words need 6 / 2.5 + 0.5 = 2.9 s; the clip keeps its length
        shots = {shot["id"]: shot for shot in plan["shots"]}
        shots["3.4"]["duration"] -= 0.5
        shots["3.3"]["duration"] += 0.5

    with pytest.raises(PlanError, match=r"shot 3.4: 6 words of narration need 2.9 s, the shot is 2.5 s"):
        _resolved(tmp_path, plan_edit=edit)


def test_a_narration_line_needs_the_narration_delivery_and_only_it(tmp_path):
    def off(plan):
        next(s for s in plan["shots"] if s["id"] == "3.4")["delivery"] = "off"

    def narrated(plan):
        next(s for s in plan["shots"] if s["id"] == "1.2")["delivery"] = "narration"

    with pytest.raises(PlanError, match="shot 3.4: a narration line needs delivery narration"):
        _resolved(tmp_path, plan_edit=off)
    with pytest.raises(PlanError, match="shot 1.2: delivery narration is only for the narrator's lines"):
        _resolved(tmp_path, plan_edit=narrated)


def test_a_narration_line_may_be_split_across_shots(tmp_path):
    def split(plan):
        shots = {shot["id"]: shot for shot in plan["shots"]}
        shots["5.3"].update(line="Three days later,", delivery="narration")
        shots["5.4"].update(line="he chose three targets.")

    def script(data):
        beats = {beat["id"]: beat for clip in data["clips"] for beat in clip["beats"]}
        beats["e1c5b3"]["line"] = _narration(CLOSING_LINE)
        beats["e1c5b4"]["line"] = None

    def plan(data):
        split(data)
        shots = {shot["id"]: shot for shot in data["shots"]}
        shots["5.4"]["beat"] = "e1c5b3"
        shots["5.4"]["overlays"] = []

    def trimmed(data):
        script(data)
        data["clips"][-1]["beats"] = [b for b in data["clips"][-1]["beats"] if b["id"] != "e1c5b4"]

    resolved = _resolved(tmp_path, script_edit=trimmed, plan_edit=plan)
    assert narration_lines(resolved)[-1].shots == ["5.3", "5.4"]
    assert narration_lines(resolved)[-1].text == CLOSING_LINE


def test_the_opening_line_is_the_scripts_first_narration_line(tmp_path):
    def other(plan):
        plan["opening"]["line"] = "April 1865. The war is done."

    def long(script):
        script["clips"][0]["beats"][0]["line"]["text"] = OPENING_LINE + " Nobody here knows what comes next."

    def long_plan(plan):
        plan["opening"]["line"] = OPENING_LINE + " Nobody here knows what comes next."

    def no_narration(script):
        script["clips"][0]["beats"].pop(0)

    with pytest.raises(PlanError, match="opening: line is not an exact part of the script's first narration line"):
        _resolved(tmp_path, plan_edit=other)
    with pytest.raises(PlanError, match="opening: line is 15 words; the opening carries at most 12"):
        _resolved(tmp_path, script_edit=long, plan_edit=long_plan)
    with pytest.raises(PlanError, match="opening: has a line but the script does not start with narration"):
        _resolved(tmp_path, script_edit=no_narration)


def test_narration_needs_a_narrator_voice_in_the_bible(tmp_path):
    episode, plan = _texts(tmp_path)
    bible = load_bible("lincoln").model_copy(update={"narrator": None})
    with pytest.raises(PlanError, match="the bible has no narrator voice"):
        resolve_plan(plan, episode, bible)


def test_the_bible_describes_the_narrators_voice():
    assert load_bible("lincoln").narrator.voice == (
        "a calm, low, unhurried American male storyteller in his fifties, warm and grave, "
        "close to the microphone, no theatrical emphasis")


# --- the prompts the video model gets ---

def test_a_narration_shot_is_silent_in_the_scene_prompt(tmp_path):
    plan = _resolved(tmp_path)
    bible = load_bible("lincoln")
    take = next(t for t in scene_takes(plan, "seedance") if t.id == "3.1")
    prompt = scene_brief(take, bible, plate=False)["prompt_seedance"]
    paragraph = next(p for p in prompt.split("\n\n") if p.startswith("Shot 4 "))
    assert paragraph.endswith("Nobody speaks.")
    assert MIDDLE_LINE not in prompt and "narrator" not in prompt.lower()
    voices = next(p for p in prompt.split("\n\n") if p.startswith("Voices:"))
    assert voices.count(" is ") == len({s.speaker for s in take.shots if s.spoken})
    assert take.line == "One shot, Lewis. In the dark. And a thousand men on top of me."


def test_a_narration_only_take_has_no_voices_paragraph_in_either_prompt(tmp_path):
    plan = _resolved(tmp_path)
    bible = load_bible("lincoln")
    shot = next(s for s in plan.shots if s.id == "5.4")
    take = scene_takes(plan.model_copy(update={"shots": [shot]}), "seedance")[0]
    prompts = scene_brief(take, bible, plate=False)
    assert "Voices:" not in prompts["prompt_seedance"] and "Nobody speaks." in prompts["prompt_seedance"]
    assert "Dialogue: Nobody speaks." in prompts["prompt_veo"] and CLOSING_LINE not in prompts["prompt_veo"]
    assert check_take(take, "", [], take.duration) is None
    heard = [{"text": w, "start": i * 0.3, "end": i * 0.3 + 0.2, "type": "word"}
             for i, w in enumerate(CLOSING_LINE.split())]
    assert check_take(take, CLOSING_LINE, heard, take.duration) == f"unexpected speech: {CLOSING_LINE}"


def test_the_opening_prompt_is_unchanged_by_its_narration(tmp_path):
    plan = _resolved(tmp_path)
    bible = load_bible("lincoln")
    silent = plan.opening.model_copy(update={"line": None})
    place = bible.places[plan.opening.place]
    assert opening_brief(place, bible, False, opening=plan.opening) == opening_brief(place, bible, False, opening=silent)


# --- the narrator's voice ---

def test_casting_designs_the_narrators_voice_once_and_locks_it_like_a_characters(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch)
    takes = read_takes(ctx, "casting")
    assert f"voice:{NARRATOR_ID}" in takes
    design = next(r for r in ctx.gateway.ledger.current() if r.step == "voice_design" and r.shot == NARRATOR_ID)
    assert design.status == "ok"
    lock = load_locks("lincoln").characters[NARRATOR_ID]
    assert lock.voice_id == takes[f"voice:{NARRATOR_ID}"].takes[0].note.removeprefix("voice id ")
    before = len(ctx.gateway.ledger.current())
    run_stage(ctx, "casting", force=True)  # locked: nothing is designed again
    assert len(ctx.gateway.ledger.current()) == before


def test_the_narrators_voice_design_uses_the_bibles_words(tmp_path, monkeypatch):
    asked = []
    submit = DryRunTransport.submit

    def recording(self, endpoint, arguments):
        asked.append((endpoint, arguments))
        return submit(self, endpoint, arguments)

    monkeypatch.setattr(DryRunTransport, "submit", recording)
    _staged(tmp_path, monkeypatch)
    design = next(a for e, a in asked if "voice-design" in e and "narrator" in a["voice_name"])
    assert design["voice_description"].startswith(load_bible("lincoln").narrator.voice)
    assert design["text"].startswith(OPENING_LINE)


def test_the_narrators_voice_can_be_locked_by_hand(tmp_path, monkeypatch):
    monkeypatch.setenv("TIH_LOCKS_FILE", str(tmp_path / "locks.yaml"))
    result = CliRunner().invoke(app, ["lock", "voice", NARRATOR_ID, "--voice-id", "abc", "--series", "lincoln"])
    assert result.exit_code == 0, result.output
    assert load_locks("lincoln").characters[NARRATOR_ID].voice_id == "abc"


# --- the narration stage ---

def test_narration_runs_before_sound_and_the_cut_needs_it():
    stages = FLOW_STAGES["seedance"]
    assert stages.index("narration") < stages.index("sound")
    assert "narration" in registry._STAGES["cut_seedance"].requires
    stage = registry._STAGES["narration"]
    assert (stage.requires, stage.gate) == (("shotplan",), registry._STAGES["scenes_seedance"].gate)


def test_every_narration_line_is_spoken_whole_once_and_timed(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch)
    before = len(ctx.gateway.ledger.current())
    assert run_stage(ctx, "narration") == "ran"
    lines = read_narration(ctx)
    assert [(n.beat, n.text, n.shots) for n in lines] == [
        ("e1c1b0", OPENING_LINE, [OPENING_ID]), ("e1c3b3", MIDDLE_LINE, ["3.4"]), ("e1c5b4", CLOSING_LINE, ["5.4"])]
    takes = read_takes(ctx, "narration")
    assert set(takes) == {"e1c1b0", "e1c3b3", "e1c5b4"}
    for line in lines:
        chosen = Path(takes[line.beat].chosen)  # the series plays the narrator a little faster, from a retimed copy
        assert Path(line.audio).name.startswith(chosen.stem) and Path(line.audio).parent == chosen.parent
        assert line.duration == pytest.approx(ffmpeg.duration(Path(line.audio)), abs=0.01)
        assert " ".join(w["text"] for w in line.words) == line.text
        assert all(0 <= w["start"] < w["end"] <= line.duration for w in line.words)
    calls = ctx.gateway.ledger.current()[before:]
    assert sorted(r.step for r in calls) == ["narration"] * 3 + ["transcribe"] * 3
    assert not any(r.step == "voice" for r in calls)
    run_stage(ctx, "narration", force=True)
    assert len(ctx.gateway.ledger.current()) == before + 6  # a re-run reuses every call


def test_narration_with_no_lines_writes_an_empty_file_and_costs_nothing(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch, narrated=False)
    before = len(ctx.gateway.ledger.current())
    run_stage(ctx, "narration")
    assert read_narration(ctx) == [] and read_takes(ctx, "narration") == {}
    assert len(ctx.gateway.ledger.current()) == before
    assert NARRATOR_ID not in load_locks("lincoln").characters


def test_narration_needs_the_locked_narrator_voice(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch)
    locks = load_locks("lincoln")
    del locks.characters[NARRATOR_ID]
    save_locks("lincoln", locks)
    approve(ctx.paths, "look")
    with pytest.raises(NotLocked, match="No locked voice id for narrator"):
        run_stage(ctx, "narration")


def _mishear(monkeypatch, transport, line, times):
    wait = transport.wait
    misheard = []

    def mishearing_wait(endpoint, request_id):
        payload = wait(endpoint, request_id)
        if payload.get("text") == line and len(misheard) < times:
            misheard.append(request_id)
            payload = {"text": line.replace("tonight", "today"), "words": [
                {**w, "text": w["text"].replace("tonight", "today")} for w in payload["words"]]}
        return payload

    monkeypatch.setattr(transport, "wait", mishearing_wait)


def test_a_misread_narration_take_is_rejected_and_taken_again(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch)
    _mishear(monkeypatch, ctx.gateway.transport, MIDDLE_LINE, 1)
    run_stage(ctx, "narration")
    takes = read_takes(ctx, "narration")["e1c3b3"].takes
    assert [bool(t.reason) for t in takes] == [True, False]
    assert "heard 'he decides today no more waiting'" in takes[0].reason


def test_narration_that_never_says_its_words_stops_the_stage(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch)
    _mishear(monkeypatch, ctx.gateway.transport, MIDDLE_LINE, 9)
    with pytest.raises(NoUsableTake, match="narration e1c3b3"):
        run_stage(ctx, "narration")


def test_the_scenes_stage_does_not_expect_narration_in_a_takes_sound(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch)
    run_stage(ctx, "scenes_seedance")
    plan = load_resolved(ctx)
    spans, captions, _ = read_timing(ctx, "seedance")
    assert set(spans) == {s.id for s in plan.shots if s.spoken}
    assert " ".join(c.text for c in captions) == " ".join(s.spoken for s in plan.shots if s.spoken)
    assert all(len(t.takes) == 1 for t in read_takes(ctx, "scenes_seedance").values())


def test_the_old_voice_stage_speaks_narration_as_an_off_line_in_the_narrators_voice(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch)
    run_stage(ctx, "voice")
    assert {"e1c3b3", "e1c5b4"} <= set(read_takes(ctx, "voice"))
    with pytest.raises(MissingStage):
        run_stage(ctx, "cut_seedance")  # the cut needs the narration stage


# --- where narration sits in the film ---

def _line(plan, beat, duration, words=None):
    line = next(n for n in narration_lines(plan) if n.beat == beat)
    step = duration / len(line.text.split())
    timed = words or [{"text": w, "start": round(i * step, 3), "end": round((i + 1) * step - 0.05, 3)}
                      for i, w in enumerate(line.text.split())]
    return Narration(**line.model_dump(), audio="x.mp3", duration=duration, words=timed)


def test_narration_starts_just_after_its_first_shot_and_the_opening_line_after_the_film_starts(tmp_path):
    plan = _resolved(tmp_path)
    lines = [_line(plan, "e1c1b0", 3.4), _line(plan, "e1c3b3", 2.3), _line(plan, "e1c5b4", 2.7)]
    starts = narration_starts(plan, lines)
    shots = {s.id: s for s in plan.shots}
    assert starts == {
        "e1c1b0": pytest.approx(OPENING_NARRATION_LEAD - OPENING_SECONDS),  # on the shots' clock: 0.5 s into the film
        "e1c3b3": pytest.approx(shots["3.4"].start_s + NARRATION_LEAD),
        "e1c5b4": pytest.approx(shots["5.4"].start_s + NARRATION_LEAD),
    }
    assert (NARRATION_LEAD, OPENING_NARRATION_LEAD) == (0.3, 0.5)


def test_narration_that_fits_its_shots_lengthens_nothing(tmp_path):
    plan = _resolved(tmp_path)
    takes = scene_takes(plan, "seedance")
    lines = [_line(plan, "e1c1b0", 3.4), _line(plan, "e1c3b3", 2.3), _line(plan, "e1c5b4", 2.7)]
    assert narration_extras(plan, takes, lines, {t.id: 0.5 for t in takes}, {}) == {}


def test_narration_longer_than_its_shots_lengthens_its_take_up_to_the_clip(tmp_path):
    plan = _resolved(tmp_path)
    takes = scene_takes(plan, "seedance")
    line = _line(plan, "e1c3b3", 3.1)  # 0.3 + 3.1 s in a 3.0 s shot, the last of take 3.1: 0.4 s over
    assert narration_extras(plan, takes, [line], {"3.1": 0.5}, {}) == {"3.1": pytest.approx(0.4)}
    with pytest.raises(NarrationTooLong, match=r"narration line e1c3b3 \('He decides tonight. No more waiting.'\) "
                                               r"runs 0.40 s past its shots; take 3.1 has 0.10 s of clip left"):
        narration_extras(plan, takes, [line], {"3.1": 0.1}, {})
    # the next take's line is heard 0.5 s into it: the narrator may finish over its silent start
    assert narration_extras(plan, takes, [line], {"3.1": 0.1}, {"4.1": (0.5, 2.0)}) == {"3.1": pytest.approx(0.1)}


def test_narration_that_ends_inside_a_take_may_only_run_on_into_silence(tmp_path):
    def earlier(plan):
        shots = {shot["id"]: shot for shot in plan["shots"]}
        shots["3.4"].update(line=None, delivery=None)
        shots["3.1"].update(line=MIDDLE_LINE, delivery="narration")

    def script(data):
        beats = {beat["id"]: beat for clip in data["clips"] for beat in clip["beats"]}
        beats["e1c3b3"]["line"] = None
        data["clips"][2]["beats"].insert(0, {"id": "e1c3b0", "start_s": 27.0, "picture": "The crowd.",
                                             "line": _narration(MIDDLE_LINE)})

    def plan(data):
        earlier(data)
        next(s for s in data["shots"] if s["id"] == "3.1")["beat"] = "e1c3b0"

    resolved = _resolved(tmp_path, script_edit=script, plan_edit=plan)
    takes = scene_takes(resolved, "seedance")
    line = _line(resolved, "e1c3b0", 3.1)  # 0.4 s past shot 3.1; shot 3.2's line is next
    assert narration_extras(resolved, takes, [line], {"3.1": 5.0}, {"3.2": (3.6, 5.0)}) == {}  # heard 0.6 s after 3.1
    with pytest.raises(NarrationTooLong, match=r"narration line e1c3b0 .* runs 0.40 s past its shots; "
                                               r"the next voice comes 0.10 s after them"):
        narration_extras(resolved, takes, [line], {"3.1": 5.0}, {"3.2": (3.1, 5.0)})


def test_the_opening_line_may_run_on_until_the_first_voice(tmp_path):
    plan = _resolved(tmp_path)
    takes = scene_takes(plan, "seedance")
    # 0.5 + 4.9 s: 0.4 s into shot 1.1, whose line starts 0.6 s into its take
    assert narration_extras(plan, takes, [_line(plan, "e1c1b0", 4.9)], {}, {"1.1": (0.6, 2.0)}) == {}
    with pytest.raises(NarrationTooLong, match=r"narration line e1c1b0 .* runs 0.40 s past the opening; "
                                               r"the next voice comes 0.20 s after it"):
        narration_extras(plan, takes, [_line(plan, "e1c1b0", 4.9)], {}, {"1.1": (0.2, 2.0)})


def test_narration_captions_page_like_other_captions(tmp_path):
    plan = _resolved(tmp_path)
    pages = narration_pages(_line(plan, "e1c5b4", 2.8), 70.0)
    assert [p.text for p in pages] == ["Three days later,", "he chose three targets."]
    assert pages[0].start == 70.0 and pages[0].end == pytest.approx(pages[1].start)
    assert pages[-1].end <= 70.0 + 2.8 + 0.4


def test_captions_never_overlap_in_time():
    pages = [Caption(text="b", start=2.0, end=4.0), Caption(text="a", start=1.0, end=2.5),
             Caption(text="c", start=3.0, end=3.0), Caption(text="d", start=3.5, end=5.0)]
    assert [(p.text, p.start, p.end) for p in without_overlap(pages)] == [
        ("a", 1.0, 2.0), ("b", 2.0, 3.5), ("d", 3.5, 5.0)]


# --- the prompts ---

def _prompt(name):
    return (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")


def test_the_writer_is_told_how_the_narrator_carries_the_story():
    text = _prompt("writer")
    for phrase in (
        "A question in the first 3 seconds",
        "within 15 seconds",
        "`speaker: NARRATOR`, `delivery: narration`, `evidence: narration`",
        "a 14-year-old",
        "at most about 16 words",
        "about a third of the runtime",
        "A beat has either a narration line or a character line",
        "A `real_words` sentence is kept whole in one beat, or at most two consecutive beats; never scatter it",
        "Character lines are short, about 5 words",
        "narration states only what the source supports",
    ):
        assert phrase in text, phrase
    assert "- Lines are short, about 5 words." not in text


def test_the_historian_checks_narration_as_fact_and_the_critic_asks_who_what_why():
    assert "A narration line is the series itself stating fact" in _prompt("historian")
    assert "who the episode follows, what they want and why" in _prompt("critic")


def test_the_director_is_told_how_to_shoot_narration():
    text = _prompt("director")
    for phrase in (
        "`delivery: narration`",
        "nobody speaks on screen",
        "words / 2.5 plus 0.5 seconds",
        "at most 12 words",
    ):
        assert phrase in text, phrase


# --- the whole film ---

def _mean_volume(path, start, seconds, band=None):
    chain = f"bandpass=f={band}:width_type=h:w=30," if band else ""
    report = ffmpeg.run(["ffmpeg", "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(path),
                         "-af", f"{chain}volumedetect", "-vn", "-f", "null", "-"])
    return float(re.search(r"mean_volume: (-?[\d.]+|-inf) dB", report).group(1))


def _dry_pitch(text):
    return 180 + int(hashlib.sha256(text.encode()).hexdigest()[:4], 16) % 400


@pytest.mark.slow
def test_a_narrated_rehearsal_has_the_narrator_in_the_master_and_no_overlapping_captions(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch)
    for stage in FLOW_STAGES["seedance"][2:]:
        run_stage(ctx, stage, force=True)
    cut_dir = ctx.paths.scope_dir / "cut_seedance"
    final = cut_dir / "final.mp4"
    assert ffmpeg.validate_master(final) == []

    placed = {row["beat"]: row for row in yaml.safe_load((cut_dir / "narration.yaml").read_text(encoding="utf-8"))}
    lines = {n.beat: n for n in read_narration(ctx)}
    plan = load_resolved(ctx)
    shots = {s.id: s for s in as_shot(plan, scene_takes(plan, "seedance"), read_timing(ctx, "seedance")[2]).shots}
    assert placed["e1c1b0"]["start"] == pytest.approx(OPENING_NARRATION_LEAD)
    assert placed["e1c3b3"]["start"] == pytest.approx(OPENING_SECONDS + shots["3.4"].start_s + NARRATION_LEAD, abs=0.1)
    assert placed["e1c5b4"]["start"] == pytest.approx(OPENING_SECONDS + shots["5.4"].start_s + NARRATION_LEAD, abs=0.1)
    assert placed["e1c5b4"]["end"] <= ffmpeg.duration(final) + 0.05  # the closing line is never cut off

    stem = cut_dir / "work" / "narration.wav"
    for beat, row in placed.items():
        assert row["end"] - row["start"] == pytest.approx(lines[beat].duration, abs=0.01)
        inside = (row["start"] + 0.1, row["end"] - row["start"] - 0.2)
        assert _mean_volume(stem, *inside) > -30  # the narrator is heard here, as loud as dialogue
        assert _mean_volume(stem, row["end"] + 0.2, 1.0) < -70  # and nowhere else
        # in the master too: the narrator's own tone is there while the line plays, and gone just before it
        pitch = _dry_pitch(lines[beat].text)
        before = max(row["start"] - inside[1] - 0.3, 0.0)
        if beat != "e1c1b0":
            assert _mean_volume(final, *inside, band=pitch) > _mean_volume(final, before, inside[1], band=pitch) + 10
        assert _mean_volume(final, *inside) > -40

    pages = [Caption.model_validate(p) for p in yaml.safe_load((cut_dir / "captions.yaml").read_text(encoding="utf-8"))]
    assert pages == sorted(pages, key=lambda p: p.start)
    assert all(a.end <= b.start + 1e-6 and a.start < a.end for a, b in zip(pages, pages[1:]))
    shown = " ".join(p.text for p in pages)
    for text in (OPENING_LINE, MIDDLE_LINE, CLOSING_LINE, "You could hit him from here."):
        assert text in shown
    for beat, row in placed.items():
        mine = [p for p in pages if row["start"] <= p.start < row["end"]]
        assert " ".join(p.text for p in mine) == lines[beat].text
