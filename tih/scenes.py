"""Takes: groups of shots the video model stages in one paid generation, plus caption and narration timing."""

import math
from typing import Literal

from pydantic import BaseModel

from tih.bible import NARRATOR_ID
from tih.shots import ResolvedPlan, ResolvedShot
from tih.stages.voice import spoken_words, words

Flow = Literal["seedance", "veo"]
FLOWS: tuple[Flow, ...] = ("seedance", "veo")
MAX_TAKE_SECONDS = 15.0
MAX_TAKE_SHOTS = 4
OPENING_ID = "0"
OPENING_SECONDS = 5.0
CAPTION_WORDS = 4
CAPTION_LEAD = 0.2  # seconds a card or super may lead its shot's first word
CAPTION_TAIL = 0.4  # seconds a caption page stays after its last word, unless the next page starts sooner
PAGE_ENDINGS = (",", ".", "?", "!", "…")
NARRATION_LEAD = 0.3  # seconds into its first shot that a narration line starts
OPENING_NARRATION_LEAD = 0.5  # seconds into the film that the opening's narration line starts
CUT_TOLERANCE = 2.0  # seconds a cut found in a clip may sit from its planned time and still be that cut


class SceneTake(BaseModel):
    id: str  # "<clip>.<n>", n from 1 within the clip; OPENING_ID for the opening
    clip: int  # 0 for the opening
    shots: list[ResolvedShot]  # empty for the opening
    place: str  # the first shot's place; the Director's opening place for the opening
    start_s: float  # the first shot's start_s; 0.0 for the opening
    duration: float  # sum of the shots' durations; OPENING_SECONDS for the opening

    @property
    def line(self) -> str:
        """The words the take's own sound must say; narration is not in it."""
        return " ".join(shot.spoken for shot in self.shots if shot.spoken)


class Caption(BaseModel):
    text: str
    start: float  # seconds from the start of the as-shot episode's shots (before the opening shift)
    end: float


def _alone(shot: ResolvedShot) -> bool:
    return shot.speed != 1.0 or shot.freeze_at is not None


def _take(clip: int, number: int, shots: list[ResolvedShot]) -> SceneTake:
    return SceneTake(
        id=f"{clip}.{number}",
        clip=clip,
        shots=shots,
        place=shots[0].place,
        start_s=shots[0].start_s,
        duration=sum(shot.duration for shot in shots),
    )


def scene_takes(plan: ResolvedPlan, flow: Flow) -> list[SceneTake]:
    """Group the plan's shots into takes; the opening is not included."""
    groups: list[list[ResolvedShot]] = []
    for shot in plan.shots:
        current = groups[-1] if groups else None
        fits = (
            flow == "seedance"
            and current is not None
            and current[0].clip == shot.clip
            and not _alone(shot)
            and not _alone(current[-1])
            and len(current) < MAX_TAKE_SHOTS
            and sum(s.duration for s in current) + shot.duration <= MAX_TAKE_SECONDS + 1e-6  # float sums
        )
        if fits:
            current.append(shot)
        else:
            groups.append([shot])
    counts: dict[int, int] = {}
    takes = []
    for group in groups:
        clip = group[0].clip
        counts[clip] = counts.get(clip, 0) + 1
        takes.append(_take(clip, counts[clip], group))
    return takes


def opening_take(plan: ResolvedPlan) -> SceneTake:
    """At the Director's opening place, or the first shot's place when the plan has no opening."""
    place = plan.opening.place if plan.opening is not None else plan.shots[0].place
    return SceneTake(
        id=OPENING_ID, clip=0, shots=[], place=place,
        start_s=0.0, duration=OPENING_SECONDS,
    )


def request_seconds(take: SceneTake, allowed: tuple[int, int]) -> int:
    low, high = allowed
    return min(max(math.ceil(take.duration), low), high)


def line_spans(take: SceneTake, timings: list[dict]) -> dict[str, tuple[float, float]]:
    """Each lined shot's first word start and last word end, in seconds from the take's start."""
    heard = spoken_words(timings)
    lined = [shot for shot in take.shots if shot.spoken]
    wanted = sum(len(words(shot.line)) for shot in lined)
    if wanted != len(heard):
        raise ValueError(f"take {take.id}: script has {wanted} words, timings have {len(heard)}")
    spans: dict[str, tuple[float, float]] = {}
    index = 0
    for shot in lined:
        mine = heard[index:index + len(words(shot.line))]
        index += len(mine)
        spans[shot.id] = (float(mine[0]["start"]), float(mine[-1]["end"]))
    return spans


