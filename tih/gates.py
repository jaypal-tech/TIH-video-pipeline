import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from tih.paths import RunPaths

GATES: dict[str, tuple[str, ...]] = {
    "story": (
        "story_room/season_plan.yaml",
        "story_room/season_ledger.yaml",
        "story_room/bible.yaml",
    ),
}


class GateNotApproved(RuntimeError):
    """The owner has not approved this gate yet."""


class ArtifactChanged(RuntimeError):
    """A file changed after the owner approved it."""


def _record_path(paths: RunPaths, gate: str) -> Path:
    return paths.scope_dir / "approvals" / f"{gate}.json"


def _hashes(paths: RunPaths, gate: str) -> dict[str, str]:
    if gate not in GATES:
        raise KeyError(f"Unknown gate '{gate}'. Available: {', '.join(sorted(GATES))}")
    hashes = {}
    for name in GATES[gate]:
        path = paths.scope_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"Cannot approve '{gate}': {name} does not exist")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
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
    scope = "season" if paths.episode is None else str(paths.episode)
    if not record.exists():
        raise GateNotApproved(
            f"'{gate}' is not approved. Review it, then run: "
            f"tih approve {scope} {gate} --series {paths.series}"
        )
    approved = json.loads(record.read_text(encoding="utf-8"))["artifacts"]
    changed = [name for name, digest in _hashes(paths, gate).items() if approved.get(name) != digest]
    if changed:
        raise ArtifactChanged(
            f"Changed since '{gate}' was approved: {', '.join(changed)}. "
            f"Review them, then run: tih approve {scope} {gate} --series {paths.series}"
        )
