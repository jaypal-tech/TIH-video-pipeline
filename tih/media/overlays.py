"""Every word the viewer reads, drawn over clean frames with ffmpeg drawtext.

Nothing readable is ever generated inside a picture. Captions come from the
locked script line of each shot; name cards, supers and the TRUE card come
from the shot plan, whose text the shotplan stage has already matched to the
script. All text sits inside the central 900x1400 safe area.
"""

import textwrap
from dataclasses import dataclass
from pathlib import Path

from tih.bible import OverlayStyle
from tih.media.ffmpeg import HEIGHT, WIDTH, escape_path
from tih.shots import ResolvedPlan
from tih.story.models import Delivery

SAFE_LEFT = (WIDTH - 900) // 2
SAFE_TOP = (HEIGHT - 1400) // 2
SAFE_BOTTOM = SAFE_TOP + 1400
NAME_CARD_SECONDS = 2.0


@dataclass(frozen=True)
class Text:
    """One line of on-screen text, in one place, for one stretch of time."""

    text: str
    start: float
    end: float
    x: str
    y: int
    size: int
    font: str
    box: bool = False


def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=width, break_long_words=False) or [text]


def _lines_centered(
    text: str, start: float, end: float, y_last: int, size: int, font: str, width: int, box: bool = False
) -> list[Text]:
    lines = _wrap(text, width)
    step = int(size * 1.25)
    top = y_last - step * (len(lines) - 1)
    return [
        Text(line, start, end, "(w-text_w)/2", top + i * step, size, font, box)
        for i, line in enumerate(lines)
    ]


def layout(plan: ResolvedPlan, style: OverlayStyle) -> list[Text]:
    """Place every caption, tag, card and super for the episode."""
    texts: list[Text] = []
    for shot in plan.shots:
        start, end = shot.start_s, shot.start_s + shot.duration
        if shot.line:
            font = style.font_italic if shot.delivery == Delivery.inner else style.font
            texts += _lines_centered(shot.line, start, end, style.caption_y, style.caption_size, font, 30)
            if shot.evidence_label:
                texts.append(
                    Text(shot.evidence_label, start, end, "(w-text_w)/2",
                         style.caption_y + int(style.caption_size * 1.2), style.tag_size, style.font_bold)
                )
        for overlay in shot.overlays:
            at = start + overlay.at
            until = at + (overlay.duration or NAME_CARD_SECONDS)
            if overlay.kind == "corner_bug":
                texts.append(Text(overlay.text, at, until, str(SAFE_LEFT + 20), SAFE_TOP + 20,
                                  style.tag_size, style.font_bold))
            elif overlay.kind == "time_super":
                texts.append(Text(overlay.text, at, until, "(w-text_w)/2", SAFE_TOP + 80,
                                  style.tag_size, style.font))
            elif overlay.kind == "name_card":
                name, _, rest = overlay.text.partition(" · ")
                texts.append(Text(name, at, until, str(SAFE_LEFT + 20), 1140,
                                  style.name_card_size, style.font_bold))
                if rest:
                    texts.append(Text(rest, at, until, str(SAFE_LEFT + 20),
                                      1140 + int(style.name_card_size * 1.3),
                                      style.tag_size, style.font))
            elif overlay.kind == "super":
                texts += _lines_centered(overlay.text, at, until, 1120, style.name_card_size + 10,
                                         style.font_bold, 28, box=True)
            elif overlay.kind == "true_card":
                texts += _lines_centered(overlay.text, at, until, 960, style.card_size,
                                         style.font_bold, 22)
    return texts


def true_card_spans(plan: ResolvedPlan) -> list[tuple[float, float]]:
    spans = []
    for shot in plan.shots:
        for overlay in shot.overlays:
            if overlay.kind == "true_card":
                at = shot.start_s + overlay.at
                spans.append((at, at + (overlay.duration or NAME_CARD_SECONDS)))
    return spans


def filters(plan: ResolvedPlan, style: OverlayStyle, workdir: Path) -> list[str]:
    """drawbox/drawtext filters for the whole episode. Text goes via files, so
    quotes, colons and the ellipsis never need escaping."""
    workdir.mkdir(parents=True, exist_ok=True)
    chain = [
        f"drawbox=x=0:y=700:w=iw:h=440:color=black@0.78:t=fill:enable='between(t,{a:.3f},{b:.3f})'"
        for a, b in true_card_spans(plan)
    ]
    for number, item in enumerate(layout(plan, style)):
        text_file = workdir / f"text_{number:03d}.txt"
        text_file.write_text(item.text, encoding="utf-8")
        box = ":box=1:boxcolor=black@0.55:boxborderw=14" if item.box else ""
        chain.append(
            f"drawtext=fontfile='{escape_path(Path(item.font))}':textfile='{escape_path(text_file)}'"
            f":fontsize={item.size}:fontcolor=white:borderw=3:bordercolor=black@0.85"
            f":x={item.x}:y={item.y}{box}"
            f":enable='between(t,{item.start:.3f},{item.end:.3f})'"
        )
    return chain
