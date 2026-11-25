import shutil
from pathlib import Path

import pytest
import yaml

from tih.cli import prepare_rehearsal
from tih.production import NoUsableTake, load_resolved, read_takes
from tih.scenes import (
    CAPTION_TAIL, CAPTION_WORDS, FLOWS, MAX_TAKE_SECONDS, MAX_TAKE_SHOTS, SceneTake, caption_pages,
    line_spans, opening_take, request_seconds, retimed, scene_takes, shifted,
)
from tih.paths import RunPaths
from tih.providers.fal import ProviderError
from tih.shots import (
    Opening, PlanError, ResolvedPlan, ShotPlan, example_plan_path, load_plan, plan_path, resolve_plan,
)
from tih.stages import record as _record  # noqa: F401  registers the stage
from tih.stages import registry
from tih.stages import scenes as scenes_stage
from tih.stages.registry import RunContext, run_stage
from tih.stages.scenes import check_take, read_timing, run_scenes
from tih.stages.voice import word_difference
from tih.story.models import Episode
from tih.bible import load_bible

ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def in_repo(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("TIH_LOCKS_FILE", raising=False)


def test_lincoln_and_tad_are_never_face_references():
    bible = load_bible("lincoln")
    assert bible.characters["lincoln"].face_reference is False
    assert bible.characters["tad"].face_reference is False
    assert bible.characters["booth"].face_reference is True
    assert bible.characters["powell"].face_reference is True


def test_reverse_lawn_view_needs_no_plate():
    bible = load_bible("lincoln")
    assert bible.places["L1R"].plate is False
    assert bible.places["L1"].plate is True


def test_seedance_film_wording_is_in_the_bible():
    bible = load_bible("lincoln")
    assert bible.style.scene_look.startswith("photorealistic period drama set in 1865, 35mm film")
    assert bible.style.scene_look.endswith("Wholly original, invented faces.")
    assert [bible.characters[c].screen_name for c in ("booth", "powell", "lincoln", "tad")] == [
        "THE ACTOR", "THE SOLDIER", "THE PRESIDENT", "THE BOY"]
    assert bible.characters["booth"].scene_look.startswith("a 26-year-old man of compact build, jet-black curly hair to the collar")
    assert bible.characters["powell"].scene_look.endswith("buttoned to the neck over a collarless grey shirt")
    assert "four black cloth-covered buttons" in bible.characters["booth"].scene_look
    assert "no hat" in bible.characters["lincoln"].scene_look and "no hat" in bible.characters["tad"].scene_look
    assert bible.characters["lincoln"].scene_voice.startswith("a high, clear, reedy tenor")
    assert bible.places["L1"].scene_look.startswith(
        "an open lawn at night in April 1865, steady drizzle, seen from behind")
    assert "no building is in frame" in bible.places["L1R"].scene_look
    assert "mansion" not in bible.places["L1R"].scene_look
    assert bible.places["L2"].scene_look.startswith("a tall open second-floor sash window")


def test_film_wording_leaves_the_locked_look_text_alone():
    bible = load_bible("lincoln")
    assert bible.characters["booth"].look.startswith("26-year-old man, 5'8\" athletic build")


# --- takes, timing and caption pages ---

@pytest.fixture
def plan(tmp_path):
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_stage(ctx, "record")
    script = ctx.paths.scope_dir / "record" / "script.yaml"
    episode = Episode.model_validate(yaml.safe_load(script.read_text(encoding="utf-8")))
    return resolve_plan(load_plan(example_plan_path("lincoln")), episode, load_bible("lincoln"))


def test_veo_flow_is_one_shot_per_take(plan):
    assert all(len(t.shots) == 1 for t in scene_takes(plan, "veo"))


def test_seedance_flow_packs_shots_within_one_clip(plan):
    takes = scene_takes(plan, "seedance")
    assert any(len(t.shots) > 1 for t in takes)
    for take in takes:
        assert len(take.shots) <= MAX_TAKE_SHOTS and take.duration <= MAX_TAKE_SECONDS
        assert {s.clip for s in take.shots} == {take.clip}


def test_seedance_take_never_mixes_places(plan):
    takes = scene_takes(plan, "seedance")
    for take in takes:
        assert {s.place for s in take.shots} == {take.place}
    for clip in {t.clip for t in takes}:
        assert [t.id for t in takes if t.clip == clip] == [
            f"{clip}.{n}" for n in range(1, 1 + len([t for t in takes if t.clip == clip]))]


@pytest.mark.parametrize("flow", FLOWS)
def test_takes_cover_every_shot_once_in_order(plan, flow):
    assert [s.id for t in scene_takes(plan, flow) for s in t.shots] == [s.id for s in plan.shots]


@pytest.mark.parametrize("flow", FLOWS)
def test_slowed_or_frozen_shot_is_alone_in_its_take(plan, flow):
    for take in scene_takes(plan, flow):
        if any(s.freeze_at is not None or s.speed != 1.0 for s in take.shots):
            assert len(take.shots) == 1


def test_opening_is_five_silent_seconds_at_the_first_place(plan):
    opening = opening_take(plan)
    assert (opening.id, opening.duration, opening.line) == ("0", 5.0, "")
    assert opening.place == plan.shots[0].place


OPENING = Opening(
    place="L1R",
    keyframe="The crowd seen from behind, torches held high.",
    motion="Men cheer and wave hats; the camera pushes slowly forward.",
    sound="crowd cheering, rain on umbrellas",
)


def _episode(tmp_path):
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_stage(ctx, "record")
    script = ctx.paths.scope_dir / "record" / "script.yaml"
    return Episode.model_validate(yaml.safe_load(script.read_text(encoding="utf-8")))


def test_plan_with_an_opening_resolves_and_round_trips(tmp_path):
    raw = load_plan(example_plan_path("lincoln")).model_copy(update={"opening": OPENING})
    again = ShotPlan.model_validate(yaml.safe_load(yaml.safe_dump(raw.model_dump(mode="json"))))
    assert again == raw
    resolved = resolve_plan(again, _episode(tmp_path), load_bible("lincoln"))
    assert resolved.opening == OPENING
    assert ResolvedPlan.model_validate(resolved.model_dump(mode="json")) == resolved


def test_opening_at_an_unknown_place_is_rejected(tmp_path):
    raw = load_plan(example_plan_path("lincoln")).model_copy(
        update={"opening": OPENING.model_copy(update={"place": "L99"})})
    with pytest.raises(PlanError, match="opening: unknown place L99"):
        resolve_plan(raw, _episode(tmp_path), load_bible("lincoln"))


def test_plan_without_an_opening_still_loads(plan):
    assert plan.opening is None


def test_opening_take_is_at_the_openings_place(plan):
    assert plan.shots[0].place != "L1R"
    with_opening = plan.model_copy(update={"opening": OPENING})
    assert opening_take(with_opening).place == "L1R"


def test_request_seconds_is_clamped():
    assert request_seconds(SceneTake.model_construct(duration=2.5), (4, 15)) == 4
    assert request_seconds(SceneTake.model_construct(duration=14.2), (4, 15)) == 15
    assert request_seconds(SceneTake.model_construct(duration=3.0), (8, 8)) == 8


def test_shifted_moves_shots_cues_and_runtime(plan):
    moved = shifted(plan, 5.0)
    assert moved.shots[0].start_s == plan.shots[0].start_s + 5.0
    assert moved.cues[0].at == plan.cues[0].at + 5.0
    assert moved.runtime_s == plan.runtime_s + 5.0


def _talking_take(plan, lines, start_s=29.0, duration=13.0):
    base = plan.shots[0]
    shots = [
        base.model_copy(update={"id": f"t{i}", "line": line, "duration": duration / len(lines)})
        for i, line in enumerate(lines)
    ]
    return SceneTake(id="9.1", clip=9, shots=shots, place=base.place, start_s=start_s, duration=duration)


def _word(text, start, end):
    return {"text": text, "start": start, "end": end, "type": "word"}


GO_ON = [_word("Go", 4.02, 4.40), _word("on.", 4.45, 4.88),
         _word("I'm", 5.88, 6.10), _word("listening.", 6.15, 6.64)]


def test_line_spans_per_shot(plan):
    take = _talking_take(plan, ["Go on.", "I'm listening."])
    assert line_spans(take, GO_ON) == {"t0": (4.02, 4.88), "t1": (5.88, 6.64)}
    with pytest.raises(ValueError):
        line_spans(take, GO_ON[:3])


def test_caption_pages_split_at_the_end_of_each_line(plan):
    take = _talking_take(plan, ["Go on.", "I'm listening."])
    pages = caption_pages(take, GO_ON)
    assert [p.text for p in pages] == ["Go on.", "I'm listening."]
    assert [p.start for p in pages] == pytest.approx([33.02, 34.88])
    assert [p.end for p in pages] == pytest.approx([29.0 + 4.88 + CAPTION_TAIL, 36.04])
    assert pages[0].end == pytest.approx(34.28)
    with pytest.raises(ValueError):
        caption_pages(take, GO_ON[:3])


def test_caption_page_leaves_soon_after_its_last_word_across_a_long_pause(plan):
    take = _talking_take(plan, ["You could hit him from here.", "Not in this crowd."], start_s=0.0)
    timings = [_word(w, s, e) for w, s, e in (
        ("You", 0.5, 0.7), ("could", 0.75, 1.0), ("hit", 1.05, 1.3), ("him", 1.35, 1.6),
        ("from", 1.7, 1.95), ("here.", 2.0, 2.24),
        ("Not", 6.44, 6.7), ("in", 6.75, 6.85), ("this", 6.9, 7.1), ("crowd.", 7.15, 7.6))]
    pages = caption_pages(take, timings)
    assert [p.text for p in pages] == ["You could hit him", "from here.", "Not in this crowd."]
    assert pages[0].end == pytest.approx(1.7)  # the next page starts before this one's tail runs out
    assert pages[1].end == pytest.approx(2.24 + CAPTION_TAIL)  # not 6.44, where the next line starts
    assert pages[2].end == pytest.approx(7.6 + CAPTION_TAIL)


def test_retimed_moves_a_shot_to_its_spoken_span(plan):
    take = _talking_take(plan, ["Go on.", "I'm listening."])
    talking = plan.model_copy(update={"shots": take.shots})
    moved = retimed(talking, [take], {"t0": (4.02, 4.88)})
    assert moved.shots[0].start_s == pytest.approx(29.0 + 3.82)
    assert moved.shots[0].duration == pytest.approx(4.88 + CAPTION_TAIL - 3.82)
    assert moved.shots[1] == take.shots[1]


def test_long_line_pages_of_four(plan):
    line = " ".join(f"w{i}" for i in range(11))
    take = _talking_take(plan, [line])
    timings = [_word(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(11)]
    assert [len(p.text.split()) for p in caption_pages(take, timings)] == [CAPTION_WORDS, 4, 3]


def test_hyphenated_and_dash_tokens_pair_with_their_timings(plan):
    take = _talking_take(plan, ["Twenty-five — yes."])
    timings = [_word("twenty", 1.0, 1.3), _word("five", 1.35, 1.7), _word("yes.", 2.0, 2.4)]
    pages = caption_pages(take, timings)
    assert [p.text for p in pages] == ["Twenty-five — yes."]
    assert pages[0].start == pytest.approx(30.0)
    assert pages[0].end == pytest.approx(29.0 + 2.4 + CAPTION_TAIL)
    with pytest.raises(ValueError):
        caption_pages(take, timings[:2])


def test_hyphenated_word_counts_as_one_caption_word(plan):
    take = _talking_take(plan, ["Twenty-five men came home"])
    timings = [_word(t, i * 0.5, i * 0.5 + 0.4) for i, t in enumerate(["twenty", "five", "men", "came", "home"])]
    pages = caption_pages(take, timings)
    assert [p.text for p in pages] == ["Twenty-five men came home"]
    assert pages[0].end == pytest.approx(29.0 + 2.4 + CAPTION_TAIL)


# --- the scenes stage ---

def test_wrong_word_is_rejected(plan):
    take = _talking_take(plan, ["Go on.", "I'm listening."])
    assert "expected" in check_take(take, "Go on. I am listening.", GO_ON)


def test_silent_take_with_speech_is_rejected(plan):
    assert check_take(opening_take(plan), "You could hit him from here.", []).startswith("unexpected speech")


def test_silent_take_with_no_speech_passes(plan):
    assert check_take(opening_take(plan), "", []) is None


def test_line_past_the_take_is_rejected(plan):
    take = _talking_take(plan, ["Go on.", "I'm listening."])
    late = [_word(w, 0.0, take.duration + 0.5) for w in ["Go", "on.", "I'm", "listening."]]
    assert check_take(take, "Go on. I'm listening.", late) == "line runs past the take"


def test_matching_take_passes(plan):
    take = _talking_take(plan, ["Go on.", "I'm listening."])
    assert check_take(take, "Go on. I'm listening.", GO_ON) is None


def _staged(tmp_path, monkeypatch, opening=None):
    """What `tih rehearse` builds before its paid stages: script and plan, approvals, locks."""
    source = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path / "src"))
    run_stage(source, "record")
    scope = source.paths.scope_dir
    (scope / "writers_room").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(scope / "record" / "script.yaml", scope / "writers_room" / "script.yaml")
    plan_path(source.paths).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example_plan_path("lincoln"), plan_path(source.paths))
    if opening is not None:
        data = yaml.safe_load(plan_path(source.paths).read_text(encoding="utf-8"))
        data["opening"] = opening.model_dump(mode="json")
        plan_path(source.paths).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    registry._write_done(source, "writers_room")
    registry._write_done(source, "director")
    monkeypatch.setenv("TIH_LOCKS_FILE", str(tmp_path / "dry" / "lincoln" / "locks.yaml"))
    return prepare_rehearsal(1, "lincoln", tmp_path / "dry", source=tmp_path / "src")


