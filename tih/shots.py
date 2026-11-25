"""Shot plans: one YAML file per episode that says what to film.

The Director writes each episode's plan to runs/<series>/epNN/director/shots.yaml;
series/<series>/shots/example.yaml is a hand-made plan the Director follows for
style. `resolve_plan` checks a plan
against the locked script and the bible, then returns shots with their start
times, exact line text, speaker and fully expanded prompts. Every generation
stage reads the resolved plan, never the raw YAML.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tih.bible import Bible, check_forbidden, expand, series_dir, tokens_in
from tih.paths import RunPaths
from tih.story.models import Delivery, Episode, Evidence

OverlayKind = Literal["corner_bug", "time_super", "name_card", "super", "true_card"]


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
    kind: Literal["music", "sfx", "ambience"]
    at: float
    duration: float
    gain_db: float = -18.0
    prompt: str
    scene: int | None = None  # ambience only: the clip whose bed this is


Side = Literal["left", "center", "right"]
SHOT_SECONDS = (1.0, 8.0)
OPENING_SIZES = ("WIDE", "MEDIUM")
MAX_SAME_FACE_RUN = 4


class Placement(BaseModel):
    """One cast member's side of the frame, as Claude writes it."""

    model_config = ConfigDict(extra="forbid")

    who: str
    side: Side


class SceneRule(BaseModel):
    """What holds for a whole clip: who stands where on screen, the light, the weather."""

    model_config = ConfigDict(extra="forbid")

    clip: int
    sides: dict[str, Side]
    light: str
    weather: str

    # Claude's answer format cannot hold a free-form map, so it writes rows; plan files keep the map
    @field_validator("sides", mode="before", json_schema_input_type=list[Placement])
    @classmethod
    def _rows_to_map(cls, value: object) -> object:
        if isinstance(value, list):
            rows = [Placement.model_validate(row) for row in value]
            return {row.who: row.side for row in rows}
        return value


class Opening(BaseModel):
    """The silent 5-second establishing shot under the title; not one of the numbered shots."""

    model_config = ConfigDict(extra="forbid")

    place: str  # a bible place id
    keyframe: str  # what the camera sees: who is there and what they are doing
    motion: str  # how the people and the camera move
    sound: str  # the specific sounds heard, comma-separated


class ShotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: int
    fps: Literal[24, 30] = 24
    scenes: list[SceneRule] = Field(default_factory=list)
    shots: list[PlannedShot]
    cues: list[Cue] = Field(default_factory=list)
    opening: Opening | None = None


class ResolvedShot(PlannedShot):
    clip: int
    start_s: float
    lens_mm: int
    speaker: str | None = None
    evidence: Evidence | None = None
    evidence_label: str | None = None
    needs_lipsync: bool = False
    scene: SceneRule | None = None
    direction: str = ""  # the script line's acting direction; sets speech speed and emotion
    keyframe_prompt: str
    negative_prompt: str
    motion_prompt: str


class ResolvedPlan(BaseModel):
    episode: int
    fps: int
    runtime_s: float
    scenes: list[SceneRule] = Field(default_factory=list)
    shots: list[ResolvedShot]
    cues: list[Cue]
    opening: Opening | None = None


class PlanError(ValueError):
    """The shot plan does not match the script or the bible."""


def plan_path(paths: RunPaths) -> Path:
    """Where the Director writes this episode's shot plan."""
    return paths.scope_dir / "director" / "shots.yaml"


def example_plan_path(series: str) -> Path:
    """A hand-made plan the Director follows for style; optional."""
    return series_dir(series) / "shots" / "example.yaml"


def load_plan(path: Path) -> ShotPlan:
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


