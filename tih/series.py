from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class KnowledgeRule(BaseModel):
    who: str
    topic: str
    not_before: int


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


def load_series(name: str) -> SeriesConfig:
    path = Path("series") / name / "series.yaml"
    return SeriesConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
