# Voice check and story events (2026-11-09)

## Purpose
1. `yamlreel check-voice SPEC` transcribes the cached narration clips and compares them with the narration text. It catches dropped negations, changed numbers and skipped words, like the clip that said "a queue **is** extra capacity".
2. An optional `events` list on outline steps, which `yamlreel story` turns into timed cues, so step scenes move with the voice instead of sitting still like labelled cards.

## Change 1: `check-voice`
**CLI.** `yamlreel check-voice SPEC [--out DIR] [--model NAME]`, built on the `common` parent parser (`--config`, `--voice-url`, `--read-screen`, `--allow-path`). `--out` works as it does in `render`: the cache is `<--out or confined output_dir>/.cache`. `--model` defaults to `base.en`. `tiny.en` is about half the size but has a noticeably higher word error rate, and a dropped "n't" is a single phoneme, so the cheapest model would give false alarms. `base.en` runs with int8 on the CPU in under 500 MB of RAM. `--model` also accepts a local model path.

**Finding the clips (read-only).** Add `voice.cached_clip_path(cache, server, text, vo) -> Path`, which returns `cache / f"narration-v{CACHE_KEY_VERSION}-{narration_cache_key(...)}.wav"`, and make `prepare_narration` use it so both commands share one source of truth. `check-voice` builds the server with `_runtime(spec, cfg)`, applies `--read-screen` exactly as `cmd_render` does, and uses `narration_text(sc, vo)` as the expected text. It never creates `_Session`, never synthesises and never creates the cache directory.
- Synthesised scene with no cached file: `note`, "not cached, run render first". This does not change the exit code.
- `narration_audio`: the file is resolved with `resolve_local` and checked against `narration` (its transcript). With no transcript the scene gets `note`, "no transcript to compare".
- Scenes with `narration: false` or no narration are skipped and print nothing. `narration_words` is ignored, because it only carries timing.
- **Raw versus processed audio:** the raw cached clip is checked. Trim only removes silence and `atempo` keeps the words, so the words are the same. Checking the raw clip needs no ffmpeg and examines exactly what the voice model produced.
- **Short clips:** speech length is `len(trim_silence(raw)) / SAMPLE_RATE`. `trim_silence` uses numpy only. Below `MIN_CHECK_SECONDS = 1.0` the scene gets `note`, "0.6 s of speech is too short to check reliably (minimum 1.0 s); listen to it", and is not transcribed. Whisper pads every input to 30 s and makes up phrases such as "Thank you." for sub-second audio. One second holds about 2.5 words.
- A cached file that cannot be read (`VoiceError`) is reported as an ERROR on that scene.

**Recogniser.** It is injectable: `Recognise = Callable[[Path], str]`. `load_recogniser(model)` imports `faster_whisper` and returns a closure that builds `WhisperModel(model, device="cpu", compute_type="int8")` on its first call. That call is `transcribe(str(path), language="en", beam_size=5, temperature=0.0, condition_on_previous_text=False, vad_filter=False)`, and the segment texts are joined. It passes no `initial_prompt`, because the expected text would bias the result. The model downloads on first use only, so a spec where nothing is cached downloads nothing.

**Normalisation** applies to both texts:
- lowercase and turn `’` into `'`
- `X n't` becomes `X not` (special cases: `can't` → `can not`, `won't` → `will not`, `shan't` → `shall not`), and `cannot` becomes `can not`
- `'re`, `'ll`, `'ve`, `'m` and `'d` expand. `'s` expands to `is` only after it, that, there, here, what, who, he and she; elsewhere the apostrophe is dropped
- thousands commas are removed, `%` becomes ` percent`, `10k` becomes `10000`, digits are split from letters (`3x` → `3 x`), and hyphens become spaces
- number words from zero to the millions become digits, for example "twenty five" → `25` and "a hundred" → `100`
- other punctuation is stripped, keeping decimal points, and the text is split on spaces

