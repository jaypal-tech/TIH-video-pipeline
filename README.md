# TIH story pipeline

Turns a series brief into finished vertical drama episodes (1080×1920, H.264,
60–90 s), with the same faces and voices in every shot and every API call
logged. A new episode is a new shot-plan file and one command.

## Setup

```bash
poetry install
echo "FAL_KEY=..." > .env   # never committed; the ledger redacts it
```

Needs `ffmpeg` and `ffprobe` on the path.

## What a series is made of

| File | What it holds |
|---|---|
| `series/<s>/source/brief.txt` | the scripts (parsed by `record`) |
| `series/<s>/bible.yaml` | every character's look and voice, places, style, banned words, overlay style |
| `series/<s>/locks.yaml` | the owner's chosen reference image, seed and voice id per character |
| `series/<s>/shots/example.yaml` | a hand-made shot plan the Director follows for style (optional) |
| `runs/<s>/epNN/director/shots.yaml` | the episode's shot plan, written by the Director: shots, prompts with `[CHARACTER]`/`[PLACE]` tokens, overlays, sound cues |
| `series/<s>/pipeline.yaml` | which model does each step, its arguments and cost estimate |
| `series/<s>/models.yaml` | prices used to log the cost of every call |

## Make an episode

```bash
S="--series lincoln"
tih run 1 --stage writers_room $S  # the episode script (after the season Story Lock)
tih approve 1 script $S            # Script Lock
tih run 1 --stage director $S      # the shot plan, written from the locked script and checked
tih run 1 --stage shotplan $S      # resolve the plan against script and bible (free)
tih run 1 --stage casting $S       # candidate looks and voices (paid, only unlocked characters)
tih lock look booth --image <chosen.png> --seed 1866 $S
tih lock voice booth --voice-id <id> $S
tih lock voice narrator --voice-id <id> $S  # once per series, when a script has narration
tih approve 1 look $S              # Look Lock: bible, locks and the shot plan
tih episode 1 $S --flow seedance   # narration, scenes_seedance, sound, cut_seedance
```

The finished file is `runs/<s>/ep01/cut_seedance/final.mp4`. The cut fails unless
`validate_master` passes (codec, size, frame rate, runtime, audio).

## Rehearse first ($0)

```bash
tih rehearse 1 $S --flow seedance
```

Needs the Writer and Director to have run for the episode. Copies the locked
script and the shot plan into `runs-dry/`, then runs every video stage with
placeholder media (flat frames, tones).
You get a real-length cut with the real captions, cards and timing, and the
estimated cost of the real run from `models.yaml`.

## Re-run one episode from its script

```bash
tih episode 1 $S --flow seedance --from director --force
```

Every stage runs again, but each generation call is matched to the earlier
one by its fingerprint (endpoint, settings, prompt, input hashes) and reused,
so nothing is paid twice. Only shots whose prompt or inputs changed cost money.

To swap one take: `tih pick 1 scenes_seedance 2.1 1`, then
`tih episode 1 $S --flow seedance --from scenes_seedance --force`.

## Safety rails

- Editing the script, shot plan, bible or locks after approval stops every paid
  stage until it is approved again.
- Shot lines must be exact pieces of the script; overlay text must come from
  the script; banned words (`forbidden` in the bible) stop the plan.
- Each spoken take is transcribed and must match the script word for word.
- Budget: warn and hard stop come from `series.yaml`; every call reserves its
  estimate first.

## The log

```bash
tih log $S --out generation_log.csv
```

One row per API call with the brief's fields (time, episode, clip, shot, step,
model, settings, prompt, inputs, output file, cost, time taken, kept, reason).

## Tests

```bash
poetry run pytest            # fast suite
poetry run pytest -m slow    # full Ep 1 rehearsal (about a minute)
```