def caption_pages(take: SceneTake, timings: list[dict]) -> list[Caption]:
    """Pages of at most CAPTION_WORDS words, cut at punctuation and at the end of each shot's line."""
    lines = [shot.spoken for shot in take.shots if shot.spoken]
    return _pages(lines, timings, take.start_s, take.duration, f"take {take.id}")


def _pages(lines: list[str], timings: list[dict], start_s: float, duration: float, name: str) -> list[Caption]:
    """The pages of some lines whose word timings count from `start_s`; no page outlasts `duration`."""
    heard = spoken_words(timings)
    # one unit per script token: [text, first timing, last timing, closes its shot's line]
    units: list[list] = []
    used = 0
    carry = ""  # a token with no words (a lone dash) rides on the previous token's text
    for line in lines:
        tokens = line.split()
        for i, token in enumerate(tokens):
            closes = i == len(tokens) - 1
            count = len(words(token))
            if count == 0 and units:
                units[-1][0] += f" {token}"
                units[-1][3] = units[-1][3] or closes
            elif count == 0:
                carry += f"{token} "
            else:
                units.append([carry + token, used, used + count - 1, closes])
                carry = ""
                used += count
    if used != len(heard):
        raise ValueError(f"{name}: script has {used} words, timings have {len(heard)}")
    pages: list[list[list]] = []
    open_page = False
    for unit in units:
        if not open_page:
            pages.append([])
            open_page = True
        pages[-1].append(unit)
        if unit[3] or len(pages[-1]) == CAPTION_WORDS or unit[0].endswith(PAGE_ENDINGS):
            open_page = False
    limit = start_s + duration
    captions = []
    for number, page in enumerate(pages):
        start = start_s + float(heard[page[0][1]]["start"])
        end = min(start_s + float(heard[page[-1][2]]["end"]) + CAPTION_TAIL, limit)
        if number + 1 < len(pages):
            end = min(end, start_s + float(heard[pages[number + 1][0][1]]["start"]))
        captions.append(Caption(text=" ".join(unit[0] for unit in page), start=start, end=end))
    return captions


def without_overlap(captions: list[Caption]) -> list[Caption]:
    """The pages in order of their start, each leaving when the next arrives; a page left no time is dropped."""
    pages = sorted((page for page in captions if page.end > page.start), key=lambda page: page.start)
    trimmed = [page.model_copy(update={"end": min(page.end, following.start)})
               for page, following in zip(pages, pages[1:])] + pages[-1:]
    return [page for page in trimmed if page.end > page.start]


class NarrationLine(BaseModel):
    beat: str  # the script beat whose line this is
    text: str
    shots: list[str]  # the shots it is laid over, in order; OPENING_ID first when the opening carries it


class Narration(NarrationLine):
    """One narration line as spoken: the narration stage writes these to narration.yaml."""

    audio: str
    duration: float
    words: list[dict]  # the spoken words' timings, in seconds from the start of `audio`


class NarrationTooLong(ValueError):
    """A narration line outlasts the picture it is laid over."""


def narration_lines(plan: ResolvedPlan) -> list[NarrationLine]:
    """Every narration line of the plan, whole, with the shots that carry its pieces."""
    lines: dict[str, NarrationLine] = {}
    if plan.opening is not None and plan.opening.line:
        lines[plan.opening_beat] = NarrationLine(beat=plan.opening_beat, text=plan.opening.line, shots=[OPENING_ID])
    for shot in plan.shots:
        if shot.speaker != NARRATOR_ID:
            continue
        if shot.beat in lines:
            line = lines[shot.beat]
            lines[shot.beat] = line.model_copy(update={"text": f"{line.text} {shot.line}", "shots": [*line.shots, shot.id]})
        else:
            lines[shot.beat] = NarrationLine(beat=shot.beat, text=shot.line, shots=[shot.id])
    return list(lines.values())


def narration_starts(plan: ResolvedPlan, lines: list[NarrationLine]) -> dict[str, float]:
    """When each line's audio starts, on the shots' clock: the opening runs from -OPENING_SECONDS to 0."""
    shots = {shot.id: shot for shot in plan.shots}
    return {
        line.beat: OPENING_NARRATION_LEAD - OPENING_SECONDS if line.shots[0] == OPENING_ID
        else shots[line.shots[0]].start_s + NARRATION_LEAD
        for line in lines
    }


