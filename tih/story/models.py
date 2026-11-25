from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Evidence(StrEnum):
    real_words = "real_words"
    reported = "reported"
    accounts_say = "accounts_say"
    true_card = "true_card"
    dramatized = "dramatized"
    narration = "narration"  # the narrator stating fact: never a quote, never invented


class Delivery(StrEnum):
    face = "face"
    off = "off"
    inner = "inner"
    narration = "narration"  # the narrator, over the picture; nobody on screen speaks


NARRATOR = "NARRATOR"  # the speaker of every narration line


class StoryFunction(StrEnum):
    hook = "hook"
    conflict = "conflict"
    reveal = "reveal"
    twist = "twist"
    decision = "decision"
    threat = "threat"
    cliff = "cliff"
    breath = "breath"


class Line(BaseModel):
    speaker: str
    text: str
    delivery: Delivery
    evidence: Evidence
    evidence_label: str | None = None
    source_id: str | None = None
    direction: str = ""


class Overlay(BaseModel):
    kind: Literal["name_card", "true_card", "super", "corner_bug", "time_super"]
    text: str
    start_s: float
    duration_s: float | None = None


class Beat(BaseModel):
    id: str
    start_s: float
    picture: str
    line: Line | None = None
    overlays: list[Overlay] = Field(default_factory=list)
    function: StoryFunction | None = None
    knowledge: dict[str, str] = Field(default_factory=dict)


class Clip(BaseModel):
    number: int
    place: str
    start_s: float
    end_s: float
    beats: list[Beat]


class Episode(BaseModel):
    number: int
    title: str
    runtime_s: int
    clips: list[Clip]
    next_opens: str


class OutlineEntry(BaseModel):
    number: int
    title: str
    outline: str


class Provenance(BaseModel):
    source_id: str
    confidence: str
    origin: Literal["brief", "researched"]


class Canon(BaseModel):
    real_words: list[str]
    reported: list[str]
    true_cards: list[str]
    name_cards: list[str]
    provenance: dict[str, Provenance]
