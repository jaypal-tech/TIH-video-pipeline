import re
from pathlib import Path

import pytest
import yaml

from tih.bible import load_bible
from tih.briefs import (
    SCORE_WORDS, SHOT_HEADER, SPOKEN, cue_brief, keyframe_brief, motion_brief, opening_brief, scene_brief, speech_settings,
    voice_design_brief, voice_preview, _setting_words, _sound_list,
)
from tih.paths import RunPaths
from tih.scenes import FLOWS, scene_takes
from tih.shots import Cue, Opening, example_plan_path, load_plan, resolve_plan
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


def test_keyframe_brief_names_each_person_s_must_haves(plan, bible):
    two = shot(plan, "1.6")
    for prompts in (keyframe_brief(two, bible), keyframe_brief(two, bible, has_plate=True)):
        for word, prompt in (("image", prompts["prompt_gemini"]), ("Figure", prompts["prompt_seedream"])):
            for n, cid in enumerate(two.cast, start=1):
                for item in bible.characters[cid].must:
                    assert item in prompt
            booth = two.cast.index("booth") + 1
            assert f"from {word} {booth}: black mustache, black kid gloves." in prompt
            assert prompt.index("black kid gloves") < prompt.index(bible.style.look[:30])


def test_keyframe_brief_without_cast_has_no_must_haves(plan, bible):
    prompt = keyframe_brief(shot(plan, "1.2"), bible)["prompt_gemini"]
    assert "Keep for" not in prompt


def test_voice_preview_joins_whole_lines_up_to_the_limit():
    lines = ["You could hit him from here.", "Then just listen.", "Go on. I'm listening.", "x" * 600]
    assert voice_preview(lines, limit=60) == "You could hit him from here. Then just listen."
    assert voice_preview(lines) == "You could hit him from here. Then just listen. Go on. I'm listening."
    assert voice_preview(["one two three four"], limit=12) == "one two"
    assert len(voice_preview(["word " * 200])) <= 500


def test_voice_preview_uses_the_speaker_s_lines_in_order(plan):
    lines = [s.line for s in plan.shots if s.speaker == "booth"]
    preview = voice_preview(lines)
    assert preview.startswith(lines[0]) and lines[1] in preview and len(preview) > len(lines[0])


def test_seedance_prompt_repeats_the_full_look_in_every_shot(plan, bible):
    take = next(t for t in scene_takes(plan, "seedance") if len(t.shots) > 1 and t.shots[0].cast)
    prompt = scene_brief(take, bible, plate=False)["prompt_seedance"]
    who = take.shots[0].cast[0]
    described = (bible.characters[who].scene_look or bible.characters[who].look).replace('"', "")
    assert prompt.count(described) >= sum(who in s.cast for s in take.shots)
    assert len(SHOT_HEADER.findall(prompt)) == len(take.shots)


def test_dialogue_wording_follows_the_guide(plan, bible):
    for take in scene_takes(plan, "seedance"):
        prompt = scene_brief(take, bible, plate=True)["prompt_seedance"]
        assert "says exactly" not in prompt
        assert prompt.count(' says: "') == len([s for s in take.shots if s.line])
    for take in scene_takes(plan, "veo"):
        prompt = scene_brief(take, bible, plate=True)["prompt_veo"]
        assert ' says: "' not in prompt


@pytest.mark.parametrize("flow", FLOWS)
def test_only_spoken_lines_are_quoted(plan, bible, flow):
    for take in scene_takes(plan, flow):
        prompt = scene_brief(take, bible, plate=True)[f"prompt_{flow}"]
        lines = [s.line for s in take.shots if s.line]
        assert SPOKEN.findall(prompt) == lines
        assert prompt.count('"') == 2 * len(lines)


def test_veo_numbers_referenced_cast_then_the_plate(plan, bible):
    take = next(t for t in scene_takes(plan, "veo") if t.shots[0].cast == ["booth"])
    prompt = scene_brief(take, bible, plate=True)["prompt_veo"]
    assert "exactly as in reference image 1" in prompt
    assert "Reference image 2 is the location" in prompt


def test_veo_prompt_is_labelled_and_within_the_guides_length(plan, bible):
    for take in scene_takes(plan, "veo"):
        prompt = scene_brief(take, bible, plate=True)["prompt_veo"]
        assert len(prompt.split()) <= 250
        assert "\nNegative: no subtitles, no captions, no text overlays, no music." in prompt
        assert "push in" not in prompt and "push-in" not in prompt


