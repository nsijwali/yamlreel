# Hardening: render lifecycle, memory, external boundaries

Source: the owner's hardening review of 55717c3 (v0.3.6). Scope: every "Fix now" and "Fix next" item, the CI items
and `--profile` extensions. "Consider later" (isolated workers, durable job state, hosting) is out of scope.

Rules for every slice: YAML format and public call signatures stay; new logic goes into NEW small modules with their
own tests; existing files only get thin call-site changes. Frame equivalence, timing tests, subtitle output and import
aliases are preserved. Example renders must hash identically before and after (frames, audio, poster, contact).

## Test seams that must keep working

- `render.ffmpeg_exe`, `render.AUDIO_FORMATS`, `render.MAX_WATERMARK_PIXELS`, `render.plan_durations`,
  `render.Timeline`, `render.ImageDraw` stay patchable on `render` and are looked up through render's globals.
- `subprocess.Popen` / `subprocess.run` / `shutil.which` are called as module attributes (tests patch the module).
- `specs.suggest` stays the "did you mean" hook for every unknown-key error, config ones included.
- `FakePopen` in tests/test_render_only.py gains `wait(timeout=None)`, `poll`, `kill`, `terminate` and touches its
  output file (plumbing for the new lifecycle and publish steps, no assertion changes).
- voice's ffmpeg lookup moves off render (slice 5); the three test_voice_speed_trim patches move to the new seam.

## Slices (one commit each)

### S1 small fixes (review 1, 5, 6, 7, 8, 10)
- `yamlreel/watermark_fit.py` (new): `fit_watermark(src_w, src_h, size_px, frame_w)` returns the destination size
  computed from the header only; height capped at `size_px`, width capped at `MAX_WATERMARK_WIDTH_FRAC` of the frame
  (aspect kept, so an extreme-wide logo shrinks); a result under 1 px is a clear error. render.py calls it before
  `convert`/`resize`. Tests: wide (100000x1), tall, transparent, normal (identical to the old formula); the resize is
  asserted without allocating the oversized image.
- polish.py:357 and render.py:156: `if key not in cache: cache[key] = build()`. Tests: one build per distinct key.
- `yamlreel/shorts.py` (new): `SHORTS_MAX_SECONDS = 180`, `EDITORIAL_MAX_SECONDS = 60`, `shorts_eligible(w, h, s)`
  (square or vertical, <= 180 s), `editorial_note(s)`. polish.audit_video and render.share_copy use it; 61-180 s is
  ok in audits, with an `info:` editorial tip that is not a warning. The incorrect 75 s assertion is fixed. Boundary
  tests 60 / 60.01 / 180 / 180.01, square, landscape.
- safety.py: mapped IPv4 is normalized explicitly before the DNS-unspecified check (`::ffff:0.0.0.0` counts as
  unspecified on every Python); literal and mixed answers keep the refusal. Test forces the old `is_unspecified`
  behaviour by monkeypatch so it is a real regression test on any Python.
- `yamlreel/errorfmt.py` (new): `describe_key`, `describe_value`, `yaml_problem`, `unknown_key` (hint hook registered
  by specs, late-bound so `specs.suggest` patches work) and `redact_url` (drops userinfo, masks query values and the
  fragment). config.py re-exports the old names. voice.py: validated JSON-RPC envelopes (dict, matching id, `result`
  or `error`, result a dict, content a list), bad JSON / base64 / missing file / truncated WAV -> `VoiceError` with
  context; every message with the server URL uses `redact_url`. Narrowly typed excepts only.
- voice.py narration cache: `tempfile.mkstemp` in the cache dir per writer, validated, then `os.replace`; temp file
  removed on any failure (interruption before publish leaves no `.part`). Cache keys unchanged. Tests: two threads
  writing the same key, interruption before publish.

### S2 staged output publication (review 2)
- `yamlreel/publish.py` (new): `Staging(dest)` makes `dest/.staging-<random>` (same filesystem), hands out staged
  paths, `remove(name)` records files to delete, `publish()` replaces each final file with `os.replace` (sidecars
  first, video last) and removes stale names; on failure or `discard()` the staging folder is deleted and the
  destination is untouched. Each replace is atomic; the set as a whole is not (documented).