def narration_extras(
    plan: ResolvedPlan, takes: list[SceneTake], lines: list[Narration], room: dict[str, float],
    spans: dict[str, tuple[float, float]],
) -> dict[str, float]:
    """Seconds each take must play longer so that no narration line is cut off.

    `plan` and `takes` are as shot. A line that outlasts its shots gets more of
    its take's clip (`room`: the seconds of each clip not played yet) when its
    last shot ends the take. Past that, and past the opening, which cannot
    grow, it may only run on into silence: it must end before the next voice
    starts (the next line's first spoken word, or the next narration line) and
    before the film does. Otherwise NarrationTooLong names the line.
    """
    shots = {shot.id: shot for shot in plan.shots}
    order = {shot.id: index for index, shot in enumerate(plan.shots)}
    take_of = {shot.id: take for take in takes for shot in take.shots}
    starts = narration_starts(plan, lines)

    def next_voice(index: int) -> float:
        for shot in plan.shots[index:]:
            if shot.speaker == NARRATOR_ID:
                return shot.start_s + NARRATION_LEAD
            if shot.spoken:
                take = take_of[shot.id]
                return take.start_s + spans.get(shot.id, (shot.start_s - take.start_s, 0.0))[0]
        return plan.runtime_s

    extras: dict[str, float] = {}
    for line in lines:
        last = line.shots[-1]
        opening = last == OPENING_ID
        covered = 0.0 if opening else shots[last].start_s + shots[last].duration
        over = starts[line.beat] + line.duration - covered
        if over <= 1e-3:
            continue
        gap = max(0.0, next_voice(0 if opening else order[last] + 1) - covered)
        take = None if opening else take_of[last]
        clip = ""
        if take is not None and take.shots[-1].id == last:
            left = room.get(take.id, 0.0)
            clip = f"take {take.id} has {left:.2f} s of clip left and "
            if min(over, left) > 1e-3:
                extras[take.id] = min(over, left)
        if over - extras.get(take.id if take else "", 0.0) > gap + 1e-3:
            raise NarrationTooLong(
                f"narration line {line.beat} ({line.text!r}) runs {over:.2f} s past "
                f"{'the opening' if opening else 'its shots'}; {clip}the next voice comes {gap:.2f} s after "
                f"{'it' if opening else 'them'}"
            )
    return extras


def moved_captions(captions: list[Caption], takes: list[SceneTake], extras: dict[str, float]) -> list[Caption]:
    """The pages after some takes grew by `extras`: a page at or after a grown take's old end moves with the film."""
    ends = [(take.start_s + take.duration, extras[take.id]) for take in takes if take.id in extras]
    moved = []
    for page in captions:
        shift = sum(extra for end, extra in ends if page.start >= end - 1e-9)
        moved.append(page.model_copy(update={"start": page.start + shift, "end": page.end + shift}))
    return moved


def narration_pages(line: Narration, start: float) -> list[Caption]:
    """A narration line's caption pages, paged like a take's, for audio that starts at `start`."""
    return _pages([line.text], line.words, start, line.duration + CAPTION_TAIL, f"narration {line.beat}")


def retimed(
    plan: ResolvedPlan, takes: list[SceneTake], spans: dict[str, tuple[float, float]]
) -> ResolvedPlan:
    """Re-time every shot that has a spoken span to the voice the model actually produced."""
    changes: dict[str, tuple[float, float]] = {}
    for take in takes:
        for shot in take.shots:
            if shot.id not in spans:
                continue
            span_start, span_end = spans[shot.id]
            offset = max(0.0, span_start - CAPTION_LEAD)
            changes[shot.id] = (
                take.start_s + offset,
                min(span_end + CAPTION_TAIL, take.duration) - offset,
            )
    shots = [
        shot.model_copy(update={"start_s": changes[shot.id][0], "duration": changes[shot.id][1]})
        if shot.id in changes else shot
        for shot in plan.shots
    ]
    return plan.model_copy(update={"shots": shots})


