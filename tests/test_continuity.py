import json
import subprocess
from pathlib import Path

import pytest
import yaml
from test_production import _plan_with, _staged, episode_one  # noqa: F401  episode_one is a fixture

from tih.cli import rehearse_episode

from tih.bible import CharacterLock, Grade, Locks, NotLocked, PlaceLock, file_sha256, load_bible, save_locks
from tih.briefs import keyframe_brief, motion_brief
from tih.budget import BudgetGuard
from tih.gates import approve
from tih.ledger import Ledger
from tih.paths import RunPaths
from tih.production import load_resolved
from tih.providers.dry import DryRunTransport
from tih.providers.fal import GatewayContext, upload_local
from tih.providers.pricing import read_cost
from tih.shots import PlanError, continuity_notes, resolve_plan
from tih.stages import camera as _camera  # noqa: F401  registers the stage
from tih.stages import plates as _plates  # noqa: F401  registers the stage
from tih.stages import record as _record  # noqa: F401  registers the stage
from tih.stages import shotplan as _shotplan  # noqa: F401  registers the stage
from tih.media.ffmpeg import FfmpegError
from tih.stages import cut
from tih.stages.cut import ambience_track, grade_filters, video_chain
from tih.stages.performance import motion_elements
from tih.stages.voice import dialogue_starts
from tih.stages.registry import RunContext, run_stage

ROOT = Path(__file__).parents[1]


