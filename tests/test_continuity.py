from pathlib import Path

import pytest
import yaml
from test_production import _plan_with, _staged, episode_one  # noqa: F401  episode_one is a fixture

from tih.bible import CharacterLock, Locks, NotLocked, PlaceLock, file_sha256, load_bible, save_locks
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
from tih.stages.performance import motion_elements
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
    assert "Match the location, light direction and weather of image 2" in prompt
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
