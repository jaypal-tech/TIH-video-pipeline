from pathlib import Path

import typer

from tih.paths import RunPaths
from tih.stages import record as _record  # noqa: F401  registers the stage
from tih.stages import story_room as _story_room  # noqa: F401  registers the stage
from tih.stages.registry import RunContext, run_stage

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run the story pipeline."""


@app.command()
def run(
    episode: str,
    stage: str = typer.Option(..., "--stage"),
    series: str = typer.Option(..., "--series"),
    force: bool = typer.Option(False, "--force"),
    root: Path = typer.Option(Path("runs"), "--root"),
) -> None:
    """Run one stage for one episode number, or for "season"."""
    if episode != "season" and not episode.isdigit():
        raise typer.BadParameter("Use an episode number or 'season'")
    number = None if episode == "season" else int(episode)
    ctx = RunContext(paths=RunPaths(series, number, root=root))
    result = run_stage(ctx, stage, force=force)
    typer.echo(f"{stage}: {result}")