def resolve_plan(
    plan: ShotPlan, episode: Episode, bible: Bible, runtime_s: tuple[int, int] = (60, 90)
) -> ResolvedPlan:
    """Check the plan against the script and bible; return ready-to-run shots."""
    errors: list[str] = []
    if plan.episode != episode.number:
        errors.append(f"plan is for episode {plan.episode}, script is episode {episode.number}")
    beats = {beat.id: (clip.number, beat) for clip in episode.clips for beat in clip.beats}
    order = {beat_id: index for index, beat_id in enumerate(beats)}
    covered: set[str] = set()
    last_order = -1
    script_overlays = {
        overlay.text for _, beat in beats.values() for overlay in beat.overlays
    }
    pieces: dict[str, list[str]] = {}
    resolved: list[ResolvedShot] = []
    start = 0.0
    seen_ids: set[str] = set()
    rules = {rule.clip: rule for rule in plan.scenes}
    side_errors: set[str] = set()
    previous: PlannedShot | None = None
    if plan.opening is not None and plan.opening.place not in bible.places:
        errors.append(f"opening: unknown place {plan.opening.place}")

    for shot in plan.shots:
        where = f"shot {shot.id}"
        if shot.id in seen_ids:
            errors.append(f"{where}: duplicate id")
        seen_ids.add(shot.id)
        if shot.beat not in beats:
            errors.append(f"{where}: unknown beat {shot.beat}")
            continue
        clip_number, beat = beats[shot.beat]
        covered.add(beat.id)
        rule = rules.get(clip_number)
        if rule is None:
            side_errors.add(f"clip {clip_number} has no scene rule")
        else:
            side_errors.update(
                f"{c} has no side in clip {clip_number}" for c in shot.cast if c not in rule.sides
            )
        low_s, high_s = SHOT_SECONDS
        if not low_s <= shot.duration <= high_s:
            errors.append(f"{where} is {shot.duration} s; shots run {low_s}-{high_s} s")
        if (previous is not None and shot.cast and previous.size == shot.size
                and sorted(previous.cast) == sorted(shot.cast)):
            errors.append(f"shots {previous.id} and {shot.id} are a jump cut (same size and cast)")
        previous = shot
        if order[beat.id] < last_order:
            errors.append(f"{where}: beat {beat.id} is out of order")
        last_order = max(last_order, order[beat.id])
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
        beat_overlays = {overlay.text for overlay in beat.overlays}
        for overlay in shot.overlays:
            if overlay.text not in script_overlays:
                errors.append(f"{where}: overlay text is not in the script: {overlay.text}")
            elif overlay.text not in beat_overlays:
                errors.append(f"{where}: overlay is not on beat {beat.id}: {overlay.text}")

        speaker = evidence = label = None
        direction = ""
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
                direction = beat.line.direction
            if shot.delivery is None:
                errors.append(f"{where}: a line needs a delivery")

        try:
            keyframe = expand(f"{shot.keyframe}, [STYLE]", bible)
            motion = expand(shot.motion, bible)
            check_forbidden(f"{shot.keyframe} {shot.motion}", bible, episode.number)
        except (KeyError, ValueError) as error:
            errors.append(f"{where}: {error}")
            continue

        if shot.line is not None and shot.delivery == Delivery.face and len(shot.cast) != 1:
            errors.append(f"{where}: lip-sync shot needs exactly one face (cast {shot.cast})")

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
                scene=rule,
                direction=direction,
                keyframe_prompt=keyframe,
                negative_prompt=bible.style.negative,
                motion_prompt=motion,
            )
        )
        start += shot.duration

    errors += sorted(side_errors)
    bedded = {cue.scene for cue in plan.cues if cue.kind == "ambience"}
    for cue in plan.cues:
        if cue.kind == "ambience" and cue.scene is None:
            errors.append(f"cue {cue.id}: an ambience cue needs its clip as scene")
    for clip_number in dict.fromkeys(beats[s.beat][0] for s in plan.shots if s.beat in beats):
        if clip_number not in bedded:
            errors.append(f"clip {clip_number} has no ambience")
    for beat_id in beats:
        if beat_id not in covered:
            errors.append(f"beat {beat_id} has no shot")
    for beat_id, (_, beat) in beats.items():
        if beat.line is None:
            continue
        rebuilt = " ".join(pieces.get(beat_id, []))
        if rebuilt != beat.line.text:
            errors.append(
                f"beat {beat_id}: shot lines rebuild {rebuilt!r}, "
                f"script says {beat.line.text!r}"
            )

    spans: dict[int, list[float]] = {}
    for shot in resolved:
        span = spans.setdefault(shot.clip, [shot.start_s, shot.start_s])
        span[1] = round(shot.start_s + shot.duration, 3)
    for cue in plan.cues:
        if cue.kind == "ambience" and cue.scene in spans:
            clip_start, clip_end = spans[cue.scene]
            length = round(clip_end - clip_start, 3)
            if abs(cue.at - clip_start) > 0.01 or abs(cue.duration - length) > 0.01:
                errors.append(f"ambience {cue.id} must start at {clip_start} s and run {length} s (its clip)")

    runtime = round(start, 3)
    low, high = runtime_s
    if not low <= runtime <= high:
        errors.append(f"runtime {runtime} s is outside {low}-{high} s")
    if errors:
        raise PlanError("Shot plan problems:\n- " + "\n- ".join(errors))
    return ResolvedPlan(
        episode=plan.episode,
        fps=plan.fps,
        runtime_s=runtime,
        scenes=plan.scenes,
        shots=resolved,
        cues=plan.cues,
        opening=plan.opening,
    )


