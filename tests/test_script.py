from pathlib import Path

import pytest
import yaml

from tih.paths import RunPaths
from tih.series import SeriesConfig
from tih.stages.record import run_record
from tih.stages.registry import RunContext
from tih.story.models import Beat, Canon, Clip, Episode, Line
from tih.story.script import ScriptChange, check_script, render_script, render_script_changes

ROOT = Path(__file__).parents[1]
LAST_SPEECH = "That is the last speech he will ever make."
CONFIG = SeriesConfig(title="Demo", episodes=30)


@pytest.fixture
def record(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    ctx = RunContext(paths=RunPaths("lincoln", None, root=tmp_path))
    run_record(ctx)
    stage = ctx.paths.stage_dir("record")
    episodes = yaml.safe_load((stage / "script.yaml").read_text("utf-8"))
    canon = Canon(**yaml.safe_load((stage / "canon.yaml").read_text("utf-8")))
    return Episode(**episodes[0]), canon


def beats(script: Episode) -> list[Beat]:
    return [beat for clip in script.clips for beat in clip.beats]


def rules(violations) -> list[str]:
    return [v.rule for v in violations]


def with_line(script: Episode, text: str) -> Beat:
    return next(b for b in beats(script) if b.line and b.line.text == text)


def test_draft_episode_one_passes(record):
    draft, canon = record
    assert check_script(draft, 1, canon, CONFIG, draft) == []


def test_wrong_episode_number_is_a_violation(record):
    draft, canon = record
    assert rules(check_script(draft, 2, canon, CONFIG, draft)) == ["number"]


@pytest.mark.parametrize("runtime", [59, 95])
def test_runtime_outside_range_is_a_violation(record, runtime):
    draft, canon = record
    script = draft.model_copy(deep=True)
    script.runtime_s = runtime
    assert "runtime" in rules(check_script(script, 1, canon, CONFIG, draft))


def test_beats_out_of_order_is_a_violation(record):
    draft, canon = record
    script = draft.model_copy(deep=True)
    beats(script)[2].start_s = 0.0
    beats(script)[1].start_s = 5.0
    assert rules(check_script(script, 1, canon, CONFIG, draft)) == ["timing"]


def test_changed_real_words_is_caught(record):
    draft, canon = record
    script = draft.model_copy(deep=True)
    beat = next(b for b in beats(script) if b.line and b.line.evidence == "real_words")
    beat.line.text = beat.line.text.replace("e", "a", 1)
    found = check_script(script, 1, canon, CONFIG, None)
    assert rules(found) == ["real_words"]
    assert beat.line.text in found[0].detail


def test_curly_apostrophe_is_not_the_same_text(record):
    draft, canon = record
    script = draft.model_copy(deep=True)
    beat = next(b for b in beats(script) if b.line and b.line.evidence == "reported")
    beat.line.text = beat.line.text.replace(".", "’.", 1) if "'" not in beat.line.text else beat.line.text.replace("'", "’")
    assert rules(check_script(script, 1, canon, CONFIG, None)) == ["reported"]


def test_dropping_a_protected_line_is_caught(record):
    draft, canon = record
    script = draft.model_copy(deep=True)
    for clip in script.clips:
        clip.beats = [b for b in clip.beats if not (b.line and b.line.text == LAST_SPEECH)]
    found = check_script(script, 1, canon, CONFIG, draft)
    assert rules(found) == ["protected_dropped"]
    assert LAST_SPEECH in found[0].detail


def test_dropping_a_repeated_protected_line_is_caught(record):
    draft, canon = record
    doubled = draft.model_copy(deep=True)
    source = with_line(doubled, LAST_SPEECH)
    copy = source.model_copy(deep=True)
    copy.id = "extra"
    copy.start_s = source.start_s
    clip = next(c for c in doubled.clips if source in c.beats)
    clip.beats.insert(clip.beats.index(source) + 1, copy)
    assert rules(check_script(draft, 1, canon, CONFIG, doubled)) == ["protected_dropped"]


def test_editing_a_dramatized_line_is_allowed(record):
    draft, canon = record
    script = draft.model_copy(deep=True)
    with_line(script, "You could hit him from here.").line.text = "One shot. From here."
    assert check_script(script, 1, canon, CONFIG, draft) == []


def test_dramatized_final_line_is_a_violation(record):
    draft, canon = record
    script = draft.model_copy(deep=True)
    beats(script)[-1].line = Line(speaker="BOOTH", text="Done.", delivery="face", evidence="dramatized")
    assert rules(check_script(script, 1, canon, CONFIG, draft)) == ["dramatized_close"]


def test_no_draft_skips_the_dropped_check(record):
    draft, canon = record
    script = draft.model_copy(deep=True)
    for clip in script.clips:
        clip.beats = [b for b in clip.beats if not (b.line and b.line.text == LAST_SPEECH)]
    assert check_script(script, 1, canon, CONFIG, None) == []


def test_render_script_shows_time_speaker_and_label(record):
    draft, _ = record
    page = render_script(draft)
    assert "0.0" in page
    assert "BOOTH: You could hit him from here." in page
    assert "REPORTED" in page


def test_render_changes_lists_before_after_reason():
    change = ScriptChange(beat="e1c1b1", before="Old line.", after="New line.", reason="Sharper hook.")
    page = render_script_changes("The Last Speech", [change], had_draft=True)
    assert all(text in page for text in ("e1c1b1", "Old line.", "New line.", "Sharper hook."))


def test_render_changes_without_draft_says_written_from_plan():
    page = render_script_changes("Episode 6", [], had_draft=False)
    assert "Written from the season plan; there was no draft." in page
