"""Write each model's prompt the way its own guide recommends.

One shot or cue is described once in the plan; each model family reads it
differently. Image editors want every reference image labelled with its job and
the shot described before the style. Video models want movement only. Speech
wants speed and emotion settings, not adjectives. Translators return named
variants; pipeline.yaml picks one per endpoint with a `{name}` template.
"""

import re
from typing import TYPE_CHECKING, Callable

from tih.bible import TOKEN, Bible, Character, Narrator, Place
from tih.shots import Cue, Opening, ResolvedShot, SceneRule

if TYPE_CHECKING:  # tih.scenes imports tih.stages.voice, which imports this module
    from tih.scenes import SceneTake

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


def narrator_voice_brief(narrator: Narrator) -> str:
    return f"{narrator.voice.strip().rstrip('.')}. Clean close-mic studio recording, no music, no reverb."


VOICE_PREVIEW_LIMIT = 500  # MiniMax voice design's preview_text maximum


def voice_preview(lines: list[str], limit: int = VOICE_PREVIEW_LIMIT) -> str:
    """The character's own lines, whole and in order, up to the limit: longer samples design steadier voices."""
    preview = ""
    for line in lines:
        joined = f"{preview} {line.strip()}".strip()
        if len(joined) > limit:
            break
        preview = joined
    if not preview and lines:
        preview = lines[0].strip()[:limit].rsplit(" ", 1)[0] if len(lines[0].strip()) > limit else lines[0].strip()
    return preview


def cue_brief(cue: Cue, bible: Bible) -> str:
    parts = [cue.prompt.strip().rstrip(".")]
    if bible.style.sound_period:
        parts.append(bible.style.sound_period)
    if cue.kind == "music":
        parts.append("instrumental only")
    return ", ".join(parts)


SHOT_HEADER = re.compile(r"Shot \d+ \((\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)s")
SPOKEN = re.compile(r'says(?: exactly)?: "([^"]+)"')

SIZE_WORDS = {
    "ECU": "extreme close-up",
    "CU": "close-up",
    "MCU": "medium close-up from the chest up",
    "MEDIUM": "medium shot",
    "TWO": "medium two-shot from the waist up",
    "WIDE": "wide shot",
    "INSERT": "close-up insert",
    "POV": "point-of-view shot",
}
COUNT_WORDS = {2: "Two", 3: "Three", 4: "Four"}
VEO_MAX_WORDS = 250
SCORE_WORDS = ("strings", "sting", "score", "pulse", "drone", "music", "piano", "choir", "riser")
NEGATIVE_VEO = "Negative: no subtitles, no captions, no text overlays, no music."
OPENING_LENS = 28
OPENING_AUDIO = "Audio: the place's own background sound. No music. No speech."


def _plain(text: str) -> str:
    """Description text without double quotes (inch marks); only a spoken line is quoted."""
    return text.replace('"', "").strip()


def _seconds(value: float) -> str:
    return f"{round(value, 2):g}"


def _unmatched_must(character: Character) -> list[str]:
    """The `must` items the look does not already say."""
    return [item for item in character.must if item.lower() not in character.look.lower()]


def _description(character: Character) -> str:
    return _plain(", ".join([character.look, *_unmatched_must(character)]))


def _scene_text(character: Character) -> str:
    return _plain(character.scene_look) if character.scene_look else _description(character)


def _screen_name(character: Character) -> str:
    return character.screen_name or f"the {role(character)}"