- render.py writes poster, srt, video and contact sheet(s) into staging; the stale-srt unlink moves into publish.
- Tests: failure injected in encoding and in the contact sheet leaves the previous video/poster/srt/contact intact
  and leaves no staging folder; success replaces all; filenames unchanged.

### S3 bounded waits, cleanup, deadlines (review 3)
- `yamlreel/proclife.py` (new): `Deadlines` (encoder_stall 300 s, encoder_finish 600 s + 2 s per video second,
  subprocess 1800 s, browser_frame 180 s; each 1..86400) parsed from a `timeouts:` user-config key; `run(cmd, what,
  timeout, error)` bounded subprocess; `stop(proc, grace)` terminate -> bounded wait -> kill; `FramePipe` owns the
  encoder process, the feeder thread and the queue: bounded `put` (stall deadline), `finish()` with a bounded wait,
  `abort()` on error/cancel (kill first so a blocked write returns, then join).
- render.py uses `FramePipe`; `_Page.close_all()` moves into `finally`; lineart `frame`/start waits get the browser
  deadline; mixdown/polish/voice subprocess calls go through `proclife.run`.
- `Runtime.deadlines` (default `Deadlines()`), CLI passes the config value. `timeouts` is a trusted key.
- Tests (real processes, tiny Python scripts as fake ffmpeg): encoder that never reads, encoder that exits early,
  drawing exception, KeyboardInterrupt: each returns promptly, the process is gone, the feeder thread ended and the
  browser close hook ran. Full suite at the end of S3.

### S4 memory budgets (review 4)
- `yamlreel/mediabudget.py` (new): `MediaBudget(limit)` charges decoded bytes per owner, evicts least recently used
  owners through their release callback, refuses one item larger than the limit; tracks `peak`.
- ShotScene: captures/validates eagerly (errors still name the scene), reads only the size, decodes on demand through
  the budget; `release()` drops the pixels.
- voice.prepare_narration: optional `max_seconds=` keyword; the running total of narrated floors and fixed scene
  lengths is checked after each clip, so an over-long video stops before the remaining clips are processed.
- contact sheets: more than `MAX_SHEET_PIXELS` -> pages `contact.png`, `contact-p2.png`, ... (stale pages removed at
  publish). Random access, transitions, posters and `--only` unchanged.
- Measured: peak RSS for 2/8/16 shot scenes before and after (fresh process per N).

### S5 dependency loops and RenderPlan (review 9)
- (a) config/theme import `errorfmt`, not specs (done in S1, finished here: no late `specs` import in config).
- (b) `yamlreel/ffmpegtools.py` (new): `ffmpeg_exe`, `AUDIO_FORMATS`; render re-exports both; voice and polish import
  it, so voice no longer imports render.
- (c) `yamlreel/renderplan.py` (new): typed `RenderPlan` (clips, words, durations, starts, total, windows, spans,
  frame runs, tag) built by `plan_render(...)`; render() takes an optional `plan=` and the CLI builds the plan once
  for all formats, so narration processing is not repeated per format. render() keeps its signature.
- Full suite at the end of S5.

### S6 CI and diagnostics
- `yamlreel/diagnostics.py` (new): FFmpeg and browser (playwright / Chromium) versions; `python -m
  yamlreel.diagnostics` prints them; `--profile` prints them with peak memory and the deadlines in force.
- `yamlreel/peakmem.py` (new): process peak RSS (guarded `resource` import).
- ci.yml: a `browser` job installs Chromium and runs browser tests with `YAMLREEL_REQUIRE_BROWSER=1`, which turns
  their skips into failures (tests/conftest.py); a `constraints` job installs with `ci/constraints.txt`; a `latest`
  job installs the newest compatible dependencies; every job records versions. A test checks that the constraints
  file pins every runtime dependency and satisfies pyproject's ranges.
- Full suite at the end of S6, example hashes compared.
