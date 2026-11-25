"""Production stages: shot plan checks, gates, locks, takes and the cut.

No paid calls: generation runs through DryRunTransport, which makes
placeholder media with ffmpeg and records every call in a temporary ledger.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tih.bible import NotLocked, load_bible
from tih.budget import BudgetGuard
from tih.cli import app, prepare_rehearsal, rehearse_episode
from tih.gates import ArtifactChanged, GateNotApproved, approve, gate_artifacts, require_approved
from tih.ledger import Ledger
from tih.media import ffmpeg
from tih.media.ffmpeg import duration, validate_master
from tih.media.overlays import filters
from tih.paths import RunPaths
from tih.production import load_pipeline, load_resolved, read_takes, render_arguments
from tih.providers.dry import DryRunTransport
from tih.providers.fal import GatewayContext, generate
from tih.assets import ShotRef
from tih.scenes import FLOWS, OPENING_SECONDS, scene_takes, shifted
from tih.series import load_series
from tih.shots import PlanError, example_plan_path, load_plan, plan_path, resolve_plan
from tih.stages.performance import model_duration, source_seconds
from tih.stages import registry
from tih.stages import cut
from tih.stages.cut import played_seconds, without_title_super
from tih.stages.registry import RunContext, run_stage
from tih.stages.voice import line_pieces, slice_for_shot, speech_text, word_difference
from tih.story.models import Episode

ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def in_repo(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("TIH_LOCKS_FILE", raising=False)


@pytest.fixture
def episode_one(tmp_path):
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_stage(ctx, "record")
    script = ctx.paths.scope_dir / "record" / "script.yaml"
    return Episode.model_validate(yaml.safe_load(script.read_text(encoding="utf-8")))


def _staged(ctx):
    """Stand in for the Writer and the Director: the draft script and the example plan."""
    scope = ctx.paths.scope_dir
    (scope / "writers_room").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(scope / "record" / "script.yaml", scope / "writers_room" / "script.yaml")
    plan_path(ctx.paths).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example_plan_path("lincoln"), plan_path(ctx.paths))
    registry._write_done(ctx, "writers_room")
    registry._write_done(ctx, "director")


def _plan_with(change):
    plan = load_plan(example_plan_path("lincoln"))
    change(plan)
    return plan


def test_episode_one_plan_matches_script(episode_one):
    plan = resolve_plan(load_plan(example_plan_path("lincoln")), episode_one, load_bible("lincoln"))
    assert (len(plan.shots), plan.runtime_s) == (29, 73.0)
    assert [s.id for s in plan.shots if s.needs_lipsync] == [
        "1.1", "1.3", "1.4", "2.3", "3.2", "3.3", "4.1", "4.4", "5.1", "5.4",
    ]
    assert {s.speaker for s in plan.shots if s.line} == {"booth", "powell", "lincoln"}


def test_every_prompt_carries_locked_look_text(episode_one):
    bible = load_bible("lincoln")
    plan = resolve_plan(load_plan(example_plan_path("lincoln")), episode_one, bible)
    for shot in plan.shots:
        assert bible.style.look in shot.keyframe_prompt
        for character in shot.cast:
            assert bible.characters[character].look in shot.keyframe_prompt


def test_changed_real_words_punctuation_is_refused(episode_one):
    def edit(plan):
        plan.shots[8].line = "We meet this evening, not in sorrow;"

    with pytest.raises(PlanError, match="exact part of the script"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


def test_dropped_line_piece_is_refused(episode_one):
    def edit(plan):
        plan.shots[9].line = None
        plan.shots[9].delivery = None

    with pytest.raises(PlanError, match="e1c2b2"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


def test_overlay_text_must_come_from_script(episode_one):
    def edit(plan):
        plan.shots[-1].overlays[0].text = "THREE DAYS LATER, HE PICKED HIS TARGETS."

    with pytest.raises(PlanError, match="not in the script"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


def test_payoff_guard_blocks_forbidden_words(episode_one):
    def edit(plan):
        plan.shots[13].motion = "He draws a knife from his coat"

    with pytest.raises(PlanError, match="knife"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


def test_character_in_prompt_must_be_cast(episode_one):
    def edit(plan):
        plan.shots[1].keyframe += ", [BOOTH] in the corner"

    with pytest.raises(PlanError, match="not in cast"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


def test_gate_blocks_until_approved_and_after_edit(tmp_path, monkeypatch):
    locks = tmp_path / "locks.yaml"
    locks.write_text("characters: {}\n")
    monkeypatch.setenv("TIH_LOCKS_FILE", str(locks))
    paths = RunPaths("lincoln", 1, root=tmp_path)
    ctx = RunContext(paths=paths)
    run_stage(ctx, "record")
    _staged(ctx)
    with pytest.raises(GateNotApproved):
        require_approved(paths, "look")
    approve(paths, "look")
    require_approved(paths, "look")
    locks.write_text("characters: {booth: {voice_id: other}}\n")
    with pytest.raises(ArtifactChanged):
        require_approved(paths, "look")


def test_render_arguments():
    template = {"prompt": "{prompt}", "urls": "{image_urls}", "seed": "{seed}",
                "nested": {"voice_id": "{voice_id}"}, "fixed": 1}
    values = {"prompt": "a", "image_urls": ["u1", "u2"], "seed": None, "voice_id": "v"}
    assert render_arguments(template, values) == {
        "prompt": "a", "urls": ["u1", "u2"], "nested": {"voice_id": "v"}, "fixed": 1,
    }
    with pytest.raises(KeyError):
        render_arguments({"text": "say {text} now"}, {})


def test_word_check_ignores_punctuation_but_not_words():
    assert word_difference("That means … citizenship.", "That means citizenship") is None
    assert word_difference("Don't wish to disturb you.", "Don’t wish to disturb you") is None
    assert word_difference("in gladness of heart.", "in the gladness of heart") is not None


def test_ellipsis_becomes_a_pause_not_a_word():
    assert speech_text("That means … citizenship.", "<#0.6#>") == "That means <#0.6#> citizenship."


def test_model_duration_covers_slow_motion_and_freeze():
    assert source_seconds(1.5, 0.5, None) == 0.75
    assert source_seconds(7.0, 1.0, 4.0) == 4.0
    assert model_duration(4.0, (3, 15)) == 4
    assert model_duration(7.0, (3, 15)) == 7


def test_motion_seconds_clamped():
    assert model_duration(1.5, (3, 15)) == 3
    assert model_duration(4.2, (3, 15)) == 5
    assert model_duration(20.0, (3, 15)) == 15


def test_pipeline_arguments_match_schemas():
    schemas = yaml.safe_load((ROOT / "series" / "lincoln" / "schemas.yaml").read_text(encoding="utf-8"))
    pipeline = yaml.safe_load((ROOT / "series" / "lincoln" / "pipeline.yaml").read_text(encoding="utf-8"))
    problems = []
    for name, step in pipeline["steps"].items():
        for endpoint, arguments in [(step["endpoint"], step["arguments"])] + [
            (f["endpoint"], f["arguments"]) for f in step.get("fallbacks", [])
        ]:
            accepted = set(schemas.get(endpoint, []))
            if not accepted:
                problems.append(f"{name}: no schema for {endpoint}")
                continue
            for key, value in arguments.items():
                if key not in accepted:
                    problems.append(f"{name}: {endpoint} does not accept {key}")
                if isinstance(value, dict):
                    problems += [f"{name}: {endpoint} does not accept {key}.{sub}"
                                 for sub in value if f"{key}.{sub}" not in accepted]
    assert problems == []


def test_series_budget_comes_from_config():
    budget = load_series("lincoln").budget
    assert (budget.warn, budget.stop, budget.proving_cap) == (1500.0, 3000.0, 100.0)


def test_fallback_uses_its_own_arguments(tmp_path):
    class Refusing(DryRunTransport):
        def __init__(self, workdir):
            super().__init__(workdir)
            self.sent = []

        def submit(self, endpoint, arguments):
            self.sent.append((endpoint, arguments))
            if endpoint == "first/model":
                from tih.providers.fal import ProviderRefused
                raise ProviderRefused("content policy", at_submit=True)
            return super().submit(endpoint, arguments)

    ledger = Ledger(tmp_path / "calls.jsonl")
    transport = Refusing(tmp_path / "dry")
    ctx = GatewayContext(RunPaths("lincoln", 1, root=tmp_path), ledger, BudgetGuard(ledger),
                         transport, tmp_path / "none.yaml")
    result = generate(ctx, "keyframe", "first/model", {"prompt": "p", "aspect_ratio": "9:16"},
                      ShotRef(1, 1, "1.1"), 0.1,
                      fallbacks=(("second/model", {"prompt": "p", "image_size": "portrait"}),))
    assert result.status == "ok" and result.endpoint == "second/model"
    assert transport.sent[1] == ("second/model", {"prompt": "p", "image_size": "portrait"})
    assert [r.status for r in ledger.current()] == ["blocked", "ok"]
    assert result.output_paths[0].parent.name == "keyframe"


def test_keyframes_refuse_without_locked_look(tmp_path, monkeypatch):
    monkeypatch.setenv("TIH_LOCKS_FILE", str(tmp_path / "locks.yaml"))
    paths = RunPaths("lincoln", 1, root=tmp_path)
    ledger = Ledger(tmp_path / "lincoln" / "calls.jsonl")
    gateway = GatewayContext(paths, ledger, BudgetGuard(ledger), DryRunTransport(tmp_path / "dry"),
                             ROOT / "series" / "lincoln" / "models.yaml")
    ctx = RunContext(paths=paths, gateway=gateway)
    run_stage(ctx, "record")
    _staged(ctx)
    run_stage(ctx, "shotplan")
    with pytest.raises(GateNotApproved):
        run_stage(ctx, "keyframes")
    approve(paths, "script")
    (tmp_path / "locks.yaml").write_text("characters: {}\n")
    approve(paths, "look")
    with pytest.raises(NotLocked, match="booth"):
        run_stage(ctx, "keyframes")
    assert ledger.current() == []


def test_validate_master_rejects_wrong_format(tmp_path):
    clip = tmp_path / "small.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=640x360:d=2",
                    "-r", "25", str(clip)], check=True)
    problems = validate_master(clip)
    assert any("1080x1920" in p for p in problems)
    assert any("frame rate" in p for p in problems)
    assert any("runtime" in p for p in problems)
    assert "no audio stream" in problems


def test_rehearsal_needs_director(tmp_path):
    with pytest.raises(FileNotFoundError, match="--stage director"):
        rehearse_episode(1, "lincoln", tmp_path / "dry", source=tmp_path / "empty")


def _rehearsed(tmp_path, flow):
    source = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path / "src"))
    run_stage(source, "record")
    _staged(source)
    return rehearse_episode(1, "lincoln", tmp_path / "dry", source=tmp_path / "src", flow=flow)


@pytest.mark.slow
@pytest.mark.parametrize("flow", FLOWS[:1])  # the veo flow is not built yet
def test_rehearsal_makes_a_valid_master_and_reruns_free(tmp_path, flow):
    ctx = _rehearsed(tmp_path, flow)
    final = ctx.paths.scope_dir / f"cut_{flow}" / "final.mp4"
    assert validate_master(final) == []
    check = json.loads((ctx.paths.scope_dir / f"cut_{flow}" / "master_check.json").read_text())
    assert abs(check["dialogue_lufs"] + 16) <= 1

    ledger = Ledger(tmp_path / "dry" / "lincoln" / "calls.jsonl")
    calls = ledger.current()
    assert all(r.status == "ok" for r in calls)
    assert {r.step for r in calls} >= {"look", "voice_design", "scene", "transcribe", "music", "sfx"}
    assert not {r.step for r in calls} & {"keyframe", "video", "voice", "lipsync"}
    scenes = read_takes(ctx, f"scenes_{flow}")
    chosen = {t.chosen_call_id for t in scenes.values()}
    for record in calls:
        if record.step == "scene":
            assert record.kept is (record.call_id in chosen)

    count = len(calls)
    for stage in (f"scenes_{flow}", "sound"):
        run_stage(ctx, stage, force=True)
    assert len(ledger.current()) == count

    approved = CliRunner().invoke(app, ["approve", "1", "picture", "--series", "lincoln",
                                        "--root", str(tmp_path / "dry")])
    assert approved.exit_code == 0, approved.output


@pytest.mark.slow
@pytest.mark.parametrize("flow", FLOWS[:1])  # the veo flow is not built yet
def test_rehearsal_cut_opens_with_the_title_and_one_part_per_take(tmp_path, flow):
    ctx = _rehearsed(tmp_path, flow)
    plan = load_resolved(ctx)
    edl = yaml.safe_load((ctx.paths.stage_dir(f"cut_{flow}") / "edl.yaml").read_text())
    assert [row["take"] for row in edl] == ["0", *(t.id for t in scene_takes(plan, flow))]
    assert edl[0]["record_in"] == 0.0 and edl[1]["record_in"] == OPENING_SECONDS
    final = ctx.paths.stage_dir(f"cut_{flow}") / "final.mp4"
    assert ffmpeg.duration(final) == pytest.approx(plan.runtime_s + OPENING_SECONDS, abs=0.2)


def test_episode_command_needs_a_known_flow():
    result = CliRunner().invoke(app, ["episode", "1", "--series", "lincoln", "--flow", "kling"])
    assert result.exit_code != 0


def test_episode_command_refuses_the_veo_flow_for_now(tmp_path):
    result = CliRunner().invoke(app, ["episode", "1", "--series", "lincoln", "--flow", "veo",
                                      "--root", str(tmp_path)])
    assert result.exit_code != 0
    assert "the veo flow is not built yet" in result.output
    assert not (tmp_path / "lincoln").exists()


def test_take_sound_plays_only_as_long_as_its_picture_moves(episode_one):
    plan = resolve_plan(load_plan(example_plan_path("lincoln")), episode_one, load_bible("lincoln"))
    takes = {t.id: t for t in scene_takes(plan, "seedance")}
    assert played_seconds(takes["1.1"]) == takes["1.1"].duration  # several shots: the whole take
    assert played_seconds(takes["5.2"]) == 0.75  # shot 5.3, 1.5 s at half speed
    assert played_seconds(takes["5.4"]) == 4.0  # shot 5.6 freezes at 4.0 s


def test_no_shot_text_is_drawn_during_the_opening(episode_one, tmp_path):
    bible = load_bible("lincoln")
    plan = resolve_plan(load_plan(example_plan_path("lincoln")), episode_one, bible)
    chain = filters(shifted(plan, OPENING_SECONDS), bible.overlays, tmp_path / "text")
    starts = [float(m) for f in chain for m in re.findall(r"between\(t,([0-9.]+),", f)]
    assert starts and min(starts) >= OPENING_SECONDS


def test_shotplan_reads_writer_script(tmp_path):
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_stage(ctx, "record")
    _staged(ctx)
    script = ctx.paths.scope_dir / "writers_room" / "script.yaml"
    script.write_text(script.read_text(encoding="utf-8").replace("Not in this crowd.", "Not here."), encoding="utf-8")
    with pytest.raises(PlanError, match="e1c1b2"):
        run_stage(ctx, "shotplan")


def test_look_lock_covers_director_plan(tmp_path, monkeypatch):
    locks = tmp_path / "locks.yaml"
    locks.write_text("characters: {}\n")
    monkeypatch.setenv("TIH_LOCKS_FILE", str(locks))
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_stage(ctx, "record")
    _staged(ctx)
    approve(ctx.paths, "look")
    require_approved(ctx.paths, "look")
    plan = plan_path(ctx.paths)
    plan.write_text(plan.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    with pytest.raises(ArtifactChanged, match="director/shots.yaml"):
        require_approved(ctx.paths, "look")


def test_plan_runtime_uses_series_range(episode_one):
    with pytest.raises(PlanError, match="outside 80-90"):
        resolve_plan(load_plan(example_plan_path("lincoln")), episode_one, load_bible("lincoln"), runtime_s=(80, 90))


def test_paid_stage_refuses_stale_shotplan(tmp_path):
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_stage(ctx, "record")
    _staged(ctx)
    run_stage(ctx, "shotplan")
    load_resolved(ctx)
    plan = plan_path(ctx.paths)
    data = yaml.safe_load(plan.read_text(encoding="utf-8"))
    data["shots"][0]["motion"] += ", rain heavier"  # any change; timing stays inside its clip
    plan.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(PlanError, match="re-run shotplan"):
        load_resolved(ctx)


def test_plan_episode_must_match_script(episode_one):
    def edit(plan):
        plan.episode = 2

    with pytest.raises(PlanError, match="episode 2"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


def test_every_beat_needs_a_shot(episode_one):
    def edit(plan):
        plan.shots = [s for s in plan.shots if s.beat != "e1c2b3"]

    with pytest.raises(PlanError, match="e1c2b3 has no shot"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


def test_beats_in_script_order(episode_one):
    def edit(plan):
        plan.shots[0], plan.shots[2] = plan.shots[2], plan.shots[0]

    with pytest.raises(PlanError, match="out of order"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


def test_overlay_must_sit_on_its_beat(episode_one):
    def edit(plan):
        plan.shots[2].overlays.append(plan.shots[0].overlays.pop(0))

    with pytest.raises(PlanError, match="not on beat"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


def test_sound_needs_look_lock(tmp_path, monkeypatch):
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
    with pytest.raises(GateNotApproved):
        run_stage(ctx, "sound")
    assert ledger.current() == []


@pytest.mark.slow
def test_second_rehearsal_uses_new_plan(tmp_path):
    source = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path / "src"))
    run_stage(source, "record")
    _staged(source)
    root = tmp_path / "dry"
    first = rehearse_episode(1, "lincoln", root, source=tmp_path / "src")
    takes = scene_takes(load_resolved(first), "seedance")
    plan = plan_path(source.paths)
    data = yaml.safe_load(plan.read_text(encoding="utf-8"))
    # move half a second from shot 1.5 (the second take's first) to shot 1.4 (the first take's last);
    # the clip (and its ambience bed) keeps its length
    assert (takes[0].shots[-1].id, takes[1].shots[0].id) == (data["shots"][3]["id"], data["shots"][4]["id"])
    data["shots"][3]["duration"] += 0.5
    data["shots"][4]["duration"] -= 0.5
    plan.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    ctx = rehearse_episode(1, "lincoln", root, source=tmp_path / "src")
    edl = yaml.safe_load((ctx.paths.scope_dir / "cut_seedance" / "edl.yaml").read_text(encoding="utf-8"))
    assert edl[1]["duration"] == takes[0].duration + 0.5
    assert edl[2]["record_in"] == OPENING_SECONDS + takes[0].duration + 0.5


def test_slice_for_shot_boundaries():
    spoken = "I would myself prefer that it were now".split()
    timed = [{"text": w, "start": i * 0.5, "end": i * 0.5 + 0.4, "type": "word"} for i, w in enumerate(spoken)]
    timed.insert(1, {"text": " ", "start": 0.4, "end": 0.5, "type": "spacing"})
    pieces = ["I would myself prefer", "that it were now"]
    # the first piece keeps the take's lead-in, the last keeps its tail: lips and soundtrack share one clock
    assert slice_for_shot(timed, pieces, 0) == (0.0, 2.0)
    assert slice_for_shot(timed, pieces, 1) == (2.0, None)


def test_slice_counts_hyphenated_words_like_the_script():
    timed = [{"text": w, "start": i * 1.0, "end": i * 1.0 + 0.8, "type": "word"}
             for i, w in enumerate(["twenty-five", "men", "came", "tonight"])]
    assert slice_for_shot(timed, ["twenty-five men", "came tonight"], 1) == (2.0, None)


def test_lipsync_pads_with_silence():
    pipeline = yaml.safe_load((ROOT / "series" / "lincoln" / "pipeline.yaml").read_text(encoding="utf-8"))
    assert pipeline["steps"]["lipsync"]["arguments"]["sync_mode"] == "silence"


def test_lipsync_shot_needs_one_face(episode_one):
    def edit(plan):
        speaking = next(s for s in plan.shots if s.id == "1.1")
        speaking.cast = ["booth", "powell"]

    with pytest.raises(PlanError, match="lip-sync shot needs exactly one face"):
        resolve_plan(_plan_with(edit), episode_one, load_bible("lincoln"))


@pytest.mark.slow
def test_line_voiced_once_and_sliced_per_shot(tmp_path):
    source = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path / "src"))
    run_stage(source, "record")
    _staged(source)
    root = tmp_path / "dry"
    ctx = prepare_rehearsal(1, "lincoln", root, source=tmp_path / "src")
    for stage in ("keyframes", "motion", "voice", "lipsync"):  # the old stages, no longer in the rehearsal
        run_stage(ctx, stage, force=True)
    plan = load_resolved(ctx)
    beats_with_lines = {s.beat for s in plan.shots if s.line}
    calls = [r for r in Ledger(root / "lincoln" / "calls.jsonl").current() if r.step == "voice"]
    assert len(calls) == len(beats_with_lines)
    voice = read_takes(ctx, "voice")
    assert set(voice) == beats_with_lines
    shot = next(s for s in plan.shots if s.id == "4.1")
    whole = duration(Path(voice[shot.beat].chosen))
    piece = duration(ctx.paths.scope_dir / "lipsync" / "audio" / "4.1.wav")
    assert 0 < piece < whole
    placed = {path.stem: at for path, at in line_pieces(ctx, plan, tmp_path / "pieces")}
    start = {s.id: s.start_s for s in plan.shots}
    assert placed["4.2"] == start["4.2"]  # later pieces sit exactly on their own shot
    assert 0 <= start["4.1"] - placed["4.1"] <= 0.25  # a line's first piece may lead its shot (J-cut)


def test_every_pipeline_step_is_used_by_a_stage():
    pipeline = yaml.safe_load((ROOT / "series" / "lincoln" / "pipeline.yaml").read_text(encoding="utf-8"))
    code = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "tih").rglob("*.py"))
    unused = [name for name in pipeline["steps"] if f'"{name}"' not in code]
    assert unused == []


def test_voices_are_designed_and_spoken_on_elevenlabs_directly():
    # fal's text filter refused "You could hit him from here." on every fal speech model (2026-11-25)
    steps = load_pipeline("lincoln").steps
    assert steps["voice_design"].endpoint == "elevenlabs-api/voice-design"
    assert steps["speech"].endpoint == "elevenlabs-api/text-to-speech"
    assert steps["transcribe"].endpoint.startswith("fal-ai/")


def test_extract_audio_is_repeatable(tmp_path):
    video = tmp_path / "tone.mp4"
    ffmpeg.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=s=160x90:d=1",
                "-f", "lavfi", "-i", "sine=frequency=300:duration=1", "-shortest",
                "-c:v", "libx264", "-c:a", "aac", str(video)])
    first = ffmpeg.extract_audio(video, tmp_path / "a.wav").read_bytes()
    second = ffmpeg.extract_audio(video, tmp_path / "b.wav").read_bytes()
    assert first == second and len(first) > 1000


def test_dry_scene_video_has_sound_and_a_known_transcript(tmp_path):
    transport = DryRunTransport(tmp_path / "dry")
    prompt = ('Shot 1 (0-3s, MCU, 85mm lens): a man waits. He says exactly: "Go on." '
              'Shot 2 (3-5s, CU, 85mm lens): he looks up. He says exactly: "I\'m listening."')
    endpoint = "bytedance/seedance-2.0/text-to-video"
    url = transport.wait(endpoint, transport.submit(endpoint, {"prompt": prompt, "duration": "5"}))["video"]["url"]
    video = tmp_path / "scene.mp4"
    transport.download(url, video)
    assert abs(duration(video) - 5.0) < 0.2
    audio = ffmpeg.extract_audio(video, tmp_path / "scene.wav")
    scribe = "fal-ai/elevenlabs/speech-to-text/scribe-v2"
    heard = transport.wait(scribe, transport.submit(scribe, {"audio_url": transport.upload(audio)}))
    assert heard["text"] == "Go on. I'm listening."
    assert [w["text"] for w in heard["words"]] == ["Go", "on.", "I'm", "listening."]
    assert heard["words"][-1]["end"] < 5.0


def test_dry_scene_words_are_spread_inside_a_short_take(tmp_path):
    transport = DryRunTransport(tmp_path / "dry")
    words = " ".join(f"w{i}" for i in range(20))
    endpoint = "bytedance/seedance-2.0/text-to-video"
    prompt = f'Shot 1 (0-4s, CU): he says exactly: "{words}"'
    url = transport.wait(endpoint, transport.submit(endpoint, {"prompt": prompt, "duration": "4"}))["video"]["url"]
    transport.download(url, tmp_path / "scene.mp4")
    audio = ffmpeg.extract_audio(tmp_path / "scene.mp4", tmp_path / "scene.wav")
    scribe = "fal-ai/elevenlabs/speech-to-text/scribe-v2"
    heard = transport.wait(scribe, transport.submit(scribe, {"audio_url": transport.upload(audio)}))
    assert len(heard["words"]) == 20 and heard["words"][-1]["end"] <= 4.0


def test_dry_scenes_of_equal_length_with_different_lines_sound_different(tmp_path):
    transport = DryRunTransport(tmp_path / "dry")
    endpoint = "bytedance/seedance-2.0/text-to-video"
    scribe = "fal-ai/elevenlabs/speech-to-text/scribe-v2"
    heard, sounds = [], []
    for name, line in (("a", "Go on."), ("b", "Not in this crowd.")):
        prompt = f'Shot 1 (0-6s, CU): he says exactly: "{line}"'
        url = transport.wait(endpoint, transport.submit(endpoint, {"prompt": prompt, "duration": "6"}))["video"]["url"]
        transport.download(url, tmp_path / f"{name}.mp4")
        audio = ffmpeg.extract_audio(tmp_path / f"{name}.mp4", tmp_path / f"{name}.wav")
        sounds.append(audio.read_bytes())
        heard.append(transport.wait(scribe, transport.submit(scribe, {"audio_url": transport.upload(audio)}))["text"])
    assert sounds[0] != sounds[1]
    assert heard == ["Go on.", "Not in this crowd."]


def test_picture_gate_covers_the_seedance_master(tmp_path):
    paths = RunPaths("lincoln", 1, root=tmp_path)
    assert gate_artifacts(paths, "picture") == [paths.scope_dir / "cut_seedance" / "final.mp4"]


def test_pick_hint_reruns_the_flow_from_a_scene_take(tmp_path):
    result = CliRunner().invoke(app, ["pick", "1", "scenes_seedance", "2.1", "1", "--series", "lincoln",
                                      "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "tih episode 1 --series lincoln --flow seedance --from scenes_seedance --force" in result.output
    picks = yaml.safe_load((RunPaths("lincoln", 1, root=tmp_path).scope_dir / "picks.yaml").read_text())
    assert picks == {"scenes_seedance": {"2.1": 1}}


def test_pick_hint_for_a_stage_outside_the_flows_reruns_that_stage(tmp_path):
    result = CliRunner().invoke(app, ["pick", "1", "keyframes", "2.3", "1", "--series", "lincoln",
                                      "--root", str(tmp_path)])
    assert "tih run 1 --stage keyframes --series lincoln --force" in result.output


def test_the_title_time_super_is_drawn_only_in_the_opening(episode_one):
    plan = resolve_plan(load_plan(example_plan_path("lincoln")), episode_one, load_bible("lincoln"))
    kinds = lambda p: [(s.id, o.kind) for s in p.shots for o in s.overlays]
    dropped = without_title_super(plan)
    assert ("1.1", "time_super") in kinds(plan)
    assert kinds(dropped) == [k for k in kinds(plan) if k != ("1.1", "time_super")]


def test_take_sound_fades_only_briefly_at_its_edges(monkeypatch, tmp_path):
    assert cut.TAKE_FADE == 0.05
    seen = []
    monkeypatch.setattr(cut.ffmpeg, "run", lambda args, **_: seen.append(args) or "")
    cut._mix_inputs([(tmp_path / "take.wav", 5.0, 9.5, 0.0)], 20.0, tmp_path / "d.wav", fade=cut.TAKE_FADE)
    graph = seen[0][seen[0].index("-filter_complex") + 1]
    assert "afade=t=in:d=0.050" in graph and "afade=t=out:st=9.450:d=0.050" in graph