def continuity_notes(plan: ResolvedPlan) -> list[str]:
    """Coverage advice the Director should weigh; not errors."""
    notes = []
    opened: set[tuple[int, str]] = set()
    for index, shot in enumerate(plan.shots):
        if (shot.clip, shot.place) in opened:
            continue
        opened.add((shot.clip, shot.place))
        first = [s for s in plan.shots[index:] if s.clip == shot.clip and s.place == shot.place][:2]
        if not any(s.size in OPENING_SIZES for s in first):
            notes.append(
                f"clip {shot.clip}: {shot.place} opens on {', '.join(s.size for s in first)}; "
                f"open a place on a WIDE or MEDIUM within its first 2 shots"
            )
    run: list[ResolvedShot] = []
    for shot in plan.shots + [None]:
        if shot is not None and len(shot.cast) == 1 and run and run[-1].cast == shot.cast:
            run.append(shot)
            continue
        if len(run) > MAX_SAME_FACE_RUN:
            notes.append(f"shots {run[0].id}-{run[-1].id}: {len(run)} shots in a row on {run[0].cast[0]}; cut to a reaction")
        run = [shot] if shot is not None and len(shot.cast) == 1 else []
    return notes


def render_plan(plan: ResolvedPlan) -> str:
    """The shot plan as a readable table for the owner."""
    rows = ["| Shot | Start | Dur | Size | Cast | Line | Lip-sync |", "|---|---|---|---|---|---|---|"]
    for shot in plan.shots:
        line = f"{shot.speaker}: {shot.line}" if shot.line else ""
        rows.append(
            f"| {shot.id} | {shot.start_s:.1f} | {shot.duration:.1f} | {shot.size} "
            f"{shot.lens_mm}mm | {', '.join(shot.cast)} | {line} | "
            f"{'yes' if shot.needs_lipsync else ''} |"
        )
    lipsync = sum(shot.needs_lipsync for shot in plan.shots)
    head = (
        f"# Episode {plan.episode} shot plan\n\n"
        f"{len(plan.shots)} shots, {plan.runtime_s:.1f} s, {lipsync} lip-sync shots, "
        f"{len(plan.cues)} sound cues.\n\n"
    )
    notes = continuity_notes(plan)
    tail = "\n## Continuity notes\n\n" + "\n".join(f"- {note}" for note in notes) + "\n" if notes else ""
    return head + "\n".join(rows) + "\n" + tail
