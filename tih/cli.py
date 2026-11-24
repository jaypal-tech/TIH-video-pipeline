import os
import shutil
from pathlib import Path

import typer

from tih.bible import (
    LOCKS_OVERRIDE,
    CharacterLock,
    file_sha256,
    load_bible,
    load_locks,
    save_locks,
    series_dir,
)
from tih.budget import BudgetGuard
from tih.gates import approve as approve_gate
from tih.ledger import Ledger
from tih.paths import RunPaths
from tih.probe import probe_estimate, run_probe
from tih.production import read_takes, save_pick
from tih.providers.dry import DryRunTransport
from tih.providers.fal import GatewayContext
from tih.stages import camera as _camera  # noqa: F401  registers the stage
from tih.stages import casting as _casting  # noqa: F401  registers the stage
from tih.stages import cut as _cut  # noqa: F401  registers the stage
from tih.stages import director as _director  # noqa: F401  registers the stage
from tih.stages import historians_desk as _historians_desk  # noqa: F401  registers the stage
from tih.stages import lipsync as _lipsync  # noqa: F401  registers the stage
from tih.stages import performance as _performance  # noqa: F401  registers the stage
from tih.stages import record as _record  # noqa: F401  registers the stage
from tih.stages import shotplan as _shotplan  # noqa: F401  registers the stage
from tih.stages import sound as _sound  # noqa: F401  registers the stage
from tih.stages import story_room as _story_room  # noqa: F401  registers the stage
from tih.stages import voice as _voice  # noqa: F401  registers the stage
from tih.stages import writers_room as _writers_room  # noqa: F401  registers the stage
from tih.stages.registry import DONE_FILE, RunContext, run_stage

# The order an episode runs in after Script Lock and Look Lock.
EPISODE_STAGES = ("director", "shotplan", "keyframes", "motion", "voice", "lipsync", "sound", "cut")

app = typer.Typer(no_args_is_help=True)
lock_app = typer.Typer(no_args_is_help=True, help="Lock a character's look or voice.")
app.add_typer(lock_app, name="lock")


@app.callback()
def main() -> None:
    """Run the story pipeline."""


def _context(episode: str, series: str, root: Path) -> RunContext:
    if episode != "season" and not episode.isdigit():
        raise typer.BadParameter("Use an episode number or 'season'")
    number = None if episode == "season" else int(episode)
    return RunContext(paths=RunPaths(series, number, root=root))


@app.command()
def run(
    episode: str,
    stage: str = typer.Option(..., "--stage"),
    series: str = typer.Option(..., "--series"),
    force: bool = typer.Option(False, "--force"),
    root: Path = typer.Option(Path("runs"), "--root"),
) -> None:
    """Run one stage for one episode number, or for "season"."""
    ctx = _context(episode, series, root)
    result = run_stage(ctx, stage, force=force)
    typer.echo(f"{stage}: {result}")


@app.command()
def episode(
    number: int,
    series: str = typer.Option(..., "--series"),
    start: str = typer.Option("director", "--from", help="First stage to run."),
    force: bool = typer.Option(False, "--force", help="Re-run stages from --from on."),
    root: Path = typer.Option(Path("runs"), "--root"),
) -> None:
    """Run an episode end to end. Takes made before are reused at no cost."""
    if start not in EPISODE_STAGES:
        raise typer.BadParameter(f"--from must be one of {', '.join(EPISODE_STAGES)}")
    ctx = _context(str(number), series, root)
    for stage in EPISODE_STAGES[EPISODE_STAGES.index(start):]:
        result = run_stage(ctx, stage, force=force)
        typer.echo(f"{stage}: {result}")


def rehearse_episode(number: int, series: str, root: Path, source: Path = Path("runs")) -> RunContext:
    """Run every video stage with placeholder media, from the real locked script and shot plan.

    The Writer's script and the Director's plan are copied from `source`; locks and
    approvals stay inside `root`.
    """
    paths = RunPaths(series, number, root=root)
    real = RunPaths(series, number, root=source)
    for stage in ("writers_room", "director"):
        if not (real.scope_dir / stage / DONE_FILE).exists():
            raise FileNotFoundError(
                f"Run the Writer and Director for episode {number} first: "
                f"tih run {number} --stage director --series {series}"
            )
    for stage in ("writers_room", "director"):
        shutil.copytree(real.scope_dir / stage, paths.scope_dir / stage, dirs_exist_ok=True)
    os.environ[LOCKS_OVERRIDE] = str(root / series / "locks.yaml")
    ledger = Ledger(root / series / "calls.jsonl")
    gateway = GatewayContext(
        paths, ledger, BudgetGuard(ledger), DryRunTransport(root / series / "dry"),
        series_dir(series) / "models.yaml",
    )
    ctx = RunContext(paths=paths, gateway=gateway)
    approve_gate(paths, "script")
    # force: a new script or plan copied in must replace the last rehearsal; reused takes cost $0
    run_stage(ctx, "shotplan", force=True)
    run_stage(ctx, "casting", force=True)
    locks = load_locks(series)
    for key, takes in read_takes(ctx, "casting").items():
        kind, character = key.split(":")
        first = next(t for t in takes.takes if t.status == "ok")
        lock = locks.characters.get(character, CharacterLock())
        if kind == "look":
            lock.reference, lock.reference_sha256 = first.path, file_sha256(Path(first.path))
        else:
            lock.voice_id = first.note.removeprefix("voice id ")
        locks.characters[character] = lock
    save_locks(series, locks)
    approve_gate(paths, "look")
    for stage in EPISODE_STAGES[2:]:
        run_stage(ctx, stage, force=True)
    return ctx


