"""Real shot boundaries: where a take's clip really cuts, and how the edit follows it.

The video model does not cut at the prompt's times, so name cards and supers
timed to planned shot starts land on the wrong face. No paid calls.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tih.bible import load_bible
from tih.cli import prepare_rehearsal
from tih.media import ffmpeg
from tih.paths import RunPaths
from tih.production import load_resolved
from tih.scenes import CUT_TOLERANCE, as_shot, kept_cuts, real_shots, scene_takes
from tih.shots import load_plan, plan_path, resolve_plan
from tih.stages import record as _record  # noqa: F401  registers the stage
from tih.stages import registry
from tih.stages import scenes as scenes_stage
from tih.stages.cut import text_plan
from tih.stages.registry import RunContext, run_stage
from tih.stages.scenes import read_shots, read_timing, run_scenes
from tih.story.models import Episode

ROOT = Path(__file__).parents[1]
FILM_PLAN = ROOT / "tests" / "fixtures" / "film_plan.yaml"


@pytest.fixture(autouse=True)
def in_repo(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("TIH_LOCKS_FILE", raising=False)


def _clip(path, colours):
    """A silent clip of solid colours: [(colour, seconds), ...]."""
    args = ["ffmpeg", "-y", "-v", "error"]
    for colour, seconds in colours:
        args += ["-f", "lavfi", "-i", f"color=c={colour}:s=180x320:r=24:d={seconds}"]
    joined = "".join(f"[{i}:v]" for i in range(len(colours))) + f"concat=n={len(colours)}:v=1:a=0[v]"
    subprocess.run([*args, "-filter_complex", joined, "-map", "[v]", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_cut_times_finds_each_hard_cut(tmp_path):
    clip = _clip(tmp_path / "three.mp4", [("0x303a4a", 4.0), ("0xd0d0c0", 3.5), ("0x101820", 2.5)])
    assert ffmpeg.cut_times(clip) == pytest.approx([4.0, 7.5], abs=0.05)


def test_cut_times_of_one_continuous_shot_is_empty(tmp_path):
    assert ffmpeg.cut_times(_clip(tmp_path / "one.mp4", [("0x303a4a", 4.0)])) == []


@pytest.fixture
def plan(tmp_path):
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_stage(ctx, "record")
    script = ctx.paths.scope_dir / "record" / "script.yaml"
    episode = Episode.model_validate(yaml.safe_load(script.read_text(encoding="utf-8")))
    return resolve_plan(load_plan(FILM_PLAN), episode, load_bible("lincoln"))


def test_cuts_near_their_planned_times_give_each_shot_its_real_span(plan):
    take = scene_takes(plan, "seedance")[0]  # shots of 4.0, 3.5, 4.0 and 3.5 s: planned cuts at 4.0, 7.5, 11.5
    assert real_shots(take, [2.25, 7.7, 11.4], 15.0) == {
        "1.1": (0.0, 2.25), "1.2": (2.25, 7.7), "1.3": (7.7, 11.4), "1.4": (11.4, 15.0)}


@pytest.mark.parametrize("cuts", [
    [4.0, 7.5],  # one cut is missing
    [4.0, 7.5, 9.0, 11.5],  # one too many
    [4.0, 7.5, 11.5 + CUT_TOLERANCE + 0.1],  # too far from its plan
    [4.0, 7.5, 14.9],
])
def test_cuts_that_do_not_match_the_plan_are_not_used(plan, cuts):
    assert real_shots(scene_takes(plan, "seedance")[0], cuts, 14.8) is None


def test_a_single_shot_take_matches_when_no_cut_is_found(plan):
    shot = plan.shots[0]
    take = scene_takes(plan.model_copy(update={"shots": [shot]}), "seedance")[0]
    assert real_shots(take, [], 4.5) == {shot.id: (0.0, 4.5)}
    assert real_shots(take, [2.0], 4.5) is None


def test_as_shot_puts_every_shot_at_its_real_start(plan):
    takes = scene_takes(plan, "seedance")
    first = takes[0]
    played = {first.id: 15.0}
    real = real_shots(first, [2.25, 7.7, 11.4], 15.0)
    shot = as_shot(plan, takes, played, real)
    mine = {s.id: s for s in shot.shots}
    assert [(mine[i].start_s, mine[i].duration) for i in ("1.1", "1.2", "1.3", "1.4")] == [
        pytest.approx(pair) for pair in ((0.0, 2.25), (2.25, 5.45), (7.7, 3.7), (11.4, 3.6))]
    plain = as_shot(plan, takes, played)
    later = [s.id for s in plan.shots if s.clip != first.clip]
    assert [mine[i].start_s for i in later] == [s.start_s for s in plain.shots if s.id in later]
    assert shot.runtime_s == plain.runtime_s and shot.cues == plain.cues
    again = scene_takes(shot, "seedance")
    assert [(t.id, len(t.shots), t.start_s) for t in again] == [(t.id, len(t.shots), t.start_s) for t in takes]
    assert again[0].duration == pytest.approx(15.0)


def test_real_cuts_that_would_move_a_shot_into_another_take_are_not_used(plan):
    # one clip that needs three generations: 1.1-1.3 (11.5 s), 2.1-2.3 (13.5 s), 2.4
    shots, clock = [], 0.0
    for shot in plan.shots:
        if shot.clip in (1, 2) and shot.id != "1.4":
            duration = 5.0 if shot.id == "2.1" else shot.duration
            shots.append(shot.model_copy(update={"clip": 1, "start_s": clock, "duration": duration}))
            clock += duration
    split = plan.model_copy(update={"shots": shots, "runtime_s": clock})
    takes = scene_takes(split, "seedance")
    assert [[s.id for s in t.shots] for t in takes] == [["1.1", "1.2", "1.3"], ["2.1", "2.2", "2.3"], ["2.4"]]
    played = {t.id: t.duration for t in takes}
    late = real_shots(takes[1], [5.4, 9.5], 13.5)
    assert kept_cuts(split, takes, played, late, "seedance") == late
    # cut at 3.2 s, shot 2.1 would fit the end of the first take (11.5 + 3.2 s) and every take id after it would shift
    early = real_shots(takes[1], [3.2, 9.5], 13.5)
    assert early is not None
    assert kept_cuts(split, takes, played, early, "seedance") == {}
    kept = kept_cuts(split, takes, played, {**early, **real_shots(takes[0], [4.2, 7.5], 11.5)}, "seedance")
    assert set(kept) == {"1.1", "1.2", "1.3"}
    again = scene_takes(as_shot(split, takes, played, kept), "seedance")
    assert [[s.id for s in t.shots] for t in again] == [[s.id for s in t.shots] for t in takes]


def test_a_name_card_follows_its_real_shot_and_an_unmatched_take_keeps_its_words(plan):
    takes = scene_takes(plan, "seedance")
    real = real_shots(takes[0], [2.25, 7.7, 11.4], 15.0)
    spans = {"1.2": (5.0, 6.5), "2.2": (4.0, 6.0)}
    shown = {s.id: s for s in text_plan(as_shot(plan, takes, {"1.1": 15.0}, real), "seedance", spans, real).shots}
    assert shown["1.2"].start_s == pytest.approx(2.25)  # POWELL's card sits on his real shot, not on his words
    assert any(o.kind == "name_card" for o in shown["1.2"].overlays)
    second = next(t for t in scene_takes(as_shot(plan, takes, {"1.1": 15.0}, real), "seedance") if t.id == "2.1")
    assert shown["2.2"].start_s == pytest.approx(second.start_s + 4.0 - 0.2)  # no real cuts: timed to its words


def _staged(tmp_path, monkeypatch):
    source = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path / "src"))
    run_stage(source, "record")
    scope = source.paths.scope_dir
    (scope / "writers_room").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(scope / "record" / "script.yaml", scope / "writers_room" / "script.yaml")
    plan_path(source.paths).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FILM_PLAN, plan_path(source.paths))
    registry._write_done(source, "writers_room")
    registry._write_done(source, "director")
    monkeypatch.setenv("TIH_LOCKS_FILE", str(tmp_path / "dry" / "lincoln" / "locks.yaml"))
    return prepare_rehearsal(1, "lincoln", tmp_path / "dry", source=tmp_path / "src")


def test_placeholder_clips_cut_at_their_planned_times_and_the_stage_records_them(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch)
    run_scenes(ctx, "seedance")
    plan = load_resolved(ctx)
    shots, cuts = read_shots(ctx, "seedance")
    takes = scene_takes(plan, "seedance")
    assert cuts == {take.id: "matched" for take in takes}
    assert set(shots) == {shot.id for shot in plan.shots}
    played = read_timing(ctx, "seedance")[2]
    for take in takes:
        clock = 0.0
        for shot in take.shots:
            assert shots[shot.id][0] == pytest.approx(clock, abs=0.05)
            clock += shot.duration
        assert shots[take.shots[-1].id][1] == played[take.id]


def test_a_take_whose_cuts_are_not_found_keeps_its_planned_times(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch)
    plan = load_resolved(ctx)
    first = scene_takes(plan, "seedance")[0]
    found = ffmpeg.cut_times
    monkeypatch.setattr(scenes_stage.ffmpeg, "cut_times", lambda video: found(video)[:1])
    run_scenes(ctx, "seedance")
    shots, cuts = read_shots(ctx, "seedance")
    assert set(cuts.values()) == {"unmatched"}
    assert shots == {}
    data = yaml.safe_load((ctx.paths.scope_dir / "scenes_seedance" / "timing.yaml").read_text(encoding="utf-8"))
    assert data["cuts"][first.id] == "unmatched" and set(data) == {"spans", "captions", "played", "shots", "cuts"}
