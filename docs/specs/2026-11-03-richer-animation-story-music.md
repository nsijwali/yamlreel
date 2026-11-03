# Richer animation, storytelling and music

Status: proposed. Base: yamlreel 0.2.2 (`yamlreel/__init__.py`). Six independent PRs (S1-S6), merged in order.
Scope: generic tool features only. Examples use neutral CS topics; nothing channel- or person-specific ships.

## 1. Problem and requirements

Today yamlreel animates *containers* (boxes, bullets, typed text) but not *data*. Scenes cut with one hard-coded
0.25 s cross-fade (`render.FADE_IN`). There are no word-timed subtitles, no story scaffolding, and one music
generator (`music.compose`, koto). Explainer reels that work animate the data itself (rows revealed one at a time,
a scan bar walking a table, a counter ticking to 10,000,000, a tree built step by step), with keyword-highlighted
subtitles, "STEP n/N" progress and a comment-question end card.

**Functional**
- F1 Animation: data scenes (table, array, tree, meter, timeline), count-up `stat`, staggered reveals, per-scene
  transitions (fade, slide, zoom, cut).
- F2 Subtitles: word-by-word burned-in captions timed from the narration audio, keyword-highlighted, plus an SRT
  sidecar. No heavy dependencies.
- F3 Storytelling: `step` progress badge, `cta` end-card scene, and `yamlreel story` (outline in, skeleton spec out:
  hook -> problem -> mechanism steps -> when to use -> CTA).
- F4 Music: four new synthesized styles (ambient, lofi, pulse, piano), more scales, per-style defaults, per-scene
  `energy`, ducking under narration.

**Quality attributes**
- Backward compatible: every spec valid on 0.2.2 still validates and renders the same frames and the same koto
  audio when no new key is set. Narrated specs keep today's constant music level unless `duck` is on.
- Dependencies: pillow, numpy, pyyaml only (already required). No new runtime dependencies.
- 8 GB Mac, one format at a time: peak RSS for a 60 s vertical render at 30 fps stays at or below 1.2 GB.
- Deterministic: same spec, seed and library versions give the same frames and audio (music seeded, scenes use no RNG).
- Strict validation as today: unknown keys are errors, every new field has a range, messages keep the
  `<path> must be ...` / `<path>: <problem>` shapes.

## 2. Constraints and assumptions

- A1. Rendering stays per-frame PIL piped to ffmpeg (`render.render`). No ffmpeg video filters, so the static
  `imageio-ffmpeg` build keeps working.
- A2. VoiceStudio's `generate_speech` returns WAV only, with no word timestamps (`voice._find_wav_b64`). We do not
  assume it ever will.
- A3. `config.py` and `voice.py` are owner-reviewed files (`batch1-trust-the-input.md`). S1, S3 and S5 touch them
  minimally.
- A4. 0.3.0 is already earmarked for dropping legacy names (`config.py` docstring). The owner decides whether these
  slices ship as 0.2.x minors or ride 0.3.0. No version bump inside a slice PR.
- A5. Tests have DejaVu fonts (existing tests already need them). No pixel goldens, because font rasterisation
  differs across Pillow/FreeType versions.
- A6. Forward compatibility is not promised: a spec that uses new keys fails on 0.2.2 with "unknown field".

## 3. Proposed design

### 3.0 Shared structure
- New `yamlreel/scenebase.py` holds `Ctx`, `Scene`, `_appear` and a static-layer cache helper. `scenes.py`
  re-exports `Ctx` and `Scene` so existing imports keep working.
- New `yamlreel/datascenes.py` holds the table, array, tree, meter, timeline and cta classes. `scenes.py` imports
  it and merges them into `SCENE_TYPES`; with the base classes in `scenebase.py` there is no import cycle.
- `textkit.py` gains `EASINGS = {"linear", "out", "in_out", "back"}` and `ease(name, t)`; `ease_out`/`ease_in_out`
  stay. Easing is internal (no per-element `ease` key), to avoid schema nobody has asked for.
