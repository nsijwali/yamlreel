# Line numbers in spec validation errors

## Purpose
Errors name a path (`scenes[1].secnds`) but not a line. Add `file:line` so editors can jump to it, without changing library message strings.

## Before / after
1. Block style: `error: scenes[1].secnds: unknown field ...` becomes `error: spec.yaml:34: scenes[1].secnds: unknown field ...`
2. Flow mapping on line 12, `- {type: stat, value: 42%, seconds: abc}`: `error: spec.yaml:12: scenes[3].seconds must be a number between 0.1 and ...`
3. Merged anchor. Line 4 is `- &look {type: title, title: Hi, eyebrow: INTRO}`, line 5 is `- <<: *look`, line 6 is `  type: bullets`. Result: `error: spec.yaml:4: scenes[1].eyebrow: unknown field for a bullets scene; ...` (the anchor definition, where the key is written).

**Format decision: `error: <file>:<line>: <message>`.** This keeps the batch-1 contract: one stderr line that starts with `error: `.

No prefix can leave every existing test unchanged. Seven CLI assertions check `startswith("error: <path>")`:
- `test_cli_validate_prints_one_error_line_and_exits_1` (4 cases)
- `test_an_alias_bomb_gives_a_short_error_fast`
- `test_a_huge_string_value_gives_a_short_error` (2 cases)

Their expected strings must gain `<file>:<line>: `. All 182 library-level `str(e)`/`match=` assertions stay unchanged. The rejected alternative, a trailing `(spec.yaml:34)`, keeps every test unchanged but editors do not reliably link it. **The caller must accept this.**

## Mechanism
**New `yamlreel/yamlpos.py`** (about 70 lines):
- `class LocatedError(ValueError)`:
  - `source: Path | None = None`, `_text: str | None = None`, and `attach(source, text)`.
  - `line` and `column` (1-based or None) are lazy, cached properties. Nothing is parsed until one is read.
- `leading_path(msg)`: the regex `^[A-Za-z_]\w*(?:\[\d+\]|\.[A-Za-z_]\w*)*`.
  - Messages start `<path> ...` or `<path>: ...` (specs.py docstring).
  - Messages without a path (`spec needs ...`, `total length ...`) start with a word that is not a top-level key. Unknown top-level keys are rejected first, so these never match.
- `locate(text, path, opaque) -> (line, col) | None`:
  - Parse with `loader = yaml.SafeLoader(text)`, `get_single_node()`, then `dispose()`.
  - **Walk only along the path.** There is no full path map, because walking an alias bomb in full would blow up.
  - **Merge keys:** before searching a mapping, call `loader.flatten_mapping(node)`, PyYAML's own merge logic. Explicit keys then beat merged ones, and the last duplicate wins, the same as `safe_load`. Search from the end. A key matches only a `tag:yaml.org,2002:str` ScalarNode.
  - **Position:**
    - A mapping entry reports its key's mark, and a list item reports its own mark: where the last path element is written.
    - A value written on its key's line, in block or flow style, gives the same line.
    - An alias used as a value (`type: *a8`) reports the key that uses it.
    - A merged-in key, or a list item that is an alias, reports the anchor definition.
  - **Not found:** return the deepest node found.
    - A missing required field (`scenes[1] (bullets): missing items`) or a defaulted or normalised value (`viewport`, `music: koto` becoming `music.style`) points at its parent.
    - It never falls back to the root, so a value that came only from the config gives None.
  - **Opaque prefixes:** `{"meta.tags", "meta.links", "meta.credits"}` stop the walk at the list. `merge` puts config entries first, so the indices differ from the file.
  - **Any exception returns None.** Locating never hides the real error.
- **Limits:** the input is the exact text `safe_load` already accepted (at most `MAX_SPEC_BYTES`). Composing and flattening redo only part of what `safe_load` did, and the walk is as long as the path. There is no new way to blow up.
- **BOM and CRLF:** `read_text` turns `\r\n` and `\r` into `\n`. PyYAML skips a leading BOM without counting it.

**specs.py:**
- `SpecError(LocatedError)`, with an `OPAQUE` set.
- `load_spec` wraps `validate()`: `except SpecError as e: e.attach(p, text); raise`.
- Read and parse errors are unchanged. YAML errors already say `at line N, column M`.

**story.py:**
- `StoryError(LocatedError)`. `load_outline` wraps `parse_outline` the same way; this is cheap.
- `generate`'s rebased `steps[j].events[n]` errors are left out, because the outline text is not available there.

**cli.py:** `_error_line(e)` adds `{e.source}:{e.line}: ` when `line` is set. `main()` and `cmd_check_voice` use it. `source` is the path as typed.

## Per command
- `validate`, `render`, `script` and `story` print located errors and exit 1.
- `check-voice` prints them the same way and exits 2.
- These carry no source and print unchanged:
  - errors raised after loading (`--fps`, the `--format` frame budget, voicecheck)
  - config-file errors

## Files
- `yamlreel/yamlpos.py` (new), `yamlreel/specs.py`, `yamlreel/story.py`, `yamlreel/cli.py`
- `tests/test_error_lines.py` (new), and the 7 assertions in `tests/test_validation.py`
- `README.md`

## Tests (`tests/test_error_lines.py`)
- `test_library_messages_unchanged`: `str(e)` from `load_spec` equals the `validate()` message.
- `test_block_unknown_field_line`: `e.line` is the line of `secnds:`, and the CLI line is `error: <f>:<n>: scenes[1].secnds...`.
- `test_flow_mapping_line_and_column`: a multi-line flow mapping reports the key's line and column.
- `test_merged_key_reports_anchor_definition`: example 3. Also checks that an explicit key beats a merged one, and that `*a` wins in `<<: [*a, *b]`.
- `test_alias_value_reports_using_key`.
- `test_missing_required_field_points_at_scene`.
- `test_config_only_value_has_no_line`: a bad `fps` from the config prints plain `error: fps ...`.
- `test_meta_tags_index_not_trusted`: with config tags present, the error reports the `tags:` line.
- `test_crlf_and_bom_lines`: CRLF and BOM versions of the same spec give the same line.
- `test_empty_and_pathless_messages_have_no_line`: an empty file, a top-level list, and `total length`.
- `test_valid_spec_never_composes`: patch `yamlpos.locate` to raise. A valid `load_spec` and `str(e)` both still work.
- `test_alias_bomb_locates_fast`: reading `.line` takes under 1 s.
- `test_locator_failure_falls_back`: when `locate` raises, the plain `error: ...` is printed.
- `test_story_outline_line`: an `steps[1].events[0].at` outline error reports its line.

## README
In "Unknown keys are errors" (line 330), show `error: spec.yaml:12: scenes[0].titel: ...`. Add that the line points at the key, at the enclosing scene for a missing field, and at the anchor for a `<<:`-merged key.

## Out of scope
- Column in CLI output
- Config-file errors
- Rebased `generate` story errors
- YAML parse-error format
- `\x85`/` `/` `, which PyYAML counts as line breaks but most editors do not

## Steps
1. `yamlpos.py` and its tests.
2. `SpecError`, `load_spec` and the CLI, plus the 7 assertions.
3. `StoryError` and `load_outline`.
4. The README.