@pytest.fixture
def staged_ctx(tmp_path, monkeypatch):
    return _staged(tmp_path, monkeypatch)


def test_stage_sends_the_directors_opening(tmp_path, monkeypatch):
    ctx = _staged(tmp_path, monkeypatch, opening=OPENING)
    transport = ctx.gateway.transport
    asked = []
    submit = transport.submit

    def recording_submit(endpoint, arguments):
        asked.append((endpoint, arguments))
        return submit(endpoint, arguments)

    monkeypatch.setattr(transport, "submit", recording_submit)
    run_scenes(ctx, "seedance")
    prompt = next(str(a.get("prompt")) for e, a in asked if "text-to-video" in e)
    assert "The crowd seen from behind, torches held high." in prompt
    assert "Audio: crowd cheering, rain on umbrellas. No music score. No speech." in prompt
    assert load_bible("lincoln").places["L1R"].scene_look in prompt


def _scene_calls(ctx, take_id):
    return [r for r in ctx.gateway.ledger.current()
            if r.step == "scene" and r.shot == f"seedance-take{take_id}"]


@pytest.mark.parametrize("flow", ["seedance"])
def test_every_take_is_chosen_and_every_line_is_timed(staged_ctx, flow):
    run_scenes(staged_ctx, flow)
    plan = load_resolved(staged_ctx)
    takes = read_takes(staged_ctx, f"scenes_{flow}")
    assert set(takes) == {"0", *(t.id for t in scene_takes(plan, flow))}
    assert all(t.chosen for t in takes.values())
    assert all(len(t.takes) == 1 for t in takes.values())  # every dry take says its words at once
    spans, captions = read_timing(staged_ctx, flow)
    assert set(spans) == {s.id for s in plan.shots if s.line}
    assert " ".join(c.text for c in captions) == " ".join(s.line for s in plan.shots if s.line)


