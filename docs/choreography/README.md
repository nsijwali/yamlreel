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
| [tile-map](tile-map/rules.md) | what is loaded now: a grid of tiles, a block in memory, the next column prefetched, the oldest evicted |

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

## Motion rules (what moves, and how much)
Weighted from the [design-motion-principles](https://github.com/kylezantos/design-motion-principles) lenses (polish first, playful for the mechanism, restraint for chrome).
The motion that explains the idea is the point of the Short; everything around it arrives quietly.
10. **Pop is for payoffs.** `pop` grows from 0.6 with an overshoot: use it on the one or two moments the video
    builds to (the failure, the answer, the big number), at most one per scene. Cards, labels and state markers use
    `slide-up` or `fade`.
11. **Exits are quieter than entrances.** Enters 0.3 to 0.5 s; exits `fade-out` 0.1 to 0.35 s. `slide-out`
    travels the full 60 u, so keep it for something that leaves the story.
12. **One pulse per video**, `repeat: 1`, on the key number. A looping pulse is noise.
13. **No auto stagger.** Set every `at` from the words: the default 0.4 s stagger on every element reads as a
    template. `split: words` only on the hook or the closing question.
14. **Easing by purpose:** `linear` for continuous flow (a packet, a car driving), `out` for arrivals and camera
    pushes, `in_out` for moves of things already on screen. `back` comes only through `pop`; there is no ease-in.
15. **Chrome does not animate.** Kickers, series tags and axis labels use `enter: none` or a 0.2 s fade.
16. **One canvas.** Beats that share a layout draw it at the same pixels in every scene with `transition: cut`
    between them; `zoom` transitions only at a real topic turn.
17. **No flashing, no big zooms.** Camera zoom stays at 1.1 or less (a focus push may reach 1.35); a shake is three
    camera keys on one impact, never on text.

## Building a Short from word times
Hand-placed `at` values drift as soon as the voice changes, so the spec is generated:
1. **Script first** (`shorts/<date>-<topic>-script.txt`): beats as input -> action -> result, one spoken line each.
2. **A builder script** (`shorts/build_<topic>_spec.py`, pattern: `build_asset_streaming_spec.py`) holds the
   narration list and one function per beat that returns `(elements, sfx, camera)`. Every time is a word:
   `c.t("dropped")` = that word's start in `out/<name>/voice/scene-NN.words.json` + 0.3 s. Before the voice exists
   the same call uses a 160 wpm estimate, so the first spec validates and renders.
3. **Scene length** = clip + 0.3 s lead + a 0.3 to 0.6 s tail (1 s or more on a recap that must be read). Pad the
   last scene so the total is a whole number of seconds (see failures).
4. **Voice pass** (`shorts/make-short.sh`): read the retake check. Reword every line it flags (the voice cache is keyed
   by the text) and run it again; flags on spelled acronyms ("G T A" heard as "GTA") are fine.
5. **Rebuild from the real times, render, run framecheck, look at every sheet**, fix the builder, re-render.
6. **Captions** come from Whisper. Map its spellings in `out/<name>/caption-fixes.json` (`"3.": "Three."`,
   `"Pre -Fetch": "Prefetch."`); `shorts/words_from_whisper.py` applies them.
7. **Deliver** the video, a designed cover and the share copy (`shorts/templates/README.md`, step 8).

## Validate
`yamlreel validate <spec>` must pass. For a snippet or a scene, wrap it in a spec with `theme: studio`,
`formats: [vertical]`, `mix: {}` and run the same command (the test does this).