@app.command()
def rehearse(
    number: int,
    series: str = typer.Option(..., "--series"),
    root: Path = typer.Option(Path("runs-dry"), "--root"),
) -> None:
    """Run an episode end to end with placeholder media: $0, real timing and text."""
    ctx = rehearse_episode(number, series, root)
    ledger = Ledger(root / series / "calls.jsonl")
    calls = ledger.current()
    priced = sum(r.cost_usd or 0.0 for r in calls)
    final = ctx.paths.scope_dir / "cut" / "final.mp4"
    typer.echo(f"Rehearsal cut: {final}")
    typer.echo(f"{len(calls)} calls; a real run would cost about ${priced:.2f} at models.yaml prices")


@app.command()
def approve(
    episode: str,
    gate: str,
    series: str = typer.Option(..., "--series"),
    root: Path = typer.Option(Path("runs"), "--root"),
) -> None:
    """Record the owner's approval of a gate (story, script, look or picture) for an episode or "season"."""
    if episode != "season" and not episode.isdigit():
        raise typer.BadParameter("Use an episode number or 'season'")
    number = None if episode == "season" else int(episode)
    record = approve_gate(RunPaths(series, number, root=root), gate)
    typer.echo(f"{gate}: approved ({record})")


@app.command()
def pick(
    episode: int,
    stage: str,
    shot: str,
    take: int,
    series: str = typer.Option(..., "--series"),
    root: Path = typer.Option(Path("runs"), "--root"),
) -> None:
    """Choose a different take for one shot, e.g. tih pick 1 keyframes 2.3 1."""
    ctx = RunContext(paths=RunPaths(series, episode, root=root))
    save_pick(ctx, stage, shot, take)
    typer.echo(f"Picked take {take} for {stage} {shot}. Re-run: "
               f"tih episode {episode} --series {series} --from {stage} --force")


def _mark_kept(series: str, root: Path, output: Path) -> None:
    ledger = Ledger(root / series / "calls.jsonl")
    for record in ledger.current():
        if record.output_file and str(output) in record.output_file.split("; "):
            ledger.update(record.call_id, kept=True, reject_reason=None)


@lock_app.command("look")
def lock_look(
    character: str,
    image: Path = typer.Option(..., "--image", exists=True, dir_okay=False),
    seed: int | None = typer.Option(None, "--seed"),
    series: str = typer.Option(..., "--series"),
    root: Path = typer.Option(Path("runs"), "--root"),
) -> None:
    """Lock a character's reference image (and seed) for every future shot."""
    if character not in load_bible(series).characters:
        raise typer.BadParameter(f"{character} is not in the bible")
    target_dir = series_dir(series) / "assets" / "characters" / character
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"identity{image.suffix}"
    shutil.copyfile(image, target)
    locks = load_locks(series)
    lock = locks.characters.get(character, CharacterLock())
    lock.reference, lock.reference_sha256, lock.seed = str(target), file_sha256(target), seed
    locks.characters[character] = lock
    save_locks(series, locks)
    _mark_kept(series, root, image)
    typer.echo(f"Locked {character}'s look: {target}. Approve again with tih approve <ep> look.")


@lock_app.command("voice")
def lock_voice(
    character: str,
    voice_id: str = typer.Option(..., "--voice-id"),
    endpoint: str | None = typer.Option(None, "--endpoint"),
    series: str = typer.Option(..., "--series"),
) -> None:
    """Lock a character's voice id for every future line."""
    bible = load_bible(series)
    if character not in bible.characters or not bible.characters[character].voice:
        raise typer.BadParameter(f"{character} is not a speaking character in the bible")
    locks = load_locks(series)
    lock = locks.characters.get(character, CharacterLock())
    lock.voice_id, lock.voice_endpoint = voice_id, endpoint
    locks.characters[character] = lock
    save_locks(series, locks)
    typer.echo(f"Locked {character}'s voice. Approve again with tih approve <ep> look.")


@app.command()
def log(
    series: str = typer.Option(..., "--series"),
    out: Path = typer.Option(..., "--out"),
    root: Path = typer.Option(Path("runs"), "--root"),
) -> None:
    """Export every API call as CSV with the brief's fields."""
    ledger = Ledger(root / series / "calls.jsonl")
    ledger.export_csv(out)
    records = ledger.current()
    spent = sum(r.cost_usd or 0.0 for r in records)
    typer.echo(f"{len(records)} calls, ${spent:.2f} logged, written to {out}")


@app.command()
def probe(
    number: int,
    series: str = typer.Option(..., "--series"),
    shot: str = typer.Option("1.1", "--shot", help="a lip-sync shot with one face in the example plan"),
    yes: bool = typer.Option(False, "--yes", help="spend real money"),
    root: Path = typer.Option(Path("runs"), "--root"),
) -> None:
    """One real call per generation step, to prove each model accepts our arguments."""
    typer.echo(f"Probe: 9 calls, budget reserve up to ${probe_estimate(series):.2f} (real cost is lower).")
    if not yes:
        typer.echo("Nothing spent. Add --yes to run it.")
        return
    results = run_probe(_context(str(number), series, root), shot)
    for r in results:
        typer.echo(f"{r.step:13} {r.status:8} {r.endpoint} {r.error}")
    typer.echo(f"Report: {root / series / 'probe' / 'report.md'}")