def test_seedance_flow_uploads_no_image_and_calls_only_text_to_video(staged_ctx, monkeypatch):
    transport = staged_ctx.gateway.transport
    asked, uploaded = [], []
    submit, upload = transport.submit, transport.upload

    def recording_submit(endpoint, arguments):
        asked.append(endpoint)
        return submit(endpoint, arguments)

    def recording_upload(path):
        uploaded.append(path)
        return upload(path)

    monkeypatch.setattr(transport, "submit", recording_submit)
    monkeypatch.setattr(transport, "upload", recording_upload)
    run_scenes(staged_ctx, "seedance")
    assert {e for e in asked if "video" in e} == {"bytedance/seedance-2.0/text-to-video"}
    assert uploaded and all(Path(path).suffix == ".wav" for path in uploaded)


def test_veo_flow_is_not_built_yet(staged_ctx):
    before = len(staged_ctx.gateway.ledger.current())
    with pytest.raises(NotImplementedError, match="veo flow is not built yet"):
        run_scenes(staged_ctx, "veo")
    assert len(staged_ctx.gateway.ledger.current()) == before


def _mishear(monkeypatch, transport, line, times):
    """Change one word of the first `times` transcripts that hear `line`."""
    wait = transport.wait
    misheard = []

    def mishearing_wait(endpoint, request_id):
        payload = wait(endpoint, request_id)
        if payload.get("text") == line and len(misheard) < times:
            misheard.append(request_id)
            wrong = line.replace("hit", "miss", 1)
            payload = {"text": wrong, "words": [
                {**w, "text": w["text"].replace("hit", "miss")} for w in payload["words"]]}
        return payload

    monkeypatch.setattr(transport, "wait", mishearing_wait)
    return misheard