def _tokens(text: str, bible: Bible, character_label: Callable[[str], str]) -> str:
    """Shot text with [CHARACTER] tokens as character_label(id); place tokens are already gone."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key.lower() in bible.characters:
            return character_label(key.lower())
        if key == "STYLE":
            return _plain(bible.style.look)
        raise KeyError(f"Unknown bible token [{key}]")

    return _sentence(_plain(TOKEN.sub(replace, text)))


def _setting_words(text: str, bible: Bible, elsewhere: str) -> str:
    """Place tokens in shot text: "in/at/on [PLACE]" goes away, "inside [PLACE]" names the crowd or the setting."""
    places = "|".join(re.escape(pid) for pid in bible.places)

    def inside(match: re.Match[str]) -> str:
        crowd = "crowd" in bible.places[match.group(2)].name.lower()
        return f"{match.group(1)}{'the crowd' if crowd else 'this setting'}"

    text = re.sub(rf"(\binside )\[({places})\]", inside, text, flags=re.IGNORECASE)
    text = re.sub(rf"\s+(?:in|at|on) \[(?:{places})\]", "", text, flags=re.IGNORECASE)
    text = re.sub(rf"\[(?:{places})\]", elsewhere, text)
    return re.sub(r" +,", ",", re.sub(r" {2,}", " ", text))


def _sounds(shots: list[ResolvedShot]) -> str:
    """The shots' sound texts as one list: no repeats, no score."""
    return _sound_list([shot.sound for shot in shots])


def _sound_list(texts: list[str]) -> str:
    """Comma-separated sound texts as one list: no repeats, no score."""
    kept: list[str] = []
    for text in texts:
        for item in _plain(text).rstrip(".").split(","):
            item = item.strip()
            words = set(re.findall(r"\w+", item.lower()))
            if item and not words & set(SCORE_WORDS) and item.lower() not in (k.lower() for k in kept):
                kept.append(item)
    return ", ".join(kept)


def _off_screen(shot: ResolvedShot, bible: Bible) -> bool:
    return bool(shot.line) and shot.speaker in bible.characters and shot.speaker not in shot.cast


def _speaker(shot: ResolvedShot, bible: Bible) -> Character:
    return bible.characters[shot.speaker if shot.speaker in bible.characters else shot.cast[0]]


def _seedance_shot(
    number: int, start: float, end: float, shot: ResolvedShot, bible: Bible, setting: str | None = None
) -> str:
    """One shot's paragraph; `setting` opens it when the take's shots are not all in one place."""
    def name(cid: str) -> str:
        return _screen_name(bible.characters[cid])

    header = f"Shot {number} ({_seconds(start)}-{_seconds(end)}s, {SIZE_WORDS[shot.size]}, {shot.lens_mm}mm lens):"
    if setting is not None:
        header += f" Setting: {_sentence(setting)}"
    who = [f"{name(cid)} - {_scene_text(bible.characters[cid])}." for cid in shot.cast]
    action = [_tokens(_setting_words(text, bible, "this setting"), bible, name) for text in (shot.keyframe, shot.motion)]
    if shot.spoken:
        speaker = _screen_name(_speaker(shot, bible))
        if _off_screen(shot, bible):
            ending = f'Off-screen, the voice of {speaker} says: "{shot.line}"'
        else:
            ending = f'{speaker.title()} says: "{shot.line}"'
    else:
        ending = "Nobody speaks."
    return " ".join([header, *who, *action, ending])


def _seedance_setting(place: Place, rule: SceneRule | None) -> str:
    """A place as the video model should see it: its film wording, else its look, weather and light."""
    if place.scene_look:
        return _plain(place.scene_look)
    return " ".join(part for part in (
        _sentence(_plain(place.look)),
        _sentence(_plain(rule.weather)) if rule else "",
        _sentence(_plain(rule.light)) if rule else "",
    ) if part)


