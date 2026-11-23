from pathlib import Path

import typer

from tih.paths import RunPaths
from tih.stages.registry import RunContext, run_stage

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run the story pipeline."""


@app.command()
def run(
    episode: int,
    stage: str = typer.Option(..., "--stage"),
    series: str = typer.Option("lincoln", "--series"),
    force: bool = typer.Option(False, "--force"),
    root: Path = typer.Option(Path("runs"), "--root"),
) -> None:
    """Run one stage for one episode."""
    ctx = RunContext(paths=RunPaths(series, episode, root=root))
    result = run_stage(ctx, stage, force=force)
    typer.echo(f"{stage}: {result}")
