from collections import Counter
from typing import Literal

from pydantic import BaseModel

from tih.series import CraftTargets, SeriesConfig
from tih.story.models import Beat, Canon, Episode, Evidence, Line
from tih.story.season import Violation

PROTECTED = (Evidence.real_words, Evidence.reported)
WORDS_PER_SECOND = 2.5


class Turn(BaseModel):
    at_s: float
    beat: str
    kind: Literal["reveal", "escalation", "refusal", "decision", "threat", "twist", "arrival"]
    what: str


class EpisodeStory(BaseModel):
    who: str
    wants: str
    blocked_by: str
    turns: list[Turn]
    question: str


class PaceReport(BaseModel):
    turns: int
    talk_share: float
    words_per_line: float
    places: int
    longest_silence_s: float


class ScriptChange(BaseModel):
    beat: str
    before: str
    after: str
    reason: str


class WriterResult(BaseModel):
    story: EpisodeStory
    changes: list[ScriptChange]
    script: Episode


def _beats(script: Episode) -> list[Beat]:
    return [beat for clip in script.clips for beat in clip.beats]


def _protected(script: Episode) -> Counter[tuple[str, str, str, str | None, str | None]]:
    return Counter(
        (
            beat.line.speaker,
            beat.line.text,
            beat.line.evidence.value,
            beat.line.evidence_label,
            beat.line.source_id,
        )
        for beat in _beats(script)
        if beat.line and beat.line.evidence in PROTECTED
    )


def check_script(
    script: Episode,
    number: int,
    canon: Canon,
    config: SeriesConfig,
    draft: Episode | None,
    speakers: dict[str, str] | None = None,
) -> list[Violation]:
    """Return every hard rule the script breaks.

    `speakers` maps a real or reported line to who said it, where the source tells us.
    """
    found = []
    speakers = speakers or {}
    if script.number != number:
        found.append(Violation("number", f"Script is episode {script.number}, expected {number}."))
    low, high = config.runtime_s
    if not low <= script.runtime_s <= high:
        found.append(Violation("runtime", f"Runtime {script.runtime_s} s is outside {low}-{high} s."))
    beats = _beats(script)
    if not script.clips or any(not clip.beats for clip in script.clips):
        found.append(Violation("empty", "The script and every clip must have at least one beat."))
    starts = [beat.start_s for beat in beats]
    if starts != sorted(starts) or (beats and beats[-1].start_s >= script.runtime_s):
        found.append(Violation("timing", "Beat start times must run in order within the runtime."))
    allowed = {Evidence.real_words: set(canon.real_words), Evidence.reported: set(canon.reported)}
    canon_lines = set(canon.real_words) | set(canon.reported)
    for beat in beats:
        if not beat.line:
            continue
        if beat.line.evidence not in PROTECTED and beat.line.text in canon_lines:
            found.append(
                Violation(
                    "mislabelled",
                    f"Beat {beat.id}: '{beat.line.text}' is a source quote but is marked "
                    f"{beat.line.evidence.value}.",
                )
            )
        expected = speakers.get(beat.line.text)
        if beat.line.evidence in PROTECTED and expected and beat.line.speaker != expected:
            found.append(
                Violation(
                    "wrong_speaker",
                    f"Beat {beat.id}: '{beat.line.text}' was said by {expected}, "
                    f"not {beat.line.speaker}.",
                )
            )
        if beat.line.evidence in PROTECTED and beat.line.text not in allowed[beat.line.evidence]:
            found.append(
                Violation(
                    beat.line.evidence.value,
                    f"Beat {beat.id}: '{beat.line.text}' is not in the source word for word.",
                )
            )
    if draft is not None:
        missing = _protected(draft) - _protected(script)
        for (speaker, text, *_), count in missing.items():
            found.append(
                Violation("protected_dropped", f"{speaker}: '{text}' was dropped or its label changed ({count} missing).")
            )
    if beats and beats[-1].line and beats[-1].line.evidence == Evidence.dramatized:
        found.append(
            Violation("dramatized_close", f"Final beat {beats[-1].id} ends on a dramatized line.")
        )
    return found