- Cue model (same as diagram `at`): every timed element takes `at` (scene-local seconds, 0..`MAX_CUE_SECONDS`).
  Without `at`, elements auto-stagger in list order from 0.35 s, `stagger` (default 0.4) apart.
- Performance rule for new scenes: all text is pre-rendered to `TextBlock`s in `__init__`, static chrome (grid, axis,
  table frame) goes to one cached RGBA layer, and `frame(t)` composites only the dynamic parts. No 2x supersampled
  full-frame layer per frame (diagram's `SS=2` allocates about 33 MB per vertical frame).
- Drift guard: `tests/test_validation.py::_scene_reads` looks up helpers in `sys.modules[cls.__module__]` instead
  of `scenes_mod`. Nested field tables (`ROW_FIELDS`, `STEP_FIELDS`, ...) get the same guard as `NODE_FIELDS`.

### 3.1 Animation (S1, S2, S6)

**Transitions (S1).** A top-level default plus a common scene field; both describe how a scene *enters*:
```yaml
transition: fade                   # default for every scene; or {type: slide, seconds: 0.35}
scenes:
  - type: table
    transition: {type: zoom, seconds: 0.4}   # fade | slide | zoom | cut; seconds 0.1..1.0
```
`Timeline.frame_at` keeps today's mechanism (blend with scene i-1's cached last frame during the first `seconds`
of scene i; total length unchanged). `fade` is exactly today's `Image.blend` over 0.25 s. `slide` pushes: the old
frame moves left by `p*W`, the new one enters from the right (`ease("in_out")`). `zoom` scales the new frame 1.08
to 1.0 while fading. `cut` does nothing. `Timeline._last` keeps only scene i-1's frame (today it keeps every scene,
about 6 MB each in vertical). `config.DEFAULTS["transition"] = {"type": "fade", "seconds": 0.25}`, and
`transition` joins `SHARED_KEYS`.

**Staggered reveals (S1).** Optional `stagger` (0.1..3 s) on `bullets` (later also table, array, tree). In
`bullets` it replaces the computed `step` only when set.

**Count-up stat (S1).**
```yaml
- type: stat
  value: "10,000,000 rows"        # final text; exactly one number in it is animated
  count: {from: 0, seconds: 2.5}   # or `count: true` (from 0 over 60% of the scene)
  label: scanned without an index
```
The validator parses the one number in `value` (`[-+]?\d[\d,]*(\.\d+)?`). Zero or two numbers is an error:
`scenes[3].count: value must contain exactly one number to count`. Prefix, suffix, thousands separator and decimals
are copied from `value` and formatted in code; a user-supplied string never reaches `str.format` (which would allow
attribute access). The value block is re-rendered only when the displayed string changes (small LRU cache).

**Table (S2).** Rows appear one by one, a scan bar walks them, a hit is highlighted, an optional counter ticks.
```yaml
- type: table
  title: users
  columns: [id, name, email]            # 1..6
  rows:                                 # 1..12, each exactly len(columns) cells, cell text <= 40 chars
    - [1, Ada, ada@example.com]
    - [2, Lin, lin@example.com]
    - ["...", "...", "..."]
  stagger: 0.25                         # row reveal spacing; `reveal: all` shows every row at once
  scan: {at: 1.5, per_row: 0.3, stop: 4, color: accent}   # bar walks from row 0, stops and pulses on `stop`
  highlight: [{row: 4, at: 4.0, text: found, color: accent2}]
  counter: {label: rows scanned, to: 10000000, at: 1.5, seconds: 3}
```
Column widths follow their longest cell; text shrinks via `ctx.block` (with a warning). In vertical the counter
sits below the table. Test accessors: `TableScene.row_box(i)`, `scan_row(t)`, `visible_rows(t)`.

