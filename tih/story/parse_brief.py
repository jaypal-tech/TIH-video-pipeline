import re
from typing import Any

from tih.story.models import (
    Beat,
    Canon,
    Clip,
    Delivery,
    Episode,
    Evidence,
    Line,
    OutlineEntry,
    Overlay,
    Provenance,
    StoryFunction,
)

SCRIPTS_START = "3. The five scripts"
SCRIPTS_END = "4. What's real, compressed and dramatized (per episode)"
EPISODE = re.compile(r'^EP (\d+): "(.+?)"')
RUNTIME = re.compile(r"^Runtime: (\d+:\d\d) ·")
OPENING = re.compile(r'^Super, 0:00–0:01[^:]*: corner bug "(.+?)", plus small type[^:]*: "(.+?)"')
CLIP = re.compile(r"^Clip (\d+) · (.+) · (\d+:\d\d)–(\d+:\d\d)$")
TIME = re.compile(r"^\d+:\d\d$")
NEXT_OPENS = re.compile(r"^Where ep \d+ opens: (.+)$")
SPOKEN = re.compile(r'^([A-Z]+) \(([^)]+)\): "(.*)"(?: (REAL WORDS|REPORTED) · (.+))?$')
NAME_CARD = re.compile(r"\s*NAME CARD: (.+)$")
TRUE_CARD = re.compile(r'\s*TRUE card: "(.+?)"\.?')
SUPER = re.compile(r'\s*(?:Overlay super|Super)(?:, lower third)?, (\d+) s: "(.+?)"\.?')
EVIDENCE = {"REAL WORDS": Evidence.real_words, "REPORTED": Evidence.reported}


def _seconds(value: str) -> int:
    minutes, seconds = value.split(":")
    return int(minutes) * 60 + int(seconds)


def _parse_line(text: str) -> Line:
    match = SPOKEN.match(text)
    if not match:
        raise ValueError(f"Unreadable script line: {text}")
    speaker, how, quote, tag, detail = match.groups()
    delivery, _, direction = how.partition(", ")
    return Line(
        speaker=speaker,
        text=quote,
        delivery=Delivery(delivery),
        evidence=EVIDENCE.get(tag, Evidence.dramatized),
        evidence_label=f"{tag} · {detail}" if tag else None,
        direction=direction,
    )


def _split_picture(raw: str, start_s: int) -> tuple[str, list[Overlay]]:
    overlays = []
    picture = raw
    for match in SUPER.finditer(raw):
        overlays.append(
            Overlay(
                kind="super",
                text=match.group(2),
                start_s=start_s,
                duration_s=int(match.group(1)),
            )
        )
    picture = SUPER.sub("", picture)
    match = TRUE_CARD.search(picture)
    if match:
        overlays.append(Overlay(kind="true_card", text=match.group(1), start_s=start_s))
        picture = TRUE_CARD.sub("", picture)
    match = NAME_CARD.search(picture)
    if match:
        overlays.append(Overlay(kind="name_card", text=match.group(1), start_s=start_s))
        picture = NAME_CARD.sub("", picture)
    return picture.strip(), overlays


def _parse_clip(
    lines: list[str], index: int, episode: int
) -> tuple[Clip, int]:
    number, place, start, end = CLIP.match(lines[index]).groups()
    end_s = _seconds(end)
    index += 1
    if lines[index : index + 3] != ["t", "PIC", "LINE"]:
        raise ValueError(f"Clip {number} of episode {episode} has no table header")
    index += 3
    beats: list[Beat] = []
    while index < len(lines) and TIME.match(lines[index]):
        start_s = _seconds(lines[index])
        if lines[index + 1] == "END":
            index += 2
            break
        picture, overlays = _split_picture(lines[index + 1], start_s)
        cell = lines[index + 2]
        line = None
        index += 2
        if not cell:
            index += 1
        elif not (TIME.match(cell) or CLIP.match(cell)):
            line = _parse_line(cell)
            index += 1
        beats.append(
            Beat(
                id=f"e{episode}c{number}b{len(beats) + 1}",
                start_s=start_s,
                picture=picture,
                line=line,
                overlays=overlays,
            )
        )
    for overlay in beats[-1].overlays:
        if overlay.kind == "true_card":
            overlay.duration_s = end_s - beats[-1].start_s
    clip = Clip(
        number=int(number),
        place=place,
        start_s=_seconds(start),
        end_s=end_s,
        beats=beats,
    )
    return clip, index


def parse_brief(text: str) -> list[Episode]:
    all_lines = text.split("\n")
    lines = all_lines[all_lines.index(SCRIPTS_START) + 1 : all_lines.index(SCRIPTS_END)]
    episodes: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if match := EPISODE.match(line):
            episodes.append(
                {"number": int(match.group(1)), "title": match.group(2), "clips": []}
            )
        elif match := RUNTIME.match(line):
            episodes[-1]["runtime_s"] = _seconds(match.group(1))
        elif match := OPENING.match(line):
            episodes[-1]["opening"] = match.groups()
        elif CLIP.match(line):
            clip, index = _parse_clip(lines, index, episodes[-1]["number"])
            episodes[-1]["clips"].append(clip)
            continue
        elif match := NEXT_OPENS.match(line):
            episodes[-1]["next_opens"] = match.group(1)
        index += 1

    parsed = []
    for data in episodes:
        bug, small_type = data.pop("opening")
        first = data["clips"][0].beats[0]
        first.overlays[:0] = [
            Overlay(kind="corner_bug", text=bug, start_s=0, duration_s=1),
            Overlay(kind="time_super", text=small_type, start_s=0, duration_s=1),
        ]
        first.function = StoryFunction.hook
        data["clips"][-1].beats[-1].function = StoryFunction.cliff
        parsed.append(Episode(**data))
    return parsed


def parse_season_outline(text: str) -> list[OutlineEntry]:
    lines = text.split("\n")
    index = lines.index("One-line outline") + 1
    entries = []
    while lines[index].isdigit():
        entries.append(
            OutlineEntry(
                number=int(lines[index]),
                title=lines[index + 1],
                outline=lines[index + 2],
            )
        )
        index += 3
    return entries


def extract_canon(episodes: list[Episode], sources: dict[str, Any]) -> Canon:
    groups: dict[str, list[str]] = {
        "real_words": [],
        "reported": [],
        "true_cards": [],
        "name_cards": [],
    }
    for episode in episodes:
        for clip in episode.clips:
            for beat in clip.beats:
                if beat.line and beat.line.evidence == Evidence.real_words:
                    groups["real_words"].append(beat.line.text)
                if beat.line and beat.line.evidence == Evidence.reported:
                    groups["reported"].append(beat.line.text)
                for overlay in beat.overlays:
                    if overlay.kind == "true_card":
                        groups["true_cards"].append(overlay.text)
                    if overlay.kind == "name_card":
                        groups["name_cards"].append(overlay.text)
    groups = {name: list(dict.fromkeys(items)) for name, items in groups.items()}

    provenance = {}
    for text in groups["real_words"] + groups["reported"] + groups["true_cards"]:
        claim = sources["claims"].get(text)
        if not claim or claim["source_id"] not in sources["sources"]:
            raise ValueError(f"No source recorded for: {text}")
        provenance[text] = Provenance(
            source_id=claim["source_id"],
            confidence=claim["confidence"],
            origin="brief",
        )
    return Canon(**groups, provenance=provenance)
