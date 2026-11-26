"""Series bible and locks: the single source for every look, voice and place.

bible.yaml describes characters and places in words. locks.yaml records what
the owner chose for each character (reference image, seed, voice id), with a
hash so a swapped file is caught. Generation stages build prompts only from
these two files, which is what keeps faces and voices the same across shots
and episodes.
"""

import hashlib
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

TOKEN = re.compile(r"\[([A-Za-z0-9_]+)\]")
NARRATOR_ID = "narrator"  # the narrator's key among the speakers and the voice locks


class Grade(BaseModel):
    """One look over the whole cut: an ffmpeg eq chain and a film-grain strength."""

    model_config = ConfigDict(extra="forbid")

    eq: str = ""
    balance: str = ""  # ffmpeg colorbalance settings, e.g. warm highlights toward the brand gold
    grain: int = 0


class Style(BaseModel):
    model_config = ConfigDict(extra="forbid")

    look: str
    negative: str
    reference_sheet: str
    lenses: dict[str, int]
    clean: str = ""  # positive "keep the frame clean" text; image models ignore negatives
    video_negative: str = ""
    sound_period: str = ""
    grade: Grade = Field(default_factory=Grade)
    scene_look: str = ""  # one style sentence for whole-scene video prompts


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    name_card: str
    episodes: list[int]
    look: str
    voice: str | None = None
    must: list[str] = Field(default_factory=list)
    designed_face: bool = False
    face_reference: bool = True  # false: never pass this face to a video model as a reference image
    screen_name: str | None = None  # what a scene prompt calls them, e.g. THE ACTOR
    scene_look: str | None = None  # how a scene prompt describes them
    scene_voice: str | None = None  # how a scene prompt describes their voice


class Narrator(BaseModel):
    """The voice that sets up and closes each episode; never seen."""

    model_config = ConfigDict(extra="forbid")

    voice: str
    direction: str = ""  # how the lines are read, in the words a line's direction uses (e.g. quick)
    tempo: float = Field(default=1.0, ge=1.0, le=1.3)  # the recorded lines are played this much faster, same pitch


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    look: str
    plate: bool = True  # false: no empty-place plate is generated for this view
    scene_look: str | None = None  # how a scene prompt describes the place


