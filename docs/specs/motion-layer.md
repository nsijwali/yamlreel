# Motion layer: a native Pillow `motion` scene

Status: proposed. Base: yamlreel 0.3.6 (`main`). This spec extends `docs/specs/2026-11-03-richer-animation-story-music.md`
(the S1-S6 motion core, transitions, data scenes and story events). It replaces nothing in it.
Slices:
- S1: timeline, rect/circle/text, presets, flash, story events.
- S2: path draw-on, arrow, image, group, camera.
- S3 (later): native characters. This spec defines only its interface.

## 1. Problem, goals, non-goals

Only the `lineart` scene has real figure motion today. It costs 76-83 ms per frame because Chrome takes a
screenshot of every frame. It needs Playwright plus a Chromium, and it quietly falls back to typed text when they
are missing (benchmark 2026-11-15, sections 2, 5 and 8). The Pillow scenes animate fixed layouts: boxes appear, rows
reveal, numbers count. No element can move, scale, rotate or draw on, and there is no camera to follow anything.
The 8 GB Mac already swaps, so another browser worker is not the answer.

**Functional goals**
- G1. A new scene type `motion` (no clash in `SCENE_TYPES`) with an element list: `text`, `rect` and `circle` (S1),
  then `arrow`, `path`, `image` and `group` (S2).
- G2. Every element can move, scale, rotate, fade and draw on, with easing and stagger. The YAML stays small:
  presets (`enter`, `emphasis`, `exit`, `easing`, `stagger`) plus at most a few `moves`, never hand-written keyframe
  curves.
- G3. Camera zoom and pan keyframes (S2), applied to coordinates before drawing.
- G4. Reuse the diagram `flash` cue and the `yamlreel story` events (`EVENT_ACTIONS`) for motion scenes.
- G5. No Chrome, and little RAM. The Chrome `lineart` scene stays, unchanged, as the optional high-fidelity renderer.

**Quality attributes**
- **Deterministic and frame-rate independent.** Element state is a pure function of the scene-local time `t`.
  There is no RNG and no frame counter, and no later frame depends on an earlier one. This is what makes `--only`,
  reference-frame comparison and skipping identical frames work. It does not enable parallel frame drawing, and
  this spec does not claim it does: each extra frame in flight costs about 6 MB plus layers on a machine that
  already swaps.
- **Speed.** 15-25 ms per animated frame at 1080x1920, `standard` quality (section 8).
- **Memory.**
  - No full-frame supersampled layer per frame. This applies to motion the rule in section 3.0 of the 2026-11-03
    spec ("No 2x supersampled full-frame layer per frame").
  - Supersampling is per element and limited to the element's box, clipped to the frame.
  - Every raster layer has a hard pixel ceiling.
  - Retained caches have one byte budget per scene.
- **Strict validation, as today.** Unknown fields are errors, every value has a range, and every list has a ceiling.
  Messages keep the `<path> must be ...` and `<path>: <problem>` shapes with exact field names.
- **Backward compatible.** Every existing spec validates and renders byte-identical frames and identical audio.

**Non-goals**
- A general keyframe or shape DSL: per-property curves, SVG import, bezier handles. `moves` is the only hand-timing
  tool, and it is capped.
- Parallel frame drawing, or any change to the frame loop in `render.py`. Reusing the bytes of identical frames in
  the pipe (benchmark fix 4) is a separate follow-up.
- A camera on the existing `diagram` scene (open question Q2). Shadows, blur, gradients and 3D.
- Swapping `downsample` from LANCZOS to `reduce(2)`, which is a measured follow-up (Q3).
- Native characters. S3 gets an interface boundary only (section 12).
- Changes to:
  - the `type`, `meter`, `diagram` and `lineart` scenes;
  - `safety.py` (protected);
  - `config.py` and `voice.py` (owner-reviewed).

## 2. Constraints and assumptions

- **C1. Text sizes are whole pixels.** Dependencies stay pillow, numpy and pyyaml. `theme.font()` rounds sizes to
  whole pixels (`int(round(size))`, lru 256), so text cannot be re-rasterised at fractional sizes. Text scaling
  resamples a mask instead (section 5.4).
- **C2. `ImageDraw` does not blend on RGBA.** It writes pixels on an RGBA image. Shapes are therefore drawn opaque
  into their own sprite, and opacity is applied when the sprite is composited.
- **C3. `frame(t)` must return a new image on every call.** `Timeline._scene_frame` and `frame_at` paste the caption
  pill, subtitles, step badge and watermark onto the image that `Scene.frame` returns, and `_last_frame` keeps that
  image for transitions. A cached frame is therefore returned as `.copy()`, and the copy (about 1-2 ms) counts
  against the budget.
- **C4. What `tests/test_validation.py` requires of a new scene type.** The test iterates `SCENE_TYPES`, so it needs:
  - a `BASE_SCENES["motion"]` entry;
  - a non-empty `REQUIRED[name]` that is a subset of `FIELDS`;
  - every name in `FIELDS` as a key of `FIELD_CHECKS` (a `TYPE_CHECKS` override alone fails);
  - every field rejecting `{}` and the type-confusion values.

  It also asserts `TOP_CHECKS == SHARED_KEYS | {"scenes"}`. So there is no new top-level key, and `config.py` is
  not touched.
- **C5. YAML exposes exactly the four existing easings.** `tests/test_motion.py:50` pins `textkit.EASINGS` to
  `linear, out, in_out, back`. YAML offers exactly these four, and `textkit.py` is not edited.
  - **Deliberate deviation.** The per-element `easing` key overrides the "Easing is internal (no per-element ease
    key)" rule (2026-11-03 spec, section 3.0, and the comment above `EASINGS`). Presets need an override to stay
    one word in YAML.
  - The README and the `anim.py` docstring record this. `textkit.py` stays untouched.
