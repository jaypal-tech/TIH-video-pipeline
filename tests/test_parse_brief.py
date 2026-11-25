from pathlib import Path

import pytest
import yaml

from tih.paths import RunPaths
from tih.stages.record import run_record
from tih.stages.registry import RunContext
from tih.story.models import Delivery, Evidence, StoryFunction
from tih.story.parse_brief import extract_canon, parse_brief, parse_season_outline

ROOT = Path(__file__).parents[1]
BRIEF = (ROOT / "series" / "lincoln" / "source" / "brief.txt").read_text(encoding="utf-8")
SOURCES = yaml.safe_load((ROOT / "series" / "lincoln" / "sources.yaml").read_text("utf-8"))


@pytest.fixture(scope="module")
def episodes():
    return parse_brief(BRIEF)


@pytest.fixture(scope="module")
def canon(episodes):
    return extract_canon(episodes, SOURCES)


@pytest.fixture(scope="module")
def outline():
    return parse_season_outline(BRIEF)


def count(beats, evidence):
    return sum(1 for b in beats if b.line and b.line.evidence == evidence)


def beats_of(episode):
    return [b for c in episode.clips for b in c.beats]


@pytest.mark.parametrize(
    "n,runtime,beats,real,reported",
    [
        (1, 72, 16, 3, 2),
        (2, 68, 16, 0, 0),
        (3, 68, 17, 0, 2),
        (4, 68, 17, 2, 0),
        (5, 74, 21, 0, 1),
    ],
)
def test_episode_structure(episodes, n, runtime, beats, real, reported):
    ep = episodes[n - 1]
    all_beats = beats_of(ep)
    assert (ep.runtime_s, len(ep.clips), len(all_beats)) == (runtime, 5, beats)
    assert count(all_beats, Evidence.real_words) == real
    assert count(all_beats, Evidence.reported) == reported


def test_real_words_are_exact(canon):
    assert "We meet this evening, not in sorrow, but in gladness of heart." in canon.real_words
    assert "Don't wish to disturb you. Are you at home?" in canon.real_words


def test_nested_quote_is_preserved(canon):
    assert "'Don't wish to disturb you. Are you at home? J. Wilkes Booth.'" in canon.real_words


def test_slur_elision_preserved(canon):
    assert "That means … citizenship." in canon.reported


def test_every_quoted_line_appears_verbatim_in_brief(episodes):
    for ep in episodes:
        for beat in beats_of(ep):
            if beat.line:
                assert f'"{beat.line.text}"' in BRIEF


def test_five_true_cards_and_fifteen_name_cards(canon):
    assert (len(canon.true_cards), len(canon.name_cards)) == (5, 15)


def test_season_outline_has_30_episodes(outline):
    assert [e.number for e in outline] == list(range(1, 31))
    assert outline[11].title == "The Shot and the Knife"


def test_every_real_words_line_has_provenance(canon):
    assert all(
        canon.provenance[t].origin == "brief" and canon.provenance[t].source_id
        for t in canon.real_words
    )


def test_reported_and_true_cards_have_known_sources(canon):
    for text in canon.reported + canon.true_cards:
        assert canon.provenance[text].source_id in SOURCES["sources"]


def test_missing_source_is_rejected(episodes):
    with pytest.raises(ValueError, match="We meet this evening"):
        extract_canon(episodes, {"sources": {}, "claims": {}})


def test_overlay_words_are_removed_from_picture(episodes):
    for ep in episodes:
        for beat in beats_of(ep):
            for marker in ("NAME CARD", "TRUE card", "Overlay super", "Super,"):
                assert marker not in beat.picture


def test_overlays_keep_text_and_timing(episodes):
    beat = episodes[0].clips[3].beats[1]
    overlay = beat.overlays[0]
    assert (overlay.kind, overlay.text, overlay.start_s, overlay.duration_s) == (
        "super",
        "HE MEANS THE VOTE. FOR SOME BLACK MEN.",
        46,
        2,
    )


def test_true_card_runs_to_the_end(episodes):
    beat = episodes[0].clips[4].beats[-1]
    card = next(o for o in beat.overlays if o.kind == "true_card")
    assert (card.start_s, card.duration_s) == (65, 7)


def test_opening_supers(episodes):
    kinds = {o.kind: o.text for o in episodes[0].clips[0].beats[0].overlays}
    assert kinds["corner_bug"] == "LINCOLN: THE CONSPIRACY · 1"
    assert kinds["time_super"] == "WASHINGTON · APRIL 11, 1865"


def test_delivery_and_direction(episodes):
    line = episodes[3].clips[1].beats[0].line
    assert (line.speaker, line.delivery, line.direction) == (
        "BOOTH",
        Delivery.off,
        "under his breath as he writes",
    )


def test_untagged_line_is_dramatized(episodes):
    line = episodes[0].clips[0].beats[0].line
    assert (line.evidence, line.evidence_label) == (Evidence.dramatized, None)


def test_clip_timing_and_next_opening(episodes):
    clip = episodes[4].clips[4]
    assert (clip.number, clip.start_s, clip.end_s) == (5, 54, 74)
    assert episodes[0].next_opens.startswith("three days later")


def test_first_beat_is_hook_and_last_is_cliff(episodes):
    for ep in episodes:
        beats = beats_of(ep)
        assert beats[0].function == StoryFunction.hook
        assert beats[-1].function == StoryFunction.cliff


def test_unreadable_script_row_is_rejected():
    broken = BRIEF.replace('BOOTH (face, low): "You could', 'BOOTH face: You could', 1)
    with pytest.raises(ValueError, match="You could"):
        parse_brief(broken)


def test_record_stage_writes_outputs(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    ctx = RunContext(paths=RunPaths("lincoln", 1, root=tmp_path))
    run_record(ctx)
    stage = ctx.paths.stage_dir("record")
    script = yaml.safe_load((stage / "script.yaml").read_text("utf-8"))
    canon = yaml.safe_load((stage / "canon.yaml").read_text("utf-8"))
    outline = yaml.safe_load((stage / "season_outline.yaml").read_text("utf-8"))
    assert script["number"] == 1
    assert script["clips"][1]["beats"][1]["line"]["source_id"] == "basler_last_address"
    assert "That means … citizenship." in canon["reported"]
    assert len(outline) == 30


def test_extra_real_words_join_the_canon_with_their_source(canon):
    sentence = "It is also unsatisfactory to some that the elective franchise is not given to the colored man."
    assert sentence in canon.real_words
    assert canon.provenance[sentence].source_id == "basler_last_address"