@pytest.fixture
def dry(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("TIH_LOCKS_FILE", str(tmp_path / "locks.yaml"))
    paths = RunPaths("lincoln", 1, root=tmp_path)
    ledger = Ledger(tmp_path / "lincoln" / "calls.jsonl")
    gateway = GatewayContext(paths, ledger, BudgetGuard(ledger), DryRunTransport(tmp_path / "dry"),
                             ROOT / "series" / "lincoln" / "models.yaml")
    ctx = RunContext(paths=paths, gateway=gateway)
    run_stage(ctx, "record")
    _staged(ctx)
    run_stage(ctx, "shotplan")
    approve(paths, "script")
    return ctx, ledger


def _image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(path.name.encode())  # stands in for a locked image; only its hash is checked
    return path


def test_plates_only_for_unlocked_places(dry, tmp_path):
    ctx, ledger = dry
    plate = _image(tmp_path / "plate_L1.png")
    save_locks("lincoln", Locks(places={"L1": PlaceLock(reference=str(plate), reference_sha256=file_sha256(plate))}))
    run_stage(ctx, "plates")
    calls = [r for r in ledger.current() if r.step == "plate"]
    assert calls and {r.shot for r in calls} == {"L2"}


def test_keyframes_need_place_plate(dry, tmp_path):
    ctx, ledger = dry
    cast = {c for s in load_resolved(ctx).shots for c in s.cast}
    looks = {}
    for character in cast:
        image = _image(tmp_path / f"{character}.png")
        looks[character] = CharacterLock(reference=str(image), reference_sha256=file_sha256(image))
    save_locks("lincoln", Locks(characters=looks))
    approve(ctx.paths, "look")
    with pytest.raises(NotLocked, match="L1"):
        run_stage(ctx, "keyframes")
    assert ledger.current() == []


def test_keyframe_brief_names_plate_image(episode_one):
    bible = load_bible("lincoln")
    plan = resolve_plan(_plan_with(lambda p: None), episode_one, bible)
    one = next(s for s in plan.shots if s.id == "1.1")
    prompt = keyframe_brief(one, bible, has_plate=True)["prompt_gemini"]
    assert "Match the location of image 2" in prompt
    assert "light direction and weather of image" not in prompt  # each clip's scene rule sets those
    empty = next(s for s in plan.shots if s.id == "1.2")
    assert "image 1" in keyframe_brief(empty, bible, has_plate=True)["prompt_gemini"]
    assert "image" not in keyframe_brief(empty, bible)["prompt_gemini"]


def _resolve(episode_one, edit):
    return resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


def test_scene_rule_needs_side_for_cast(episode_one):
    def edit(plan):
        del plan.scenes[0].sides["powell"]

    with pytest.raises(PlanError, match="powell has no side in clip 1"):
        _resolve(episode_one, edit)


def test_clip_needs_scene_rule(episode_one):
    def edit(plan):
        plan.scenes = [rule for rule in plan.scenes if rule.clip != 2]

    with pytest.raises(PlanError, match="clip 2 has no scene rule"):
        _resolve(episode_one, edit)


def test_jump_cut_rejected(episode_one):
    def edit(plan):
        a, b = plan.shots[2], plan.shots[3]  # 1.3 powell MCU, 1.4 booth MCU
        b.cast, b.keyframe = list(a.cast), a.keyframe

    with pytest.raises(PlanError, match="shots 1.3 and 1.4 are a jump cut"):
        _resolve(episode_one, edit)


def test_shot_length_bounds(episode_one):
    def edit(plan):
        plan.shots[1].duration = 0.5
        plan.shots[0].duration += 1.5

    with pytest.raises(PlanError, match="shot 1.2 is 0.5 s; shots run 1.0-8.0 s"):
        _resolve(episode_one, edit)


def test_continuity_notes_flag_no_wide_open(episode_one):
    plan = _resolve(episode_one, lambda p: None)
    assert not any(note.startswith("clip 2: L1") for note in continuity_notes(plan))  # 2.1 WIDE opens L1

    def edit(p):
        p.shots[6].size = "CU"  # 2.1
        p.shots[9].size = "MCU"  # 2.4, the next L1 shot in clip 2

    notes = continuity_notes(_resolve(episode_one, edit))
    assert any(note.startswith("clip 2: L1") and "WIDE or MEDIUM" in note for note in notes)


def test_keyframe_brief_states_sides_light_weather(episode_one):
    bible = load_bible("lincoln")
    plan = _resolve(episode_one, lambda p: None)
    shot = next(s for s in plan.shots if s.id == "1.6")
    prompt = keyframe_brief(shot, bible)["prompt_gemini"]
    assert "the 26-year-old man from image 1 stands screen-left" in prompt
    assert "the 20-year-old man from image 2 stands screen-right" in prompt
    assert shot.scene.light in prompt and shot.scene.weather in prompt
    assert shot.scene.weather in motion_brief(shot, bible)["prompt_kling"]


KLING = "fal-ai/kling-video/v3/pro/image-to-video"


def test_elements_double_price():
    models = ROOT / "series" / "lincoln" / "models.yaml"
    plain, _ = read_cost(models, KLING, {"duration": "3", "generate_audio": False}, None, 9.0)
    bound, _ = read_cost(models, KLING, {"duration": "3", "generate_audio": False,
                                         "elements": [{"frontal_image_url": "u"}]}, None, 9.0)
    assert plain == pytest.approx(3 * 0.112)
    assert bound == pytest.approx(3 * 0.112 * 2)


def test_motion_passes_elements_in_cast_order(dry, tmp_path, episode_one):
    ctx, _ = dry
    bible = load_bible("lincoln")
    looks = {c: _image(tmp_path / f"{c}.png") for c in ("booth", "powell")}
    locks = Locks(characters={c: CharacterLock(reference=str(p), reference_sha256=file_sha256(p)) for c, p in looks.items()})
    plan = resolve_plan(_plan_with(lambda p: None), episode_one, bible)
    two = next(s for s in plan.shots if s.id == "1.6")
    elements = motion_elements(ctx.gateway, locks, two)
    assert [e["frontal_image_url"] for e in elements] == [upload_local(ctx.gateway, looks[c]) for c in two.cast]
    assert elements[0]["reference_image_urls"] == [elements[0]["frontal_image_url"]]
    prompt = motion_brief(two, bible)["prompt_kling"]
    assert "@Element1" in prompt and "@Element2" in prompt
    assert "@Element" not in motion_brief(two, bible)["prompt_hailuo"]


def test_no_elements_without_cast(dry, episode_one):
    ctx, _ = dry
    plan = resolve_plan(_plan_with(lambda p: None), episode_one, load_bible("lincoln"))
    empty = next(s for s in plan.shots if s.id == "1.2")
    assert motion_elements(ctx.gateway, Locks(), empty) is None


def test_grade_filters_before_text(episode_one, tmp_path):
    bible = load_bible("lincoln")
    assert grade_filters(Grade(eq="contrast=1.05", grain=6)) == ["eq=contrast=1.05", "noise=alls=6:allf=t"]
    assert grade_filters(Grade()) == []
    plan = resolve_plan(_plan_with(lambda p: None), episode_one, bible)
    chain = video_chain(plan, bible, tmp_path / "text")
    first_text = next(i for i, f in enumerate(chain) if f.startswith("drawtext"))
    assert chain.index(f"eq={bible.style.grade.eq}") < first_text


def _two_lines(episode_one, edit=lambda p: None):
    plan = resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))
    beats = [s.beat for s in plan.shots if s.line]
    return plan, list(dict.fromkeys(beats))


def test_jcut_lead_applied_after_silent_shot(episode_one):
    plan, beats = _two_lines(episode_one)
    shot = next(s for s in plan.shots if s.id == "2.3")  # Lincoln's first line, after a silent insert
    starts = dialogue_starts(plan, {b: 0.0 for b in beats})
    assert starts[shot.beat] == pytest.approx(shot.start_s - 0.25)
    first = plan.shots[0]
    assert starts[first.beat] == first.start_s  # nothing before the first shot


