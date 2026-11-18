# Sound design: scene cues, the mix path, `drive` and `studio`

Status: implemented on the motion-layer branch, additive. A spec without `mix:`, `sfx:` or `theme: studio` renders
the same frames and audio as before (the example frame hashes are checked unchanged).

## Why
The classic audio path places narration and a ducked music bed with `amix` and no loudness control: measured renders
came out near -18 LUFS with no effects tied to what happens on screen.

## Design
- `sfx.py`: synthesised one-shot effects (numpy only, seeded), `cue_track` (sample-exact placement plus a short bus
  reverb) and `hush_gain` (music drops, computed only over each hush's own window).
- `render.scene_cues`: a scene's `sfx` cues on the video clock, rounded to frames; cues at or past their scene's
  length are dropped with a warning.
- `mixdown.master`: voice chain (ffmpeg filters), music volume, fades, pocket EQ and hushes, the effects bus, then the
  sum is scaled to a peak of `HEADROOM` (0.9, never clipped), measured with `ebur128`, gained to the target and
  limited by `alimiter` at 4x oversampling, measured again and corrected once. Silence is left silent. User music files
  are decoded to mono 44.1 kHz before mixing.
- Validation: `sfx` per scene (40 max), `MAX_CUES` (400) per spec, `sfx` requires `mix`, `mix` requires a planned
  length of at most `MAX_MIX_SECONDS` (600 s; `master` checks the final length again), since the path holds whole tracks
  in memory.
- `music.py`: style `drive` (layers gated by scene `energy`, beat-keyed pump on the pad and bass layers).
- `theme.py` / `scenebase.py`: optional theme fields `bg2`, `glow`, `faces` (None for the classic themes); `studio`
  uses a precomputed gradient background and a face set that falls back to DejaVu.

## Limits and follow-ups
- Streaming the mix in blocks would lift the 600 s limit.
- The voice chain and limiter need ffmpeg's `equalizer`, `acompressor` and `alimiter` filters.
- `voiceover.debreath` can silence unvoiced consonants; a word-timed or stricter voicing test is a follow-up.