def kept_cuts(
    plan: ResolvedPlan, takes: list[SceneTake], played: dict[str, float], real: dict[str, tuple[float, float]],
    flow: Flow,
) -> dict[str, tuple[float, float]]:
    """`real` without the takes whose real cuts would change which shots each take holds.

    A clip that needs two generations is grouped by its shots' lengths; a first
    shot that really ends early could slip into the take before it, and every
    take id after it would name other shots. Such a take keeps its plan.
    """
    real = dict(real)
    while True:
        heads = {take.shots[0].id for take in scene_takes(as_shot(plan, takes, played, real), flow)}
        slipped = [take for take in takes if take.shots and take.shots[0].id in real and take.shots[0].id not in heads]
        if not slipped:
            return real
        for take in slipped:
            for shot in take.shots:
                del real[shot.id]


def shifted(plan: ResolvedPlan, seconds: float) -> ResolvedPlan:
    return plan.model_copy(update={
        "runtime_s": plan.runtime_s + seconds,
        "shots": [s.model_copy(update={"start_s": s.start_s + seconds}) for s in plan.shots],
        "cues": [c.model_copy(update={"at": c.at + seconds}) for c in plan.cues],
    })


def real_shots(take: SceneTake, cuts: list[float], played: float) -> dict[str, tuple[float, float]] | None:
    """Each shot's real (start, end) in its take's clip, from the cuts found in the clip.

    The video model does not cut at the prompt's times. The cuts are used when
    there is one per planned cut, in order, each within CUT_TOLERANCE of its
    plan and inside the played length; otherwise None, and the take keeps its plan.
    """
    planned, clock = [], 0.0
    for shot in take.shots[:-1]:
        clock += shot.duration
        planned.append(clock)
    edges = [0.0, *cuts, played]
    if len(cuts) != len(planned) or any(b <= a for a, b in zip(edges, edges[1:])):
        return None
    if any(abs(cut - plan) > CUT_TOLERANCE for cut, plan in zip(cuts, planned)):
        return None
    return {shot.id: (edges[index], edges[index + 1]) for index, shot in enumerate(take.shots)}


def as_shot(
    plan: ResolvedPlan, takes: list[SceneTake], played: dict[str, float],
    real: dict[str, tuple[float, float]] | None = None,
) -> ResolvedPlan:
    """The plan as the takes were shot: a take that plays longer than planned lengthens its last shot.

    Every later shot, and every cue at or after that take's planned end, moves by
    the extra; the clip's ambience bed grows by it; the runtime grows by the total.
    A take with no extra is unchanged. Inside a take whose real cuts were found
    (`real`, from `real_shots`), each shot starts at its real cut.
    """
    extra_after: dict[str, float] = {}  # a take's last shot id -> its extra seconds
    ends: list[tuple[float, float]] = []  # (planned end, extra) per lengthened take
    grown: dict[int, float] = {}  # clip -> its takes' extra, so the clip's ambience bed covers it
    for take in takes:
        extra = played.get(take.id, take.duration) - take.duration
        if extra > 0 and take.shots:
            extra_after[take.shots[-1].id] = extra
            ends.append((take.start_s + take.duration, extra))
            grown[take.clip] = grown.get(take.clip, 0.0) + extra
    if not ends and not real:
        return plan
    shots, moved = [], 0.0
    for shot in plan.shots:
        update = {"start_s": shot.start_s + moved}
        if shot.id in extra_after:
            update["duration"] = shot.duration + extra_after[shot.id]
            moved += extra_after[shot.id]
        shots.append(shot.model_copy(update=update))
    if real:
        timed = {shot.id: shot for shot in shots}
        for take in takes:
            if not take.shots or take.shots[0].id not in real:
                continue
            start = timed[take.shots[0].id].start_s
            end = timed[take.shots[-1].id].start_s + timed[take.shots[-1].id].duration
            for shot, following in zip(take.shots, [*take.shots[1:], None]):
                at = start + real[shot.id][0]
                until = end if following is None else start + real[following.id][0]
                timed[shot.id] = timed[shot.id].model_copy(update={"start_s": at, "duration": until - at})
        shots = [timed[shot.id] for shot in shots]
    cues = [c.model_copy(update={
        "at": c.at + sum(extra for end, extra in ends if c.at >= end - 1e-9),
        "duration": c.duration + (grown.get(c.scene, 0.0) if c.kind == "ambience" else 0.0),
    }) for c in plan.cues]
    return plan.model_copy(update={
        "shots": shots, "cues": cues, "runtime_s": plan.runtime_s + sum(extra for _, extra in ends),
    })
