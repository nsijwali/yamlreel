# Grid scene (`type: grid`)

## Purpose
An animated cell matrix for a Short on QR error correction: a stain spreads, cells are lost, a status line keeps score, and the cells come back. It also suits bitmaps and cache grids. The change only adds things.

## YAML
- Fields: `title`, `cells` (required), `steps`, `seed` (integer 0–1000000, default 0), `seconds` (default 10).
- `cells` is 1–33 rows of equal length (1–33). Each character is `#` the foreground colour, `.` the background card colour or `o` the accent.
- Quote rows that start with `#`, because YAML reads them as comments.
- Steps are flat, like `array` steps. Each one has `at` and exactly one action:

| action | value | extra keys |
|---|---|---|
| `reveal` | `sweep`\|`pop` | `seconds` (0.1–10, default 1) |
| `stain` | `[row, col]` (centre) | `radius` (required, 0.5–33), `seconds` (default 1.5) |
| `clear_stain`, `unmark`, `fail` | `true` | `fail` also takes `text` |
| `lose` | `[[r, c], ...]` | none |
| `recover` | `all`\|`[[r, c], ...]` | `per_cell` (0.01–2, default 0.08), `order: row\|random` |
| `status` | text | `color` (COLOR_NAMES, default `fg`) |
| `outline` | `[r0, c0, r1, c1]` | `text`, `color` (default `accent`) |

`unmark` clears the outlines. I cut `color: bad`: no theme has an error hue, and the rule is no new hues, so `accent` serves instead.

```yaml
- type: grid
  title: Why a stained QR still scans
  caption: Up to 30% can be lost
  seconds: 14
  seed: 7
  cells: ["#######.#..#..#######", "#.....#..##.#.#.....#", "#.###.#.#.#...#.###.#",
          "#.###.#..#..#.#.###.#", "#.###.#.##.#..#.###.#", "#.....#..#.##.#.....#",
          "#######.#.#.#.#######", "........#.##.........", "#.##..#.#..#.o.oo..o.",
          ".#..#..#.##.#.oo.o.oo", "##.#.##..#.##o..oo.o.", "..##...##..#..o.o.oo.",
          "#..#.##.#.#..oo..o.oo", "........#..#.o.oo.o.o", "#######..#.#.oo.o..oo",
          "#.....#.#..##.o.oo.o.", "#.###.#..##.#o.o..oo.", "#.###.#.#.#...oo.o.oo",
          "#.###.#...#.#o..oo..o", "#.....#.#.##..o.o.oo.", "#######..#..#oo.o.o.o"]
  steps:
    - {at: 0.3, reveal: sweep, seconds: 1.2}
    - {at: 2.0, outline: [0, 0, 6, 6], text: finder pattern}
    - {at: 3.2, unmark: true}
    - {at: 3.4, stain: [15, 16], radius: 4.5, seconds: 1.6}
    - {at: 5.4, clear_stain: true}
    - {at: 5.6, status: "Coffee 1 | Math 0", color: accent}
    - {at: 6.2, fail: true, text: "Can't read?"}
    - {at: 7.6, status: "Coffee 1 | Math 1", color: accent2}
    - {at: 7.8, recover: all, per_cell: 0.06, order: random}
    - {at: 12.5, status: Scanned, color: accent2}
```

## Validation (specs.py)
**`cells`** is checked with `_list(…, 1, MAX_GRID, "MAX_GRID")`. Errors:
- `cells[k] must be text; quote rows that start with # ("#..#")`
- `cells[4] has 20 cells; row 0 has 21`
- `cells[4]: 'x' at column 7 is not a cell; use # (foreground), . (card) or o (accent)`

**Steps** (at most MAX_STEPS = 40):
- Unknown keys are rejected with `_no_unknown(…, GridScene.STEP_FIELDS, "a grid step")`.
- `needs exactly one of …[, found a and b]`
- `at` uses `_number(…, 0, sc["seconds"])`. Out of order: `steps[3].at must not be earlier than the previous step (2.4)`.
- Out of range: `steps[1].stain must be [row, col] inside the 21 x 21 grid`. An outline adds `with row0 <= row1 and col0 <= col1`.
- `steps[2].radius: only a stain step has radius`, and `steps[2]: a stain needs radius`.
- `status` and `text` use `_cell_text` (one line, at most 40 characters).

**Replay at load.** `_grid_steps` calls `grid_states` and re-raises `GridStepError` (a subclass of `ArrayStepError`) the way `_steps` does. The problems it reports:
- `no cells are lost`
- `cell [3, 4] is not lost`
- `there is no stain`
- `the grid already has a stain`
- `the stain is still spreading`
- `the grid is already shown`
- `the grid is not shown yet` (a non-status step before a `reveal`)

Example: `scenes[2].steps[5].recover: no cells are lost at 6s`.

**Registration:**
- Add `cells` and `seed` to `FIELD_CHECKS`.
- `TYPE_CHECKS["grid"] = {"steps": _grid_steps}`
- `_CHECKED_FIRST_BY_TYPE["grid"] = ("cells", "seconds", "seed")`
- Add `REQUIRED` and `DEFAULT_SECONDS` entries.

