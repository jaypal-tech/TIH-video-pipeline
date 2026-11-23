from pathlib import Path
from typing import Any

import yaml

from tih.series import load_series
from tih.stages.registry import RunContext, Stage, register
from tih.story.parse_brief import extract_canon, parse_brief, parse_season_outline

SOURCE_FORMATS = {
    "table_script": (parse_brief, parse_season_outline),
    "freeform": (lambda text: [], lambda text: []),
}


def _write_yaml(path: Path, data: Any) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=10000),
        encoding="utf-8",
    )


def run_record(ctx: RunContext) -> None:
    series_dir = Path("series") / ctx.paths.series
    config = load_series(ctx.paths.series)
    brief = (series_dir / "source" / "brief.txt").read_text(encoding="utf-8")
    sources_file = series_dir / "sources.yaml"
    sources = {"sources": {}, "claims": {}}
    if sources_file.exists():
        sources = yaml.safe_load(sources_file.read_text(encoding="utf-8"))
    parse_script, parse_outline = SOURCE_FORMATS[config.source_format]
    episodes = parse_script(brief)
    canon = extract_canon(episodes, sources)
    for episode in episodes:
        for clip in episode.clips:
            for beat in clip.beats:
                if beat.line and beat.line.text in canon.provenance:
                    beat.line.source_id = canon.provenance[beat.line.text].source_id

    stage_dir = ctx.paths.stage_dir("record")
    selected = [e for e in episodes if e.number == ctx.paths.episode]
    if ctx.paths.episode is None:
        script: Any = [e.model_dump(mode="json") for e in episodes]
    elif selected:
        script = selected[0].model_dump(mode="json")
    elif not episodes:
        script = None
    else:
        raise ValueError(f"Episode {ctx.paths.episode} has no script in the source")
    _write_yaml(stage_dir / "script.yaml", script)
    _write_yaml(stage_dir / "canon.yaml", canon.model_dump(mode="json"))
    _write_yaml(
        stage_dir / "season_outline.yaml",
        [entry.model_dump(mode="json") for entry in parse_outline(brief)],
    )


register(Stage(name="record", requires=(), gate=None, run=run_record))
