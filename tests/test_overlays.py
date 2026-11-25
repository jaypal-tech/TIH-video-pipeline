from pathlib import Path

import pytest
from test_production import _plan_with, episode_one  # noqa: F401  episode_one is a fixture

from tih.bible import Grade, OverlayStyle, load_bible
from tih.media.overlays import filters, layout, track
from tih.shots import resolve_plan
from tih.stages.cut import grade_filters

ROOT = Path(__file__).parents[1]
GOLD, WARM, INK = "#D3A445", "#EBE6DF", "#060605"


def _style(**extra):
    base = dict(font="r.ttf", font_bold="b.ttf", font_italic="i.ttf")
    return OverlayStyle(**{**base, **extra})


def _brand():
    return _style(label_font="label.ttf", text_color=WARM, accent_color=GOLD, card_color=INK,
                  brand_name="Today In History", brand_tagline="Every date has a story.",
                  time_kicker="On this day", label_tracking=1)


@pytest.fixture
def plan(episode_one):
    return resolve_plan(_plan_with(lambda p: None), episode_one, load_bible("lincoln"))


def _texts(plan, style, shot_id):
    shot = next(s for s in plan.shots if s.id == shot_id)
    return [t for t in layout(plan, style) if shot.start_s <= t.start < shot.start_s + shot.duration]


def test_track_spaces_capital_letters():
    assert track("Booth", 0) == "BOOTH"
    assert track("Ab C", 1) == "A\u200aB C"  # letters spaced, words keep one space


def test_default_style_stays_plain_white(plan):
    assert {t.color for t in layout(plan, _style())} == {"white"}


def test_name_card_name_is_gold_label_and_role_is_warm_text(plan):
    texts = _texts(plan, _brand(), "1.3")
    name = next(t for t in texts if "P O W" in t.text)
    role = next(t for t in texts if "Confederate veteran" in t.text)
    assert (name.color, name.font) == (GOLD, "label.ttf")
    assert (role.color, role.font) == (WARM, "r.ttf")


def test_captions_use_warm_text_color(plan):
    shot = next(s for s in plan.shots if s.id == "1.1")
    caption = next(t for t in _texts(plan, _brand(), "1.1") if t.text in shot.line or shot.line.startswith(t.text))
    assert caption.color == WARM


def test_time_super_gets_a_gold_kicker_above_it(plan):
    texts = _texts(plan, _brand(), "1.1")
    kicker = next(t for t in texts if t.text == track("On this day", 1))
    date = next(t for t in texts if "APRIL 11, 1865" in t.text)
    assert kicker.color == GOLD and kicker.y < date.y


def test_true_card_closes_on_brand_name_and_tagline(plan):
    end = plan.shots[-1]
    texts = [t for t in layout(plan, _brand()) if t.start >= end.start_s]
    name = next(t for t in texts if t.text == track("Today In History", 1))
    tagline = next(t for t in texts if t.text == "Every date has a story.")
    card = next(t for t in texts if "THREE DAYS LATER" in t.text)
    assert name.color == GOLD and card.y < name.y < tagline.y


def test_brand_name_in_corner_after_series_bug_until_true_card(plan):
    texts = layout(plan, _brand())
    corner = [t for t in texts if t.text == track("Today In History", 1) and t.y < 400]
    assert len(corner) == 1
    bug = next(t for t in texts if track("CONSPIRACY", 1) in t.text)
    card = next(t for t in texts if "THREE DAYS LATER" in t.text)
    assert corner[0].start == bug.end and corner[0].end == card.start


def test_grade_balance_pushes_toward_gold():
    assert grade_filters(Grade(eq="contrast=1.05", balance="rh=0.03:bh=-0.03")) == [
        "eq=contrast=1.05", "colorbalance=rh=0.03:bh=-0.03"]


def test_true_card_is_nearly_solid(plan, tmp_path):
    boxes = [f for f in filters(plan, _brand(), tmp_path) if f.startswith("drawbox")]
    assert boxes and all(f"color=0x{INK[1:]}@0.92" in b for b in boxes)


def test_small_labels_get_a_thinner_outline(plan, tmp_path):
    texts = [f for f in filters(plan, _brand(), tmp_path) if f.startswith("drawtext")]
    assert texts and all(("borderw=1" if "label.ttf" in f else "borderw=2") in f for f in texts)


def test_corner_brand_name_is_readable(plan):
    style = _brand()
    corner = next(t for t in layout(plan, style) if t.text == track("Today In History", 1) and t.y < 400)
    assert corner.size == style.tag_size - 2
