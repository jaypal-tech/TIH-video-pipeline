import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tih.paths import RunPaths

DONE_FILE = "DONE.json"


class MissingStage(RuntimeError):
    """A required earlier stage has not completed."""


@dataclass(frozen=True)
class RunContext:
    paths: RunPaths


@dataclass(frozen=True)
class Stage:
    name: str
    requires: tuple[str, ...]
    gate: str | None
    run: Callable[[RunContext], None]


_STAGES: dict[str, Stage] = {}


def register(stage: Stage) -> None:
    _STAGES[stage.name] = stage


def _done_path(ctx: RunContext, stage: str) -> Path:
    return ctx.paths.stage_dir(stage) / DONE_FILE


def _hash_outputs(stage_dir: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(p for p in stage_dir.rglob("*") if p.is_file()):
        if path.name == DONE_FILE or path.name.startswith(f"{DONE_FILE}."):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[path.relative_to(stage_dir).as_posix()] = digest.hexdigest()
    return hashes


def _write_done(ctx: RunContext, stage: str) -> None:
    done = _done_path(ctx, stage)
    temp = done.with_name(f"{DONE_FILE}.tmp")
    temp.write_text(json.dumps({"outputs": _hash_outputs(done.parent)}, indent=2))
    os.replace(temp, done)


def run_stage(
    ctx: RunContext, name: str, force: bool = False
) -> Literal["ran", "skipped"]:
    if name not in _STAGES:
        available = ", ".join(sorted(_STAGES)) or "none"
        raise KeyError(f"Unknown stage '{name}'. Available: {available}")
    stage = _STAGES[name]
    done = _done_path(ctx, name)
    if done.exists() and not force:
        return "skipped"
    missing = [req for req in stage.requires if not _done_path(ctx, req).exists()]
    if missing:
        raise MissingStage(f"Stage '{name}' needs: {', '.join(missing)}")
    done.unlink(missing_ok=True)
    stage.run(ctx)
    _write_done(ctx, name)
    return "ran"
