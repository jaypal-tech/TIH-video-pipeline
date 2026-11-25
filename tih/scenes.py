"""Takes: groups of shots the video model stages in one paid generation, plus caption timing."""

import math
from typing import Literal

from pydantic import BaseModel

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


class SceneTake(BaseModel):
    id: str  # "<clip>.<n>", n from 1 within the clip; OPENING_ID for the opening
    clip: int  # 0 for the opening
    shots: list[ResolvedShot]  # empty for the opening
    place: str  # the first shot's place; the Director's opening place for the opening
    start_s: float  # the first shot's start_s; 0.0 for the opening
    duration: float  # sum of the shots' durations; OPENING_SECONDS for the opening

    @property
    def line(self) -> str:
        return " ".join(shot.line for shot in self.shots if shot.line)


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
    lined = [shot for shot in take.shots if shot.line]
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
    heard = spoken_words(timings)
    # one unit per script token: [text, first timing, last timing, closes its shot's line]
    units: list[list] = []
    used = 0
    carry = ""  # a token with no words (a lone dash) rides on the previous token's text
    for shot in take.shots:
        tokens = shot.line.split() if shot.line else []
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
        raise ValueError(f"take {take.id}: script has {used} words, timings have {len(heard)}")
    pages: list[list[list]] = []
    open_page = False
    for unit in units:
        if not open_page:
            pages.append([])
            open_page = True
        pages[-1].append(unit)
        if unit[3] or len(pages[-1]) == CAPTION_WORDS or unit[0].endswith(PAGE_ENDINGS):
            open_page = False
    limit = take.start_s + take.duration
    captions = []
    for number, page in enumerate(pages):
        start = take.start_s + float(heard[page[0][1]]["start"])
        end = min(take.start_s + float(heard[page[-1][2]]["end"]) + CAPTION_TAIL, limit)
        if number + 1 < len(pages):
            end = min(end, take.start_s + float(heard[pages[number + 1][0][1]]["start"]))
        captions.append(Caption(text=" ".join(unit[0] for unit in page), start=start, end=end))
    return captions


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


def shifted(plan: ResolvedPlan, seconds: float) -> ResolvedPlan:
    return plan.model_copy(update={
        "runtime_s": plan.runtime_s + seconds,
        "shots": [s.model_copy(update={"start_s": s.start_s + seconds}) for s in plan.shots],
        "cues": [c.model_copy(update={"at": c.at + seconds}) for c in plan.cues],
    })


def as_shot(plan: ResolvedPlan, takes: list[SceneTake], played: dict[str, float]) -> ResolvedPlan:
    """The plan as the takes were shot: a take that plays longer than planned lengthens its last shot.

    Every later shot, and every cue at or after that take's planned end, moves by
    the extra; the clip's ambience bed grows by it; the runtime grows by the total.
    A take with no extra is unchanged.
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
    if not ends:
        return plan
    shots, moved = [], 0.0
    for shot in plan.shots:
        update = {"start_s": shot.start_s + moved}
        if shot.id in extra_after:
            update["duration"] = shot.duration + extra_after[shot.id]
            moved += extra_after[shot.id]
        shots.append(shot.model_copy(update=update))
    cues = [c.model_copy(update={
        "at": c.at + sum(extra for end, extra in ends if c.at >= end - 1e-9),
        "duration": c.duration + (grown.get(c.scene, 0.0) if c.kind == "ambience" else 0.0),
    }) for c in plan.cues]
    return plan.model_copy(update={
        "shots": shots, "cues": cues, "runtime_s": plan.runtime_s + sum(extra for _, extra in ends),
    })