**Array (S2).** One row of cells, for queues, stacks, sorted inserts, marks and sliding windows.
```yaml
- type: array
  title: Sorted index on email
  items: [ada, kim]                     # initial cells, 0..12
  steps:                                # 0..40; exactly one action key per step
    - {at: 0.8, insert: lin}            # sorted insert: cells shift apart, new cell drops in
    - {at: 1.6, push: zoe}              # append at end (enqueue)
    - {at: 2.4, pop: front}             # front | back (dequeue / pop)
    - {at: 3.2, mark: [1, 2], text: match, color: accent}
    - {at: 4.0, window: [0, 2], text: "last 3"}   # bracket over cells; later window steps slide it
    - {at: 5.0, unmark: true}
```
`datascenes.array_states(spec) -> list[ArrayState]` is a pure function: (t, cells with stable ids, marks, window).
`frame(t)` interpolates cell positions from the previous state over 0.4 s (`back` for drops, `in_out` for shifts).
Validation replays the states, so errors surface at load time: `steps[3].pop: array is empty at 2.4s`,
`steps[4].window: [0, 5] is outside 0..3 cells`, and more than 12 cells at once.

**Tree, meter, timeline (S6).**
```yaml
- type: tree                            # tidy top-down auto-layout; <= 31 nodes, depth <= 5, <= 6 children
  title: B-tree index
  nodes:
    - {id: r, label: "M"}               # first node is the root; parents must be declared earlier
    - {id: a, label: "D H", parent: r}
    - {id: b, label: "Q T", parent: r, at: 1.2}
    - {id: c, label: "A B C", parent: a}
  path: [{at: 3.5, nodes: [r, a, c], text: "3 hops"}]   # edges+nodes light up in sequence, 0.35 s per hop

- type: meter                           # fill-and-drain: bucket | bar | slots (slots: capacity <= 20)
  title: Token bucket
  shape: bucket
  capacity: 10
  start: 10
  unit: tokens
  in_label: refill 1/s                  # optional; drops fall in while the level rises
  out_label: requests                   # optional; drops leave while the level falls
  steps: [{at: 1.0, level: 4, seconds: 0.8, text: "burst of 6"}, {at: 3.0, level: 0, text: "empty = 429", color: accent},
          {at: 4.5, level: 10, seconds: 4}]

- type: timeline                        # items on an axis plus a sliding window with a live count
  title: Requests in the last 60 s
  axis: {from: 0, to: 120, unit: s, ticks: 30}
  events: [{x: 5, at: 0.4}, {x: 22, at: 0.6, label: req}, {x: 71, at: 0.8, color: accent}]   # <= 60
  window: [{at: 1.0, range: [0, 60]}, {at: 3.0, range: [40, 100], text: slides}]
  count: true                           # "N in window", updates as the window moves
```
`tree_layout(nodes) -> {id: (x, y)}` is pure, in frame fractions: leaves get consecutive slots and each parent is
centred over its children. Meter levels are validated against 0..capacity.

### 3.2 Subtitles (S3)
```yaml
subtitles:                       # or `subtitles: true` for all defaults; default off
  style: karaoke                 # karaoke (active word boxed in accent) | pop (active word scales 1.15) | plain
  words: 4                       # words per chunk on screen, 1..8
  position: lower                # lower (ctx.caption_y) | middle
  size: 1.0                      # 0.6..1.6 x base (52u, x1.2 in vertical)
  uppercase: false
  keywords: [token bucket, "429"]  # case-insensitive whole-phrase match, drawn in accent2
  numbers: true                  # numbers count as keywords
  srt: true                      # write <out>/<fmt>/subtitles.srt
scenes:
  - type: diagram
    narration: ...
    keywords: [refill]           # per-scene additions
    subtitles: false             # per-scene opt-out
  - type: bullets
    narration_audio: take3.wav
    narration: "The exact words spoken in take3.wav"   # transcript when audio is given
    narration_words: take3.words.json                  # optional exact timings
```
**Word timings, in order of preference:**
1. `narration_words`: JSON `[{"word": str, "start": s, "end": s}, ...]` relative to clip start, at most 2000 entries
   and 256 KB, confined with `resolve_local` like `narration_audio`. Any offline aligner (whisper, or
   MoneyPrinterTurbo output) can produce it outside yamlreel. Times must be monotonic and within clip length + 0.2 s.