def _seedance_scene(take: "SceneTake", bible: Bible) -> str:
    shots = take.shots
    style = _plain(bible.style.scene_look or bible.style.look)
    count = f"{COUNT_WORDS[len(shots)]} shots with hard cuts between them, " if len(shots) > 1 else "One continuous shot, no cuts, "
    one_place = len({shot.place for shot in shots}) <= 1
    paragraphs = [count + style]
    if one_place:
        paragraphs.append("Setting: " + _seedance_setting(bible.places[take.place], shots[0].scene))
    clock = 0.0
    for number, shot in enumerate(shots, start=1):
        start, clock = clock, clock + shot.duration
        setting = None if one_place else _seedance_setting(bible.places[shot.place], shot.scene)
        paragraphs.append(_seedance_shot(number, start, clock, shot, bible, setting))
    voices = []
    for shot in shots:
        if shot.spoken:
            speaker = _speaker(shot, bible)
            sentence = f"{_screen_name(speaker).title()} is {_plain(speaker.scene_voice or speaker.voice or '').rstrip('.')}."
            if sentence not in voices:
                voices.append(sentence)
    if voices:
        paragraphs.append("Voices: " + " ".join(voices))
    if len(shots) > 1:
        per_shot = [(number, _sound_list([shot.sound])) for number, shot in enumerate(shots, start=1)]
        listed = " ".join(f"Shot {number}: {sounds}." for number, sounds in per_shot if sounds)
        paragraphs.append(f"Audio: {listed} No music score." if listed else "Audio: No music score.")
    else:
        sounds = _sounds(shots)
        paragraphs.append(f"Audio: {sounds}. No music score." if sounds else "Audio: No music score.")
    return "\n\n".join(paragraphs)


def _dolly(text: str) -> str:
    text = re.sub(r"\bpush[- ]in\b", "dolly in", text, flags=re.IGNORECASE)
    return re.sub(r"\bpull back\b", "dolly out", text, flags=re.IGNORECASE)


def _veo_scene(take: "SceneTake", bible: Bible, plate: bool) -> str:
    shot = take.shots[0]
    numbers = {cid: n for n, cid in enumerate((c for c in shot.cast if bible.characters[c].face_reference), start=1)}

    def who(cid: str) -> str:
        return f"the {_plain(role(bible.characters[cid]))}"

    def identity(cid: str) -> str:
        if cid in numbers:
            return f"{who(cid)} (reference image {numbers[cid]})"
        character = bible.characters[cid]
        detail = _description(character).removeprefix(_plain(role(character))).lstrip(", ")
        return f"{who(cid)} - {detail}" if detail else who(cid)

    lines = []
    references = [f"keep the {role(bible.characters[cid])} exactly as in reference image {n}" for cid, n in numbers.items()]
    if plate:
        location = f"Reference image {len(numbers) + 1} is the location, keep it exactly as shown."
    if numbers or plate:
        lines.append("References: " + " ".join(part for part in (
            ", ".join(references) + "." if numbers else "", location if plate else "") if part))
    if shot.cast:
        lines.append("Subject: " + "; ".join(identity(cid) for cid in shot.cast) + ".")
    elsewhere = "the location" if plate else "this setting"
    action = " ".join(
        _tokens(_setting_words(_dolly(text), bible, elsewhere), bible, who) for text in (shot.keyframe, shot.motion)
    )
    lines.append("Action: " + action)
    rule = shot.scene
    scene = [] if plate else [_sentence(_plain(bible.places[shot.place].look))]
    if rule:
        scene.append(_sentence(_plain(rule.weather)))
    if scene:
        lines.append("Scene: " + " ".join(scene))
    lines.append(f"Camera: {SIZE_WORDS[shot.size]}, {shot.lens_mm}mm lens.")
    if rule:
        lines.append("Lighting: " + _sentence(_plain(rule.light)))
    style = [part.strip() for part in _plain(bible.style.look).rstrip(".").split(",")]
    if shot.spoken:
        speaker = _speaker(shot, bible)
        voice = _plain(speaker.voice or "").rstrip(".")
        voice_sentence = f" Voice: {voice[0].upper()}{voice[1:]}." if voice else ""
        if _off_screen(shot, bible):
            heard = f'An off-screen voice says exactly: "{shot.line}" Nobody on screen speaks.'
        else:
            heard = f'{pronoun(speaker)} says exactly: "{shot.line}" Nobody else speaks.'
        lines.append(f"Dialogue: {heard}{voice_sentence}")
    else:
        lines.append("Dialogue: Nobody speaks.")
    sounds = _sounds([shot])
    if sounds:
        lines.append("Audio: " + _sentence(sounds))
    lines.append(NEGATIVE_VEO)
    return _within_limit(lines, style)


