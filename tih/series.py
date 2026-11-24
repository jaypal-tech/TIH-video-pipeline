from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class KnowledgeRule(BaseModel):
    who: str
    topic: str
    not_before: int


class CraftTargets(BaseModel):
    """Short-drama pace targets a series aims for. Reported, never enforced."""

    turns: tuple[int, int]
    talk_share: float
    words_per_line: float
    max_places: int


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warn: float = 1500.0
    stop: float = 3000.0
    proving_cap: float = 100.0


class SeriesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    episodes: int
    true_story: bool = False
    paywalls: dict[int, Literal["early", "late"]] = Field(default_factory=dict)
    held_back: dict[str, int] = Field(default_factory=dict)
    knowledge_rules: list[KnowledgeRule] = Field(default_factory=list)
    tone: str = ""
    story_notes: list[str] = Field(default_factory=list)
    ending_convention: str = ""
    batch_size: int = 10
    source_format: Literal["table_script", "freeform"] = "freeform"
    runtime_s: tuple[int, int] = (60, 90)
    script_notes: dict[int, list[str]] = Field(default_factory=dict)
    craft: CraftTargets | None = None
    budget: Budget = Field(default_factory=Budget)


def load_series(name: str) -> SeriesConfig:
    path = Path("series") / name / "series.yaml"
    return SeriesConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
