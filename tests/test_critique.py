from tih.story.critique import (
    EPISODE_CHECKS,
    TRUTH_CHECKS,
    Change,
    EndingLabel,
    Finding,
    Recheck,
    RecheckResult,
    check_text,
    flagged,
    render_changes,
    render_checks,
    render_critique,
    review_findings,
)

from tih.story.season import Violation

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


def result(finding, verdict):
    return RecheckResult(finding=finding, quote="q", reason="Because.", verdict=verdict)


def test_flagged_is_every_hard_finding_not_fixed_and_rechecked():
    kept, _ = review(finding("E5", 1), finding("E5", 2), finding("E5", 3), finding("E5", 5), finding("E6", 4))
    changes = [
        Change(finding="F1", action="fixed", note="Rewrote."),
        Change(finding="F2", action="declined", note="It works."),
        Change(finding="F4", action="fixed", note="Rewrote."),
    ]
    rechecked = Recheck(results=[result("F1", "pass"), result("F4", "fail")])
    assert [(f.id, why) for f, why in flagged(kept, changes, rechecked)] == [
        ("F2", "declined: It works."),
        ("F3", "not addressed"),
        ("F4", "re-check fail: Because."),
    ]


def test_fixed_rule_finding_needs_no_critic_recheck():
    kept, _ = review_findings([], [], valid_plan(), [Violation("wall", "Ep 5 must have wall 'early'")])
    changes = [Change(finding="F1", action="fixed", note="Moved.")]
    assert flagged(kept, changes, Recheck(results=[])) == []


def test_finding_quoting_only_a_title_is_dropped():
    title = finding("E10", 2, quote="the box")
    kept, dropped = review_findings([title], [], valid_plan(), titles={"The Box"})
    assert (kept, dropped) == ([], 1)


def test_check_text_covers_labels_and_rules():
    assert check_text("E5").startswith("The ending leaves")
    assert "opens a question" in check_text("E4")
    assert check_text("RULE") == "A series rule from the config."


def test_changes_page_shows_decision_and_recheck():
    kept, _ = review(finding("E5", 3))
    page = render_changes(
        kept, [Change(finding="F1", action="fixed", note="New ending.")], Recheck(results=[result("F1", "pass")])
    )
    assert "**fixed**: New ending." in page and "re-check: **pass**" in page


def test_checks_are_rendered_with_their_ids():
    text = render_checks(EPISODE_CHECKS)
    assert "E1: The hook is a moment" in text
    assert "T1" not in text and "T1" in render_checks(TRUTH_CHECKS)


def test_critique_page_shows_verdict_table_and_findings():
    kept, dropped = review(finding("E5", 3), finding(quote="not there"))
    page = render_critique(valid_plan(), kept, dropped, "consider", ["Fix episode 3."], 0)
    assert "Overall verdict: **consider**" in page
    assert "| 3 | E5 |" in page
    assert "1 finding dropped" in page
