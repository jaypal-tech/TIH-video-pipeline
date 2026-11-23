from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunPaths:
    series: str
    episode: int | None
    root: Path = Path("runs")

    @property
    def scope_dir(self) -> Path:
        scope = "season" if self.episode is None else f"ep{self.episode:02d}"
        return self.root / self.series / scope

    def stage_dir(self, stage: str) -> Path:
        path = self.scope_dir / stage
        path.mkdir(parents=True, exist_ok=True)
        return path