**Rules, per scene, in this order:**
| Check | Level |
|---|---|
| The count of any of `not`, `no`, `never` or `without` differs (missing **or added**: both flip the meaning) | ERROR |
| The multisets of numeric values differ | ERROR |
| `1 - matched/len(expected)` is above 0.20, where `matched` is the sum of `difflib.SequenceMatcher(autojunk=False)` matching blocks | WARNING |

The 80% threshold: Whisper on clean TTS usually gets more than 95% of words right. A misheard proper noun such as "yamlreel" costs one word, and a 5-word line with one miss is exactly 80%, which still passes. A skipped clause or a cut-off sentence falls well below 80%.

**Output** goes to stdout, one line per problem. Each text is quoted in full, up to 200 characters:
```
scene 2 ERROR negation missing (not): expected "But a queue isn't extra capacity." heard "But a queue is extra capacity."
scene 5 ERROR numbers differ (expected 3, 50; heard 3, 15): expected "…" heard "…"
scene 1 WARNING 35% of the narration words not heard (limit 20%): expected "…" heard "…"
scene 4 note: not cached, run render first
checked 5 of 7 narrated scenes: 2 errors, 1 warning
```
**Exit codes:**
- 0: clean, notes only.
- 1: at least one ERROR or WARNING.
- 2: a setup problem. `cmd_check_voice` catches these itself and prints `error: …` to stderr. They are:
  - `faster_whisper` cannot be imported. This is checked first, and the only output is `error: check-voice needs faster-whisper: pip install "yamlreel[verify]"`.
  - SpecError, ConfigError or UnsafeInput.
  - The confined output directory check fails (catch its `SystemExit`).
  - A synthesised scene exists but no voice server is configured: "check-voice needs the voice_server.url (or --voice-url) used to render, to find cached clips".
  - `voiceover.language` is set and does not start with `en`: "check-voice checks English narration only".
  - The model fails to load or transcribe.

## Change 2: outline `events`
This fits in about 60 lines of `story.py`. Add `"events"` to `STEP_KEYS`.
```yaml
steps:
  - name: Queue
    say: "A queue absorbs the burst. But a queue isn't extra capacity."
    show: meter
    events:
      - {at: 1.0, text: Burst arrives}
      - {at: 3.5, text: Queue full}
```
**Rules:**
- Allowed keys are `at` and `text`, and both are required.
- `at` is a number from 0 to 60 and must not decrease within a step.
- `text` is 1 to 40 characters (`MAX_CELL_CHARS`).
- A step can have at most 8 events. `events: []` is treated as if `events` were absent.