2. Default: a pure-numpy estimate, `subtitles.estimate_words(text, samples)`. (a) RMS over 20 ms frames,
   threshold max(5% of peak, 90th-percentile floor), trim leading/trailing silence. (b) Find silent gaps of 120 ms
   or more. (c) Split text on whitespace; each word weighs its vowel-group count (min 1), digit runs 1.5 per digit.
   (d) Boundaries come from `,;:` (weak) and `.!?` (strong); assign the longest gaps to boundaries in time order,
   strong first, and fall back to a proportional split when gaps run out. (e) Inside each segment, split time by
   weight. Deterministic and O(n), with about 150 ms expected error per word, mostly hidden by showing a 3-5 word
   chunk with the active word highlighted.
3. Whisper is not added: it needs torch or ctranslate2 (not pure Python, about 1 GB RAM). See section 4.

**Source of truth.** `voice.narration_text(sc, vo) -> str | None` is extracted from `prepare_narration`, so speech
and subtitles always use the same string (including `read_screen`). A clip with no text gets no subtitles and a
warning: `scene 4: narration_audio has no transcript; add narration: text for subtitles`.

**Rendering.** `render.render` computes `words = subtitles.timings(spec, clips)` (absolute times via `tl.starts +
NARRATION_LEAD`) and passes it to `Timeline(..., words=...)`. `Timeline._scene_frame` draws the chunk after the
scene frame, so transitions, poster and contact sheet include it. Chunk masks are pre-rendered once per chunk.
Words get a 0.08 em stroke in `theme.bg` (`ImageDraw.text(stroke_width=...)`); `textkit.layout_words(words, font,
max_w)` returns word boxes. To avoid overlap, a scene with active subtitles shows its `caption` pill only after its
speech ends (+0.2 s), as the takeaway. Scenes without narration behave as today.

### 3.3 Storytelling (S4)

**Step badge.** New common field `step: "Token bucket"`. N is the number of distinct step labels in order, and n is
this scene's ordinal (consecutive scenes may share a step). Timeline draws a top-centre badge `STEP n/N · LABEL`
with an N-segment progress bar at 0.035H, clear of the diagram title at 0.09H. It warns when a top-left watermark
wider than 0.3W would collide.

**CTA end card.**
```yaml
- type: cta
  question: Which limiter does your API use?     # required
  answers: [Token bucket, Sliding window]        # 0..4, A/B/C/D chips, staggered
  prompt: Comment A or B                          # default "Answer in the comments"
```
The bubble pops in (`ease("back")`) and the prompt pulses once a second. `share_copy` appends the first cta
`question` to the description, before the hashtags, unless it is already there. `voice.screen_text` reads
question, answers and prompt.

**Story generator.** `yamlreel story outline.yaml --out spec.yaml [--theme T] [--format F] [--force]`: offline,
deterministic. Outline schema (`story.py`, strict, unknown keys are errors):
```yaml
topic: Rate limiting                          # required -> meta.title, final title card
hook: One noisy client can take your API down.  # required
problem: One burst starves every other client.  # optional
steps:                                        # 1..6, required
  - {name: Token bucket, say: "Each request spends a token. Tokens refill at a steady rate.", show: meter}
  - {name: Sliding window, say: "Count requests in the last sixty seconds.", show: timeline}
when_to_use: ["Bursty traffic: token bucket", "Exact limits: sliding window"]   # optional
question: Which one does your API use?        # optional -> cta
answers: [Token bucket, Sliding window]
```
Output is a plain spec:
- hook: `type`, `energy: high`, `transition: cut`
- problem: `title` with eyebrow `THE PROBLEM`
- each step: a scene of type `show` (default `bullets`) with `step: name`, `narration: say`, `energy: mid`, and
  placeholder required fields from `story.PLACEHOLDERS[type]` (for example a 3x3 TODO table)
- `when_to_use`: `bullets` titled "When to use"; then `cta` with `energy: low`
- top level: `subtitles: true`, `voiceover: {duck: true}`

Seconds are `max(DEFAULT_SECONDS[t], words / 2.5 + 1)` (narration stretches them anyway). A header comment points
at the TODO fields. The output must pass `load_spec`. `show` must be a `PLACEHOLDERS` key (grows when S6 lands).
Without `--force` it refuses to overwrite, like `init`. `templates/explainer.yaml` (`yamlreel init explainer`)
shows the same arc by hand.