## Pure logic (datascenes.py)
- `grid_states(spec) -> list[GridState]`. `GridState` is frozen and holds `t, step, action, shown, lost, stain, outlines, status, status_color, recovered`.
- `stain_cells(center, radius, seed, step, rows, cols)` works in cell units, so it is the same in every format. A cell is covered when `hypot(dr, dc) <= radius·(1 + Σ_{k=3..5} a_k·sin(kθ+φ_k))`, with `a_k ∈ [0.04, 0.09]`.
- Every random choice uses `random.Random(seed*1000 + step)`. That covers the stain edge, the `pop` reveal order and `order: random`.

## Rendering (GridScene)
**Layout:**
- The title comes first. A status band (64·u·fs) follows it only when there are status steps, so the status never overlaps the grid.
- The grid area ends at H·0.72 (vertical) or H·0.84 (landscape) and is `fill_margin` wide.
- `pitch = floor((side-48u)/max(rows, cols))`, and the gap between cells is `max(1, round(0.06·pitch))`.
- The frame extends 24u past the cells, with a `line` outline and a `lerp(card, line, 0.25)` fill.

**Colours:**
- `#` fg, `.` card, `o` accent.
- Lost: `lerp(card, bg, 0.6)` with `muted` hatching.
- Stain: `lerp(accent2, fg, 0.35)` at 85% opacity.

**Motion:**
- `sweep` reveals row by row. `pop` reveals cells in shuffled order. With no reveal step, everything fades in at 0.1 s.
- The stain is a 48-point polygon that grows with `ease_out`, pasted through an `L` mask. `clear_stain` fades it over 0.5 s while the covered cells become lost.
- `lose` fades cells to lost over 0.3 s.
- On recovery, each cell flashes accent at 1.2× size and settles over 0.35 s.
- New status text rises 14u with `ease("back")` over 0.3 s.
- Outline labels sit in card pills.
- `fail` pulses the frame twice in accent over 0.9 s and shows `text` for 1.5 s.

**Performance.** `__init__` precomputes the cell rects, the per-cell event times, the lost tile, the stain polygon and all text blocks. There is one static layer, `grid`, which keeps the two-layer cache bound. A frame draws only the hidden area and the cells that have events, and makes no font or block calls.

**Warnings:**
- `ctx.warn`: "a grid step ends after the scene does; …".
- `layout_warnings(caption)`:
  - `grid cells are {p}px; use fewer rows or columns, or a shorter title` when p < MIN_CELL_PX·u (16)
  - `the caption overlaps the grid; shorten the caption`
  - `grid status {text!r} shrunk to {n}px to fit`

## Files
- `yamlreel/datascenes.py`
- `yamlreel/specs.py`
- `yamlreel/scenes.py`
- `yamlreel/story.py`: placeholder `{"cells": ["#.#", ".#.", "#.#"]}`, so `test_story` passes.
- `tests/test_validation.py`: a `BASE_SCENES["grid"]` entry.
- `README.md`: a table row, a Grid bullet and the `show` list.
- `examples/grid-demo.yaml`
- `tests/test_grid.py`

## Tests (`tests/test_grid.py`)
1. `test_bad_cells_are_rejected`: each `cells` error, and 34 rows.
2. `test_unquoted_hash_row_gets_a_quote_hint`: through `load_spec`.
3. `test_bad_grid_steps_are_rejected`: each step error above, plus 41 steps.
4. `test_replay_errors_name_step_and_time`: the full messages.
5. `test_states_track_lost_cells`: lose, stain and clear, then a partial recover.
6. `test_stain_cells_are_seeded_and_format_free`: deterministic, a different seed gives a different set, the centre is covered, cells lie within 1.3·radius, and vertical and landscape agree.
7. `test_reveal_and_recover_orders`: permutations that are stable per seed, and the last recover is at `at + (n-1)·per_cell`.
8. `test_geometry_in_every_format`: square cells, an integer pitch, the grid above `caption_y`, and a 33×33 vertical grid that stays readable.
9. `test_lost_cell_pixels_follow_the_story`: a cell shows the lost colour, then its original colour.
10. `test_frames_are_deterministic`: two Timelines give identical bytes.
11. `test_frame_builds_no_text`: with `make_block` and `font` counted, frames make no calls.
12. `test_layout_warnings`
13. `test_late_steps_warn`
14. `test_grid_reads_only_declared_fields`: like the meter test.
15. `test_example_validates_and_renders`

## Order
1. Pure logic, with tests 4–7.
2. Registration, with tests 1–3.
3. `GridScene`, with tests 8–14.
4. The example and README, with test 15.

## Out of scope and risks
**Out of scope:**
- A counter
- Per-cell text or colours
- Extra cell states
- QR encoding
- Zoom
- Reading the status aloud
- `color: bad`

**Risks:**
- In `midnight` the accent is blue, so the fail flash reads cool rather than red.
- A 33×33 `pop` reveal draws up to 1089 rectangles per frame, but only while the reveal runs.
