from typing import Literal

from pydantic import BaseModel

from tih.story.models import Episode


class TruthFinding(BaseModel):
    beat: str
    quote: str
    kind: Literal["false_statement", "false_implication", "lost_label", "unsupported"]
    problem: str
    proposal: str


class NoteAnswer(BaseModel):
    note: str
    status: Literal["resolved", "unverified"]
    answer: str
    source: str | None


class TruthReport(BaseModel):
    findings: list[TruthFinding]
    notes: list[NoteAnswer]


MIN_QUOTE = 3


def _beat_texts(script: Episode) -> dict[str, list[str]]:
    texts = {}
    for clip in script.clips:
        for beat in clip.beats:
            texts[beat.id] = [beat.picture] + [overlay.text for overlay in beat.overlays]
            if beat.line:
                texts[beat.id].append(beat.line.text)
    return texts


def review_report(
    report: TruthReport, script: Episode, notes: list[str]
) -> tuple[list[TruthFinding], int, list[str]]:
    """Keep findings that quote their own beat, and list notes left unanswered."""
    texts = _beat_texts(script)
    kept = [
        f
        for f in report.findings
        if len(f.quote.strip()) >= MIN_QUOTE and any(f.quote in text for text in texts.get(f.beat, []))
    ]
    answered = {answer.note for answer in report.notes}
    unanswered = [note for note in notes if note not in answered]
    return kept, len(report.findings) - len(kept), unanswered


def render_truth_report(
    title: str,
    kept: list[TruthFinding],
    dropped: int,
    answers: list[NoteAnswer],
    unanswered: list[str],
) -> str:
    out = [f"# Truth report: {title}", ""]
    out.append(f"{len(kept)} findings. {dropped} dropped because their quote is not in the beat they name.")
    out.append("")
    if unanswered:
        out += ["## Not answered", ""] + [f"- {note}" for note in unanswered] + [""]
    out += ["## Findings", ""]
    if not kept:
        out += ["No problems found.", ""]
    for f in kept:
        out += [
            f"### {f.beat}: {f.kind.replace('_', ' ')}",
            f"- **Script:** {f.quote}",
            f"- **Problem:** {f.problem}",
            f"- **Proposal:** {f.proposal}",
            "",
        ]
    if answers:
        out += ["## Script notes", ""]
        for a in answers:
            source = f" (source: {a.source})" if a.source else ""
            out += [f"- **{a.note}**", f"  - {a.status}: {a.answer}{source}"]
        out.append("")
    return "\n".join(out)
