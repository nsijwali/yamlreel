# `render --only`: render some scenes

## Purpose
Editing one scene today means a full re-render. `--only` renders just the chosen scenes, cut out of the full video.

## CLI
```bash
yamlreel render spec.yaml --only 3            # 0-based, as in `script` and `check-voice`
yamlreel render spec.yaml --only 2-4 --preview
yamlreel render spec.yaml --only 1,5 --format all
```

## Behaviour
- **Full plan, partial frames.** Loading, narration, `plan_durations`, `Timeline`, `limits.max_seconds` and every warning stay exactly as today, for the whole video. Consecutive chosen scenes form one window `[starts[i], starts[j]+dur[j])`. Windows play back to back.
- **Frames.** A window takes the full video's frame indices `ceil(a*fps) .. ceil(b*fps)-1`. The poster is never frame 0.
- **Transitions.** Inside a window they are unchanged. A window's first scene starts with a cut, so no unchosen scene shows. To do this, `Timeline.cut_in: set[int]` (empty by default) is checked in `frame_at`.
- **Audio.** Music (generated, ducked, faded over the full length) and narration are built as today. A final `apad,asplit,atrim,asetpts,concat` cuts each window at its frame times. Music keeps its full-video position, and audio length equals video length. Cuts between windows are hard.
- **Poster.** Not written. `tl.poster_time()` still runs, so a bad `poster.scene` still errors.
- **Contact sheet.** Only the chosen scenes' rows, labelled with full-video numbers and times. Uses `contact_sheet(..., scenes=None)`.
- **Subtitles.** Burned-in karaoke is scene-local, so unchanged. The SRT holds the chosen cues, shifted to slice time. The existing `subtitles.srt` is neither written nor deleted.
- **share-copy.txt.** Not written, because it describes the full video. Its warnings are still printed, computed with the full length.
- **Names.** `<out>/<fmt>/video-scenes-<L>.mp4`, `contact-scenes-<L>.png`, `subtitles-scenes-<L>.srt`. `<L>` is the windows joined by `_`, each `A-B` or `N`, for example `2-4`, `1_5`. Full outputs are never touched.
- **No title or end card is added.** Step badges and the watermark look as in the full video.
- **`--preview` and `--format all`.** Both combine with `--only`, with the same windows in every format.
- **Validation** (`cli.parse_only(text, n) -> list[int]`, raises `SpecError`):
  - `--only: 'x' is not a scene number or range; use 3, 2,4 or 2-4 (scenes are numbered 0 to 7)`. Also for an empty item, `-1` and `2.5`.
  - `--only: scene 9 does not exist; scenes are numbered 0 to 7`
  - `--only: 4-2 is backwards; write 2-4`. Reverse ranges are rejected.
  - Duplicates and overlaps are merged, and the order becomes spec order.
  - `render()` checks again and raises `RenderError`.
- **Note** (one stderr line): `only: scenes 2-4 of 0-7, 12.40-21.95s of the 38.20s video (9.55s)`. Several windows are joined with ` + `.
- **Frame budget.** The CLI and `render()` count only the chosen scenes. `max_seconds` still counts the full length.
- **Voice.** No new voice code. `prepare_narration` runs unchanged, because every scene's length places the window and the music. Cached clips are read, and only missing ones are synthesised. After one render, editing scene 3 synthesises only scene 3. On a cold cache everything is synthesised once and reused later. With no server, the existing error points to `--no-voice`. `--no-voice` strips narration as today, so the slice matches the `--no-voice` full video.

## Files
- `yamlreel/cli.py`: the flag, `parse_only`, the budget, the note, skipping share copy.
- `yamlreel/render.py`: `render(only=None)`, `scene_windows`, `only_label`, the frame loop, the audio cut, names, `cut_in`, `contact_sheet(scenes=)`, slicing the SRT. The result adds `full_seconds` and `windows`.
- `README.md` and the new `tests/test_render_only.py`.
- `voice.py` and `specs.py` do not change.

## Tests (`tests/test_render_only.py`)
1. `test_parse_only_normalises`: `3`, `2,4`, `2-4`, `4,2`, `2-4,3` and `" 1 , 3"` give sorted, unique lists.
2. `test_parse_only_errors`: `x`, `""`, `2,,3`, `-1`, `9` (n=8) and `4-2` each give the exact message.
3. `test_only_duration_is_sum_of_chosen_scenes`: `--only 1-2` with a stretched narrated scene lasts `dur[1]+dur[2]`, within 1/fps + 0.05 s.
4. `test_only_audio_matches_video_length`: music plus narration, and a music file shorter than the window. Audio length equals video length within 0.05 s.
5. `test_only_music_is_the_full_slice`: `--only 2` audio correlates above 0.95 with the full render's audio from `starts[2]`.
6. `test_only_non_contiguous_skips_the_gap`: `--only 0,2` lasts `dur0+dur2`, and scene 2's first frame is not blended.
7. `test_only_names_do_not_collide`: after a full render and then `--only 1`, every full output is byte-unchanged and the `*-scenes-1.*` files exist.
8. `test_render_without_only_is_unchanged`: fake `Popen` records argv and the frames. Argv equals the golden argv from eda9f02, frame 0 is the poster, and there are `round(total*fps)` frames. `test_a_spec_without_new_keys_renders_the_same_frames_as_0_2_2` still passes. The builder records an output sha256 comparison of eda9f02 against the branch in handoff.md.
9. `test_only_frame_budget_counts_only_the_slice`: a spec over `MAX_FRAMES` in full passes the budget check with `--only 0`.
10. `test_only_contact_sheet_rows`: `--contact-frames 1 --only 1-2` gives two tile rows, labelled `1:` and `2:`.
11. `test_only_note_on_stderr`: the exact note line.

## README
- Add `--only N,A-B` to the `render` row of the Commands table.
- Add a short "Rendering some scenes" section after Script sheet: numbering, names, what is skipped, and the voice cache.

## Out of scope
Per-scene caching, picking scenes by name, de-click fades, and a poster for a slice.

## Steps
1. `parse_only` (tests 1, 2).
2. `cut_in`, `contact_sheet(scenes=)`, `scene_windows`, `only_label` (test 8).
3. `render(only=)` (tests 3-7, 10).
4. CLI wiring (tests 9, 11), then the README.
