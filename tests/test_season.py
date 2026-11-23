from pathlib import Path

import pytest

from tih.series import KnowledgeRule, SeriesConfig
from tih.story.parse_brief import parse_season_outline
from tih.story.season import (
    Ending,
    EpisodeDetail,
    EpisodePlan,
    HeldMoment,
    KnowledgeEvent,
    SeasonPlan,
    SeasonSpine,
    Setup,
    assemble_plan,
    check_season,
    ending_repeats,
    fix_fields,
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
TYPES = ["revelation", "threat", "decision"]


def valid_plan() -> SeasonPlan:
    walls = {5: "early", 11: "late"}
    reveals = {12: ["the shot", "the knife"], 14: ["Lincoln's death"]}
    return SeasonPlan(
        season_question="Can anyone stop the night?",
        theme="Small choices carry history.",
        promise="The road to the night nobody remembers.",
        phases=[],
        arcs=[],
        episodes=[
            EpisodePlan(
                number=e.number,
                title=e.title,
                opens_with="Picks up the last ending.",
                hook=f"A hand closes on a letter in episode {e.number}.",
                viewer_knows="More than the characters.",
                unknown="Whether he is seen.",
                turn="She chooses to go.",
                payoff="A question is answered.",
                cliff=f"A door opens on a stranger in episode {e.number}.",
                cliff_type=TYPES[e.number % 3],
                reveals=reveals.get(e.number, []),
                job="Move the countdown.",
                wall=walls.get(e.number, "none"),
            )
            for e in OUTLINE
        ],
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
        risks=["Confirm the names before use."],
    )


def spine_of(plan: SeasonPlan) -> SeasonSpine:
    endings = [
        Ending(
            number=e.number,
            title=f"Working title {e.number}",
            turn=e.turn,
            ending=e.cliff,
            ending_type=e.cliff_type,
        )
        for e in plan.episodes
    ]
    data = plan.model_dump(exclude={"episodes"})
    return SeasonSpine(**data, tentpole_endings=endings[-1:], endings=endings)


def details_of(plan: SeasonPlan) -> list[EpisodeDetail]:
    return [
        EpisodeDetail(**e.model_dump(exclude={"title", "wall"})) for e in plan.episodes
    ]


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
    assert "Can anyone stop the night?" in page
    assert "The Shot and the Knife" in page
    assert "EARLY WALL" in page and "LATE WALL" in page
    assert len(page.splitlines()) <= 60


def test_one_page_plan_puts_unresolved_findings_first():
    page = render_season_plan(valid_plan(), CONFIG.title, ["F2 (E5, ep 9): no threat"])
    lines = page.splitlines()
    assert lines[2] == "## Unresolved"
    assert "F2 (E5, ep 9): no threat" in lines[3]


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


def test_assembly_takes_titles_from_outline_and_walls_from_config():
    plan = valid_plan()
    built = assemble_plan(spine_of(plan), details_of(plan), OUTLINE, CONFIG)
    assert built == plan


def test_assembly_uses_spine_titles_without_an_outline():
    config = SeriesConfig(title="Harbor", episodes=30)
    plan = valid_plan()
    built = assemble_plan(spine_of(plan), details_of(plan), [], config)
    assert built.episodes[0].title == "Working title 1"
    assert {e.wall for e in built.episodes} == {"none"}


def test_fix_fields_overwrites_model_written_walls_and_titles():
    plan = valid_plan()
    plan.episodes[0].wall = "late"
    plan.episodes[0].title = "Changed"
    fixed = fix_fields(plan, {e.number: e.title for e in OUTLINE}, CONFIG)
    assert (fixed.episodes[0].wall, fixed.episodes[0].title) == ("none", "The Last Speech")


def test_ending_repeats_counts_same_type_twice_in_a_row():
    plan = valid_plan()
    assert ending_repeats(plan) == 0
    plan.episodes[1].cliff_type = plan.episodes[0].cliff_type
    assert ending_repeats(plan) == 1
