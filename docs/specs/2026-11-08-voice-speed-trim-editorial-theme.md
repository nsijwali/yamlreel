# Voice speed and pause trimming, and the `editorial` theme

Status: ready to build. Branch `feat/voice-speed-trim-editorial-theme`, from main at af2d840.

## Why

Viewers of a narrated Short said the voice felt slow and the breaths distracting. Today the only control is `gap`
(the pause after a clip). A creator who wants a quicker read has to post-process the cached WAVs by hand. This adds
two `voiceover` options that do it inside yamlreel, and a theme that matches a cream, ink and burnt-orange brand.

## 1. `voiceover.speed` and `voiceover.trim`

```yaml
voiceover: {speed: 1.08, trim: true}
```

- `speed`: number from 0.8 to 1.3, default 1.0. Plays each narration clip faster or slower **without changing its
  pitch** (time-stretch). 1.0 is a no-op and must not touch the audio or require ffmpeg.
- `trim`: bool, default false. For each clip: cut leading and trailing silence, and shorten any quiet gap inside the
  clip that is longer than 0.3 s to 0.15 s (breaths sit in those gaps).
- Both apply to every narration clip, spoken by the server or supplied as `narration_audio`.
- They are applied **after** the clip is read from the cache. The cache keeps the raw server audio and its key does
  not change, so changing `speed` or `trim` never re-speaks a line, and re-rendering never stacks the effect.
- Order: trim first, then speed. Scene lengths (`min_scene_seconds`, `scene_floors`) and the subtitle timing must be
  computed from the **processed** clip, so scenes shrink to fit the faster voice.
- Silence detection: frame RMS (about 20 ms frames) against a threshold relative to the clip's own loudness (for
  example 40 dB under the clip's peak RMS), not a fixed absolute level, so quiet and loud voices both work. Keep a
  short pad (about 30 ms) at the cut edges so word starts and ends are not clipped. Never return an empty clip: if
  everything is "silence", return the original.
- Time-stretch: use ffmpeg's `atempo` through the same ffmpeg lookup the renderer already uses. If `speed` is not 1.0
  and no ffmpeg is found, fail with the same style of message as the other missing-ffmpeg errors. `trim` alone is pure
  numpy and needs no ffmpeg.
- Validation in `specs.py` `_voiceover`: add `speed` and `trim` to the allowed keys; `speed` is a number in
  [0.8, 1.3]; `trim` is a bool. Error messages follow the existing `voiceover.gap` style.
- `yamlreel validate` and the scene-length floor must use the processed durations.

## 2. `editorial` theme

Add to `THEMES` in `theme.py`, next to `washi`:

| Field | Value |
| --- | --- |
| bg | Cream `#F3EEE3` = (243, 238, 227) |
| fg | Ink black `#111111` = (17, 17, 17) |
| accent | Burnt orange `#D9541E` = (217, 84, 30) |
| muted | ink at 62% over cream |
| accent2 | derived from the three colours (for example ink at 80% over cream), never a new hue |
| card | cream lightened a little (about 249, 246, 238) |
| line | ink at 22% over cream |
| display | serif |
| frame | True |

Only these three colours plus blends of them. Contrast: `fg` on `bg` and `accent` on `bg` must stay readable (accent
on cream is about 4:1, fine for large text and highlights; do not use accent for small body text).

## 3. Docs

- README: add `speed` and `trim` to the `voiceover:` line in the spec reference and describe them where `gap` and
  ducking are described (one short paragraph with the example above); add `editorial` to every place the themes are
  listed (the theme line near line 203 and the reference near line 237) with a one-line description; add a short
  entry to the changelog or upgrade notes section.
- Add `examples/` only if an existing example can show `speed`/`trim` with one added line; do not add a new example
  file just for this.

## 4. Tests (targeted, in `tests/`)

- `trim`: leading and trailing silence removed; a 0.8 s internal gap becomes 0.15 s; a clip with no silence is
  unchanged within a few ms; an all-quiet clip is returned unchanged; speech is not clipped at the edges.
- `speed`: 1.0 returns the identical array and never calls ffmpeg; 1.25 shortens a clip by about 20% (skip the test if
  ffmpeg is not available); out-of-range values are rejected by validation.
- Cache: with `speed` and `trim` set, the cache file is untouched and its key is unchanged.
- `scene_floors` / `prepare_narration`: scene length follows the processed clip.
- Theme: `editorial` exists, uses only the three base colours and their blends, and renders a scene without error.
- Run only the targeted tests during development; the full suite once at the end.

## Out of scope

- Detecting breaths separately from silence (the gap shortening is the whole feature).
- Changing the voice server calls or the cache key version.
- Channel artwork, the Short specs and `voicepolish.py` (they live outside this repo).