**What each scene type gets** (event `n`):
- `diagram`: `flash: [{node: a, at, text}]`. Node `a` is visible from the start. The placeholder nodes and edges stay.
- `meter`: replaces the placeholder `steps` with `[{at, level, text}]`, where `level` alternates 4, 10, 4, … (the placeholder's drain and refill). The README says to set real levels.
- `array`: `steps: [{at, mark: [n % 3], text}]` on the three TODO items.
- Any other `show`, including the default `bullets`, is a StoryError: `steps[1].events: events work with show: diagram, meter or array; this step shows bullets`.

**Timing:** `at` is seconds from the scene's start, the same clock as every spec cue (speech starts at `NARRATION_LEAD`, 0.3 s). Narration stretching only adds time at the end (`max(seconds, floor)`), so cues never move. Beat snapping can shorten a scene by up to half a beat, so the step scene gets `seconds = max(narrated_seconds(kind, say), last_at + EVENT_HOLD)`, with `EVENT_HOLD = 1.5`.

## Alternatives rejected
- **Events tied to words:** word times exist only at render time, are estimated (±0.2 s) and are missing with `--no-voice`. This would be a Timeline feature, not a story change.
- **Checking the processed audio:** needs ffmpeg and checks nothing new.
- **Comparing phonemes or using an aligner:** heavier, with no gain for these three failure types.
- **`tiny.en`:** gives false alarms on short negations.

## Risks
- Whisper may "correct" ungrammatical speech. Because there is no prompt and temperature is 0, the risk is low, but this is a safety net, not proof, and the README says so.
- Homophones ("no" and "know") could raise a false ERROR. The full line is printed, so a person can judge it.

## Files to change
- `yamlreel/voice.py`: add `cached_clip_path`.
- `yamlreel/voicecheck.py` (new): normalisation, `compare`, `check_spec(spec, server, cache, resolve_audio, recognise) -> list[Finding]`, `load_recogniser`.
- `yamlreel/cli.py`: the `check-voice` subcommand.
- `yamlreel/story.py`: `events`.
- `pyproject.toml`: add `verify = ["faster-whisper>=1.0"]`.
- `README.md`.
- `tests/test_voicecheck.py` (new) and `tests/test_story.py`.

## Tests
All voice tests use a fake recogniser and WAVs from `fakes._tone_wav_bytes`, with no download.

`test_voicecheck.py`:
- `test_normalise_matches_contractions_numbers_and_punctuation`: isn't/is not, can't, 1,000, twenty-five, 50%, 10k.
- `test_dropped_negation_is_an_error`: the queue line. `test_added_negation_is_an_error`.
- `test_changed_number_is_an_error_but_three_equals_3`.
- `test_overlap_threshold`: 4/5 words heard is clean, 3/5 is a WARNING.
- `test_reads_the_raw_clip_by_render_key_without_the_server`:
  - `_Session` and `change_speed` are monkeypatched to raise.
  - `trim` and `speed` are set in the spec.
  - The recogniser receives the `narration-v2-<key>.wav` path.
- `test_uncached_scene_is_a_note_and_nothing_is_written`: exit 0 and no `.cache` directory is created.
- `test_narration_audio_uses_its_transcript_and_no_transcript_is_a_note`.
- `test_short_clip_is_a_note_and_not_transcribed`: a 0.5 s tone.
- `test_silent_scenes_and_narration_words_are_ignored`.
- `test_read_screen_flag_finds_the_same_clip_as_render`.
- `test_exit_codes`:
  - 0 when clean, 1 for an ERROR, 1 for a WARNING only.
  - 2 when `sys.modules["faster_whisper"] = None`: one stderr line with the hint, and empty stdout.
  - 2 with no voice server, 2 for `language: ja`, 2 for a bad spec.
- `test_load_recogniser_is_lazy_and_uses_cpu_int8_english`: uses a fake `faster_whisper` module in `sys.modules`.

`test_story.py`:
- `test_events_become_cues_for_diagram_meter_and_array`: checks the exact generated `flash`/`steps`, and that `load_spec` passes.
- `test_events_on_other_scene_types_are_an_error`: covers bullets (the default), table and timeline.
- `test_event_validation`: covers each rule above.
- `test_events_lengthen_the_scene_to_hold_the_last_cue`: `at: 9` → 10.5 s, and a longer `say` wins.
- The existing `test_the_template_and_the_shipped_example_match` must still pass.

## README
- **Checking the voice** (after Voiceover): what the command does, the install line `pip install "yamlreel[verify]"` (the first run downloads `base.en`, about 145 MB), the levels, the exit codes, and that it is a safety net, so listen to any flagged scene.
- **Outline `events`** (under Story outlines): the YAML above, what each type gets, the error, and how timing works.
- Add a row to the install table and a row to the Commands table.
- Troubleshooting: `check-voice needs faster-whisper` and `not cached, run render first`.
- Add an Unreleased bullet.

## Out of scope
Auto-fixing, re-speaking, music, subtitles, non-English narration, events on timeline, tree or grid scenes, and `level` on events.

## Implementation steps
1. Add `cached_clip_path` and refactor `prepare_narration` to use it. The existing narration tests must stay green.
2. Write `voicecheck.py` normalisation and `compare`, with their unit tests.
3. Write `check_spec` and `load_recogniser`, with tests.
4. Add the CLI subcommand and the pyproject extra, with the exit-code tests.
5. Add story `events`, with tests.
6. Update the README.
