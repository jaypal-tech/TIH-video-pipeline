from pathlib import Path

import yaml

from tih.llm import LlmContext, ask_json
from tih.series import load_series
from tih.stages.registry import RunContext, Stage, register
from tih.stages.story_room import PROMPTS, _tag, _yaml
from tih.story.models import Episode
from tih.story.truth import TruthReport, render_truth_report, review_report


def run_historians_desk(ctx: RunContext) -> None:
    llm = LlmContext.for_run(ctx.paths)
    try:
        _run(ctx, llm)
    finally:
        llm.ledger.export_csv(llm.ledger.path.with_name("calls.csv"))


def _run(ctx: RunContext, llm: LlmContext) -> None:
    config = load_series(ctx.paths.series)
    source = (Path("series") / ctx.paths.series / "source" / "brief.txt").read_text("utf-8")
    script_text = (ctx.paths.stage_dir("writers_room") / "script.yaml").read_text("utf-8")
    script = Episode(**yaml.safe_load(script_text))
    notes = config.script_notes.get(script.number, [])

    user = _tag("series_config", _yaml(config.model_dump(mode="json")))
    user += _tag("source", source)
    user += _tag("script", script_text)
    if notes:
        user += _tag("script_notes", _yaml(notes))
    user += "Check every beat of this script against the source, and answer every script note."
    system = (PROMPTS / "historian.md").read_text(encoding="utf-8")
    report = ask_json(llm, "historian", system, user, TruthReport)

    kept, dropped, unanswered = review_report(report, script, notes)
    stage_dir = ctx.paths.stage_dir("historians_desk")
    (stage_dir / "truth_report.yaml").write_text(
        _yaml(report.model_dump(mode="json")), encoding="utf-8"
    )
    (stage_dir / "truth_report.md").write_text(
        render_truth_report(script.title, kept, dropped, report.notes, unanswered), encoding="utf-8"
    )


register(
    Stage(name="historians_desk", requires=("writers_room",), gate=None, run=run_historians_desk)
)
