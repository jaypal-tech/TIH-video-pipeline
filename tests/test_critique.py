from tih.story.critique import (
    EPISODE_CHECKS,
    TRUTH_CHECKS,
    Change,
    EndingLabel,
    Finding,
    render_checks,
    render_critique,
    review_findings,
    unresolved,
)

from test_season import valid_plan


def finding(check="E5", episode=3, quote=None, verdict="no"):
    plan = valid_plan()
    quote = quote if quote is not None else plan.episodes[episode - 1].cliff
    return Finding(check=check, episode=episode, quote=quote, problem="Weak.", verdict=verdict)


def review(*findings, labels=()):
    return review_findings(list(findings), list(labels), valid_plan())


def test_failed_hard_check_must_be_fixed():
    kept, dropped = review(finding("E5"))
    assert (kept[0].id, kept[0].hard, dropped) == ("F1", True, 0)


def test_failed_advice_check_is_advice():
    kept, _ = review(finding("E6"))
    assert kept[0].hard is False


def test_unclear_verdict_is_never_hard():
    kept, _ = review(finding("E5", verdict="unclear"))
    assert kept[0].hard is False


def test_finding_whose_quote_is_not_in_the_plan_is_dropped():
    kept, dropped = review(finding(quote="words the plan never used"))
    assert (kept, dropped) == ([], 1)


def test_quote_match_ignores_spacing_and_case():
    kept, _ = review(finding(quote="  a DOOR opens   on a stranger in episode 3. "))
    assert len(kept) == 1


def test_finding_for_unknown_check_is_dropped():
    kept, dropped = review(finding("Z9"))
    assert (kept, dropped) == ([], 1)


def test_resolving_ending_becomes_a_hard_finding():
    kept, _ = review(labels=[EndingLabel(number=2, label="resolves")])
    assert (kept[0].check, kept[0].episode, kept[0].hard) == ("E4", 2, True)
    assert kept[0].quote == valid_plan().episodes[1].cliff


def test_ending_that_states_an_outcome_is_advice():
    kept, _ = review(labels=[EndingLabel(number=2, label="states_outcome")])
    assert kept[0].hard is False


def test_ending_that_opens_a_question_passes():
    assert review(labels=[EndingLabel(number=2, label="opens_question")]) == ([], 0)


def test_unresolved_is_every_hard_finding_not_marked_fixed():
    kept, _ = review(finding("E5", 1), finding("E5", 2), finding("E5", 3), finding("E6", 4))
    changes = [
        Change(finding="F1", action="fixed", note="Rewrote."),
        Change(finding="F2", action="declined", note="It works."),
    ]
    assert [f.id for f in unresolved(kept, changes)] == ["F2", "F3"]


def test_checks_are_rendered_with_their_ids():
    text = render_checks(EPISODE_CHECKS)
    assert "E1: The hook is a moment" in text
    assert "T1" not in text and "T1" in render_checks(TRUTH_CHECKS)


def test_critique_page_shows_verdict_table_and_findings():
    kept, dropped = review(finding("E5", 3), finding(quote="not there"))
    page = render_critique(
        valid_plan(), kept, dropped, "consider", ["Fix episode 3."], "revision", 0
    )
    assert "Overall verdict: **consider**" in page
    assert "| 3 | E5 |" in page
    assert "1 finding dropped" in page
    assert "Blind comparison: **revision**" in page
