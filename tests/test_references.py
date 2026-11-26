from pathlib import Path

import pytest
import yaml

from tih import briefs
from tih.bible import CostumeReference, PlaceReference, References, load_bible
from tih.briefs import scene_brief, scene_references
from tih.paths import RunPaths
from tih.scenes import scene_takes
from tih.shots import load_plan, resolve_plan
from tih.stages import record as _record  # noqa: F401  registers the stage
from tih.stages.registry import RunContext, run_stage
from tih.story.models import Episode

ROOT = Path(__file__).parents[1]
FILM_PLAN = ROOT / "tests" / "fixtures" / "film_plan.yaml"

REFS = References(
    characters={
        "booth": CostumeReference(image="a/booth.png", wears="his flat-crowned hat, white collar and dark frock coat"),
        "powell": CostumeReference(image="a/powell.png", wears="his grey forage cap and dark sack coat"),
        "lincoln": CostumeReference(image="a/lincoln.png", wears="his black frock coat and bow tie"),
        "tad": CostumeReference(image="a/tad.png", wears="his small dark wool jacket"),
    },
    places={
        "L1": PlaceReference(image="a/L1.png", shows="the lawn, the mansion and the light"),
        "L2": PlaceReference(image="a/L2.png", shows="the window, the candle and the room"),
    },
)


@pytest.fixture(autouse=True)
def in_repo(monkeypatch):
    monkeypatch.chdir(ROOT)


@pytest.fixture
def bible():
    return load_bible("lincoln")


@pytest.fixture
def takes(tmp_path, bible):
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_stage(ctx, "record")
    script = Episode.model_validate(yaml.safe_load((ctx.paths.scope_dir / "record" / "script.yaml").read_text(encoding="utf-8")))
    plan = resolve_plan(load_plan(FILM_PLAN), script, bible)
    return {t.id: t for t in scene_takes(plan, "seedance_ref")}


def test_costumes_come_first_in_cast_order_then_places_in_order_of_appearance(takes):
    listed = scene_references(takes["2.1"], REFS)
    assert [(r.kind, r.key, r.image) for r in listed] == [
        ("costume", "lincoln", "a/lincoln.png"),
        ("costume", "tad", "a/tad.png"),
        ("place", "L1", "a/L1.png"),
        ("place", "L2", "a/L2.png"),
    ]


def test_a_character_or_place_without_a_picture_is_left_out(takes):
    only_places = References(places=REFS.places)
    assert [r.kind for r in scene_references(takes["2.1"], only_places)] == ["place", "place"]
    assert scene_references(takes["2.1"], References()) == []


def test_references_are_capped_at_the_models_limit(takes, monkeypatch):
    monkeypatch.setattr(briefs, "MAX_REFERENCES", 2)
    assert len(scene_references(takes["2.1"], REFS)) == 2


def test_prompt_labels_each_picture_with_its_owner_and_its_shots(takes, bible):
    prompt = scene_brief(takes["2.1"], bible, False, REFS)["prompt_seedance"]
    assert "@Image1 is a costume reference for THE PRESIDENT: a tailor's dress form wearing his black frock coat" in prompt
    assert "@Image2 is a costume reference for THE BOY: a tailor's dress form wearing his small dark wool jacket" in prompt
    assert "@Image3 is the location of shots 1 and 3: take the lawn, the mansion and the light from it." in prompt
    assert "@Image4 is the location of shots 2 and 4: take the window, the candle and the room from it." in prompt
    assert "Each person wears exactly the clothes of their own reference" in prompt
    assert "never appear in the film" in prompt
    assert "background extras and are never THE PRESIDENT or THE BOY" in prompt
    assert "Lincoln" not in prompt and "Tad" not in prompt


def test_a_place_that_covers_every_shot_says_so(takes, bible):
    prompt = scene_brief(takes["1.1"], bible, False, REFS)["prompt_seedance"]
    assert "@Image3 is the location of all four shots" in prompt
    assert "@Image1 is a costume reference for THE ACTOR" in prompt and "@Image2 is a costume reference for THE SOLDIER" in prompt


def test_references_sit_between_the_setting_and_the_first_shot(takes, bible):
    prompt = scene_brief(takes["1.1"], bible, False, REFS)["prompt_seedance"]
    assert prompt.index("Setting:") < prompt.index("References:") < prompt.index("Shot 1 (")


def test_a_take_without_cast_only_names_the_location(takes, bible):
    base = takes["1.1"]
    empty = base.model_copy(update={"shots": [base.shots[3].model_copy(update={"cast": []})]})
    prompt = scene_brief(empty, bible, False, REFS)["prompt_seedance"]
    assert "costume reference" not in prompt and "background extras" not in prompt
    assert "@Image1 is the location of the shot" in prompt


def test_without_references_the_prompt_is_exactly_the_text_only_prompt(takes, bible):
    for take in takes.values():
        plain = scene_brief(take, bible, False)["prompt_seedance"]
        assert "@Image" not in plain
        assert scene_brief(take, bible, False, References())["prompt_seedance"] == plain