def test_veo_describes_an_unreferenced_face_in_words(plan, bible):
    take = next(t for t in scene_takes(plan, "veo") if t.shots[0].cast == ["lincoln"])
    prompt = scene_brief(take, bible, plate=False)["prompt_veo"]
    lincoln = bible.characters["lincoln"].look.replace('"', "")
    assert lincoln.split(", ", 1)[1] in prompt and lincoln.split(",")[0] in prompt
    assert bible.characters["lincoln"].scene_look not in prompt
    assert "reference image" not in prompt


def test_real_names_never_reach_a_prompt(plan, bible):
    prompts = [p for flow in FLOWS for t in scene_takes(plan, flow) for p in scene_brief(t, bible, plate=False).values()]
    prompts += opening_brief(bible.places[plan.shots[0].place], bible, plate=True).values()
    for prompt in prompts:
        assert "Lincoln" not in prompt and "Booth" not in prompt and "Powell" not in prompt


def test_opening_is_silent_and_only_veo_names_the_plate(plan, bible):
    prompts = opening_brief(bible.places["L1"], bible, plate=True)
    for prompt in prompts.values():
        assert "Nobody speaks." in prompt and '"' not in prompt
    assert "Reference image 1 is the location" in prompts["prompt_veo"]
    assert "eference" not in prompts["prompt_seedance"]


LAWN_OPENING = Opening(
    place="L1",
    keyframe="The crowd seen from behind, hats and umbrellas packed shoulder to shoulder, torches held high, all facing the lit mansion.",
    motion="Men cheer and wave hats, torches sway, umbrellas shift; the camera pushes slowly forward over their shoulders.",
    sound="crowd cheering, brass band playing a march, rain on umbrellas, torches crackling",
)


def test_seedance_opening_is_the_directors_shot(bible):
    prompt = opening_brief(bible.places["L1"], bible, plate=False, opening=LAWN_OPENING)["prompt_seedance"]
    assert ("Shot 1 (0-5s, wide establishing shot, 28mm lens, deep focus): " + LAWN_OPENING.keyframe
            + " " + LAWN_OPENING.motion + " Nobody speaks.") in prompt
    for sound in LAWN_OPENING.sound.split(", "):
        assert sound in prompt
    assert prompt.endswith("Audio: crowd cheering, brass band playing a march, rain on umbrellas, "
                           "torches crackling. No music score. No speech.")
    assert "Setting for every shot: " + bible.places["L1"].scene_look in prompt
    assert "very slow push forward" not in prompt and '"' not in prompt


def test_opening_sound_drops_score_and_repeats_and_place_tokens(bible):
    opening = Opening(place="L1R", keyframe="Torches sway inside [L1R]", motion="The camera holds",
                      sound="rain, Rain, low strings, crowd murmur.")
    prompts = opening_brief(bible.places["L1R"], bible, plate=False, opening=opening)
    assert "Torches sway inside the crowd. The camera holds. Nobody speaks." in prompts["prompt_seedance"]
    assert "Audio: rain, crowd murmur. No music score. No speech." in prompts["prompt_seedance"]
    assert "[" not in prompts["prompt_seedance"] and "[" not in prompts["prompt_veo"]


def test_veo_opening_carries_the_directors_shot(bible):
    veo = opening_brief(bible.places["L1"], bible, plate=True, opening=LAWN_OPENING)["prompt_veo"]
    assert LAWN_OPENING.keyframe in veo
    assert "torches sway, umbrellas shift" in veo
    assert "Nobody speaks." in veo and '"' not in veo
    assert "rain on umbrellas, torches crackling" in veo
    assert "Reference image 1 is the location" in veo


def test_opening_without_the_directors_shot_keeps_the_generic_text(bible):
    prompts = opening_brief(bible.places["L1"], bible, plate=False)
    assert "Shot 1 (wide establishing shot, 28mm lens, deep focus): a very slow push forward. Nobody speaks." in prompts["prompt_seedance"]
    assert prompts["prompt_seedance"].endswith("Audio: the place's own background sound. No music. No speech.")


@pytest.mark.parametrize("place", ["L1", "L1R"])
def test_inside_a_lawn_view_is_inside_the_crowd(bible, place):
    assert _setting_words(f"Torches inside [{place}] sway", bible, "this setting") == "Torches inside the crowd sway"