### 3.4 Music (S5)

`MUSIC_STYLES = {koto, ambient, lofi, pulse, piano, none}`.

| style | voices (`synth.py` primitives) | default bpm / scale | layers by energy (low / mid / high) |
|---|---|---|---|
| koto | unchanged `compose` | 72 / hirajoshi | ignores energy |
| ambient | detuned additive pad chords (i-VI-III-VII), filtered-noise air | 60 / major | pad / +bell motif / +sub pulse |
| lofi | FM e-piano 7ths, sine-sweep kick, noise snare/hat, 55% swing, sparse crackle | 78 / dorian | keys / +hat,kick / +snare,bass |
| pulse | soft pluck 8th arpeggio (FIR-lowpassed), sub bass | 100 / minor | arp / +bass / +kick 1&3 |
| piano | additive partials, slight inharmonicity, exp decay; seeded walk + LH fifths | 76 / major | RH / +LH / +octave doubling |

New `SCALES`: major, minor, dorian, pentatonic_major, pentatonic_minor (the old four stay; any scale works with any
style).

**Per-style defaults.** `config.DEFAULTS["music"]` drops bpm/root/scale and becomes `{style: koto, seed: 7,
volume: 0.9, snap: false}`. `specs._music` fills missing bpm/root/scale from `music.STYLE_DEFAULTS[style]` (koto's
for `none` and audio files, so `snap` still has a bpm) and records them in `music["_filled"]`. `--music STYLE`
refills only `_filled` keys from the override's defaults. Koto specs resolve to 72/D/hirajoshi exactly as today.

**Energy.** New common field `energy: low|mid|high` (default mid). `render` builds `sections = [(start, end,
level)]` from `tl.starts` and passes them to the style. Boundaries snap to the nearest beat; layers crossfade over
one beat.

**Generation, memory-bounded.** Each new style first builds a global event list (time, voice, pitch, velocity,
layer), cheap and seeded by `default_rng(seed)`. It then renders 30 s blocks (plus a 3 s tail) in float32 with
overlap-add reverb (`synth.reverb_ola`, FFT blocks of 2^17). Gain staging is fixed with a `tanh` soft clip instead
of whole-track peak normalising, and blocks stream through `music.write_wav_stream(path, blocks)`. Peak memory is
O(block), about 30 MB, at any length. Koto keeps its whole-buffer path so it stays bit-identical (pre-existing risk,
section 6).