def test_wrong_word_take_is_rejected_and_retaken(staged_ctx, monkeypatch):
    plan = load_resolved(staged_ctx)
    target = scene_takes(plan, "seedance")[0]
    assert "hit" in target.line
    misheard = _mishear(monkeypatch, staged_ctx.gateway.transport, target.line, times=1)
    run_scenes(staged_ctx, "seedance")
    assert len(misheard) == 1
    chosen = read_takes(staged_ctx, "scenes_seedance")[target.id]
    first, second = chosen.takes
    assert first.reason == word_difference(target.line, target.line.replace("hit", "miss", 1))
    assert second.reason is None and chosen.chosen_call_id == second.call_id
    calls = _scene_calls(staged_ctx, target.id)
    assert len(calls) == 2 and calls[0].call_id != calls[1].call_id
    assert [c.settings["take"] for c in calls] == [0, 1]


def test_scene_with_no_take_that_says_its_words_stops_the_stage(staged_ctx, monkeypatch):
    plan = load_resolved(staged_ctx)
    target = scene_takes(plan, "seedance")[0]
    _mishear(monkeypatch, staged_ctx.gateway.transport, target.line, times=99)
    with pytest.raises(NoUsableTake):
        run_scenes(staged_ctx, "seedance")
    assert len(_scene_calls(staged_ctx, target.id)) == 2


