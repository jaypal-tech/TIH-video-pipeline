"""Write each model's prompt the way its own guide recommends.

One shot or cue is described once in the plan; each model family reads it
differently. Image editors want every reference image labelled with its job and
the shot described before the style. Video models want movement only. Speech
wants speed and emotion settings, not adjectives. Translators return named
variants; pipeline.yaml picks one per endpoint with a `{name}` template.
"""

import re

from tih.bible import TOKEN, Bible, Character, Place
from tih.shots import Cue, ResolvedShot

CAMERA_MOVES = ("push", "pull", "pan", "tilt", "track", "handheld", "orbit", "dolly", "crane", "zoom")

SPEECH_RULES: tuple[tuple[str, str, float | str], ...] = (
    (r"\b(quiet\w*|low|under (his|her|their) breath|whisper\w*|soft\w*)\b", "speed", 0.95),
    (r"\b(fast|quick\w*|urgent\w*|hurried\w*)\b", "speed", 1.08),
    (r"\b(angr\w*|furious\w*)\b", "emotion", "angry"),
    (r"\b(afraid|fear\w*|frightened)\b", "emotion", "fearful"),
    (r"\b(sad\w*|grie\w*)\b", "emotion", "sad"),
)


def role(character: Character) -> str:
    """Who a character is, in a few words: the look up to its first comma."""
    return character.look.split(",")[0].strip()


def pronoun(character: Character) -> str:
    words = role(character).lower()
    if re.search(r"\b(woman|girl|lady)\b", words):
        return "She"
    if re.search(r"\b(man|boy)\b", words):
        return "He"
    return "They"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _describe(text: str, bible: Bible, labels: dict[str, str]) -> str:
    """Shot text with [CHARACTER] tokens as their labels and [PLACE] tokens as the place look."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key.lower() in bible.characters:
            return labels.get(key.lower(), f"the {role(bible.characters[key.lower()])}")
        if key in bible.places:
            return f"({bible.places[key].look})"
        if key == "STYLE":
            return bible.style.look
        raise KeyError(f"Unknown bible token [{key}]")

    return TOKEN.sub(replace, text)


def _keyframe_prompt(shot: ResolvedShot, bible: Bible, word: str, has_plate: bool) -> str:
    labels = {
        cid: f"the {role(bible.characters[cid])} from {word} {n}" for n, cid in enumerate(shot.cast, start=1)
    }
    parts = []
    if shot.cast:
        who = " and ".join(labels[cid] for cid in shot.cast)
        if len(shot.cast) == 1:
            possessive = {"He": "his", "She": "her"}.get(pronoun(bible.characters[shot.cast[0]]), "their")
            keep = f"keep {possessive} face, hair and costume exactly as in the reference"
        else:
            keep = "keep their faces, hair and costumes exactly as in the references"
        parts.append(f"Using {who}, {keep}.")
        for cid in shot.cast:
            must = bible.characters[cid].must
            if must:
                parts.append(f"Keep for {labels[cid]}: {', '.join(must)}.")
    parts.append(_sentence(_describe(shot.keyframe, bible, labels)))
    if shot.scene is not None:
        if shot.cast:
            sides = ", ".join(f"{labels[c]} stands screen-{shot.scene.sides[c]}" for c in shot.cast)
            parts.append(f"Screen positions: {sides}.")
        parts += [_sentence(shot.scene.light), _sentence(shot.scene.weather)]
    if has_plate:
        plate = len(shot.cast) + 1
        parts.append(
            f"Match the location of {word} {plate}; change nothing about the place."
        )
    parts.append(_sentence(bible.style.look))
    if bible.style.clean:
        parts.append(_sentence(bible.style.clean))
    return " ".join(parts)


def keyframe_brief(shot: ResolvedShot, bible: Bible, has_plate: bool = False) -> dict[str, str]:
    """The plate, when attached, is the last reference: after the cast, in cast order."""
    return {
        "prompt_gemini": _keyframe_prompt(shot, bible, "image", has_plate),
        "prompt_seedream": _keyframe_prompt(shot, bible, "Figure", has_plate),
    }


def plate_brief(place: Place, bible: Bible) -> dict[str, str]:
    """An empty establishing view of a place, the backdrop every shot there must match."""
    prompt = " ".join([
        _sentence(f"Empty establishing view, eye level, of {place.look}"),
        "No people in the foreground; nobody's face visible; the place exactly as it looks behind the actors.",
        _sentence(bible.style.look),
        _sentence(bible.style.clean) if bible.style.clean else "",
    ]).strip()
    return {"prompt_gemini": prompt}


def _mouth(shot: ResolvedShot, bible: Bible) -> str:
    if shot.needs_lipsync:
        who = bible.characters[shot.speaker] if shot.speaker in bible.characters else bible.characters[shot.cast[0]]
        return f"{pronoun(who)} speaks quietly, small natural mouth movement, head steady."
    if shot.cast:
        return "Mouth closed, listening."
    return ""


def _motion_body(shot: ResolvedShot, bible: Bible, labels: dict[str, str], lead: str) -> str:
    movement = _sentence(_describe(shot.motion, bible, labels))
    weather = _sentence(shot.scene.weather) if shot.scene is not None else ""
    ending = "Real-time speed, one continuous take."
    return " ".join(part for part in (lead, movement, weather, _mouth(shot, bible), ending) if part)


def motion_brief(shot: ResolvedShot, bible: Bible) -> dict[str, str]:
    """Kling gets each cast member as @ElementN, in cast order, matching the elements it is sent."""
    elements = {cid: f"@Element{n}" for n, cid in enumerate(shot.cast, start=1)}
    lead = ""
    if shot.cast:
        lead = " ".join(f"{elements[c]} is the {role(bible.characters[c])}." for c in shot.cast)
    plain = _motion_body(shot, bible, {}, "")
    moves = any(word in shot.motion.lower() for word in CAMERA_MOVES)
    return {
        "prompt_kling": _motion_body(shot, bible, elements, lead),
        "prompt_hailuo": plain if moves else f"[Static shot] {plain}",
        "negative_video": bible.style.video_negative,
    }


def speech_settings(direction: str) -> dict[str, float | str | None]:
    settings: dict[str, float | str | None] = {"speed": None, "emotion": None}
    lowered = direction.lower()
    for pattern, key, value in SPEECH_RULES:
        if settings[key] is None and re.search(pattern, lowered):
            settings[key] = value
    return settings


def voice_design_brief(character: Character) -> str:
    voice = (character.voice or "").strip().rstrip(".")
    return f"{voice}. {role(character)}. Clean close-mic studio recording, no music, no reverb."


def cue_brief(cue: Cue, bible: Bible) -> str:
    parts = [cue.prompt.strip().rstrip(".")]
    if bible.style.sound_period:
        parts.append(bible.style.sound_period)
    if cue.kind == "music":
        parts.append("instrumental only")
    return ", ".join(parts)
