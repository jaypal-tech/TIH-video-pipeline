from collections import Counter

from pydantic import BaseModel

from tih.series import SeriesConfig
from tih.story.models import Beat, Canon, Episode, Evidence, Line
from tih.story.season import Violation

PROTECTED = (Evidence.real_words, Evidence.reported)


class ScriptChange(BaseModel):
    beat: str
    before: str
    after: str
    reason: str


class WriterResult(BaseModel):
    changes: list[ScriptChange]
    script: Episode


def _beats(script: Episode) -> list[Beat]:
    return [beat for clip in script.clips for beat in clip.beats]


def _protected(script: Episode) -> Counter[tuple[str, str, str]]:
    return Counter(
        (beat.line.speaker, beat.line.text, beat.line.evidence.value)
        for beat in _beats(script)
        if beat.line and beat.line.evidence in PROTECTED
    )


def check_script(
    script: Episode, number: int, canon: Canon, config: SeriesConfig, draft: Episode | None
) -> list[Violation]:
    """Return every hard rule the script breaks."""
    found = []
    if script.number != number:
        found.append(Violation("number", f"Script is episode {script.number}, expected {number}."))
    low, high = config.runtime_s
    if not low <= script.runtime_s <= high:
        found.append(Violation("runtime", f"Runtime {script.runtime_s} s is outside {low}-{high} s."))
    beats = _beats(script)
    starts = [beat.start_s for beat in beats]
    if starts != sorted(starts) or (beats and beats[-1].start_s >= script.runtime_s):
        found.append(Violation("timing", "Beat start times must run in order within the runtime."))
    allowed = {Evidence.real_words: set(canon.real_words), Evidence.reported: set(canon.reported)}
    for beat in beats:
        if beat.line and beat.line.evidence in PROTECTED and beat.line.text not in allowed[beat.line.evidence]:
            found.append(
                Violation(
                    beat.line.evidence.value,
                    f"Beat {beat.id}: '{beat.line.text}' is not in the source word for word.",
                )
            )
    if draft is not None:
        missing = _protected(draft) - _protected(script)
        for (speaker, text, _), count in missing.items():
            found.append(
                Violation("protected_dropped", f"{speaker}: '{text}' was dropped ({count} missing).")
            )
    if beats and beats[-1].line and beats[-1].line.evidence == Evidence.dramatized:
        found.append(
            Violation("dramatized_close", f"Final beat {beats[-1].id} ends on a dramatized line.")
        )
    return found


def _line(line: Line | None) -> str:
    if line is None:
        return ""
    text = f"{line.speaker}: {line.text}"
    if line.evidence != Evidence.dramatized:
        text += f" [{line.evidence_label or line.evidence.value.upper()}]"
    return text


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_script(script: Episode) -> str:
    """The script as one readable table per clip."""
    out = [f"# Episode {script.number}: {script.title}", "", f"Runtime: {script.runtime_s} s", ""]
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
