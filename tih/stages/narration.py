"""Stage: narration. Speak every narration line in the narrator's locked voice.

Paid (small). The narrator sets up and closes the episode and says what its
turn means; the video model never hears these lines. Each one is spoken whole,
once, even when the Director lays it over several shots, then transcribed and
compared word for word with the script, as the voice stage does. narration.yaml
lists, per line, its audio, its length, the shots it covers and its word
timings; the cut places it and captions it from that file. An episode with no
narration writes an empty file and makes no call.
"""

import json
from pathlib import Path

import yaml

from tih.assets import ShotRef
from tih.bible import NARRATOR_ID, load_bible, load_locks, require_voice
from tih.gates import require_approved
from tih.media import ffmpeg
from tih.production import TakeSet, choose, gateway_for, load_pipeline, load_resolved, write_takes
from tih.scenes import Narration, narration_lines, narration_pages
from tih.stages.registry import RunContext, Stage, register
from tih.stages.voice import spoken_takes, word_timings_path

NARRATION_FILE = "narration.yaml"


def run_narration(ctx: RunContext) -> None:
    require_approved(ctx.paths, "script")
    require_approved(ctx.paths, "look")
    plan = load_resolved(ctx)
    lines = narration_lines(plan)
    sets: dict[str, TakeSet] = {}
    spoken: list[Narration] = []
    if lines:
        lock = require_voice(load_locks(ctx.paths.series), NARRATOR_ID)
        steps = load_pipeline(ctx.paths.series).steps
        narrator = load_bible(ctx.paths.series).narrator
        gateway = gateway_for(ctx)
    for line in lines:
        ref = ShotRef(plan.episode, None, f"narration-{line.beat}")
        takes = spoken_takes(gateway, steps["speech"], steps["transcribe"], "narration", line.text, lock.voice_id, narrator.direction, ref)
        sets[line.beat] = choose(ctx, gateway.ledger, "narration", line.beat, takes)
        audio = Path(sets[line.beat].chosen)
        words = [
            {"text": str(word.get("text", "")), "start": float(word["start"]), "end": float(word["end"])}
            for word in json.loads(word_timings_path(audio).read_text(encoding="utf-8"))
            if word.get("type", "word") == "word"
        ]
        if narrator.tempo != 1.0:
            audio = ffmpeg.retime_audio(audio, audio.with_name(f"{audio.stem}-x{narrator.tempo:g}.wav"), narrator.tempo)
            words = [{**word, "start": word["start"] / narrator.tempo, "end": word["end"] / narrator.tempo} for word in words]
        spoken.append(Narration(**line.model_dump(), audio=str(audio), duration=round(ffmpeg.duration(audio), 3), words=words))
        narration_pages(spoken[-1], 0.0)  # the words must page, or the cut could not caption the line
    stage_dir = ctx.paths.stage_dir("narration")
    write_takes(stage_dir, sets)
    (stage_dir / NARRATION_FILE).write_text(
        yaml.safe_dump([line.model_dump(mode="json") for line in spoken], allow_unicode=True, sort_keys=False, width=10000),
        encoding="utf-8",
    )


def read_narration(ctx: RunContext) -> list[Narration]:
    """Every narration line as spoken, in script order."""
    path = ctx.paths.scope_dir / "narration" / NARRATION_FILE
    return [Narration.model_validate(line) for line in yaml.safe_load(path.read_text(encoding="utf-8")) or []]


register(Stage(name="narration", requires=("shotplan",), gate="look", run=run_narration))