**Ducking.** `voiceover.duck: true|false`, default false (today's constant level). When true:
- Speech regions come from clip placement, `[start_i + lead, start_i + lead + len_i]`; no detection needed.
- Gain is 1 outside speech and `music_level` inside, ramping down 0.15 s before speech and back up over 0.5 s
  after. Gaps under 0.6 s stay ducked.
- The envelope is applied in numpy, per block, to generated music. File music is first decoded to mono 44.1 kHz WAV
  with the same `-protocol_whitelist file -format_whitelist AUDIO_FORMATS` guard, then enveloped the same way.
- The ffmpeg graph then uses `volume=vol`, not `vol*level`.

**CLI.** `yamlreel music --style lofi --seconds 30 ...`; `cli._runtime` treats every `MUSIC_STYLES` name as built
in, not as a path.

### 3.5 Data model / contract summary

| Change | Where |
|---|---|
| Top-level `transition`, `subtitles` | `config.SHARED_KEYS`, `DEFAULTS`, `specs.TOP_CHECKS` |
| `voiceover.duck` | `specs._voiceover` allowed keys |
| Music styles, scales, `_filled` | `specs.MUSIC_STYLES`, `music.SCALES`, `STYLE_DEFAULTS` |
| Common scene fields `transition`, `step`, `energy`, `subtitles`, `keywords`, `narration_words` | `Scene.COMMON_FIELDS`, `FIELD_CHECKS` |
| `stat.count`, `bullets.stagger` | `StatScene.FIELDS`, `BulletsScene.FIELDS` |
| New types `table`, `array`, `cta`, `tree`, `meter`, `timeline` | `SCENE_TYPES`, `REQUIRED`, `DEFAULT_SECONDS`, nested `*_FIELDS` |
| New limits (`specs.py`) | `MAX_TABLE_ROWS=12`, `MAX_TABLE_COLS=6`, `MAX_CELLS=12`, `MAX_STEPS=40`, `MAX_TREE_NODES=31`, `MAX_EVENTS=60`, `MAX_WORDS_ENTRIES=2000` |
| Outputs | `<fmt>/subtitles.srt` (when `subtitles.srt`); `--contact-frames N` (1..8) stills per scene |

## 4. Alternatives considered

- **Generic keyframe/shape DSL** (rect/text/arrow with keyframes): rejected for now. Too much YAML per video, specs
  read badly, and it is hard to validate. Semantic data scenes cover the reference reels in about 10 lines each. A
  `shapes` escape hatch can come later if real specs need it.
- **Manim, MoviePy, Cairo**: rejected. Native dependencies (cairo, LaTeX) and a second render path; MoviePy adds
  nothing over PIL plus ffmpeg.
- **ffmpeg `xfade` transitions or libass `\k` karaoke**: rejected. We stream one rawvideo pipe (xfade needs
  segmented inputs), libass is missing from some static builds, and its font handling is not deterministic. PIL
  compositing reuses theme fonts and is unit-testable.
- **Whisper (openai-whisper or faster-whisper) as a dependency or extra**: rejected for core. torch/ctranslate2 are
  not pure Python, need a model download and about 1 GB RAM. **aeneas** is AGPL and needs espeak. `narration_words`
  import gives the same accuracy with zero dependencies.
- **Render-time `story:` macro expanded in `validate`**: rejected. Errors would point at generated paths, scenes
  could not be hand-tuned, and the renderer would gain hidden behaviour. The generator writes an explicit spec.
- **ffmpeg `sidechaincompress` for ducking**: rejected. The exact speech intervals are already known, and a numpy
  envelope is deterministic and testable.
- **Bundled loops or AI music APIs**: rejected for licensing (a MoneyPrinterTurbo lesson), paid keys and network
  access.
- **Keeping bpm/root/scale in `DEFAULTS`**: rejected. The merged spec cannot tell "user set 72" from "default 72",
  so per-style defaults could never apply. Cost: one named test edit (S5).

## 5. Migration, rollout, backward compatibility

- One PR per slice from `main`, each with its own test file. Existing tests pass unchanged except two named edits:
  S2 changes the `_scene_reads` module lookup; S5 changes `tests/test_validation.py:623` (asserts
  `cfg.data["music"]["bpm"] == 72`) to assert on the validated spec.
- Defaults reproduce 0.2.2: fade 0.25, subtitles off, duck off, koto ignores energy, no badge, koto 72/D/hirajoshi.
- Visible changes: `yamlreel config show` stops listing music bpm/root/scale (resolved per style); `--music` accepts
  the new style names.
- Each slice updates the README scene table and "All spec options" block in the same PR and adds one runnable
  example under `examples/` (neutral topics: index scan, queue, cache).

## 6. Risks, failure modes, edge cases, observability

| Risk | Mitigation / check |
|---|---|
| Memory: full-res frames (6 MB), `_last` cache, music buffers | Keep only scene i-1's last frame; new styles stream 30 s blocks; static layers cached once per scene. Each PR records peak RSS from `/usr/bin/time -l yamlreel render examples/<x>.yaml --format vertical` (60 s, 30 fps); limit 1.2 GB. Koto's whole-buffer FFT reverb (several GB for an hour-long video) is a pre-existing risk left as is. |
| Render time | A new scene's `frame(t)` costs at most 1.5x a diagram frame at the same size. Each PR states `--preview` and full-render timings of its example. No timing asserts in CI (flaky). |
| Test determinism | No pixel goldens. Use geometry accessors (`row_box`, `scan_row`, `array_states`, `tree_layout`) and coarse pixel samples inside known boxes. Music: in-process equality, length, peak, spectral bands; koto checked against a vendored copy of the 0.2.2 function (`tests/_koto_v022.py`). |
| Subtitle drift (estimate) | Chunking hides most of it. Warn for scenes whose detected gaps under-match boundaries by more than 50%. `subtitles.srt` lets you check timing in any player; `narration_words` gives exact timing. |
| Dense tables/arrays in vertical | `ctx.block` shrinks and warns; hard caps on rows/cols/cells; warn when a cell font is under 22u. |
| Badge, watermark and title collisions | Badge at 0.035H, warning for a wide top-left watermark; the contact sheet is the gate. |
| Array/meter step logic errors | Replayed at validate time; errors name the step path and time. |
| Story output that fails validation | A test runs `load_spec` on generated specs for every `show` type. |
| Owner-reviewed files (`config.py`, `voice.py`) | Minimal changes, listed per slice. |

**Observability.** Everything goes through the existing `ctx.warn` / render `warnings` list (shrunk text, estimated
subtitles, deferred captions, collisions). `--contact-frames N` gives N evenly spaced stills per scene (default 2,
as today). `subtitles.srt` holds the cue times. `yamlreel validate` adds narrated/subtitled scene counts to `ok:`.

## 7. Test plan (the builder must satisfy all of it)

Each slice adds `tests/test_<slice>.py`; the full suite stays green. **Visual gate per slice:** render the slice's
example with `--preview --contact-frames 6` in vertical and landscape and attach both `contact.png` files to the PR.
The reviewer checks that reveals progress across the stills and that nothing overlaps or clips.

- **S1 `test_motion.py`.** Default fade equals `Image.blend(prev_last, cur, lt/0.25)` pixel for pixel. `cut`
  returns the current frame. `slide` at p=0.5 puts the old frame's left half in the output's left half. `zoom`
  stays in frame. Out-of-range `transition.seconds` is rejected. `_last` holds at most one frame. `stagger` changes
  bullet appear times. `count` on "10,000,000 rows" shows "0 rows" at t=0, a formatted mid value, and the exact
  final text at the end; zero or two numbers is an error. `--contact-frames 4` gives 4 tiles per scene.
- **S2 `test_data_scenes.py`.** `visible_rows(t)` is monotonic and reaches all rows. `scan_row` walks 0..stop then
  holds, and the pixel inside `row_box(stop)` has the accent tint after `at`. The counter reaches `to`.
  `array_states` covers sorted insert, push/pop front/back and window clamp. Errors: pop on empty, window out of
  range, more than 12 cells, two action keys in one step, row length mismatch. Both types render in all three
  formats (extend the existing size test). The drift guard covers the new nested tables.
- **S3 `test_subtitles.py`.** On a synthetic clip (three tone bursts split by 300 ms silences; text with two
  periods), `estimate_words` puts sentence boundaries inside the silences (±50 ms), keeps words monotonic and within
  the clip, and is deterministic. `narration_words` JSON loads, is confined to allowed folders, and is rejected when
  oversize, non-monotonic or out of bounds. `narration_text` matches between synthesis and subtitles (including
  `read_screen`). Keyword and number matching works. The caption shows only after speech in subtitled scenes. The
  SRT is well-formed (index, `HH:MM:SS,mmm`, ascending) and matches chunk times. An end-to-end render with the fake
  voice server (`tests/fakes.py`) writes `subtitles.srt`. Scene `subtitles: false` suppresses them.
- **S4 `test_story.py`.** Badge text is right (`STEP 2/3`, repeated labels counted once). `cta` validates 0..4
  answers and renders. `share_copy` appends the question exactly once. `yamlreel story` output passes `load_spec`
  for every `PLACEHOLDERS` type; the command refuses to overwrite without `--force` and errors on unknown outline
  keys and on 0 or 7 steps. `screen_text(cta)` is correct.
- **S5 `test_music_styles.py`.** Every style is deterministic per seed, differs across seeds, has the exact length
  and a peak in (0.2, 1.0]. Koto equals the vendored 0.2.2 copy. For lofi and pulse, `high` has more energy above
  2 kHz than `low`. The ducking envelope is `music_level` mid-speech and 1.0 one second after, with the specified
  ramps. File music with duck goes through the decode whitelist. Per-style defaults fill only missing keys, and
  `--music lofi` refills only `_filled` keys. The streamed WAV equals the concatenated blocks. 600 s of lofi peaks
  under 150 MB in `tracemalloc` (numpy allocations are tracked).
- **S6** (extends `test_data_scenes.py`). `tree_layout` centres parents over children, has no overlapping boxes at
  depth 5 with 6-wide fan-out, and errors on a parent used before it is declared. Meter level interpolation,
  clamping and slots work. Timeline window interpolation and the live count work. All three render in every format.

## 8. Slices and ordered implementation steps

Ordered by value and risk. Each ships alone; S4 depends on S2 only softly (`show` hints). One step is about one
reviewable commit.

**S1 Motion core** (low risk, foundation). Touches `scenebase.py` (new), `scenes.py`, `textkit.py`, `render.py`,
`specs.py`, `config.py` (`SHARED_KEYS`, `DEFAULTS`), `cli.py`, README.
Steps: (1) move `Ctx`/`Scene`/`_appear` to `scenebase.py` with re-exports, a pure move with the suite green;
(2) `textkit.ease`/`EASINGS`; (3) `transition` validation, `Timeline` transitions and `_last` eviction;
(4) `bullets.stagger`; (5) `stat.count`; (6) `contact_sheet(fracs=...)` and `--contact-frames`; (7) example, README.

**S2 Data scenes I: table + array** (highest visual value). Touches `datascenes.py` (new), `scenes.py`
(registration), `specs.py` (checkers, limits, `REQUIRED`, `DEFAULT_SECONDS`), `voice.screen_text` (title
fallback), the drift guard in `test_validation.py`, README.
Steps: (1) static-layer cache in `scenebase.py`; (2) table layout, reveal, scan, highlight, counter and validation;
(3) `array_states` and validation replay; (4) array rendering with interpolation; (5) `examples/index-scan.yaml`,
README.

**S3 Subtitles** (high value, medium risk: the audio/narration path). Touches `subtitles.py` (new), `voice.py`
(`narration_text` extraction only), `render.py`, `specs.py`, `config.py` (`SHARED_KEYS`, `DEFAULTS`), `textkit.py`
(`layout_words`), README.
Steps: (1) `narration_text` refactor with no behaviour change; (2) `estimate_words` and the confined JSON loader;
(3) chunking and keyword matching; (4) Timeline overlay and caption deferral; (5) SRT writer; (6) validation of
`subtitles`/`keywords`/`narration_words`; (7) example, README.

**S4 Story layer** (medium value, low risk). Touches `datascenes.py` (`cta`), `render.py` (badge, `share_copy`),
`specs.py` (`step`), `story.py` (new), `cli.py` (`story`), `templates/explainer.yaml`, `voice.screen_text`.
Steps: (1) `step` field and badge; (2) `cta` scene; (3) `share_copy` question line; (4) `story.py` outline
validation and `PLACEHOLDERS`; (5) spec emission with a `load_spec` self-check; (6) CLI and template.

**S5 Music styles, energy, ducking** (medium risk: changes config defaults). Touches `synth.py` (new), `music.py`,
`specs.py`, `config.py` (`DEFAULTS.music`), `render.py` (`_prepare_audio` signature, ffmpeg graph), `cli.py`,
README.
Steps: (1) vendor the 0.2.2 koto into tests and add the equality test; (2) `synth.py` primitives with block OLA
reverb; (3) event list, block renderer, `write_wav_stream`; (4) ambient and piano; (5) lofi and pulse; (6) scales,
`STYLE_DEFAULTS`, `_filled`, `DEFAULTS` change and the test edit; (7) `energy` and sections; (8) ducking for
generated and decoded-file music; (9) `music --style`, `--music` names, README.

**S6 Data scenes II: tree, meter, timeline** (low risk once S2 lands). Touches `datascenes.py`, `specs.py`,
`story.PLACEHOLDERS`, README, examples.
Steps: (1) `tree_layout` and path highlight; (2) meter (bucket/bar/slots, drops); (3) timeline (axis, events,
window, count); (4) examples, README.
