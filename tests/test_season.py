from pathlib import Path

import pytest

from tih.series import KnowledgeRule, SeriesConfig
from tih.story.parse_brief import parse_season_outline
from tih.story.season import (
    EpisodePlan,
    HeldMoment,
    KnowledgeEvent,
    SeasonPlan,
    Setup,
    check_season,
    render_season_plan,
)

ROOT = Path(__file__).parents[1]
OUTLINE = parse_season_outline(
    (ROOT / "series" / "lincoln" / "source" / "brief.txt").read_text(encoding="utf-8")
)
CONFIG = SeriesConfig(
    title="Test Series",
    episodes=30,
    paywalls={5: "early", 11: "late"},
    held_back={"the shot": 12, "the knife": 12, "Lincoln's death": 14},
    knowledge_rules=[
        KnowledgeRule(who="Atzerodt", topic="the killing plan", not_before=5)
    ],
)


def valid_plan() -> SeasonPlan:
    walls = {5: "early", 11: "late"}
    reveals = {12: ["the shot", "the knife"], 14: ["Lincoln's death"]}
    return SeasonPlan(
        promise="The road to the night nobody remembers.",
        phases=[],
        episodes=[
            EpisodePlan(
                number=e.number,
                title=e.title,
                job="Move the countdown.",
                hook="A hook.",
                cliff="A true cliff.",
                reveals=reveals.get(e.number, []),
                wall=walls.get(e.number, "none"),
            )
            for e in OUTLINE
        ],
        arcs=[],
        setups=[
            Setup(id="card", planted_ep=4, payoff_ep=8, description="Card", rule="Keep it")
        ],
        knowledge=[
            KnowledgeEvent(
                episode=5,
                who="George Atzerodt",
                topic="the killing plan",
                learns="Booth expects him to kill Johnson.",
                evidence="reported",
            )
        ],
        held_back=[
            HeldMoment(name="the shot", earliest_ep=12),
            HeldMoment(name="the knife", earliest_ep=12),
            HeldMoment(name="Lincoln's death", earliest_ep=14),
        ],
        motifs=[],
        open_questions=["Who was the card for?"],
    )


def rules(plan):
    return {v.rule for v in check_season(plan, OUTLINE, CONFIG)}


def test_valid_plan_passes():
    assert check_season(valid_plan(), OUTLINE, CONFIG) == []


def test_season_covers_30_episodes_matching_outline():
    plan = valid_plan()
    plan.episodes.pop()
    assert "outline_mismatch" in rules(plan)


def test_changed_title_fails():
    plan = valid_plan()
    plan.episodes[0].title = "A Better Title"
    assert "outline_mismatch" in rules(plan)


def test_walls_after_ep5_and_ep11():
    plan = valid_plan()
    plan.episodes[4].wall = "none"
    assert "wall" in rules(plan)


def test_payoff_cannot_precede_setup():
    plan = valid_plan()
    plan.setups[0].payoff_ep = 3
    assert "payoff_before_setup" in rules(plan)


def test_held_back_moment_cannot_appear_early():
    plan = valid_plan()
    plan.episodes[9].reveals = ["the shot"]
    assert "held_back_early" in rules(plan)


def test_required_held_moment_cannot_be_missing():
    plan = valid_plan()
    plan.held_back.pop()
    assert "held_back_missing" in rules(plan)


def test_atzerodt_does_not_know_before_ep5():
    plan = valid_plan()
    plan.knowledge[0].episode = 4
    assert "knowledge_too_early" in rules(plan)


def test_one_page_plan_lists_every_episode():
    page = render_season_plan(valid_plan(), CONFIG.title)
    assert page.startswith("# Test Series, season plan")
    assert "The Shot and the Knife" in page
    assert "EARLY WALL" in page and "LATE WALL" in page
    assert len(page.splitlines()) <= 60


@pytest.mark.parametrize("wall", ["early", "late"])
def test_wall_cannot_move(wall):
    plan = valid_plan()
    plan.episodes[0].wall = wall
    assert "wall" in rules(plan)


def test_series_without_rules_needs_only_its_episode_count():
    config = SeriesConfig(title="Harbor", episodes=2)
    plan = valid_plan()
    plan.episodes = plan.episodes[:2]
    for episode in plan.episodes:
        episode.wall = "none"
    plan.held_back = []
    plan.knowledge = []
    assert check_season(plan, [], config) == []
    plan.episodes.pop()
    assert {v.rule for v in check_season(plan, [], config)} == {"outline_mismatch"}