def test_seedance_prompt_follows_the_first_film(plan, bible):
    take = next(t for t in scene_takes(plan, "seedance") if len(t.shots) > 1 and "booth" in t.shots[0].cast)
    prompt = scene_brief(take, bible, plate=True)["prompt_seedance"]
    assert bible.style.scene_look in prompt.split("\n\n")[0]
    assert {s.place for s in take.shots} == {take.place}
    assert prompt.split("\n\n")[1] == "Setting: " + bible.places[take.place].scene_look
    assert "Setting for every shot" not in prompt
    assert "THE ACTOR - " + bible.characters["booth"].scene_look in prompt
    assert bible.characters["booth"].look not in prompt
    assert "eference" not in prompt
    assert prompt.rstrip().endswith("No music score.")


def test_seedance_prompt_never_glues_a_place_into_a_description(plan, bible):
    for take in scene_takes(plan, "seedance"):
        prompt = scene_brief(take, bible, plate=False)["prompt_seedance"]
        assert "the same place" not in prompt and " ," not in prompt and "  " not in prompt


def _descriptions(bible):
    found = {}
    for cid, c in bible.characters.items():
        found[cid] = {
            d.replace('"', "") for d in (c.scene_look, ", ".join([c.look, *[m for m in c.must if m.lower() not in c.look.lower()]])) if d
        }
    return found


def _every_prompt(plan, bible):
    for flow in FLOWS:
        for take in scene_takes(plan, flow):
            for plate in (True, False):
                yield flow, take, plate, scene_brief(take, bible, plate)[f"prompt_{flow}"]


def test_possessives_follow_a_name_never_a_description(plan, bible):
    names = {c.screen_name for c in bible.characters.values() if c.screen_name}
    names |= {f"the {c.look.split(',')[0].strip()}" for c in bible.characters.values()}
    for _, _, _, prompt in _every_prompt(plan, bible):
        assert not re.search(r"reference image \d's", prompt)
        story = [p for p in re.split(r"\n\n|\n", prompt) if p.startswith(("Shot ", "Action:"))]
        for text in story:
            for match in re.finditer(r"'s\b", text):
                assert text[: match.start()].endswith(tuple(names)), text[max(0, match.start() - 40): match.end()]


def test_no_description_runs_into_the_next_words(plan, bible):
    every = _descriptions(bible)
    for _, _, _, prompt in _every_prompt(plan, bible):
        for variants in every.values():
            for text in variants:
                assert not re.search(re.escape(text) + r" [a-z]", prompt)


def test_seedance_describes_each_character_once_per_shot(plan, bible):
    every = _descriptions(bible)
    for take in scene_takes(plan, "seedance"):
        prompt = scene_brief(take, bible, plate=False)["prompt_seedance"]
        for cid, variants in every.items():
            shots = sum(cid in s.cast for s in take.shots)
            assert sum(prompt.count(text) for text in variants) == shots
        for paragraph in prompt.split("\n\n"):
            if SHOT_HEADER.match(paragraph):
                shot = take.shots[int(paragraph.split()[1]) - 1]
                assert paragraph.count(" - ") == len(shot.cast)


def test_veo_action_names_roles_only_and_stays_in_the_cap(plan, bible):
    every = _descriptions(bible)
    for take in scene_takes(plan, "veo"):
        for plate in (True, False):
            prompt = scene_brief(take, bible, plate)["prompt_veo"]
            assert len(prompt.split()) <= 250
            action = next(line for line in prompt.split("\n") if line.startswith("Action:"))
            for variants in every.values():
                assert not any(text in action for text in variants)
            assert "the same place" not in prompt and " ," not in prompt and "  " not in prompt


def test_empty_sound_leaves_no_empty_audio_line(plan, bible):
    for flow, take, plate, prompt in _every_prompt(plan, bible):
        assert "Audio: ." not in prompt
    silent = scene_takes(plan, "veo")[0].model_copy(deep=True)
    silent.shots[0].sound = ""
    prompts = scene_brief(silent, bible, plate=True)
    assert "Audio:" not in prompts["prompt_veo"]
    assert prompts["prompt_seedance"].endswith("\n\nAudio: No music score.")


