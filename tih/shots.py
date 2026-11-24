"""Shot plans: one YAML file per episode that says what to film.

`load_plan` reads series/<series>/shots/epNN.yaml. `resolve_plan` checks it
against the locked script and the bible, then returns shots with their start
times, exact line text, speaker and fully expanded prompts. Every generation
stage reads the resolved plan, never the raw YAML.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from tih.bible import Bible, check_forbidden, expand, tokens_in
from tih.story.models import Delivery, Episode, Evidence

OverlayKind = Literal["corner_bug", "time_super", "name_card", "super", "true_card"]
MIN_RUNTIME_S = 60.0
MAX_RUNTIME_S = 90.0


class ShotOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: OverlayKind
    text: str
    at: float = 0.0
    duration: float | None = None


class PlannedShot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    beat: str
    duration: float
    size: Literal["CU", "ECU", "MCU", "MEDIUM", "TWO", "WIDE", "INSERT", "POV"]
    cast: list[str] = Field(default_factory=list)
    place: str
    keyframe: str
    motion: str
    line: str | None = None
    delivery: Delivery | None = None
    speed: float = 1.0
    freeze_at: float | None = None
    flash_at: float | None = None
    sound: str = ""
    overlays: list[ShotOverlay] = Field(default_factory=list)


class Cue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["music", "sfx"]
    at: float
    duration: float
    gain_db: float = -18.0
    prompt: str


class ShotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: int
    fps: Literal[24, 30] = 24
    shots: list[PlannedShot]
    cues: list[Cue] = Field(default_factory=list)


class ResolvedShot(PlannedShot):
    clip: int
    start_s: float
    lens_mm: int
    speaker: str | None = None
    evidence: Evidence | None = None
    evidence_label: str | None = None
    needs_lipsync: bool = False
    keyframe_prompt: str
    negative_prompt: str
    motion_prompt: str


class ResolvedPlan(BaseModel):
    episode: int
    fps: int
    runtime_s: float
    shots: list[ResolvedShot]
    cues: list[Cue]


class PlanError(ValueError):
    """The shot plan does not match the script or the bible."""


def plan_path(series: str, episode: int) -> Path:
    return Path("series") / series / "shots" / f"ep{episode:02d}.yaml"


def load_plan(series: str, episode: int) -> ShotPlan:
    path = plan_path(series, episode)
    return ShotPlan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _speaker_id(speaker: str, bible: Bible) -> str:
    """Map a script speaker name (e.g. "MARY") to a bible character id."""
    key = speaker.strip().lower().replace(" ", "_")
    if key in bible.characters:
        return key
    for character_id, character in bible.characters.items():
        if speaker.strip().lower() in character.name.lower().split():
            return character_id
    raise PlanError(f"Speaker {speaker} is not in the bible")


def resolve_plan(plan: ShotPlan, episode: Episode, bible: Bible) -> ResolvedPlan:
    """Check the plan against the script and bible; return ready-to-run shots."""
    errors: list[str] = []
    beats = {beat.id: (clip.number, beat) for clip in episode.clips for beat in clip.beats}
    script_overlays = {
        overlay.text for _, beat in beats.values() for overlay in beat.overlays
    }
    pieces: dict[str, list[str]] = {}
    resolved: list[ResolvedShot] = []
    start = 0.0
    seen_ids: set[str] = set()

    for shot in plan.shots:
        where = f"shot {shot.id}"
        if shot.id in seen_ids:
            errors.append(f"{where}: duplicate id")
        seen_ids.add(shot.id)
        if shot.beat not in beats:
            errors.append(f"{where}: unknown beat {shot.beat}")
            continue
        clip_number, beat = beats[shot.beat]
        if shot.size not in bible.style.lenses:
            errors.append(f"{where}: no lens for size {shot.size}")
        if shot.place not in bible.places:
            errors.append(f"{where}: unknown place {shot.place}")
        for character_id in shot.cast:
            if character_id not in bible.characters:
                errors.append(f"{where}: unknown character {character_id}")
        for token in tokens_in(shot.keyframe) + tokens_in(shot.motion):
            if token.lower() in bible.characters and token.lower() not in shot.cast:
                errors.append(f"{where}: [{token}] is in a prompt but not in cast")
        named = {token.lower() for token in tokens_in(shot.keyframe)}
        for character_id in shot.cast:
            if character_id not in named:
                errors.append(f"{where}: {character_id} is cast but not named in the keyframe")
        for overlay in shot.overlays:
            if overlay.text not in script_overlays:
                errors.append(f"{where}: overlay text is not in the script: {overlay.text}")

        speaker = evidence = label = None
        if shot.line is not None:
            if beat.line is None:
                errors.append(f"{where}: has a line but beat {beat.id} has none")
            elif shot.line not in beat.line.text:
                errors.append(f"{where}: line is not an exact part of the script line")
            else:
                pieces.setdefault(beat.id, []).append(shot.line)
                speaker = _speaker_id(beat.line.speaker, bible)
                evidence = beat.line.evidence
                label = beat.line.evidence_label
            if shot.delivery is None:
                errors.append(f"{where}: a line needs a delivery")

        try:
            keyframe = expand(f"{shot.keyframe}, [STYLE]", bible)
            motion = expand(shot.motion, bible)
            check_forbidden(f"{shot.keyframe} {shot.motion}", bible, plan.episode)
        except (KeyError, ValueError) as error:
            errors.append(f"{where}: {error}")
            continue

        resolved.append(
            ResolvedShot(
                **shot.model_dump(),
                clip=clip_number,
                start_s=round(start, 3),
                lens_mm=bible.style.lenses.get(shot.size, 50),
                speaker=speaker,
                evidence=evidence,
                evidence_label=label,
                needs_lipsync=shot.line is not None and shot.delivery == Delivery.face,
                keyframe_prompt=keyframe,
                negative_prompt=bible.style.negative,
                motion_prompt=motion,
            )
        )
        start += shot.duration

    for beat_id, (_, beat) in beats.items():
        if beat.line is None:
            continue
        rebuilt = " ".join(pieces.get(beat_id, []))
        if rebuilt != beat.line.text:
            errors.append(
                f"beat {beat_id}: shot lines rebuild {rebuilt!r}, "
                f"script says {beat.line.text!r}"
            )

    runtime = round(start, 3)
    if not MIN_RUNTIME_S <= runtime <= MAX_RUNTIME_S:
        errors.append(f"runtime {runtime} s is outside 60-90 s")
    if errors:
        raise PlanError("Shot plan problems:\n- " + "\n- ".join(errors))
    return ResolvedPlan(
        episode=plan.episode,
        fps=plan.fps,
        runtime_s=runtime,
        shots=resolved,
        cues=plan.cues,
    )
