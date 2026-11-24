from pathlib import Path

import pytest
import yaml

from tih.bible import load_bible
from tih.briefs import cue_brief, keyframe_brief, motion_brief, speech_settings, voice_design_brief
from tih.paths import RunPaths
from tih.shots import Cue, example_plan_path, load_plan, resolve_plan
from tih.stages import record  # noqa: F401  registers the record stage
from tih.stages.registry import RunContext, run_stage
from tih.story.models import Episode

ROOT = Path(__file__).parents[1]


@pytest.fixture
def plan(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_stage(ctx, "record")
    script = yaml.safe_load((ctx.paths.scope_dir / "record" / "script.yaml").read_text(encoding="utf-8"))
    return resolve_plan(load_plan(example_plan_path("lincoln")), Episode.model_validate(script), load_bible("lincoln"))


def shot(plan, shot_id):
    return next(s for s in plan.shots if s.id == shot_id)


@pytest.fixture
def bible(monkeypatch):
    monkeypatch.chdir(ROOT)
    return load_bible("lincoln")


def test_keyframe_brief_labels_references_in_cast_order(plan, bible):
    prompt = keyframe_brief(shot(plan, "1.6"), bible)["prompt_gemini"]
    first_sentence = prompt.split(". ")[0]
    assert "image 1" in first_sentence and "26-year-old man" in first_sentence
    assert prompt.index("image 1") < prompt.index("image 2")
    assert "[BOOTH]" not in prompt and "[POWELL]" not in prompt and "[L1]" not in prompt


def test_keyframe_brief_puts_shot_before_style(plan, bible):
    prompt = keyframe_brief(shot(plan, "1.1"), bible)["prompt_gemini"]
    assert prompt.index("medium close-up") < prompt.index(bible.style.look[:30])
    assert bible.style.clean in prompt
    assert "negative" not in prompt.lower()


def test_keyframe_brief_without_references(plan, bible):
    prompt = keyframe_brief(shot(plan, "1.2"), bible)["prompt_gemini"]
    assert "image" not in prompt.lower().replace("imagine", "")


def test_seedream_uses_figure_numbers(plan, bible):
    prompts = keyframe_brief(shot(plan, "1.6"), bible)
    assert "Figure 1" in prompts["prompt_seedream"] and "image 1" not in prompts["prompt_seedream"]


def test_motion_brief_mouth_for_speaker_and_listener(plan, bible):
    speaking = motion_brief(shot(plan, "1.1"), bible)["prompt_kling"]
    listening = motion_brief(shot(plan, "1.5"), bible)["prompt_kling"]
    assert "small natural mouth movement" in speaking
    assert "Mouth closed" in listening
    assert "Real-time speed, one continuous take." in speaking
    assert motion_brief(shot(plan, "1.1"), bible)["negative_video"] == bible.style.video_negative


def test_motion_brief_hailuo_static_unless_camera_named(plan, bible):
    pushing = shot(plan, "1.1")
    assert "push" in pushing.motion.lower()
    assert not motion_brief(pushing, bible)["prompt_hailuo"].startswith("[Static shot]")
    still = next(s for s in plan.shots if not any(
        w in s.motion.lower() for w in ("push", "pull", "pan", "tilt", "track", "handheld", "orbit", "dolly", "crane")))
    assert motion_brief(still, bible)["prompt_hailuo"].startswith("[Static shot]")


def test_speech_settings_from_direction():
    assert speech_settings("quietly, almost to himself")["speed"] == 0.95
    assert speech_settings("angrily")["emotion"] == "angry"


def test_speech_settings_ignore_unknown_direction():
    assert speech_settings("") == {"speed": None, "emotion": None}
    assert speech_settings("looking at the window") == {"speed": None, "emotion": None}


def test_voice_design_brief_is_structured(bible):
    text = voice_design_brief(bible.characters["lincoln"])
    assert text.startswith(bible.characters["lincoln"].voice.rstrip("."))
    assert "man aged 56" in text and "Clean close-mic studio recording" in text


def test_cue_brief_adds_period_and_instrumental(bible):
    music = cue_brief(Cue(id="m1", kind="music", at=0, duration=10, prompt="low cello drone"), bible)
    sfx = cue_brief(Cue(id="s1", kind="sfx", at=0, duration=1, prompt="glove creak"), bible)
    assert bible.style.sound_period in music and music.endswith("instrumental only")
    assert "instrumental" not in sfx and bible.style.sound_period in sfx