def _audio_items(prompt):
    line = next(line.removeprefix("Audio: ") for line in prompt.replace("\n\n", "\n").split("\n") if line.startswith("Audio: "))
    return [item.strip().lower() for item in line.removesuffix("No music score.").removesuffix("No music score").strip().rstrip(".").split(",") if item.strip()]


def test_a_line_from_outside_the_cast_is_an_off_screen_voice(plan, bible):
    seen = 0
    for flow in FLOWS:
        for take in scene_takes(plan, flow):
            prompts = scene_brief(take, bible, plate=True)
            prompt = prompts[f"prompt_{flow}"]
            for shot in take.shots:
                if shot.line and shot.speaker not in shot.cast:
                    seen += 1
                    wording = "Off-screen, the voice of " if flow == "seedance" else "An off-screen voice says exactly"
                    assert wording in prompt
                    assert "Nobody on screen speaks." in prompt or flow == "seedance"
            assert SPOKEN.findall(prompt) == [s.line for s in take.shots if s.line]
            assert '" Nobody' in prompt or flow == "seedance" or not take.line
    assert seen


def test_audio_keeps_only_diegetic_sound_once(plan, bible):
    for flow, take, plate, prompt in _every_prompt(plan, bible):
        if "Audio: " not in prompt:
            continue
        if flow == "seedance" and len(take.shots) > 1:
            continue
        items = _audio_items(prompt)
        assert len(items) == len(set(items))
        for item in items:
            assert not set(re.findall(r"\w+", item)) & set(SCORE_WORDS), item


def _seedance_audio(prompt):
    return next(line for line in prompt.split("\n\n") if line.startswith("Audio: "))


def test_multi_shot_seedance_audio_is_per_shot(plan, bible):
    seen = 0
    for take in scene_takes(plan, "seedance"):
        audio = _seedance_audio(scene_brief(take, bible, plate=True)["prompt_seedance"])
        if len(take.shots) == 1:
            assert not audio.startswith("Audio: Shot ")
            continue
        seen += 1
        body = audio.removeprefix("Audio: ")
        assert body.endswith("No music score.")
        parts = re.findall(r"Shot (\d+): (.*?)\.(?= Shot \d+: |\s*No music score\.$)", body)
        expected = [(n, _sound_list([s.sound])) for n, s in enumerate(take.shots, start=1)]
        assert parts == [(str(n), sounds) for n, sounds in expected if sounds]
        for _, sounds in parts:
            items = [i.strip().lower() for i in sounds.split(",")]
            assert len(items) == len(set(items))
            for item in items:
                assert not set(re.findall(r"\w+", item)) & set(SCORE_WORDS), item
    assert seen


def test_multi_shot_audio_omits_silent_shots_and_single_shot_keeps_form(plan, bible):
    take = next(t for t in scene_takes(plan, "seedance") if len(t.shots) > 1).model_copy(deep=True)
    take.shots[0].sound = "rain, low strings, rain"
    take.shots[1].sound = ""
    audio = _seedance_audio(scene_brief(take, bible, plate=True)["prompt_seedance"])
    assert audio.startswith("Audio: Shot 1: rain.")
    assert "Shot 2:" not in audio
    for shot in take.shots:
        shot.sound = "strings"
    assert _seedance_audio(scene_brief(take, bible, plate=True)["prompt_seedance"]) == "Audio: No music score."
    single = next(t for t in scene_takes(plan, "seedance") if len(t.shots) == 1).model_copy(deep=True)
    single.shots[0].sound = "rain, wind"
    assert _seedance_audio(scene_brief(single, bible, plate=True)["prompt_seedance"]) == "Audio: rain, wind. No music score."


def test_a_take_that_changes_place_sets_each_shot(plan, bible):
    take = next(t for t in scene_takes(plan, "seedance") if len({s.place for s in t.shots}) > 1)
    paragraphs = scene_brief(take, bible, plate=False)["prompt_seedance"].split("\n\n")
    assert not any(p.startswith("Setting") for p in paragraphs)
    shots = [p for p in paragraphs if SHOT_HEADER.match(p)]
    assert len(shots) == len(take.shots)
    for paragraph, shot in zip(shots, take.shots):
        header, rest = paragraph.split("): ", 1)
        assert rest.startswith("Setting: " + bible.places[shot.place].scene_look + " ")
        others = {p for p in {s.place for s in take.shots} if p != shot.place}
        assert not any(bible.places[p].scene_look in paragraph for p in others)
