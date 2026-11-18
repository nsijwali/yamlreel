# Choreography guide for Shorts

How to build a Short at the quality of the approved reference
(`shorts/2026-11-18-rate-limiting-pro.yaml`, theme `studio`, vertical). Read this page, then the folder for each
move you use. Every `rules.md` has a YAML snippet that `tests/test_choreography_docs.py` validates, so copy it.
Pipeline steps (script, voice, render, deliver) are in `shorts/templates/README.md`; the engine is specified in
`docs/specs/motion-layer.md` and `docs/specs/sound-design.md`.

## Moves
| Folder | Use it for |
|---|---|
| [packet-flow](packet-flow/rules.md) | a request travelling between boxes, passing or bouncing at a gate |
| [zoom-focus](zoom-focus/rules.md) | camera push onto one part of a diagram, then pull back |
| [counter-tick](counter-tick/rules.md) | a number that steps (97, 98, 99, 100) as events land |
| [timeline-burst](timeline-burst/rules.md) | events on a time axis, a reset line, bursts either side, a total |
| [error-flash](error-flash/rules.md) | the payoff: a red flash, a big code (429) punching in, camera shake |
| [card-stack](card-stack/rules.md) | a list or option set revealed one card at a time |
| [number-chain](number-chain/rules.md) | a back-of-envelope result derived on screen, or a 1 : 100 comparison drawn to scale |
| [ending-card](ending-card/rules.md) | the series ending: next part, follow and subscribe, channel name last |
| [lyric-breath](lyric-breath/rules.md) | a sung line with transliteration and translation, fading like a breath (`dusk` theme) |

Failures we already hit, one line each with the fix: [failures.md](failures.md).

## Shared rules (all moves)
1. **Look at frames before saying done.** Run `shorts/framecheck.py <spec> <out>`; open every sheet. Check 0.04 s,
   0.5 s, 1 s and every scene's start and end. Frame 0 is the poster frame: it must already show the hook.
2. **One move per narrated beat.** A beat is one sentence or clause. Never start a second move while the first is
   still moving; leave the previous move's end plus 0.1 s.
3. **A sound cue on every move** (`sfx:` needs `mix:`). Impacts land 0.1 s before the word they punctuate.
   Meanings: tick = a request lands or a counter steps, pop = a card lands, click = a label lands, whoosh =
   scene change or camera push, deny = refused, hush then riser then hit = the payoff.
4. **Timing comes from the audio.** Set each element's `at` to the word time in `scene-NN.words.json` + 0.3 s
   (narration lead). Scene `seconds` = clip length + 0.5 to 0.8 s.
5. **Readcheck hold:** text stays on screen at least `letters / 15 + 1.5` seconds (letters of that text). Do not
   exit or replace it sooner.
6. **Entry before motion.** An `emphasis` or the first `move` starts at or after `at + enter.seconds`. Movers use
   `enter: none`. Each move starts after the previous one ends.
7. **Safe zones (studio theme).** Vertical: every element bottom `y <= 0.69` (zoom included), captions at 0.76 H.
   Landscape (1920x1080): content `y <= ~0.76`, captions at 0.88 H; px sizes mean the same pixels as in vertical but y
   fractions divide 1080, so a 130 px card is 0.12 tall (vertical 0.068): recompute every bottom as `y + h/2/1080`. Under 60 drawable leaves per scene (groups and split
   words count).
8. **Measure audio, do not guess.** Target -14 LUFS, true peak `<= -1` dBTP via `shorts/audiomeasure.py`. Voice
   comes from `shorts/voicedeliver.py` (Whisper-timed pauses), never `debreath`.
9. **Say what was measured and what was assumed** in the hand-off: "measured: loudness, peak, frames at X;
   assumed: Y".

## Validate
`yamlreel validate <spec>` must pass. For a snippet or a scene, wrap it in a spec with `theme: studio`,
`formats: [vertical]`, `mix: {}` and run the same command (the test does this).
