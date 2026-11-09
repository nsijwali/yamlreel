# Scene `kicker`

## Purpose
A small numbered tag above a data scene's headline ("01 / THE BURST"), giving a video visible rhythm and a set of videos a series look. Only `title` and `stat` have `eyebrow` today.

## YAML
```yaml
- type: meter
  kicker: "01 / THE BURST"
  title: Tokens drain
  capacity: 10
```
Allowed on `bullets`, `code`, `diagram`, `table`, `array`, `meter`, `grid`, `tree`, `timeline`. Not on `title` (has `eyebrow`), `shot` (its `title` is never drawn), `stat`, `type`, `cta`.

## Validation
`_kicker` in `specs.py` `FIELD_CHECKS`:
- `scenes[i].kicker must be text`
- `scenes[i].kicker is 61 characters; the limit is 48 (MAX_KICKER_CHARS)`
- `scenes[i].kicker must be one line of text, without a line break`
- `scenes[i].kicker must not be empty`
- Other types: the usual `scenes[0].kicker: unknown field for a title scene; allowed: ...`

## Rendering
- **Style:** eyebrow's block, `make_block(text.upper(), "sans_bold", 28*u, max_w, tracking=7*u, min_size=10)`, in `theme.accent` (not `accent2`, so it stands apart from the accent2 heads of bullets/code/table). Tracked text never wraps; it shrinks.
- **Shared helpers in `scenebase.py`:** `make_kicker(ctx, spec, max_w) -> TextBlock | None` and `kicker_shift(ctx, kicker, title_top) -> d`, with `d = max(0, 0.05*H + k + g - title_top)`, `k` the kicker height, `g = 16*u`, 0 without a kicker. `Scene` gains class attributes `kicker = None`, `kicker_box = None`.
- **Rule (every format):** the kicker's bottom sits `g` above the title's top, aligned like the title. If that would put it above `0.05*H`, the title and everything under it move down `d` and the scene's height budget shrinks by `d`; its lower limit is unchanged, so nothing enters the caption.

| Scene | Title top | Anchor | Absorbs `d` |
| --- | --- | --- | --- |
| bullets | `y-70u` | margin `tl` | `avail -= d` |
| code | `top-54u` | margin `tl` | size bound `H*0.6 - d` |
| table | `top` | margin `tl` | `area_h -= d` |
| diagram | `0.09H` | `W/2 tc` | nodes fixed; `layout_warnings` checks kicker box |
| array | `top` | `W/2 tc` | none (`d == 0`, tested) |
| meter | `top` | `W/2 tc` | `body_h -= d` |
| grid | `top` | `W/2 tc` | `side` budget `-= d` |
| tree, timeline | `0.06H` | `W/2 tc` | `tree_top`/`chart_top += d`; bottom fixed |

None is free (no shared title layout exists); each needs about 4 lines.

**Warnings** (`render.py` layout loop, after badges): `scenes[i]: kicker shrunk to {size}px to fit; shorten it` and `scenes[i]: the kicker overlaps the step badge; drop one of them`.

**No kicker:** `d = 0`, nothing drawn, no existing expression changes.

## Files
`yamlreel/scenebase.py`, `scenes.py`, `datascenes.py`, `specs.py`, `render.py`, `README.md`, tests.

## Tests
1. `test_kicker_validation`: each error above; 48 characters pass; rejected on `title`, `shot`, `stat`.
2. Drift guard: `make_kicker` reads `spec`, so a missing `FIELDS` entry fails; assert exactly the 9 types list `kicker`.
3. `test_kicker_renders[type-format]` (9 x 3): accent pixels inside `kicker_box`; `kicker_box[3] + g <= title top`; `kicker_box[1] >= 0`; content bottom (`difference(frame, background).getbbox()[3]`) `<= max(caption top, no-kicker bottom)`.
4. `test_kicker_shrinks_and_warns`: 48 "W"s, vertical.
5. `test_kicker_badge_overlap_warns`: diagram plus `step`.
6. `test_array_kicker_needs_no_shift`: 1 cell, all formats, `d == 0`.
7. `test_data_scenes_without_kicker_match_pinned_hashes`: hashes of the 6 data fixtures captured on `7d5b404` before any source edit, guarded by `HASH_FINGERPRINT`. With the existing 0.2.2 test (bullets, code, diagram), this proves the frames are byte-identical. On every platform, also assert `kicker` and `kicker_box` are `None`.

## README
Add `kicker` to the 9 scene-table rows, and this bullet:

> **Kicker:** `kicker: "01 / THE BURST"` on a scene with a `title` (except `title`, which has `eyebrow`) draws a small tracked tag in the accent colour above the title. One line, up to 48 characters; shrinks with a warning if too wide.

## Rejected
- **Common field:** it would be accepted on scenes that cannot draw it.
- **Counting the kicker into the centred stacks:** it moves content even when there is room.
- **A shared title layout:** a large refactor that risks byte-identical frames.

## Risks
- `editorial`'s orange is weaker at small sizes.
- Diagram nodes near the top can be covered; this is warned.

## Out of scope
- Auto-numbering
- A colour per scene
- Narrating the kicker
- `shot` titles

## Steps
1. Pin the hashes.
2. Helpers and validation (tests 1, 2).
3. bullets, code, diagram.
4. table, array, meter, grid.
5. tree, timeline.
6. Warnings and README.