def test_failed_transcription_rejects_the_take(staged_ctx, monkeypatch):
    transport = staged_ctx.gateway.transport
    wait, failed = transport.wait, []

    def failing_wait(endpoint, request_id):
        if "speech-to-text" in endpoint and not failed:
            failed.append(request_id)
            raise ProviderError("scribe is down", status_code=503)
        return wait(endpoint, request_id)

    monkeypatch.setattr(transport, "wait", failing_wait)
    run_scenes(staged_ctx, "seedance")
    opening = read_takes(staged_ctx, "scenes_seedance")["0"]
    first, second = opening.takes
    assert first.reason == "transcription returned error"
    assert second.reason is None and opening.chosen_call_id == second.call_id


def _hearing(monkeypatch, transport, change):
    """Pass every transcript payload through `change` before the stage sees it."""
    wait = transport.wait

    def changed_wait(endpoint, request_id):
        payload = wait(endpoint, request_id)
        return change(payload) if "speech-to-text" in endpoint else payload

    monkeypatch.setattr(transport, "wait", changed_wait)


WIND = {"text": "(wind blowing)", "start": 0.1, "end": 0.9, "type": "audio_event"}


def test_sound_events_in_a_silent_take_are_not_speech(staged_ctx, monkeypatch):
    def wind(payload):
        if payload["text"]:
            return payload
        return {"text": "(wind blowing)", "words": [WIND]}

    _hearing(monkeypatch, staged_ctx.gateway.transport, wind)
    run_scenes(staged_ctx, "seedance")
    opening = read_takes(staged_ctx, "scenes_seedance")["0"]
    assert [t.reason for t in opening.takes] == [None]


def test_sound_events_around_a_correct_line_pass(staged_ctx, monkeypatch):
    def wind(payload):
        if not payload["text"]:
            return payload
        return {"text": "(wind blowing) " + payload["text"], "words": [WIND, *payload["words"]]}

    _hearing(monkeypatch, staged_ctx.gateway.transport, wind)
    run_scenes(staged_ctx, "seedance")
    takes = read_takes(staged_ctx, "scenes_seedance")
    assert all(len(t.takes) == 1 and t.takes[0].reason is None for t in takes.values())
    spans, captions = read_timing(staged_ctx, "seedance")
    assert "(wind" not in " ".join(c.text for c in captions)


