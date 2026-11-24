from pathlib import Path

import typer

from tih.gates import approve as approve_gate
from tih.paths import RunPaths
from tih.stages import historians_desk as _historians_desk  # noqa: F401  registers the stage
from tih.stages import record as _record  # noqa: F401  registers the stage
from tih.stages import story_room as _story_room  # noqa: F401  registers the stage
from tih.stages import writers_room as _writers_room  # noqa: F401  registers the stage
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


@app.command()
def approve(
    episode: str,
    gate: str,
    series: str = typer.Option(..., "--series"),
    root: Path = typer.Option(Path("runs"), "--root"),
) -> None:
    """Record the owner's approval of a gate, for one episode number or for "season"."""
    if episode != "season" and not episode.isdigit():
        raise typer.BadParameter("Use an episode number or 'season'")
    number = None if episode == "season" else int(episode)
    record = approve_gate(RunPaths(series, number, root=root), gate)
    typer.echo(f"{gate}: approved ({record})")
