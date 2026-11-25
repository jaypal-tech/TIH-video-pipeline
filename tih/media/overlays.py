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
    color: str = "white"


def track(text: str, spaces: int) -> str:
    """Uppercase, with hair spaces between letters: drawtext has no letter spacing."""
    upper = text.upper()
    if not spaces:
        return upper
    return " ".join(("\u200a" * spaces).join(word) for word in upper.split(" "))


def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=width, break_long_words=False) or [text]


def _lines_centered(
    text: str, start: float, end: float, y_last: int, size: int, font: str, width: int,
    box: bool = False, color: str = "white",
) -> list[Text]:
    lines = _wrap(text, width)
    step = int(size * 1.25)
    top = y_last - step * (len(lines) - 1)
    return [
        Text(line, start, end, "(w-text_w)/2", top + i * step, size, font, box, color)
        for i, line in enumerate(lines)
    ]


def layout(plan: ResolvedPlan, style: OverlayStyle) -> list[Text]:
    """Place every caption, tag, card and super for the episode."""
    texts: list[Text] = []
    label_font = style.label_font or style.font_bold
    tracking = style.label_tracking

    def label(text: str) -> str:
        return track(text, tracking) if style.label_font else text

    for shot in plan.shots:
        start, end = shot.start_s, shot.start_s + shot.duration
        if shot.line:
            font = style.font_italic if shot.delivery == Delivery.inner else style.font
            texts += _lines_centered(shot.line, start, end, style.caption_y, style.caption_size, font, 30,
                                     color=style.text_color)
            if shot.evidence_label:
                texts.append(
                    Text(label(shot.evidence_label), start, end, "(w-text_w)/2",
                         style.caption_y + int(style.caption_size * 1.2), style.tag_size, label_font,
                         color=style.accent_color)
                )
        for overlay in shot.overlays:
            at = start + overlay.at
            until = at + (overlay.duration or NAME_CARD_SECONDS)
            if overlay.kind == "corner_bug":
                texts.append(Text(label(overlay.text), at, until, str(SAFE_LEFT + 20), SAFE_TOP + 20,
                                  style.tag_size, label_font, color=style.accent_color))
            elif overlay.kind == "time_super":
                y = SAFE_TOP + 80
                if style.time_kicker:
                    texts.append(Text(label(style.time_kicker), at, until, "(w-text_w)/2", y,
                                      style.tag_size - 6, label_font, color=style.accent_color))
                    y += int(style.tag_size * 1.5)
                texts.append(Text(overlay.text, at, until, "(w-text_w)/2", y,
                                  style.tag_size, style.font, color=style.text_color))
            elif overlay.kind == "name_card":
                name, _, rest = overlay.text.partition(" · ")
                texts.append(Text(label(name), at, until, str(SAFE_LEFT + 20), 1140,
                                  style.name_card_size, label_font, color=style.accent_color))
                if rest:
                    texts.append(Text(rest, at, until, str(SAFE_LEFT + 20),
                                      1140 + int(style.name_card_size * 1.3),
                                      style.tag_size, style.font, color=style.text_color))
            elif overlay.kind == "super":
                texts += _lines_centered(overlay.text, at, until, 1120, style.name_card_size + 10,
                                         style.font_bold, 28, box=True, color=style.text_color)
            elif overlay.kind == "true_card":
                texts += _lines_centered(overlay.text, at, until, 960, style.card_size,
                                         style.font_bold, 22, color=style.text_color)
                if style.brand_name:
                    texts.append(Text(label(style.brand_name), at, until, "(w-text_w)/2", 1050,
                                      style.tag_size, label_font, color=style.accent_color))
                if style.brand_tagline:
                    texts.append(Text(style.brand_tagline, at, until, "(w-text_w)/2", 1100,
                                      style.tag_size, style.font_italic, color=style.text_color))
    texts += _brand_corner(texts, plan, style, label_font)
    return texts


def _brand_corner(texts: list[Text], plan: ResolvedPlan, style: OverlayStyle, label_font: str) -> list[Text]:
    """The brand name, small in the top-right corner, after the series bug and until the closing card."""
    if not style.brand_name:
        return []
    bugs = [
        shot.start_s + o.at + (o.duration or NAME_CARD_SECONDS)
        for shot in plan.shots for o in shot.overlays if o.kind == "corner_bug"
    ]
    cards = true_card_spans(plan)
    start = max(bugs, default=0.0)
    end = min((a for a, _ in cards), default=plan.runtime_s)
    if end <= start:
        return []
    name = track(style.brand_name, style.label_tracking) if style.label_font else style.brand_name
    return [Text(name, start, end, f"w-text_w-{SAFE_LEFT + 20}", SAFE_TOP + 20,
                 style.tag_size - 2, label_font, color=style.accent_color)]


def _color(value: str) -> str:
    """ffmpeg colours: '#D3A445' becomes 0xD3A445; names like 'white' pass through."""
    return f"0x{value[1:]}" if value.startswith("#") else value


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
    card = _color(style.card_color)
    chain = [
        f"drawbox=x=0:y=700:w=iw:h=480:color={card}@0.92:t=fill:enable='between(t,{a:.3f},{b:.3f})'"
        for a, b in true_card_spans(plan)
    ]
    for number, item in enumerate(layout(plan, style)):
        text_file = workdir / f"text_{number:03d}.txt"
        text_file.write_text(item.text, encoding="utf-8")
        box = f":box=1:boxcolor={card}@0.55:boxborderw=14" if item.box else ""
        border = 1 if item.font == style.label_font else 2  # small labels muddy under a thick outline
        chain.append(
            f"drawtext=fontfile='{escape_path(Path(item.font))}':textfile='{escape_path(text_file)}'"
            f":fontsize={item.size}:fontcolor={_color(item.color)}:borderw={border}:bordercolor=black@0.6"
            f":shadowx=0:shadowy=3:shadowcolor=black@0.55"
            f":x={item.x}:y={item.y}{box}"
            f":enable='between(t,{item.start:.3f},{item.end:.3f})'"
        )
    return chain