- **C6. No committed pixel goldens.** Assumption A5 of the 2026-11-03 spec still holds ("no pixel goldens, font
  rasterisation differs"). Frame-hash criteria are therefore either in-process or a before/after comparison on the
  same machine (section 9).
- **C7. `downsample()` moves to `scenebase.py`.**
  - Today it lives in `scenes.py` with `SS = 2`, and `tests/test_render_speed.py` imports it from there.
  - `motion_scene.py` cannot import from `scenes.py`. `scenes.py` imports the motion module at its bottom to
    register it (the `lineart` pattern), so whichever module is imported first would meet a half-initialised one.
  - So `SS`, `_SS_MARGIN` and `downsample` move to `scenebase.py`, and `scenes.py` re-exports them. Existing import
    paths keep working.
- **C8. `read_screen` sees only the title and caption.** `voice.screen_text` sends unknown types to its `else` branch
  (title + caption). So `read_screen` reads a motion scene's `title` and `caption`, not its text elements. Changing
  that means editing `voice.py`, which is owner-reviewed (Q4).
- **A1. "Luna-style beat" means the input -> action -> result explainer beat.** The example uses a neutral topic
  (Q8).

## 3. Architecture

```
specs.py ──validates──> scene dict ──> scenes.build_scene ──> MotionScene(spec, ctx)      (motion_scene.py)
   │ field checkers (_elements, _motion_flash, _quality, _camera)       │ __init__: normalise tree, text blocks,
   │ limits/presets from anim.py; per-kind tables from KINDS            │          images, auto-times
   │                                                                     │ frame(t):
   │                                                                     │   states = anim.resolve(tree, t)  (pure)
   │                                                                     │   key = anim.state_key(states)
   │                                                                     │   hit -> last.copy()
   │                                                                     │   else: bg copy -> z-ordered composite of
   │                                                                     │         sprites/text/images -> flash tags
   └─ story.py: EVENT_ACTIONS["motion"] = ("flash",)                    └─ KINDS registry: rect, circle, text (S1)
                                                                           + arrow, path, image, group (S2), figure (S3)
```

| Module | Role | Pure? | Slice |
|---|---|---|---|
| `yamlreel/anim.py` (new) | Preset tables, the `Timing` and `State` dataclasses, `resolve(tree, t)`, group composition, `state_key`, timing limits | yes (no PIL) | S1 only |
| `yamlreel/motion_scene.py` (new) | `MotionScene(Scene)`, the `FIELDS` and nested `*_FIELDS` tables, the `KINDS` registry, the retained cache, rect/circle/text drawing, flash tags | no | S1, extended in S2 |
| `yamlreel/scenebase.py` | Gains `SS`, `_SS_MARGIN` and `downsample(layer, factor=SS)`, moved here unchanged at factor 2 | n/a | S1 |
| `yamlreel/scenes.py` | Re-exports those three names, and registers `SCENE_TYPES["motion"]` at the bottom, as `lineart` does | n/a | S1 |
| `yamlreel/specs.py` | Field checkers, plus `REQUIRED["motion"] = ["elements"]`, `DEFAULT_SECONDS["motion"] = 6.0`, `TYPE_CHECKS["motion"] = {"flash": _motion_flash}` and `_CHECKED_FIRST_BY_TYPE["motion"] = ("stagger", "elements")` | n/a | S1, S2 |
| `yamlreel/story.py` | `EVENT_ACTIONS`, `EVENT_EXTRAS`, `EVENT_SHOWS` and `PLACEHOLDERS` entries for `motion`. The flash branch of `_events` and `_apply_events` stops being diagram-only | n/a | S1 |
| `yamlreel/motion_paths.py` (new) | Polyline length and prefix (draw-on), Catmull-Rom sampling, dashes, arrow clipping to element boxes (a port of diagram's `exit_t`) | yes | S2 |
| `yamlreel/motion_camera.py` (new) | `Viewport(zoom, fx, fy)`, `camera_at(keys, t)`, `to_screen(pt)` | yes | S2 |
| `yamlreel/motion_image.py` (new) | Loads local images only, guards against decompression bombs, pre-scales once | no | S2 |

**Where validation lives: `specs.py`.**
- This is the existing pattern. The datascenes keep their tables and constants in the scene module and their
  checkers in `specs.py`.
- A separate `motionspec.py` could not import `SpecError` without a cycle: `specs -> scenes -> motion_scene`.
- Timing rules come from constants in `anim.py`, and preset applicability comes from `KINDS`. The validator and the
  renderer therefore cannot drift apart.
- Errors carry the full path, for example `scenes[2].elements[3].children[1].w`.

**The element kind registry: `motion_scene.KINDS: dict[str, ElementKind]`**

```python
class ElementKind(Protocol):
    FIELDS: frozenset[str]                     # kind-specific fields (plus COMMON_ELEMENT_FIELDS)
    PRESETS: frozenset[str]                    # enter/emphasis/exit presets this kind supports (e.g. "draw")
    def prepare(self, el: dict, scene: "MotionScene") -> None   # once in __init__: text blocks, images, geometry
    def shape_key(self, el: dict, st: State) -> tuple            # everything that changes the sprite's pixels
    def bbox(self, el: dict, st: State) -> tuple[float, float, float, float]   # px, relative to the anchor
    def raster(self, el: dict, st: State, f: int, clip: Box) -> Sprite   # RGBA at 1x + offset from the anchor
```

S2 and S3 add a kind by adding an entry and its field table. The engine code (resolve, z-order, caches, camera)
stays as it is, and so does `anim.py`.

## 4. Timeline semantics

**Time.**
- `t` is scene-local seconds, the same `t` that `Scene.frame(t)` gets.
- Every element time is absolute scene-local seconds, as diagram `at` is: `at`, `end`, `moves[].at`,
  `emphasis.at`, `flash[].at` and `camera[].at`. A child's time is never relative to its group.

**Auto times.**
- A top-level element without `at` gets `0.35 + k * stagger`, where:
  - `k` counts the earlier auto-timed top-level elements;
  - `stagger` is the scene's `stagger`: default 0.4, range 0.1..3, checked by the existing `_stagger`.
- **Group children.** Defaults are `enter: none` and the group's `stagger: 0`, so children appear with their group
  and move with it. A child without `at` gets `group.at + k * group.stagger`. Staggering and per-child enters are
  opt-in.
- **`split: words` text.** The parent text acts as a group whose own enter is `none`.
  - The parent's `enter` preset is given to each word.
  - Each word gets `at = parent.at + k * stagger` (element `stagger`, default 0.08).
  - The parent's `emphasis`, `exit` and `moves` stay on the parent and apply to the line as a whole.
  - `split: words` together with `enter: draw` is rejected.

**What each element has.**
- A start `at` and an enter duration (`enter.seconds`).
- An optional exit start `end` and an exit duration (`exit.seconds`).
- An `easing` and a `layer`.
- A **final (rest) state**: the element's own fields `x, y, scale, rotate, opacity`.
- An **initial state**: the rest state with the enter preset applied at p = 0.

**The state function.** All values are floats, and opacity is clamped to [0, 1] after every step.
1. Before `at`, the element is hidden.
2. `base(t)` is the rest state changed by `moves`, in time order:
   - during move k: `lerp(R[k-1], R[k], ease(easing_k, (t - at_k) / seconds_k))` for `x, y, scale, rotate, opacity`;
   - after it: `R[k]`; any field the move does not set carries over.
3. Enter: `p = ease(enter.easing, clamp((t - at) / enter.seconds))`, then the preset is applied to `base`
   (section 6.3).
4. Emphasis: a multiplicative scale (pulse).
5. Flash: changes the stroke colour and width (the scene's `flash` cue).
6. Exit: when `end` is set and `t >= end`, `q = ease(exit.easing, clamp((t - end) / exit.seconds))`, then the
   preset is applied. After `end + exit.seconds`, the element is hidden.
7. Group composition:
   - `child.world_anchor = parent.anchor + R(parent.rotate) * (child.offset * parent.scale)`;
   - `scale` and `opacity` multiply, and `rotate` adds;
   - a group's opacity multiplies into each child, so overlapping children of a fading group show where they
     overlap (accepted and documented, R6).

These steps run in the order above, which makes every state fully determined. The closed-form tests (AC-2) check
exactly this order.

**Timing rules (validation errors).**
- `end >= at + enter.seconds`.
- The first move starts at or after `at + enter.seconds`.
- Each move starts at or after the previous move ends.
- An emphasis starts at or after `at + enter.seconds`, and ends at or before `end` when `end` is set.
- Camera keys do not overlap.
- A flash's `at` is at or after its element's explicit `at` (the diagram rule).

**Scene length.**
- Narration may stretch `seconds` (`plan_durations`). Because times are absolute, a stretched scene simply holds its
  final state longer.
- An element that enters, moves or exits after the scene's `seconds` gets a warning, not an error, for example:
  `motion element 'x' enters at 7.2s, after the 6s scene ends`.

**Layer order.**
- Elements are sorted by `(effective layer, document order)`.
- The effective layer is the element's own `layer`, else its parent's, else 0.
- Text, shapes and images interleave correctly, because each one is its own sprite (section 5).

**Determinism contract.**
- `MotionScene.frame(t)` depends only on the spec, the ctx size and theme, and `t`.
- Every cache (sprites, text resamples, the last frame) is keyed on quantised content, so a cache hit gives exactly
  the pixels a redraw would.

## 5. Drawing

### 5.1 Retained sprites, each supersampled on its own (the core performance decision)

For each visible element at `t`:

1. Resolve the element's state and quantise it (section 5.3).
2. **Snap the anchor to a whole pixel.**
   - A root (a top-level element) is rounded to an integer pixel.
   - A member (a group child or a split word) sits at `root_int + round(offset_world_px)`.
   - At scale 1 with no rotation, the offsets are constant, so a card and its label never move relative to each
     other.
3. **Clip.** The element's screen bbox is intersected with the frame plus `_SS_MARGIN`, aligned to the f-grid. An
   element that falls entirely outside is culled. When a sprite is clipped, the clip window joins its `shape_key`.
4. **Look up the sprite.** `shape_key` is looked up in the scene's retained LRU. The key holds no position, so an
   element that only translates reuses its sprite.
5. **On a miss, `raster`:**
   - Allocate an RGBA layer covering the clipped bbox plus margin, at f times the size, aligned to the frame's
     f-grid. The alignment works because the anchor is a whole pixel.
   - **Hard ceiling:** `MAX_LAYER_PIXELS = 12_000_000` (48 MB RGBA). If a layer would exceed it (only a huge
     on-screen element at `high` can), that element drops to f = 2 for that frame, with one warning. Clipped to
     the frame, f = 2 is at most 2160x3840 plus margins, which is under the ceiling.
   - Draw the geometry opaque, at f times the size, with the primitives diagram uses (`rounded_rectangle`,
     `ellipse`, `line`, `polygon`).
   - Call `downsample(layer, f)`. Away from the frame edges, the result has the same pixels as a whole-frame
     layer, f times the size, holding only this element (AC-5). That is how the motion scene matches diagram's
     look without a frame-sized layer.
6. **Composite** onto the RGB canvas with `canvas.paste(sprite, pos, alpha)`. When opacity < 1, alpha is the
   sprite's alpha passed through a 256-entry lookup table.

**Shape details.**
- A rotated rect is drawn as a polygon, with each corner arc sampled as 8 segments at f times the size. An
  unrotated rect uses `rounded_rectangle`, as diagram does.
- `draft` (f = 1) draws straight into the 1x sprite with no downsample: aliased, fast.

**Retained budget, one per scene.**
- `MOTION_CACHE_BYTES = 48 MB` covers sprites and text hi-masks together, as one LRU that is free to evict.
- Image sources are pinned for the scene's lifetime and capped separately: `MAX_IMAGE_BYTES = 48 MB` in total per
  scene. Going over is a build error at `scenes[i].elements[j].source`.
- The last frame adds about 6 MB.
- `release()` clears the LRU and the last frame. The Timeline already calls `release()` on every scene that is not
  current or the one before it.
- `retained_bytes()` reports the total, for tests and the memory bound.

### 5.2 Whole-frame cache (identical frames)

- `frame(t)` computes `key = (state_key(states), flash-tag alphas, title alpha)`.
- If the key equals the previous one, it returns `self._last_img.copy()` (C3).
- Once every animation has ended, each frame is a cache hit (budget in section 8).

### 5.3 Quantisation (what makes the caches exact)

| Property | Quantum |
|---|---|
| Anchor | 1 px (rounded) |
| scale (element x camera) | 1/512 |
| rotate | 0.05 degrees |
| opacity | 1/255 |
| draw progress | 1/1024 (whole characters for text type-on) |
| stroke-width multiplier (flash) | 1/64 |

Drawing reads only the quantised values, so equal keys always give equal pixels.

### 5.4 Text

**The block.** Built once with `ctx.block(text, kind, size * u * fs, max_w * W, align=...)`, which warns when it
shrinks the text.

**At rest** (scale 1, rotate 0, zoom 1). The cached block is pasted exactly as `blit` would: at 1x, sharp.

**Scaled or rotated.** A "hi" mask is rendered once, at `base_size * s_max`, then resampled per frame.
- `s_max` is the largest effective scale the timeline can reach. The safe bounds are:
  - 1.1 for `pop`, whose real peak is 0.6 + 0.4 x 1.1 = 1.04;
  - 1.08 for `pulse`;
  - `moves[].scale`;
  - the largest camera `zoom`;
  - the parent scales.
- `s_max` is capped at `TEXT_SUPER_MAX = 3.0` and at a 4 MP mask. Past the cap the mask is upscaled instead, with
  one warning.
- Each frame:
  1. The hi-mask is resampled down to the needed size with LANCZOS (on an L mask).
  2. If needed, it is rotated with `rotate(..., BICUBIC, expand=True)`.
  3. The result goes into the retained LRU, keyed on the quantised scale and rotation.

**`enter: draw` on text** is a typewriter reveal.
- The prefix is drawn at the final line layout, with left edges taken from the full lines. Centred text therefore
  does not shift while it types.
- Masks are cached per character count (LRU 64, like `StatScene.COUNT_CACHE`).

**`split: words`**
- `textkit.layout_words(words, font, max_w)` lays the words out once.
- Each word becomes a child at its box centre minus the block centre. Its timing follows section 4.

### 5.5 Camera (S2)

- `camera_at(keys, t)` returns `Viewport(zoom, fx, fy)`.
- A world point `P` (px) maps to `S = (P - F) * zoom + C`, where `F = (fx * W, fy * H)` and `C = (W/2, H/2)`.
- Zoom 1 with focus (0.5, 0.5) is the identity, and then nothing is recomputed.
- The camera transforms anchors, offsets, sizes, stroke widths, radii and text scale, all before rasterising. It is
  never applied as a raster affine.
- Elements whose screen bbox misses the frame are culled.
- The background, title, caption, badge and watermark do not move.
- During a camera move, every visible element's `shape_key` changes, so every element re-rasterises. Each one is
  still clipped to the frame and under `MAX_LAYER_PIXELS`. Section 8 gives these frames their own budget.
- `vfocus` overrides `focus` in vertical frames, as `vx`/`vy` override `x`/`y`.

## 6. Elements, properties, presets

### 6.1 Common element fields (`COMMON_ELEMENT_FIELDS`)

| Field | Type / range | Default | Notes |
|---|---|---|---|
| `kind` | `text, rect, circle` (S1); `+ arrow, path, image, group` (S2) | required | |
| `id` | text or int, 1..100 chars, unique within the scene, nested elements included | none | Needed when a flash, arrow or camera refers to the element |
| `x`, `y` | number -0.5..1.5, as frame fractions, giving the element centre | 0.5, 0.5 | Inside a group: an offset from the group centre, -1..1, default 0 |
| `vx`, `vy` | same as `x`, `y` | none | Used in vertical frames (the diagram convention) |
| `at` | 0..600 (`MAX_CUE_SECONDS`) | auto (section 4) | |
| `enter` | a preset name, or `{type, seconds 0.05..10, easing}` | `fade`; `none` for group children | Normalised to the mapping form, like `transition` |
| `emphasis` | `pulse`, or `{type: pulse, at, seconds 0.1..5, repeat 1..10}` | none | |
| `end` | 0..600 | none (stays to the end of the scene) | When the exit starts |
| `exit` | a preset name, or `{type, seconds, easing}` | `fade-out` when `end` is set | `exit` without `end` is an error |
| `easing` | `linear, out, in_out, back` | the preset's own | Applies to enter and exit unless their mapping sets one |
| `layer` | integer -100..100 | the parent's, else 0 | |
| `opacity`, `scale`, `rotate` | 0..1, 0.05..8, -720..720 degrees | 1, 1, 0 | The rest state |
| `moves` | a list of 0..8 (`MAX_MOVES`) entries `{at, seconds 0.05..10 (0.6), easing (in_out), x, y, vx, vy, scale, rotate, opacity}` | none | Each move needs at least one target field |
| `color` | `accent, accent2, fg, muted, card, line, bg` (`MOTION_COLORS`) | per kind | Text colour, or the stroke colour of an arrow or path |

### 6.2 Kind fields

Sizes are in `u`: pixels at a 1080 short side.

| Kind | Fields (default) | Presets beyond the common set | Slice |
|---|---|---|---|
| `text` | `text` (required, at most 300 chars, `MAX_MOTION_TEXT`), `size` 12..400 (64, times `ctx.fs`), `font` `display`, `body` or `mono` (`display`), `max_w` 0.1..1 of W (0.84), `align` `left`, `center` or `right` (`center`), `split` `none` or `words` (`none`; at most 40 words, `MAX_SPLIT_WORDS`), `stagger` 0..3 (0.08), `color` (`fg`) | `draw` (type-on) | S1 |
| `rect` | `w`, `h` 1..4000 (required), `radius` 0..1000 (18), `fill` a colour or `none` (`card`), `stroke` a colour or `none` (`accent2`), `stroke_width` 0..40 (4) | `draw` from S2 (outline) | S1 |
| `circle` | `r` 1..2000 (required), `fill` (`accent`), `stroke` (`none`), `stroke_width` (4) | `draw` (arc) | S1 |
| `arrow` | `from` and `to`, each an element id or `[x, y]` (required); `color` (`accent2`), `stroke_width` (5), `dashed` (false), `head` (true) | `draw` | S2 |
| `path` | `points`: 2..200 entries of `[x, y]` (`MAX_PATH_POINTS`); `closed` (false); `smooth` (false; Catmull-Rom, at most 2000 samples, `MAX_PATH_SAMPLES`); `color` (`fg`); `stroke_width` (5); `fill` (`none`, closed paths only) | `draw` | S2 |
| `image` | `source` (local file only); `w`, `h` or both (at least one is required); `radius` (0). At most 8 per scene (`MAX_IMAGES`) | none | S2 |
| `group` | `children` 1..20 (`MAX_GROUP_CHILDREN`); `stagger` 0..3 (0); nesting depth at most 3 (`MAX_GROUP_DEPTH`); no paint fields | none | S2 |

Every kind supports the common presets: `none`, `fade`, `slide-*`, `pop`, `pulse` and the exits. `draw` works only
where the table lists it. The validator reads `KINDS[kind].PRESETS`, so S2 widens `draw` by registering kinds,
without editing `anim.py`.

**Arrow endpoints.** An id endpoint attaches to that element's current box, clipped at the box edge plus 10u (as
diagram's `exit_t` does). The arrow therefore follows a moving box.

**Path draw-on.** Progress is a fraction of the path's arc length.

**Image `source`.**
- Resolved with `safety.resolve_local(source, ctx.base_dir, ctx.roots)`; `safety.py` itself is not edited.
- URLs are rejected with `<path>: must be a local file; use a shot scene for web pages`.
- Decoding is guarded by `shots.MAX_IMAGE_PIXELS`.
- The image is pre-scaled once to its largest on-screen size, capped at 4 MP, and counts against
  `MAX_IMAGE_BYTES`.
- Errors are raised from inside `MotionScene` with the path `scenes[i].elements[j].source`. Timeline's
  `CaptureError` wrapper would label them `scenes[i].source`.

**Scene fields (`MotionScene.FIELDS`).**
- `elements`: required. 1..60 top-level entries, and at most 60 drawable leaves once groups and split words are
  expanded (`MAX_ELEMENTS`).
- `stagger`.
- `title`: drawn as diagram draws its title (display font, 56u x fs, at 0.09 H, `_appear(t, 0.05)`).
- `flash`.
- `quality`: `draft`, `standard` or `high`; default `standard`.
- `camera` (S2): a list of 1..16 entries (`MAX_CAMERA_KEYS`), each `{at, seconds 0..10 (0.8), zoom 0.25..4,
  focus [x, y], vfocus, easing}`. It is a list on purpose, so that `camera: {}` is rejected (C4).

**`flash`** reuses `DiagramScene.FLASH_FIELDS` (`node, at, text, color`). Here `node` names an element `id`.
- **Colour.** From `at` on, the element's stroke takes `color`. Text, and elements without a stroke, change their
  fill or text colour instead. On a group, every child changes.
- **Width.** The stroke width is multiplied by `1 + 1.5 * max(0, 1 - (t - at) / 0.7)`. That is diagram's 4 -> 10u
  pulse.
- **Tag.** Laid out as diagram does it: sans bold, 36u in vertical frames, 30u otherwise.
  - Vertical: 22u to the right of the element's current box, anchored middle-left.
  - Otherwise: 20u below it, anchored top-centre.
  - It fades in over 0.3 s.
- When an element has several flashes, the last one wins.

### 6.3 Presets (exact)

Symbols used in the table:
- `p = ease(easing, clamp((t - at) / seconds))`;
- `q = ease(easing, clamp((t - end) / seconds))`;
- `lin = clamp((t - at) / seconds)`;
- `D = SLIDE_DISTANCE * u = 60u`;
- `ease` is `textkit.ease`.

`p` and `q` can exceed 1 with `back` (peak 1.1). Opacity is clamped to [0, 1] after every step.

| Group | Preset | Default seconds / easing | Definition (applied to `base`) |
|---|---|---|---|
| enter | `none` | 0 / n/a | Visible from `at`, at rest |
| enter | `fade` | 0.4 / `out` | `opacity *= p` |
| enter | `slide-up` | 0.5 / `out` | `y_px += (1 - p) * D`; `opacity *= p` |
| enter | `slide-down` | 0.5 / `out` | `y_px -= (1 - p) * D`; `opacity *= p` |
| enter | `slide-left` | 0.5 / `out` | `x_px += (1 - p) * D`; `opacity *= p` |
| enter | `slide-right` | 0.5 / `out` | `x_px -= (1 - p) * D`; `opacity *= p` |
| enter | `pop` | 0.45 / `back` | `scale *= 0.6 + 0.4 * p` (peaks at 1.04 with `back`); `opacity *= min(1, 2 * lin)` |
| enter | `draw` | 0.8 / `in_out` | Sets progress = p (details below) |
| emphasis | `pulse` | 0.6 per cycle, repeat 1; `at` defaults to `at + enter.seconds + 0.3` | `c = frac((t - e.at) / seconds)` while `t < e.at + repeat * seconds`; `scale *= 1 + 0.08 * sin(pi * c)` |
| exit | `fade-out` | 0.35 / `in_out` | `opacity *= 1 - q` |
| exit | `slide-out` | 0.4 / `in_out` | `y_px += q * D`; `opacity *= 1 - q` |
| exit | `pop-out` | 0.3 / `in_out` | `scale *= 1 - 0.4 * q`; `opacity *= 1 - q` |

What `draw` progress (p) means for each kind:
- **Text:** shows `floor(p * n_chars)` characters.
- **Circle:** an arc from 12 o'clock, clockwise, to `360 * p` degrees.
- **Rect:** a prefix of the outline's perimeter, starting at the top-left corner.
- **Path:** a prefix of its arc length.
- **Arrow:** a prefix of the shaft. The head appears once `p >= 0.98`, as in diagram.
- **Fill** (shapes): fades in with alpha `clamp((p - 0.7) / 0.3)`.

A preset that the kind does not support is rejected, for example
`scenes[i].elements[j].enter: draw works with text and circle elements` (in S1).

### 6.4 Validation messages (exact shapes; the builder tests these)

- `scenes[2].elements[3].kind must be one of text, rect, circle`
- `scenes[2].elements[3].foo: unknown field for a rect element; allowed: ...` (via `_unknown_key`)
- `scenes[2].elements has 61 entries; the limit is 60 (MAX_ELEMENTS)`
- `scenes[2].elements: 64 drawable elements after groups and split words; the limit is 60 (MAX_ELEMENTS)`
- `scenes[2].elements[1].id: duplicate element id 'a'`
- `scenes[2].elements[3].end: 1.0s is before the element has entered (1.25s)`
- `scenes[2].elements[0].moves[1].at: starts at 1.2s, before the move before it ends (1.4s)`
- `scenes[2].elements[0].exit: needs end`
- `scenes[2].elements[0].split: words cannot be combined with enter: draw`
- `scenes[2].flash[0].node: unknown element 'x'`
- `scenes[2].flash[0].at: element 'a' appears at 1.5s, so it cannot flash at 1.0s`

## 7. YAML examples

**A. Input -> action -> result** (uses S2 kinds; shown complete). The labels arrive with their cards, because group
children default to `enter: none`.

```yaml
- type: motion
  title: A cache hit
  seconds: 6
  stagger: 0.5
  elements:
    - id: input
      kind: group
      y: 0.24
      enter: slide-up
      children:
        - {kind: rect, w: 760, h: 150}
        - {kind: text, text: "GET /user/42", font: mono, size: 54}
    - {id: cache, kind: group, y: 0.48, at: 0.6, enter: pop,
       children: [{kind: circle, r: 110, fill: card, stroke: accent2, stroke_width: 5}, {kind: text, text: cache, size: 44}]}
    - {kind: arrow, from: input, to: cache, at: 1.0, enter: draw}
    - {kind: circle, r: 16, fill: fg, y: 0.31, at: 1.6, moves: [{at: 2.0, seconds: 0.7, y: 0.40}], end: 2.7}
    - id: result
      kind: group
      y: 0.70
      at: 2.9
      enter: pop
      emphasis: {type: pulse, at: 3.7, repeat: 2}
      children:
        - {kind: rect, w: 640, h: 140, stroke: accent}
        - {kind: text, text: "HIT in 0.3 ms", size: 56, color: accent}
  flash:
    - {node: cache, at: 2.7, text: found, color: accent}
```

**B. Kinetic type** (S1 only). Each word pops in, 0.12 s apart.

```yaml
- type: motion
  seconds: 4
  elements:
    - {kind: text, text: "Stop guessing.", size: 110, y: 0.36, split: words, stagger: 0.12, enter: pop}
    - {kind: text, text: "Measure.", size: 150, color: accent, y: 0.50, at: 1.1,
       enter: {type: slide-up, easing: back}, emphasis: {type: pulse, at: 2.2}}
    - {kind: rect, w: 420, h: 10, radius: 5, fill: accent, stroke: none, y: 0.57, at: 1.5, enter: pop,
       end: 3.4, exit: fade-out}
```

**C. Camera zoom on a diagram** (S2). A motion scene that draws a diagram.

```yaml
- type: motion
  title: Where the time goes
  seconds: 7
  elements:
    - {id: app, kind: group, y: 0.26, children: [{kind: rect, w: 420, h: 130}, {kind: text, text: App, size: 50}]}
    - {id: db, kind: group, x: 0.28, y: 0.58, children: [{kind: rect, w: 360, h: 130, stroke: accent}, {kind: text, text: DB, size: 50}]}
    - {id: cache, kind: group, x: 0.72, y: 0.58, children: [{kind: rect, w: 360, h: 130}, {kind: text, text: Cache, size: 50}]}
    - {kind: arrow, from: app, to: db, color: accent, dashed: true, at: 1.4, enter: draw}
    - {kind: arrow, from: app, to: cache, at: 1.8, enter: draw}
  camera:
    - {at: 2.6, seconds: 1.2, zoom: 1.8, focus: [0.28, 0.58]}
    - {at: 5.2, seconds: 1.0, zoom: 1.0, focus: [0.5, 0.5]}
  flash:
    - {node: db, at: 4.0, text: "300 ms", color: accent}
```

**D. Staggered cards** (groups need S2). In S1 the same effect works with rect + text pairs that share `at` and
`enter`. The scene `stagger` spaces the cards 0.45 s apart.

```yaml
- type: motion
  title: Three ways to scale reads
  stagger: 0.45
  elements:
    - {kind: group, y: 0.30, enter: slide-left, children: [{kind: rect, w: 860, h: 190}, {kind: text, text: Read replicas, size: 52}]}
    - {kind: group, y: 0.46, enter: slide-left, children: [{kind: rect, w: 860, h: 190}, {kind: text, text: A cache in front, size: 52}]}
    - {kind: group, y: 0.62, enter: slide-left, emphasis: pulse,
       children: [{kind: rect, w: 860, h: 190, stroke: accent}, {kind: text, text: Precomputed views, size: 52, color: accent}]}
```

## 8. Quality levels, performance budget, benchmark method

**Quality levels**

| `quality` | Factor f | Downsample | Use |
|---|---|---|---|
| `draft` | 1 | None: shapes are aliased, text is still anti-aliased | Fast iteration |
| `standard` (default) | 2 | `downsample(layer, 2)`, LANCZOS. Same pixels as diagram | Final renders |
| `high` | 3 | `downsample(layer, 3)`, LANCZOS | Opt-in. Each sprite layer has 2.25x the pixels of `standard` |

- `downsample(layer, factor=SS)` generalises the even-bounds crop to multiples of `factor`.
- `_SS_MARGIN = 16` is larger than the LANCZOS reach of `3 * factor` for any f up to 5.
- Factor 2 runs exactly today's code path.
- `reduce(f)` is not used (Q3).

**Budgets at 1080x1920 on the M1 8 GB.** These are measured in the PR. CI has no timing asserts, because runs on
this Mac vary by up to 35%.

| Frame class | `standard` | `draft` | `high` |
|---|---|---|---|
| Nothing changing (cache hit: a copy) | <= 3 ms | <= 3 ms | <= 3 ms |
| Animated: up to 4 elements changing shape, up to 20 visible | <= 25 ms | <= 10 ms | Report only (target <= 60) |
| Camera move, up to 20 visible (all re-rasterise) | <= 50 ms | <= 20 ms | Report only |
| Scene average, `examples/motion-bench.yaml` scene 1 | <= 20 ms, and <= 1.5x the diagram scene in the same run | n/a | n/a |

**Method**
1. Bench specs:
   - `examples/motion-bench.yaml` (S1) has four 6 s scenes: (0) a `diagram` reference, (1) a 12-element beat,
     (2) kinetic type with 3 lines of split words, (3) an idle hold where everything is at rest after 1 s.
   - `examples/motion-camera.yaml` (S2) has a diagram reference scene and the camera example C.
2. Free memory first and record the `vm_stat` free pages. Then run each scene alone, 3 times:
   `yamlreel render examples/motion-bench.yaml --format vertical --profile --only N --no-voice --music none`
3. Take the median `ms/frame` from the `drawing by scene type` line, for `motion` and for `diagram`. Report the
   absolute value and the ratio of the two.
4. Measure peak RSS with
   `/usr/bin/time -l yamlreel render examples/motion-bench.yaml --format vertical --no-voice --music none`
   (maximum resident set size). Run the same command on `examples/motion-demo.yaml` in the same session for
   comparison.
5. Record the numbers in the PR and in `handoff.md`. Reuse them unless the drawing code changes.

## 9. Acceptance criteria (measurable)

All criteria are tested in CI unless marked [PR].

**AC-1 Determinism.** Compare `sha256` of the frame bytes at t in {0, 0.37, 1.0, 2.5, duration - 0.05}. They must
be equal across:
- two fresh `MotionScene` instances;
- forward, reverse and shuffled draw orders;
- the frame cache on and off (test hook `_frame_cache=False`);
- `Timeline._scene_frame(i, t)` when the timeline holds only this scene (the `--only` path) and when it holds all
  scenes.

The test spec uses `transition: cut` and also compares `frame_at`, so a transition blend cannot enter the
comparison.

**AC-2 Pure timeline.** Tested in `test_anim.py`.
- `anim.resolve(tree, t)` equals the closed forms of sections 4 and 6.3 to within 1e-9, for every preset at p in
  {0, 0.5, 1}.
- The same holds for group-child and split-word composition, with their default timing.
- Calling `frame(0.5)` and then `frame(1.0)` gives the same `frame(1.0)` as a fresh scene.

**AC-3 Sharp text.** At rest (scale 1, rotate 0, zoom 1), the text's box in `frame(t)` is byte-equal to
`blit(ctx.background(), block, pos, color)`, cropped to the same box.

**AC-4 No jitter.** Checked through the `placements(t)` accessor and frame hashes.
- (a) A linear `slide-up` sampled at 30 fps: integer y positions are monotonic, and per-frame deltas differ by at
  most 1 px.
- (b) The offset between a group's rect and its text placement is constant over every frame at scale 1. In S1 the
  test uses a rect + text pair that shares `at`.
- (c) After the last animation, consecutive frames are byte-identical.

**AC-5 Supersample parity.** Tested in `test_motion_scene.py` and `test_render_speed.py`.
- A rect or circle sprite pasted onto the background is byte-equal to `downsample(full f-x layer with the same
  shape, f)` pasted, for f = 2 and f = 3.
- The shape sits at least 20 px inside the frame, because the frame-edge clamp differs.
- A new test beside the existing 2x ones checks that `downsample(layer, 3)` equals a LANCZOS resize of the whole
  layer.

**AC-6 Memory.** Checked through accessors, plus `/usr/bin/time -l` for the [PR] part.
- Draw every frame of a scene with 60 large, constantly pulsing elements. Afterwards
  `retained_bytes() <= MOTION_CACHE_BYTES + MAX_IMAGE_BYTES + one frame`.
- No raster layer is larger than the frame-clipped bbox plus margins, times f squared, or larger than
  `MAX_LAYER_PIXELS` (accessor `max_layer_pixels()`). The test includes a deliberately oversized element (rect
  4000u, scale 8, zoom 4, `high`), which must stay within both limits.
- [PR] Peak RSS for `motion-bench.yaml` is at most 100 MB above `motion-demo.yaml` at `standard`, at most 200 MB
  above at `high`, and under the 1.2 GB project cap.

**AC-7 Per-frame budget.** [PR] The section 8 table, median of 3 runs, measured with `--profile`.

**AC-8 Audio untouched.** Tested in `test_motion_scene.py`. Take two specs that differ only in one scene's type:
`motion` in one, `title` in the other, with the same `seconds`, `energy` and narration.
- `plan_durations` and `energy_sections` are equal for both.
- The `music.wav` that `render._prepare_audio` writes is byte-identical (koto, 3 s).

**AC-9 Existing specs unchanged.**
- The full suite passes, with no edits beyond those named in section 10.
- [PR] Before/after baseline with a scratch script (not committed): render every non-Chrome scene of
  `examples/*.yaml` and `yamlreel/templates/*.yaml` at the `CONTACT_DEFAULT_FRACS` times, scale 0.25, once on `main`
  and once on the branch. All sha256 values match.

**AC-10 Validation.** Tested in `test_validation.py` and `test_motion_validation.py`.
- Every new field, nested ones included, rejects `{}` and the type-confusion values.
- The section 6.4 messages match exactly.
- The nested `*_FIELDS` tables are covered by the drift guard.

**AC-11 Chrome-free.** With `sys.modules["playwright"] = None` (monkeypatch), a motion scene builds and renders,
and no `lineart_scene._Page` is created.

## 10. Test plan

Run only the targeted tests during development, and the full suite at each slice boundary.

**S1**
- `tests/test_anim.py` (new):
  - closed forms for every preset (AC-2);
  - auto times and stagger;
  - group-child defaults (`enter: none`, stagger 0) and split-word timing;
  - move interpolation and carry-over;
  - composition;
  - opacity clamping with `back`;
  - quantisation and `state_key` equality;
  - hidden before `at` and after the exit.
- `tests/test_motion_validation.py` (new):
  - every message in section 6.4;
  - the limits: `MAX_ELEMENTS` leaves, `MAX_MOVES`, `MAX_SPLIT_WORDS`, `MAX_MOTION_TEXT`;
  - the timing rules;
  - preset applicability, read from `KINDS[...].PRESETS`;
  - `exit` without `end`;
  - `split` together with `draw`;
  - flash references and times;
  - `quality` choices.
- `tests/test_motion_scene.py` (new):
  - AC-1, AC-3, AC-4, AC-5 (sprite parity at 2x and 3x), the AC-6 accessors with the oversized element, AC-8 and
    AC-11;
  - `frame()` returns a new object on every call (C3);
  - `release()` empties the caches;
  - type-on keeps the line layout;
  - all three formats render;
  - the flash tag is placed where diagram would put it.
- `tests/test_render_speed.py` (additive edit): a `downsample(layer, 3)` parity case, and a check that
  `scenes.downsample is scenebase.downsample`.
- `tests/test_validation.py` (named edits):
  - add `BASE_SCENES["motion"]`;
  - run the type-confusion and `{}` loops over `KINDS[kind].FIELDS`, `COMMON_ELEMENT_FIELDS`, `MOVE_FIELDS` and
    `ENTER_FIELDS`;
  - add a nested drift guard: `_literal_keys(MotionScene.__init__ / KINDS[k].prepare, {"el"}) <= table`.
- `tests/test_story.py` (named edit):
  - the `_events` message changes from "diagram, meter or array" to "diagram, meter, array or motion";
  - add a motion step with flash events that round-trips through `load_spec`.

**S2**
- `tests/test_motion_paths.py` (new):
  - polyline length and prefix;
  - the Catmull-Rom sample cap;
  - dash phase;
  - arrow clipping matches diagram's `exit_t` on the same boxes;
  - draw-on progress for paths, rects and arrows (the ink area grows monotonically);
  - group children drawn in layer order.
- `tests/test_motion_camera.py` (new):
  - zoom 1 with focus 0.5 is byte-equal to no camera;
  - the `to_screen` maths;
  - key interpolation, and errors for overlapping keys;
  - culling;
  - the background and title are not transformed;
  - every layer stays clipped and under the ceiling while zoomed (`max_layer_pixels`);
  - `camera: {}` and `camera: []` are rejected.
- `tests/test_motion_image.py` (new). A local file loads. These are rejected:
  - a URL;
  - a path outside the roots (via `resolve_local`);
  - an image over `MAX_IMAGE_PIXELS`, reported at the `elements[j].source` path;
  - more than `MAX_IMAGE_BYTES` in total;
  - more than `MAX_IMAGES` images.
- `tests/test_validation.py`: the nested loops extend to the S2 kinds and to `CAMERA_FIELDS`.

**Visual gate** (at each milestone, not on every commit).
1. Render the slice's example with `--preview --contact-frames 6`, vertical, and attach `contact.png`.
2. Look at one full-size still at 1080x1920. Render landscape only when asked.
3. Check that motion progresses across the stills, and that nothing clips or overlaps the caption zone.

## 11. Slices and ordered steps

**Deliberate deviation: the slices share some files.** The slices are sequential, and S2 extends S1. Making their
file sets fully disjoint would need a plugin loader, which is over-engineering. Instead the shared set is kept
small: `motion_scene.py` (the `KINDS` entries and the camera hook), `specs.py`, `tests/test_validation.py` and
`README.md`. `anim.py` is complete in S1, since it already composes trees for split words and reads preset support
from `KINDS`, so S2 does not touch it. Each slice ships alone as one PR with one integrated review.

**S1: timeline, rect/circle/text, easing, presets, flash, story events**

Creates:
- `yamlreel/anim.py`
- `yamlreel/motion_scene.py`
- `examples/motion-scene.yaml`
- `examples/motion-bench.yaml`
- `tests/test_anim.py`
- `tests/test_motion_validation.py`
- `tests/test_motion_scene.py`

Modifies:
- `yamlreel/scenebase.py`
- `yamlreel/scenes.py`
- `yamlreel/specs.py`
- `yamlreel/story.py`
- `README.md`
- `tests/test_validation.py`
- `tests/test_render_speed.py`
- `tests/test_story.py`

Steps:
1. Move `SS`, `_SS_MARGIN` and `downsample` to `scenebase.py`, add the `factor` parameter, and re-export them from
   `scenes.py`. This is a pure move plus the 3x parity test, and the suite stays green.
2. `anim.py`: presets, `Timing`/`State`, `resolve`, composition with the child and split defaults, clamping,
   quantisation, `state_key`. Add `test_anim.py`.
3. Wire up validation:
   - in `motion_scene.py`: the field tables, the `KINDS` skeleton with `PRESETS`, and a stub `MotionScene`;
   - registration in `scenes.py`;
   - in `specs.py`: the checkers (`_elements`, `_quality`, `_motion_flash`), plus `REQUIRED`, `DEFAULT_SECONDS`,
     `TYPE_CHECKS` and `_CHECKED_FIRST_BY_TYPE`;
   - in `tests/test_validation.py`: `BASE_SCENES` and the nested guards;
   - add `test_motion_validation.py`.
4. Drawing:
   - rect and circle sprites, each supersampled on its own;
   - the frame clip, `MAX_LAYER_PIXELS` and the f = 2 fallback;
   - z-order and opacity;
   - the retained LRU;
   - `release()`, `retained_bytes()` and `max_layer_pixels()`.
5. Text:
   - drawing at rest, scaled or rotated, and type-on, plus `split: words`;
   - the whole-frame cache with `.copy()`;
   - the `placements(t)` accessor.
6. Flash:
   - `flash` and its tags;
   - `layout_warnings`: the title and caption against element rest boxes, in diagram's wording;
   - warnings for late elements.
7. `story.py`:
   - set `EVENT_ACTIONS["motion"] = ("flash",)` and `EVENT_EXTRAS["motion"] = ("text", "color")`;
   - add `motion` to `EVENT_SHOWS`;
   - add `PLACEHOLDERS["motion"]`: a rect `a` and a text `t`, both TODO;
   - generalise the diagram-only flash branches in `_events` and `_apply_events` to an id table per show;
   - make the named edit to `test_story.py`.
8. Finish:
   - add the examples;
   - update the README: the scene table, all spec options, and the easing note from C5;
   - record the PR measurements (AC-6 RSS, AC-7, AC-9).

**S2: path draw-on, arrow, image, group, camera**

Creates:
- `yamlreel/motion_paths.py`
- `yamlreel/motion_camera.py`
- `yamlreel/motion_image.py`
- `examples/motion-camera.yaml`
- `tests/test_motion_paths.py`
- `tests/test_motion_camera.py`
- `tests/test_motion_image.py`

Modifies:
- `yamlreel/motion_scene.py` (`KINDS` entries, camera hook)
- `yamlreel/specs.py` (the S2 kinds, `_camera`)
- `README.md`
- `tests/test_validation.py`

Steps:
1. `motion_paths.py` geometry, with its tests.
2. The `group` kind: validation, flash on a group, the group bbox. Rect `draw` is registered in `PRESETS`.
3. The `path` and `arrow` kinds: draw-on, dashes, attaching to element ids.
4. `motion_image.py` and the `image` kind, including the `MAX_IMAGE_BYTES` budget.
5. `motion_camera.py`, the `camera` field, and the transform applied before rasterising.
6. Examples A, C and D in `motion-camera.yaml`, the README, and PR measurements for camera frames.

**S3 (later): native characters**
- Creates `yamlreel/figure.py` and `tests/test_figure.py`.
- Modifies only a `KINDS` registration line, one field table and one checker in `specs.py`.
- See section 12.

## 12. S3 interface boundary (characters, not designed here)

- **A new element kind, `figure`.** It consumes this engine and changes nothing in it.
- **`figure.py` holds pure Python ports** of the line-art maths in `yamlreel/lineart/engine.js`:
  - `knee()`: two-bone IK;
  - `gait()`: foot phases and `pose(phase, go)`;
  - `limb()`: a variable-width limb outline;
  - `figure().pose(...)`: arms for carry, wave and point.
- **The contract** is `figure_strokes(opts, t_local) -> list[Stroke]`, pure and deterministic. A `Stroke` is
  `(points: list[(x, y)] in u-units relative to the figure anchor, width_u: float, color: str, closed: bool)`.
- **How the engine treats a figure: like a group of paths.**
  - It applies the state, the group transform, the camera and the quality level.
  - It rasterises with the S2 polyline routine (`joint="curve"`).
  - The `shape_key` includes the quantised pose phase. A walking figure therefore re-rasterises every frame, but
    each layer is clipped to its box and stays under the pixel ceiling.
- **Out of scope until S3:** walking along `moves` (the gait phase taken from path speed), pointing at element ids,
  and carrying a child element.

## 13. Alternatives considered

- **A shape or keyframe DSL.** The 2026-11-03 spec rejected one: "too much YAML, reads badly, hard to validate".
  Presets answer that objection, since a typical element is one line with `enter: pop`. `moves` (at most 8) is the
  only hand-timed tool, and it is validated like every other cue.
- **One supersampled layer for the whole inked area, as diagram does.** Rejected.
  - Any change re-rasterises and re-resamples the whole inked area every frame. LANCZOS alone costs 71-83 ms on a
    full 2x frame.
  - Getting text in the right z-order would force several layers.
  - Identical sprites would not be reused.
  - The per-element approach has one small cost: where elements overlap, the anti-aliased edges are composited at
    1x. It wins on every frame where little changes.
- **A raster camera (`resize` or `affine` on the composed frame).** Rejected by an earlier decision. The result is
  soft, it costs a full-frame resample, and it would move the background and the overlays.
- **Text drawn into the supersampled layer.** Rejected: softer than the 1x FreeType text every other scene uses,
  and slower.
- **Re-rasterising text at each zoomed size.** Rejected: `font()` takes whole pixels only (C1), so the text would
  step in size.
- **Sub-pixel anchors.** Rejected.
  - They need up to f squared sprite variants per shape.
  - A card would wobble against its 1x text.
  - Whole-pixel anchors give steps of 4 px or more at typical slide speeds. Slow drift under 1 px per frame steps
    visibly; that is documented as R4.
- **Driving `engine.js` faster (JPEG, 3 workers).** Kept as the optimisation path for `lineart` (benchmark section 9).
  It is still Chrome, at about 200 MB per page, and the decision was to have a Chrome-free path.
- **`reduce(2)` now.** It is 4.7x faster per resize, but it changes pixels against diagram (mean difference 2.35 of
  255). Deferred to a measured follow-up that switches both scenes together (Q3).
- **Validation in a separate `motionspec.py`.** Rejected because of the `SpecError` import cycle. All validation
  stays in one file, as today.

## 14. Rollout and compatibility

- **Additive only.** `motion` is a new scene type. Without `type: motion` nothing renders differently (AC-9).
- **No version bump** inside a slice PR, as before.
- **No forward compatibility.** A spec with `type: motion` fails on 0.3.6 with "unknown type".
- **`downsample` stays compatible.** It keeps its import path and its default behaviour.
- **`lineart` and Chrome are unchanged** and remain the high-fidelity option.
- **Process.**
  - Builders keep `handoff.md` up to date: completed work, changed files, the latest targeted test output,
    measurements and pending decisions.
  - Each slice gets one integrated review, reporting blockers and majors only.

## 15. Risks, failure modes, observability

| ID | Risk | Mitigation / check |
|---|---|---|
| R1 | Camera moves re-rasterise everything and blow the budget | A separate budget (section 8), frame-clipped layers, `MAX_LAYER_PIXELS` and culling, all tested. Recommend `draft` for previews. If measurements come in over budget, Q3 (`reduce`) is the lever |
| R2 | Memory growth from huge elements, pulses or images | Layers are clipped to the frame and capped at 12 MP each. The retained LRU is capped at 48 MB, images at 48 MB, and there is one cached frame. All are released when the scene is not current. The AC-6 test uses an oversized element and 60 pulsing elements |
| R3 | The frame cache returns a stale or shared image, so overlays leak into later frames | Always return `.copy()` (C3). A test checks that two calls give different objects and that pasting overlays onto one leaves the next untouched |
| R4 | Slow motion stair-steps because anchors are whole pixels | Documented. Presets move 4 px or more per frame. Warn when a `move` travels less than 0.5 px per frame for more than 1 s |
| R5 | Text looks softer while it is scaled or rotated | Only during transient states: rest frames are byte-equal to `blit` (AC-3). The hi-mask is capped at 3x and 4 MP, with a warning past the cap |
| R6 | Overlapping translucent children of a fading group show seams | The semantics are documented in section 4. Revisit only if real specs need the group flattened before its opacity is applied |
| R7 | Near the frame edge, a sprite differs from diagram's clamped crop (within about 3 px of the edge) | Accepted. The AC-5 test keeps shapes 20 px inside the frame |
| R8 | The validator and the renderer drift apart on timing or preset support | Timing lives in `anim.py` and preset support in `KINDS`, and both sides use them. Nested drift guards |
| R9 | Unsafe image inputs: paths, decompression bombs | `resolve_local` (in the unchanged `safety.py`), `MAX_IMAGE_PIXELS`, local files only, `MAX_IMAGES`, `MAX_IMAGE_BYTES` |
| R10 | Timings on the 8 GB Mac are noisy because it swaps | Median of 3 runs, a ratio to diagram in the same run, free memory recorded. No timing asserts in CI |
| R11 | `--only` and contact-sheet frames differ from full renders | AC-1 covers it, and frames depend on `t` only |

**Observability**
- `--profile` already lists `motion` ms per frame among the scene types.
- `ctx.warn` reports:
  - shrunk text;
  - elements that appear late, or sit off-frame at rest;
  - the hi-mask cap;
  - the f = 2 fallback;
  - slow `moves`;
  - title or caption overlaps.
- `--contact-frames N` gives stills.
- `MotionScene.retained_bytes()`, `max_layer_pixels()` and `placements(t)` serve tests and debugging.

## 16. Open questions

- **Q1. A CLI quality override?** A `--quality draft|standard|high` flag, and whether `--preview` should imply
  `draft`. Either needs changes to `cli.py` and `render.py` plus a `Ctx` field. Proposed after the S1 measurements.
- **Q2. A camera on the existing `diagram` scene?** Possible by reusing `motion_camera.Viewport`, but it puts
  diagram's pixel parity at risk. Proposal: not now; example C covers the need with a motion scene.
- **Q3. Switch `downsample` to `reduce(f)`?** It would apply to motion and diagram together. Measured: 82.6 -> 17.5
  ms per full frame, mean difference 2.35 of 255. An owner decision after the S1/S2 numbers.
- **Q4. `read_screen` for motion text elements.** Needs a new branch in `voice.screen_text`, which is an
  owner-reviewed file.
- **Q5. `kicker` on motion scenes.** Cheap, since it reuses `make_kicker` and `place_kicker`. Left out of S1 to keep
  the slice small.
- **Q6. Reuse frame bytes for identical frames in `render.py`** (benchmark fix 4). The motion cache makes identical
  frames cheap to detect: `state_key` equality could be surfaced through a `Scene.same_as_previous(t)` hint. This
  needs its own spec.
- **Q7. Is `MAX_ELEMENTS = 60` leaves enough** for reels heavy on kinetic type? Raising it costs only validation.
- **Q8. Is "Luna-style beat" read correctly?** See A1.
