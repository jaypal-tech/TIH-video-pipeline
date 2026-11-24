"""Owner approvals: Story Lock, Script Lock, Look Lock and Picture Lock.

`approve` records the SHA-256 of each file the owner looked at. Every stage that
depends on a lock calls `require_approved` first, so editing a locked file after
approval stops the pipeline until the owner approves again.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from tih.bible import file_sha256, locks_path, series_dir
from tih.paths import RunPaths
from tih.shots import plan_path

GATE_NAMES = ("story", "script", "look", "picture")


class GateNotApproved(RuntimeError):
    """The owner has not approved this gate yet."""


class ArtifactChanged(RuntimeError):
    """A file changed after the owner approved it."""


def gate_artifacts(paths: RunPaths, gate: str) -> list[Path]:
    """The files each gate covers."""
    if gate == "story":
        story = paths.scope_dir / "story_room"
        return [story / "season_plan.yaml", story / "season_ledger.yaml", story / "bible.yaml"]
    if gate == "script":
        return [paths.scope_dir / "writers_room" / "script.yaml"]
    if gate == "look":
        return [
            series_dir(paths.series) / "bible.yaml",
            locks_path(paths.series),
            plan_path(paths),
        ]
    if gate == "picture":
        return [paths.scope_dir / "cut" / "final.mp4"]
    raise KeyError(f"Unknown gate '{gate}'. Available: {', '.join(GATE_NAMES)}")


def _name(paths: RunPaths, path: Path) -> str:
    """Files inside the run folder are named relative to it, others by their path."""
    if path.is_relative_to(paths.scope_dir):
        return path.relative_to(paths.scope_dir).as_posix()
    return str(path)


def _scope(paths: RunPaths) -> str:
    return "season" if paths.episode is None else str(paths.episode)


def _record_path(paths: RunPaths, gate: str) -> Path:
    return paths.scope_dir / "approvals" / f"{gate}.json"


def _hashes(paths: RunPaths, gate: str) -> dict[str, str]:
    hashes = {}
    for path in gate_artifacts(paths, gate):
        if not path.is_file():
            raise FileNotFoundError(f"Cannot approve '{gate}': {_name(paths, path)} does not exist")
        hashes[_name(paths, path)] = file_sha256(path)
    return hashes


def approve(paths: RunPaths, gate: str) -> Path:
    """Record the owner's approval of the gate's files as they are now."""
    record = _record_path(paths, gate)
    record.parent.mkdir(parents=True, exist_ok=True)
    data = {"approved_at": datetime.now(UTC).isoformat(), "artifacts": _hashes(paths, gate)}
    record.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return record


def require_approved(paths: RunPaths, gate: str) -> None:
    """Raise unless the gate is approved and none of its files changed since."""
    record = _record_path(paths, gate)
    if not record.exists():
        raise GateNotApproved(
            f"'{gate}' is not approved. Review it, then run: "
            f"tih approve {_scope(paths)} {gate} --series {paths.series}"
        )
    approved = json.loads(record.read_text(encoding="utf-8"))["artifacts"]
    current = {}
    for path in gate_artifacts(paths, gate):
        current[_name(paths, path)] = file_sha256(path) if path.is_file() else None
    changed = [name for name, digest in current.items() if approved.get(name) != digest]
    if changed:
        raise ArtifactChanged(
            f"Changed since '{gate}' was approved: {', '.join(changed)}. "
            f"Review them, then run: tih approve {_scope(paths)} {gate} --series {paths.series}"
        )
