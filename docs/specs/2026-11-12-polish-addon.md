# Polish add-on: `audit` and `polish`

## Purpose
yamlreel makes the picture and the music, but nothing checks the finished file or adds sound effects. Short-form
platforms reward loud-enough, non-clipping, loopable videos. This adds two optional commands that work on a finished
video file, and leaves the render path alone.

## Principles
- **Add-on, not core.** `yamlreel/polish.py` is imported only by the two new CLI commands. `render`, the spec
  validator and every existing output are unchanged. No new dependency (numpy and ffmpeg are already required).
- **Any video.** Input is a video file, so it also polishes videos from other tools.
- **Safe inputs.** ffmpeg runs with `-protocol_whitelist file`; audio recordings also use the existing
  `AUDIO_FORMATS` whitelist, are capped at 30 s, and must live in the events file's folder or an `--allow-path` folder.

## CLI
```bash
yamlreel audit VIDEO [--target-lufs -14] [--strict]
yamlreel polish VIDEO [--sfx EVENTS.yaml] [--loudness LUFS] [--out FILE] [--allow-path DIR] [--force]
```

## Behaviour
- **audit.** `ok`/`warn` lines. Loudness is ffmpeg `ebur128` (integrated LUFS, LRA, true peak). Warns outside
  target ±2 LUFS, above -1 dBTP, with no or silent audio, for a vertical video over 60 s, and when the loop jump is
  larger than `max(0.004, 3 x median neighbouring-frame step)`. The loop check scales every frame to 64x64 gray and
  compares the last frame with the first; it refuses videos over 180 s. `--strict` exits 1 on any warning.
- **polish.** Builds a mono WAV of the effects exactly as long as the video, mixes it with the video's audio
  (`amix`, no normalisation), then `alimiter=limit=0.95`, and with `--loudness` first `loudnorm=I=..:TP=-1.5:LRA=11`.
  The video stream is copied. AAC 192k stereo. Refuses to overwrite the input, or an existing output without `--force`.
- **Events file.** `sfx:` list (1 to 500 items) of `{at, kind | file, gain}`; unknown fields, bad times and gains, both or
  neither of `kind`/`file`, and files outside the allowed folders are errors that name the item.
- **Effects.** `tap thump clatter splash scrape whoosh pop click tick rustle ping`: numpy, deterministic (seeded by the
  event's position), each scaled to a peak of 1 before `gain`.

## Not in this change
- Per-scene `sfx:` in specs and automatic sounds for typing or transitions (changes the spec schema and render path).
- A camera move on every scene type, a contrast lint and an HTML scene type. Each needs its own spec.
- Two-pass loudness. Single-pass `loudnorm` lands within about 1 LU of the target.

## Files
`yamlreel/polish.py`, `yamlreel/cli.py` (two subcommands), `tests/test_polish.py`, `README.md`.