def test_jcut_lead_never_overlaps(episode_one):
    plan, beats = _two_lines(episode_one)
    shot = next(s for s in plan.shots if s.id == "2.3")
    before = beats[beats.index(shot.beat) - 1]
    ends = {b: 0.0 for b in beats}
    ends[before] = shot.start_s - 0.1
    assert dialogue_starts(plan, ends)[shot.beat] == pytest.approx(shot.start_s - 0.1)
    ends[before] = shot.start_s + 0.3
    assert dialogue_starts(plan, ends)[shot.beat] == pytest.approx(shot.start_s)


def _mean_db(path, start, length):
    out = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(length), "-i", str(path),
                          "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    return float(out.split("mean_volume:")[1].split("dB")[0])


def test_ambience_loops_to_span(tmp_path):
    source = tmp_path / "rain.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=2:a=0.3", str(source)], check=True)
    bed = ambience_track(source, 40.0, tmp_path / "bed.wav")
    assert 39.9 < float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                       str(bed)], capture_output=True, text=True).stdout) < 40.1
    assert _mean_db(bed, 35, 1) > -40


def test_clip_needs_ambience(episode_one):
    def edit(plan):
        plan.cues = [c for c in plan.cues if not (c.kind == "ambience" and c.scene == 3)]

    with pytest.raises(PlanError, match="clip 3 has no ambience"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


@pytest.mark.slow
def test_all_parts_conformed(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    source = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path / "src"))
    run_stage(source, "record")
    _staged(source)
    ctx = rehearse_episode(1, "lincoln", tmp_path / "dry", source=tmp_path / "src")
    parts = sorted((ctx.paths.scope_dir / "cut_seedance" / "work" / "takes").glob("*.mp4"))
    assert parts
    for part in parts:
        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate,pix_fmt,sample_aspect_ratio", "-of", "json", str(part)],
            capture_output=True, text=True).stdout)["streams"][0]
        assert (probe["width"], probe["height"], probe["r_frame_rate"], probe["pix_fmt"]) == (1080, 1920, "24/1", "yuv420p"), part
        assert probe.get("sample_aspect_ratio", "1:1") == "1:1", part


def test_empty_ambience_fails_instead_of_hanging(tmp_path):
    empty = tmp_path / "empty.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", "0", str(empty)], check=True)
    with pytest.raises(FfmpegError, match="too short to loop"):
        ambience_track(empty, 10.0, tmp_path / "bed.wav")


def test_ambience_must_span_its_clip(episode_one):
    def late(plan):
        bed = next(c for c in plan.cues if c.kind == "ambience" and c.scene == 2)
        bed.at += 1.0

    with pytest.raises(PlanError, match="ambience bed_2 must start at 13.0 s and run 14.0 s"):
        resolve_plan(_plan_with(late), episode_one, load_bible("lincoln"))

    def past_end(plan):
        bed = next(c for c in plan.cues if c.kind == "ambience" and c.scene == 5)
        bed.at, bed.duration = 80.0, 5.0

    with pytest.raises(PlanError, match="ambience bed_5 must start at 53.0 s"):
        resolve_plan(_plan_with(past_end), episode_one, load_bible("lincoln"))


def test_motion_checks_every_look_before_spending(dry, tmp_path):
    ctx, ledger = dry
    plan = load_resolved(ctx)
    cast = list(dict.fromkeys(c for s in plan.shots for c in s.cast))
    looks = {c: _image(tmp_path / f"{c}.png") for c in cast}
    plates = {p: _image(tmp_path / f"plate_{p}.png") for p in dict.fromkeys(s.place for s in plan.shots)}
    save_locks("lincoln", Locks(
        characters={c: CharacterLock(reference=str(i), reference_sha256=file_sha256(i)) for c, i in looks.items()},
        places={p: PlaceLock(reference=str(i), reference_sha256=file_sha256(i)) for p, i in plates.items()},
    ))
    approve(ctx.paths, "look")
    run_stage(ctx, "keyframes")
    late = next(c for c in reversed(cast) if c not in plan.shots[0].cast)
    looks[late].write_bytes(b"changed after the lock")
    with pytest.raises(NotLocked, match=late):
        run_stage(ctx, "motion")
    assert [r for r in ledger.current() if r.step == "video"] == []


def test_mix_pads_silence_only_to_the_runtime(monkeypatch, tmp_path):
    # endless apad before atrim froze ffmpeg 9.1 in about 1 run in 8
    seen = []
    monkeypatch.setattr(cut.ffmpeg, "run", lambda args, **_: seen.append(args) or "")
    cut._mix_inputs([(tmp_path / "a.wav", 0.0, None, 0.0), (tmp_path / "b.wav", 2.0, 1.0, -6.0)], 73.0,
                    tmp_path / "bed.wav")
    graph = seen[0][seen[0].index("-filter_complex") + 1]
    assert "apad=whole_dur=73.000,atrim=0:73.000[out]" in graph