def pace_report(script: Episode, story: EpisodeStory) -> PaceReport:
    """Measure the script against short-drama pace. Information only."""
    lines = [beat for beat in _beats(script) if beat.line]
    words = [len(beat.line.text.split()) for beat in lines]
    talk = sum(words) / WORDS_PER_SECOND / script.runtime_s if script.runtime_s else 0
    starts = [0.0] + [beat.start_s for beat in lines] + [float(script.runtime_s)]
    return PaceReport(
        turns=len(story.turns),
        talk_share=round(talk, 2),
        words_per_line=round(sum(words) / len(words), 1) if words else 0,
        places=len({clip.place.split("·")[0].strip() for clip in script.clips}),
        longest_silence_s=max(b - a for a, b in zip(starts, starts[1:])),
    )


def _timecode(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _render_story(script: Episode, story: EpisodeStory) -> list[str]:
    ids = {beat.id for beat in _beats(script)}
    out = [
        "## The story",
        "",
        f"- **Who:** {story.who}",
        f"- **Wants:** {story.wants}",
        f"- **Blocked by:** {story.blocked_by}",
        "- **Turns:**",
    ]
    for number, turn in enumerate(story.turns, start=1):
        missing = "" if turn.beat in ids else " (beat not found)"
        out.append(f"  {number}. {_timecode(turn.at_s)} · {turn.kind} · {turn.what}{missing}")
    out += [f"- **Ending question:** {story.question}", ""]
    return out


def _render_pace(report: PaceReport, targets: CraftTargets | None) -> list[str]:
    def target(value: str) -> str:
        return value if targets else "—"

    return [
        "## Pace",
        "",
        "| Measure | Target | This script |",
        "|---|---|---|",
        f"| Turns | {target(f'{targets.turns[0]}–{targets.turns[1]}' if targets else '')} | {report.turns} |",
        f"| Talk time (estimate) | {target(f'{targets.talk_share:.0%}' if targets else '')} | {report.talk_share:.0%} |",
        f"| Words per line | {target(f'about {targets.words_per_line:g}' if targets else '')} | {report.words_per_line:g} |",
        f"| Places | {target(f'at most {targets.max_places}' if targets else '')} | {report.places} |",
        f"| Longest stretch without a line | — | {report.longest_silence_s:g} s |",
        "",
    ]


def _line(line: Line | None) -> str:
    if line is None:
        return ""
    text = f"{line.speaker}: {line.text}"
    if line.evidence != Evidence.dramatized:
        text += f" [{line.evidence_label or line.evidence.value.upper()}]"
    return text


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_script(
    script: Episode,
    story: EpisodeStory | None = None,
    report: PaceReport | None = None,
    targets: CraftTargets | None = None,
) -> str:
    """The script as one readable table per clip, led by its story and pace."""
    out = [f"# Episode {script.number}: {script.title}", "", f"Runtime: {script.runtime_s} s", ""]
    if story is not None:
        out += _render_story(script, story)
    if report is not None:
        out += _render_pace(report, targets)
    for clip in script.clips:
        out += [f"### Clip {clip.number} · {clip.place}", "", "| Time | Picture | Line | On screen |", "|---|---|---|---|"]
        for beat in clip.beats:
            screen = "; ".join(overlay.text for overlay in beat.overlays)
            out.append(
                f"| {beat.start_s:.1f} | {_cell(beat.picture)} | {_cell(_line(beat.line))} | {_cell(screen)} |"
            )
        out.append("")
    out.append(f"**Next episode opens:** {script.next_opens}")
    return "\n".join(out) + "\n"


def render_script_changes(title: str, changes: list[ScriptChange], had_draft: bool) -> str:
    """Every change from the draft, with its reason."""
    out = [f"# Script changes: {title}", ""]
    if not had_draft:
        return "\n".join(out + ["Written from the season plan; there was no draft.", ""])
    if not changes:
        return "\n".join(out + ["No changes from the draft.", ""])
    for change in changes:
        out += [
            f"## {change.beat}",
            f"- **Before:** {change.before}",
            f"- **After:** {change.after}",
            f"- **Why:** {change.reason}",
            "",
        ]
    return "\n".join(out)
