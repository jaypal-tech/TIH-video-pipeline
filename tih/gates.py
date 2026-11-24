"""Owner approvals (Script Lock, Look Lock, Picture Lock).

`approve` records the SHA-256 of each file the owner looked at. Any stage that
spends money calls `require_approved` first, so editing a locked script, shot
plan, bible or lock file after approval stops the pipeline until the owner
approves again.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from tih.bible import file_sha256, locks_path, series_dir
from tih.shots import plan_path


class GateNotApproved(RuntimeError):
    """The owner has not approved this gate yet."""


class ArtifactChanged(RuntimeError):
    """A file changed after the owner approved it."""


def gate_artifacts(paths, gate: str) -> list[Path]:
    """The files each gate covers. `paths` is the run's RunPaths."""
    if gate == "script":
        return [
            paths.scope_dir / "record" / "script.yaml",
            plan_path(paths.series, paths.episode),
        ]
    if gate == "look":
        return [series_dir(paths.series) / "bible.yaml", locks_path(paths.series)]
    if gate == "picture":
        return [paths.scope_dir / "cut" / "final.mp4"]
    raise ValueError(f"Unknown gate '{gate}'. Use script, look or picture.")


def _approval_file(paths, gate: str) -> Path:
    return paths.scope_dir / "approvals" / f"{gate}.json"


def approve(paths, gate: str) -> Path:
    artifacts = gate_artifacts(paths, gate)
    missing = [str(path) for path in artifacts if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Cannot approve {gate}; missing: {', '.join(missing)}")
    record = {
        "gate": gate,
        "approved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artifacts": {str(path): file_sha256(path) for path in artifacts},
    }
    target = _approval_file(paths, gate)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return target


def require_approved(paths, gate: str) -> None:
    target = _approval_file(paths, gate)
    if not target.exists():
        raise GateNotApproved(f"Gate '{gate}' is not approved. Run: tih approve {gate}")
    record = json.loads(target.read_text(encoding="utf-8"))
    for name, digest in record["artifacts"].items():
        path = Path(name)
        if not path.exists() or file_sha256(path) != digest:
            raise ArtifactChanged(
                f"{name} changed after the '{gate}' approval. Review it and approve again."
            )