def _within_limit(lines: list[str], style: list[str]) -> str:
    """Join the lines with the Style line before Dialogue; over the cap, drop the Style tail, then the Scene line."""
    cut = next(i for i, line in enumerate(lines) if line.startswith(("Dialogue:", "Audio:", "Negative:")))

    def joined() -> str:
        return "\n".join([*lines[:cut], "Style: " + ", ".join(style) + ".", *lines[cut:]])

    while len(joined().split()) > VEO_MAX_WORDS and len(style) > 1:
        style.pop()
    if len(joined().split()) > VEO_MAX_WORDS:
        lines = [line for line in lines if not line.startswith("Scene:")]
        cut = next(i for i, line in enumerate(lines) if line.startswith(("Dialogue:", "Audio:", "Negative:")))
    return joined()


def scene_brief(take: "SceneTake", bible: Bible, plate: bool) -> dict[str, str]:
    """Both video models' prompts for one take; Seedance reads every shot, Veo reads the single shot."""
    return {
        "prompt_seedance": _seedance_scene(take, bible),
        "prompt_veo": _veo_scene(take, bible, plate) if len(take.shots) == 1 else "",
    }


def _opening_audio(opening: Opening, music: str) -> str:
    sounds = _sound_list([opening.sound])
    return f"Audio: {sounds}. No {music}. No speech." if sounds else f"Audio: No {music}. No speech."


def opening_brief(place: Place, bible: Bible, plate: bool, opening: Opening | None = None) -> dict[str, str]:
    """A silent wide establishing shot of the place, the episode's opening; the Director's shot when it wrote one."""
    setting = _plain(place.scene_look) if place.scene_look else _plain(place.look)

    def action(texts: tuple[str, str], elsewhere: str, label: Callable[[str], str]) -> str:
        return " ".join(_tokens(_setting_words(text, bible, elsewhere), bible, label) for text in texts)

    if opening is None:
        shot = f"Shot 1 (wide establishing shot, {OPENING_LENS}mm lens, deep focus): a very slow push forward. Nobody speaks."
        audio = OPENING_AUDIO
    else:
        seen = action((opening.keyframe, opening.motion), "this setting",
                      lambda cid: _screen_name(bible.characters[cid]))
        shot = f"Shot 1 (0-5s, wide establishing shot, {OPENING_LENS}mm lens, deep focus): {seen} Nobody speaks."
        audio = _opening_audio(opening, "music score")
    seedance = "\n\n".join([
        "One continuous shot, no cuts, " + _plain(bible.style.scene_look or bible.style.look),
        "Setting for every shot: " + setting,
        shot,
        audio,
    ])
    if opening is None:
        veo_shot = f"One continuous wide establishing shot of {_plain(place.look)}, {OPENING_LENS}mm lens, deep focus, very slow push forward."
        veo_audio = OPENING_AUDIO
    else:
        veo_shot = " ".join([
            f"One continuous wide establishing shot of {_plain(place.look)}, {OPENING_LENS}mm lens, deep focus.",
            action((opening.keyframe, _dolly(opening.motion)), "the location" if plate else "this setting",
                   lambda cid: f"the {_plain(role(bible.characters[cid]))}"),
        ])
        veo_audio = _opening_audio(opening, "music")
    veo = [
        _sentence(_plain(bible.style.look)),
        "No text, no subtitles, no captions.",
        veo_shot,
        "Nobody speaks.",
        veo_audio,
    ]
    if plate:
        veo.insert(2, "Reference image 1 is the location: keep it exactly as shown.")
    return {"prompt_seedance": seedance, "prompt_veo": " ".join(veo)}