def test_real_words_in_a_silent_take_are_still_speech(staged_ctx, monkeypatch):
    def talking(payload):
        if payload["text"]:
            return payload
        return {"text": "Hello there.", "words": [
            {"text": "Hello", "start": 0.2, "end": 0.5, "type": "word"},
            {"text": " ", "start": 0.5, "end": 0.6, "type": "spacing"},
            {"text": "there.", "start": 0.6, "end": 0.9, "type": "word"}]}

    _hearing(monkeypatch, staged_ctx.gateway.transport, talking)
    with pytest.raises(NoUsableTake, match=" 0: "):
        run_scenes(staged_ctx, "seedance")


def _first_transcript_changed(monkeypatch, transport, change):
    """Change only the first transcript payload (the opening's first take)."""
    seen = []

    def once(payload):
        if seen:
            return payload
        seen.append(payload)
        return change(payload)

    _hearing(monkeypatch, transport, once)


def test_text_without_any_word_list_is_rejected_not_silence(staged_ctx, monkeypatch):
    _first_transcript_changed(
        monkeypatch, staged_ctx.gateway.transport, lambda payload: {"text": "Hello there."})
    run_scenes(staged_ctx, "seedance")
    opening = read_takes(staged_ctx, "scenes_seedance")["0"]
    assert [t.reason for t in opening.takes] == ["transcript has no word timings", None]


def test_text_with_an_empty_word_list_is_rejected_not_silence(staged_ctx, monkeypatch):
    _first_transcript_changed(
        monkeypatch, staged_ctx.gateway.transport, lambda payload: {"text": "Hello there.", "words": []})
    run_scenes(staged_ctx, "seedance")
    opening = read_takes(staged_ctx, "scenes_seedance")["0"]
    assert [t.reason for t in opening.takes] == ["transcript has no word timings", None]


def test_audio_events_only_is_still_silence(staged_ctx, monkeypatch):
    def wind(payload):
        return {"text": "(wind blowing)", "words": [WIND]} if not payload["text"] else payload

    _hearing(monkeypatch, staged_ctx.gateway.transport, wind)
    run_scenes(staged_ctx, "seedance")
    assert [t.reason for t in read_takes(staged_ctx, "scenes_seedance")["0"].takes] == [None]


def test_clip_shorter_than_its_take_is_rejected(staged_ctx, monkeypatch):
    transport = staged_ctx.gateway.transport
    wait, shortened = transport.wait, []

    def short_wait(endpoint, request_id):
        payload = wait(endpoint, request_id)
        if "text-to-video" in endpoint and not shortened:  # the opening's first take
            shortened.append(request_id)
            transport.pending[payload["video"]["url"]]["seconds"] = 3.0
        return payload

    monkeypatch.setattr(transport, "wait", short_wait)
    run_scenes(staged_ctx, "seedance")
    opening = read_takes(staged_ctx, "scenes_seedance")["0"]
    assert [t.reason for t in opening.takes] == ["clip is shorter than the take", None]


def test_take_whose_words_cannot_be_timed_is_rejected(staged_ctx, monkeypatch):
    failed = []

    def failing_spans(take, timings):
        if take.line and not failed:
            failed.append(take.id)
            raise ValueError("script has 15 words, timings have 14")
        return line_spans(take, timings)

    monkeypatch.setattr(scenes_stage, "line_spans", failing_spans)
    run_scenes(staged_ctx, "seedance")
    chosen = read_takes(staged_ctx, "scenes_seedance")[failed[0]]
    assert [t.reason for t in chosen.takes] == [
        "words could not be timed: script has 15 words, timings have 14", None]
    spans, _ = read_timing(staged_ctx, "seedance")
    assert set(spans) == {s.id for s in load_resolved(staged_ctx).shots if s.line}


def test_running_the_stage_again_costs_nothing(staged_ctx):
    run_scenes(staged_ctx, "seedance")
    ledger = staged_ctx.gateway.ledger
    before = [(r.call_id, r.cost_usd) for r in ledger.current()]
    run_scenes(staged_ctx, "seedance")
    assert [(r.call_id, r.cost_usd) for r in ledger.current()] == before