class Forbidden(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all: list[str] = Field(default_factory=list)
    by_episode: dict[int, list[str]] = Field(default_factory=dict)

    def for_episode(self, episode: int) -> list[str]:
        return [*self.all, *self.by_episode.get(episode, [])]


class OverlayStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font: str
    font_bold: str
    font_italic: str
    caption_size: int = 54
    tag_size: int = 30
    name_card_size: int = 34
    card_size: int = 58
    caption_y: int = 1380
    # brand: every default keeps plain white text on black, so a series opts in
    label_font: str | None = None  # small uppercase labels: names, tags, kickers; falls back to font_bold
    label_tracking: int = 0  # hair spaces between label letters, like the app's spaced labels
    text_color: str = "white"
    accent_color: str = "white"
    card_color: str = "black"
    brand_name: str = ""  # shown small in the corner, and under the closing card
    brand_tagline: str = ""
    time_kicker: str = ""  # a label above each time super, e.g. "On this day"


class Bible(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: Style
    naming_policy: str = "appearance"
    characters: dict[str, Character]
    narrator: Narrator | None = None
    places: dict[str, Place]
    props: dict[str, str] = Field(default_factory=dict)
    motifs: dict[str, str] = Field(default_factory=dict)
    forbidden: Forbidden = Field(default_factory=Forbidden)
    overlays: OverlayStyle


class CharacterLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str | None = None
    reference_sha256: str | None = None
    seed: int | None = None
    voice_id: str | None = None
    voice_endpoint: str | None = None


class PlaceLock(BaseModel):
    """One approved empty view of a place, attached to every keyframe set there."""

    model_config = ConfigDict(extra="forbid")

    reference: str
    reference_sha256: str | None = None


class Locks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    characters: dict[str, CharacterLock] = Field(default_factory=dict)
    places: dict[str, PlaceLock] = Field(default_factory=dict)


class CostumeReference(BaseModel):
    """A picture of one character's clothes alone (no face), for a video model that refuses faces."""

    model_config = ConfigDict(extra="forbid")

    image: str
    wears: str  # what the picture shows, as the prompt names it: "his flat-crowned hat, ..."


class PlaceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    shows: str  # what to take from the picture: "the crowd, umbrellas, torches, mud, smoke and light"


class References(BaseModel):
    """Costume and place pictures sent to Seedance's reference flow; faces stay in words."""

    model_config = ConfigDict(extra="forbid")

    characters: dict[str, CostumeReference] = Field(default_factory=dict)
    places: dict[str, PlaceReference] = Field(default_factory=dict)


class NotLocked(RuntimeError):
    """A character needed by a shot has no locked look or voice yet."""


class ForbiddenInPrompt(ValueError):
    """A prompt mentions something the bible bans for this episode."""


def series_dir(series: str) -> Path:
    return Path("series") / series


def load_bible(series: str) -> Bible:
    path = series_dir(series) / "bible.yaml"
    return Bible.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


LOCKS_OVERRIDE = "TIH_LOCKS_FILE"


def locks_path(series: str) -> Path:
    """series/<series>/locks.yaml, unless a rehearsal points elsewhere."""
    override = os.environ.get(LOCKS_OVERRIDE)
    return Path(override) if override else series_dir(series) / "locks.yaml"


def load_locks(series: str) -> Locks:
    path = locks_path(series)
    if not path.exists():
        return Locks()
    return Locks.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def load_references(series: str) -> References:
    path = series_dir(series) / "references.yaml"
    if not path.exists():
        return References()
    return References.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def save_locks(series: str, locks: Locks) -> None:
    data = locks.model_dump(mode="json", exclude_none=True)
    locks_path(series).write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8"
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def character_text(bible: Bible, character_id: str) -> str:
    """The words a prompt uses for a character, following the naming policy."""
    character = bible.characters[character_id]
    if bible.naming_policy == "real_names":
        return f"{character.name}, {character.look}"
    return character.look


def expand(text: str, bible: Bible) -> str:
    """Replace [CHARACTER_ID], [PLACE_ID], [STYLE] and [NEG] tokens with bible text."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key.lower() in bible.characters:
            return f"({character_text(bible, key.lower())})"
        if key in bible.places:
            return f"({bible.places[key].look})"
        if key == "STYLE":
            return bible.style.look
        if key == "NEG":
            return bible.style.negative
        raise KeyError(f"Unknown bible token [{key}]")

    return TOKEN.sub(replace, text)


def tokens_in(text: str) -> list[str]:
    return TOKEN.findall(text)


def check_forbidden(prompt: str, bible: Bible, episode: int) -> None:
    lowered = prompt.lower()
    found = [word for word in bible.forbidden.for_episode(episode) if word in lowered]
    if found:
        raise ForbiddenInPrompt(f"Prompt mentions {', '.join(found)}")


def require_look(locks: Locks, character_id: str) -> CharacterLock:
    """Return the locked look, checking the reference file is the one approved."""
    lock = locks.characters.get(character_id)
    if not lock or not lock.reference:
        raise NotLocked(f"No locked reference image for {character_id}. Run tih lock look.")
    reference = Path(lock.reference)
    if not reference.exists():
        raise NotLocked(f"Locked reference for {character_id} is missing: {reference}")
    if lock.reference_sha256 and file_sha256(reference) != lock.reference_sha256:
        raise NotLocked(f"Reference for {character_id} changed after it was locked")
    return lock


def require_plate(locks: Locks, place_id: str) -> PlaceLock:
    """Return the locked plate, checking the file is the one approved."""
    lock = locks.places.get(place_id)
    if not lock:
        raise NotLocked(f"No locked plate for place {place_id}. Run tih run <ep> --stage plates, then tih lock place.")
    reference = Path(lock.reference)
    if not reference.exists():
        raise NotLocked(f"Locked plate for {place_id} is missing: {reference}")
    if lock.reference_sha256 and file_sha256(reference) != lock.reference_sha256:
        raise NotLocked(f"Plate for {place_id} changed after it was locked")
    return lock


def require_voice(locks: Locks, character_id: str) -> CharacterLock:
    lock = locks.characters.get(character_id)
    if not lock or not lock.voice_id:
        raise NotLocked(f"No locked voice id for {character_id}. Run tih lock voice.")
    return lock
