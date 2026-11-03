<div align="center">

# yamlreel

**Write a short YAML file. Get a finished video.**

<sub>Created by <a href="https://github.com/nsijwali">Nitin Sijwali</a></sub>

yamlreel turns a YAML file into a finished video: system-design explainers, code tips, project launches and
feature demos for YouTube, Shorts and LinkedIn, with diagrams, typed code, original music and an optional
voiceover in your own voice.

[![ci](https://github.com/nsijwali/yamlreel/actions/workflows/ci.yml/badge.svg)](https://github.com/nsijwali/yamlreel/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/yamlreel)](https://pypi.org/project/yamlreel/)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

<img src="https://raw.githubusercontent.com/nsijwali/yamlreel/main/docs/demo.gif" alt="A diagram building itself: a YAML spec and VoiceStudio feed yamlreel, which hands frames and audio to ffmpeg to produce the video" width="720">

**▶ [Watch the 1-minute demo](https://github.com/nsijwali/yamlreel/releases/tag/v0.2.0)**: a video about yamlreel, made by yamlreel from one YAML file, with no video editor.

<img src="https://raw.githubusercontent.com/nsijwali/yamlreel/main/docs/preview.jpg" alt="Contact sheet of a rendered video: a typed hook, a cache diagram, bullet lists, highlighted code, a big stat and an end card" width="820">

<sub>Every scene of <a href="https://github.com/nsijwali/yamlreel/blob/main/examples/cache-stampede-45s.yaml">examples/cache-stampede-45s.yaml</a>, rendered from one YAML file.</sub>

</div>

---

## What you get

Every render writes this layout (the output folder is `yamlreel-output/` unless you pass `--out DIR`):

```
yamlreel-output/
├── vertical/            one folder per format: landscape 1920×1080, vertical 1080×1920, square 1080×1080
│   ├── video.mp4        H.264 (+ AAC when there is music or a voice). Frame 0 is the poster, so the thumbnail is never blank.
│   ├── poster.jpg       the settled hook frame, ready to upload as a thumbnail
│   └── contact.png      two labelled stills per scene: look at this before you publish
└── share-copy.txt       title, description and hashtags for the whole video, ready to paste
```

`share-copy.txt` adds `#Shorts` for vertical clips up to 60 seconds. Videos can be any length, and only a
vertical video over 3 minutes gets a warning. Music is generated on your machine (plucked strings in a
Japanese scale), so there is nothing to license or credit.

## Quick start

You need **Python 3.10 or newer**. pip installs everything else except fonts on macOS and Windows (see the note below).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install "yamlreel[ffmpeg]"
```

Prefer an isolated command-line install? `pipx install "yamlreel[ffmpeg]"` works too.

Make your first video:

```bash
yamlreel init system-design my-video.yaml     # a starter spec (or: project, code-tip)
yamlreel render my-video.yaml --preview       # half size, 12 fps: check layout and timing in seconds
yamlreel render my-video.yaml                 # full quality, every format in the spec
```

Open `yamlreel-output/<format>/contact.png` first. To try the showcased example without setting up a voice:

```bash
curl -LO https://raw.githubusercontent.com/nsijwali/yamlreel/main/examples/cache-stampede-45s.yaml
yamlreel render cache-stampede-45s.yaml --no-voice
```

> **Fonts on macOS and Windows:** yamlreel draws text with the free DejaVu fonts, which most Linux systems
> include. On macOS run `brew install --cask font-dejavu` once. On Windows, install them from
> [dejavu-fonts.github.io](https://dejavu-fonts.github.io) ("Install for me" is fine).

<details>
<summary><b>Install details for each system</b></summary>

<br>

| | macOS | Linux | Windows (PowerShell) |
| --- | --- | --- | --- |
| Python 3.10+ | `brew install python@3.12` or [python.org](https://www.python.org/downloads/) | usually installed; Debian/Ubuntu also need `python3-venv` | [python.org](https://www.python.org/downloads/), then use `py` instead of `python3` |
| Activate the venv | `source .venv/bin/activate` | same | `.venv\Scripts\Activate.ps1` (if scripts are blocked: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`) |
| ffmpeg | the `[ffmpeg]` extra (or `brew install ffmpeg`) | the extra, or `sudo apt install ffmpeg`. On Fedora use the extra: its own `ffmpeg-free` cannot encode H.264 | the `[ffmpeg]` extra |
| Fonts | `brew install --cask font-dejavu` | Debian/Ubuntu: `sudo apt install fonts-dejavu-core`; Fedora: `sudo dnf install dejavu-sans-fonts dejavu-serif-fonts dejavu-sans-mono-fonts`; Arch: `sudo pacman -S ttf-dejavu` | DejaVu from [dejavu-fonts.github.io](https://dejavu-fonts.github.io) |
| Web captures (optional) | `pip install "yamlreel[capture]"` then `playwright install chromium` | same | same |

- **ffmpeg:** yamlreel uses the `ffmpeg` on your PATH if there is one, and otherwise the copy that the `[ffmpeg]` extra (the `imageio-ffmpeg` package) brings along. You only need one of them.
- **Fonts:** yamlreel looks in your `font_dir` setting (or `YAMLREEL_FONT_DIR`) first, then the system font folders, then your own (`~/Library/Fonts` on macOS, `%LOCALAPPDATA%\Microsoft\Windows\Fonts` on Windows). If your fonts live anywhere else, point `font_dir` at that folder. Japanese text uses Noto CJK when it is installed.
- **Web captures** are only needed for `shot` scenes that screenshot a URL. `chromium_path` in your user config (or `YAMLREEL_CHROMIUM`) picks a different browser.
- **Windows** is not covered by CI yet. Reports are welcome.

</details>

## Word-by-word subtitles

Add `subtitles: true` and every narrated scene gets burned-in captions that show three to five words at a time,
with the word being spoken highlighted. Nothing else is needed: no extra dependency and no network.

```yaml
subtitles:
  style: karaoke            # karaoke: the spoken word sits in an accent box; pop: it grows 15%; plain: no highlight
  words: 4                  # words on screen at once, 1 to 8
  position: lower           # lower (above the Shorts UI) | middle
  size: 1.0                 # 0.6 to 1.6 times the base size
  uppercase: false
  keywords: [token bucket, "429"]   # whole words or phrases, any case, drawn in the second accent colour
  numbers: true             # words with digits count as keywords (default on)
  srt: true                 # also write <out>/<format>/subtitles.srt
scenes:
  - type: bullets
    narration: Each request spends a token.
    keywords: [token]       # keywords for this scene only
  - type: title
    narration: That is the whole idea.
    subtitles: false        # this scene has no subtitles
```

- **Where the timing comes from.** The text is what the voice says (`narration:`, or the screen text with
  `read_screen`). Without a word-timing file, yamlreel works the timing out from the audio: it finds the loud parts and
  the silent gaps, pairs each sentence or clause end with the gap nearest where the text puts it (keeping both in
  order, so one missing pause does not push the others out of place), and shares the time inside each stretch by how
  long each word is to say. On a fast, tightly paced 40 s recording the average error was about 0.2 s per word; it is
  an estimate, and a recording with long pauses is closer. Showing a few words at a time hides most of it. It is reported
  as a warning, and another warning says when fewer than half of the sentence ends could be placed in a pause.
  `keywords` entries are text: write a decimal as `"3.50"` in quotes.
- **Exact timing.** Give a scene `narration_words: clip.words.json`, a JSON list of
  `{"word": "Each", "start": 0.12, "end": 0.35}` entries (seconds from the start of the clip; at most 2000 entries and
  256 KB; times must not go backwards and must fit inside the clip). Any aligner can produce it outside yamlreel. The file
  must be inside the spec folder or an allowed folder, like `narration_audio`. Its words are the ones shown. It only
  applies to a scene that is narrated and has subtitles on; otherwise a warning says it is ignored.
- **Your own recordings.** With `narration_audio: take.wav`, put the words spoken in `narration:` as the transcript. A
  recording with no transcript and no `narration_words` gets no subtitles, and a warning says so.
- **Captions and subtitles.** A scene's `caption` appears after its speech has ended (0.2 s later), so it never covers the
  subtitles, and the minimum length of a subtitled scene with a caption is raised by 1.4 s so the caption can be read. The caption also
  waits for the last subtitle to leave. Scenes without narration,
  or with `subtitles: false`, behave as before.
- **SRT.** `srt: true` writes the same chunks and times as a `.srt` file, for uploading as captions. A file from an earlier
  run is deleted when a render writes none.
- `--no-voice` drops the narration, so there is nothing to caption; render with the voice (or `narration_audio`) to see
  subtitles, and use `--preview --contact-frames 6` to check them.

## Story outlines

`yamlreel story outline.yaml` writes a plain spec with the arc that holds attention: hook, problem, one scene per
step, when to use, and a question end card. It works offline and the same outline always gives the same spec; the
spec has no special `story:` key, so edit any scene afterwards.

```yaml
topic: Load balancing                      # required: the video title
hook: One busy server should not slow down every user.   # required: typed on screen and spoken
problem: Requests pile up on one machine.  # optional
steps:                                     # 1 to 6, required; each name becomes a badge label
  - {name: Round robin, say: "Hand each request to the next server.", show: array}
  - {name: Least connections, say: "Pick the server with the fewest open connections.", show: table}
when_to_use: ["Similar requests: round robin", "Uneven requests: least connections"]   # optional, up to 6
question: Which strategy does your service use?   # optional: the end card; without it the video ends on a title card
answers: [Round robin, Least connections]         # optional, up to 4
```

`show` is the scene type for a step (`bullets` by default; `title`, `type`, `code`, `stat`, `diagram`, `table` or
`array`). The scene gets placeholder content marked `TODO` (for example a 3 by 3 table), which the header comment of the
file lists; replace them, then render with `--preview --no-voice`. Unknown keys, no steps and more than six steps are
errors. Each scene is at least `words / 2.5 + 1` seconds (narration stretches it if needed). The command refuses to
overwrite a file unless you pass `--force`; `--theme` and `--format` set them in the spec.

## Examples

| Spec | What it is |
| --- | --- |
| [`examples/cache-stampede-45s.yaml`](https://github.com/nsijwali/yamlreel/blob/main/examples/cache-stampede-45s.yaml) | A 45-second system-design explainer with a diagram, code and a narrated script. |
| [`examples/motion-demo.yaml`](https://github.com/nsijwali/yamlreel/blob/main/examples/motion-demo.yaml) | Scene transitions, staggered bullets and counting-up numbers. |
| [`examples/index-scan.yaml`](https://github.com/nsijwali/yamlreel/blob/main/examples/index-scan.yaml) | A table that is scanned row by row, a sorted index taking inserts, and a queue. |
| [`examples/load-balancing-story.yaml`](https://github.com/nsijwali/yamlreel/blob/main/examples/load-balancing-story.yaml) | The output of `yamlreel story` for [`examples/outlines/load-balancing.yaml`](https://github.com/nsijwali/yamlreel/blob/main/examples/outlines/load-balancing.yaml): hook, problem, two steps with badges, when to use, and a question end card. |
| [`examples/subtitles-demo.yaml`](https://github.com/nsijwali/yamlreel/blob/main/examples/subtitles-demo.yaml) | Narrated scenes with karaoke subtitles, highlighted keywords and numbers, and an SRT file. |

It is narrated; add `--no-voice` to render it with music only. For more starting points, run
`yamlreel init` with the `explainer`, `system-design`, `project` or `code-tip` template.

## Commands

| Command | What it does |
| --- | --- |
| `yamlreel --version` | Prints the installed version and exits. |
| `yamlreel init <template> <file>` | Writes a starter spec: `explainer`, `system-design`, `project` or `code-tip`. |
| `yamlreel story <outline> [--out FILE] [--theme T] [--format F] [--force]` | Turns an outline into a starter spec; see [Story outlines](#story-outlines). |
| `yamlreel validate <spec>` | Checks a spec and prints its scene count, length and how many scenes are narrated and subtitled, without rendering. |
| `yamlreel render <spec>` | Renders. Useful flags: `--preview`, `--out DIR`, `--format vertical\|landscape\|square\|all`, `--music koto\|none\|file.mp3`, `--fps N`, `--contact-frames N`, `--no-voice`, `--voice-url URL`, `--read-screen`, `--allow-path DIR`, `--allow-private-hosts`. |
| `yamlreel config init [--user]` / `config show` | Writes a starter config, or shows the merged settings and the files they came from. |
| `yamlreel music --seconds 30 --out track.wav` | Generates the music track on its own (`--bpm`, `--root`, `--scale`, `--seed`). |

## Writing a spec

A spec is a list of scenes. Each scene has a `type`, how many `seconds` it lasts, and an optional `caption`
(a lower third that fades in).

```yaml
meta: {title: "Cache stampede in 45 seconds", tags: [systemdesign, caching]}
theme: midnight                     # sumi | washi | midnight | your own
formats: [vertical, landscape]
scenes:
  - type: type
    text: What is a cache stampede?
    seconds: 3
  - type: bullets
    title: The fix
    items: [Let one request rebuild the key, Serve stale while it refreshes, Add jitter to every TTL]
  - type: stat
    value: "10,000 → 1"
    label: database queries per expiry
```

| Scene | Shows | Main fields |
| --- | --- | --- |
| `title` | eyebrow, title, subtitle and URL, easing in line by line | `title`, `eyebrow`, `subtitle`, `url` |
| `type` | one line typed out with a cursor: your hook | `text` |
| `bullets` | points that appear one at a time | `title`, `items`, `stagger` |
| `code` | code typed on screen, with highlighted lines | `code`, `highlight`, `title` |
| `stat` | one big number or phrase, optionally counting up | `value`, `label`, `eyebrow`, `count` |
| `table` | rows that appear one by one, a scan bar walking down them, highlighted rows and a ticking counter | `title`, `columns`, `rows`, `stagger`, `reveal`, `scan`, `highlight`, `counter` |
| `array` | one row of cells that take sorted inserts, pushes and pops, marks and a sliding window | `title`, `items`, `steps`, `stagger` |
| `cta` | an end card: a question bubble, lettered answer chips (A to D) and a pulsing prompt | `question`, `answers`, `prompt` |
| `diagram` | boxes and arrows that appear in order, packets flowing, nodes flashing | `nodes`, `edges`, `flash`, `title` |
| `shot` | a screenshot of a URL or an image, with a slow camera move | `source`, `zoom`, `focus`, `fit`, `click`, `scroll_y` |

<details>
<summary><b>All spec options</b></summary>

<br>

```yaml
meta: {title, description, tags: [], links: [], credits: []}
theme: sumi | washi | midnight | <a theme from your config>
formats: [vertical, landscape, square]
music: {style: koto | none | path/to.mp3, bpm: 72, root: D, scale: hirajoshi, seed: 7, snap: false, volume: 0.9}
transition: fade              # fade | slide | zoom | cut, or {type: slide, seconds: 0.35}; default fade, 0.25 s
watermark: "Your Name"        # or {text|image: logo.png, position: top-left|top-right|bottom-left|bottom-right, opacity: 0.75, size: 30}
poster: {scene: 0, at: 2.6}   # optional; default is the first scene, just before it ends
voiceover: {voice, language, gap, music_level, read_screen}
subtitles: {style: karaoke | pop | plain, words: 4, position: lower | middle, size: 1.0, uppercase: false, keywords: [], numbers: true, srt: false}   # or `true`; default off
scenes: [...]                 # every scene also takes: step (badge label), transition, subtitles (false opts out), keywords (extra subtitle keywords), narration_words (JSON of exact word times)
```

- **Diagrams:** `nodes` take `id, label, kind (client|service|cache|db), x, y, at`, where `x` and `y` are fractions of the frame. `vx, vy` override them in vertical frames, so one diagram fits both shapes. `edges` take `from, to, label, bad, dashed, at, color` and `flash` takes `node, at, text, color`.
- **Shots:** `source` (URL or image), `fit: cover | card`, `zoom: [from, to]`, `focus: [x, y]`, `focus_to`, `click: [selectors]`, `scroll_y`, `wait_ms`, `viewport`, `scale`. Screenshots are cached in `<output folder>/.cache`; delete it to capture again.
- **Transitions:** `transition` at the top sets how every scene enters; a scene's own `transition` overrides it, and a field it leaves out (`type` or `seconds`) comes from the top-level one. A transition never lasts longer than its scene. `fade` cross-fades, `slide` pushes the old scene out to the left, `zoom` settles the new scene in from slightly larger, `cut` switches at once. `seconds` is 0.1 to 1.0 (default 0.25); a scene's length never changes.
- **Stagger:** `stagger: 0.7` on a `bullets` scene sets the seconds between bullets (0.1 to 3). Without it the spacing is worked out from the scene length.
- **Count-up:** `count: true` on a `stat` scene counts the one number in `value` up from 0 over 60% of the scene; `count: {from: 0, seconds: 2.5}` sets both. Prefix, suffix, thousands separators and decimals are kept, and the scene ends on exactly the `value` text. `value` must hold exactly one number.
- **Table:** `columns` (1 to 6) and `rows` (1 to 12, each with one cell per column, cells up to 40 characters; quote a number to keep its formatting). Rows appear `stagger` seconds apart (default 0.4; `reveal: all` shows them at once). `scan: {at: 1.5, per_row: 0.3, stop: 4, color: accent}` walks a bar down from the first row and stays on row `stop` (rows count from 0; `scan: true` walks to the last row). `highlight: [{row: 4, at: 4.0, text: found, color: accent2}]` tints a row and tags it. `counter: {label: rows scanned, to: 10000000, at: 1.5, seconds: 3}` ticks a number up beside the table (below it in vertical). Text shrinks to fit, with a warning, and a warning says when a cue comes before its row appears.
- **Array:** `items` (0 to 12 cells) and up to 40 `steps`, each with `at` and exactly one action: `insert: x` (a sorted insert: before the first cell that sorts after it, numbers by value and before text), `push: x` (at the end), `pop: front|back`, `mark: [1, 2]` (cell indices, with optional `text` and `color`), `window: [0, 2]` (a bracket over cells; a later `window` slides it, and it is trimmed if cells leave) and `unmark: true` (clears marks and the window). A step without `at` comes `stagger` (default 0.8) seconds after the one before; steps run in time order. The steps are replayed when the spec loads, so `scenes[2].steps[3].pop: array is empty at 2.4s` is an error before rendering.
- **Step badge:** `step: Token bucket` on a scene draws `STEP n/N · TOKEN BUCKET` and an N-segment progress bar at the top centre. N is the number of distinct `step` labels in the spec, in order of first appearance; n is this scene's label. Consecutive scenes may share a label. A warning appears when the watermark would overlap the badge.
- **CTA end card:** `type: cta` with `question` (required), `answers` (0 to 4, shown as A to D) and `prompt` (default "Answer in the comments"). The bubble pops in, the answers follow one by one and the prompt pulses once a second. The first `cta` question is also added to `share-copy.txt` (before the links and hashtags, unless it is already in the description), and `--read-screen` reads the question, answers and prompt.
- **Subtitles:** see [Word-by-word subtitles](#word-by-word-subtitles).
- **Contact sheet:** `render --contact-frames N` (1 to 8) writes `contact.png` with N stills per scene, one row per scene, to check that reveals progress and nothing overlaps. Default: two per scene.
- **Unknown keys are errors:** a misspelled or extra key stops the render, for example `error: scenes[0].titel: unknown field for a title scene; allowed: caption, eyebrow, narration, ...`, instead of being silently ignored.
- **Limits:** `limits.max_seconds` and `limits.max_scenes` are capped at 3600 and 1000, whatever a spec or config sets.
- **Files:** images, pages, music and logos must sit in the spec's folder, a config folder, or a folder you allow with `--allow-path DIR` (or `allow_paths` in your user config).
- **Music:** `seed` changes the melody and `snap: true` rounds scene lengths to beats. To use your own track, point `style` at an audio file and credit it in `meta.credits`.
- **Watermark:** appears on every frame and the poster. The default corner is bottom-right for landscape and top-left for vertical, because the Shorts buttons cover the right edge and the bottom.

</details>

**Tips**
- Text never silently overflows. It wraps, then shrinks, and `render` warns you when it had to.
- In vertical frames, content sits above the lower third, clear of the Shorts UI.
- A 15-second video carries one idea. Before you publish, check anything you simplified.

## Voiceover in your own voice

Give a scene a `narration:` line and yamlreel speaks it, lowers the music under the voice, and makes the scene as
long as the speech needs. Speech comes from a text-to-speech server **you choose**: one on your own machine, for example
[VoiceStudio](https://github.com/debpalash/VoiceStudio) (which can also clone your voice from a short recording), or a remote one.
Your narration text goes to that server, so it stays on your machine only if the server is local.

```yaml
scenes:
  - type: type
    text: What is a cache stampede?
    narration: Ever seen one expired cache key take down a whole database? That's a cache stampede.
  - type: title
    title: Cache stampede
    narration: false          # silent end card
```

> **Write narration, don't read the screen.** The voice sounds natural when it *explains* what the viewer sees,
> in a sentence or two. `read_screen: true` (or `--read-screen`) speaks every title, bullet and caption word
> for word, for scenes without their own `narration:`. That is fine as a quick draft, but it sounds robotic in a
> finished video. `--no-voice` renders any spec with music only.

<details>
<summary><b>Set up VoiceStudio, step by step</b></summary>

<br>

1. **Install and open VoiceStudio.** Use the [desktop app](https://github.com/debpalash/VoiceStudio/releases/latest)
   (easiest), or run it from source (`uv sync`, then `uv run uvicorn main:app --app-dir backend --port 3900`).
   Its speech server listens on `http://127.0.0.1:3900/mcp/` while it is running.
2. **Accept the model licences.** Open *Model Catalogue* and accept the terms for **OmniVoice** and its audio
   tokenizer. Until you do, every request fails with `403 Forbidden`. The first request also downloads the
   model (about 2.3 GB).
3. **(Optional) Clone your voice.** In *Voice cloning*, add a 5 to 10 second recording of yourself in a quiet
   room, with its exact transcript. Copy the voice's **profile id**.
4. **Tell yamlreel where the server is, once.** Run `yamlreel config init --user` and open
   `~/.config/yamlreel/config.yaml`. At the bottom, uncomment `voice_server:` and its `url:` line. Under the
   existing `voiceover:` block, uncomment `voice:` and paste your profile id there (leave it out to use the
   server's default voice). You can also pass `--voice-url http://127.0.0.1:3900/mcp/` on each render.

Prefer to record yourself? Use `narration_audio: clip.wav` (PCM WAV, inside the spec folder or an allowed
folder) instead of `narration:`.

</details>

<details>
<summary><b>How voiceover works: other servers, privacy, caching</b></summary>

<br>

- **Any compatible server works.** yamlreel speaks MCP "Streamable HTTP" and calls the server's `generate_speech` tool, which must return the audio inline (VoiceStudio's default). VoiceStudio is just one server that does this.
- **Privacy:** narration text goes to the server you configured. A local server (such as VoiceStudio on `127.0.0.1`) keeps it on your machine; a remote URL sends it to that host. A spec cannot choose the server: the address comes only from your user config or the command line.
- **Precedence:** a scene's own `narration:` wins over `read_screen`, and `narration: false` keeps a scene silent.
- **Caching:** audio is cached in `<output folder>/.cache/narration-v2-*.wav`, keyed by the server address (scheme, host, port and path; never the `user:password@` part, query or fragment), the voice (or `client_id` when no voice is set), the language and the text, so re-renders do not speak lines again. The cache is never pruned. Clear it with `rm "<output folder>"/.cache/narration-*.wav`. Upgrading to 0.2.2 re-voices every line once. Changing the engine, model or account behind the **same** server URL, or the recording behind a voice, is not detected: run that `rm` afterwards.
- **Licences:** VoiceStudio is AGPL-3.0, and its models have their own licences, which its Model Catalogue shows per model. OmniVoice's weights are non-commercial (CC-BY-NC), and that may carry over to the speech they generate. Check before you monetise a video.

</details>

## Configuration

Defaults live in a config file, so a spec only needs to say what is different for that video. You can adapt
yamlreel to your own name, colours and music without touching code.

```bash
yamlreel config init            # ./yamlreel.config.yaml: this project (add it to your .gitignore)
yamlreel config init --user     # ~/.config/yamlreel/config.yaml: every project
yamlreel config show            # the merged settings, and the files they came from
```

Settings apply in this order, lowest to highest: **built-in defaults → user config → project config → the spec → command-line flags**.
Nested settings merge, so setting only `music.bpm` keeps the rest. `meta.tags`, `links` and `credits` add up across layers.

What you can set: `theme`, your own `themes` (colours, `extends`, `display: serif | sans`, `frame`), `formats`,
`fps`, `output_dir`, `watermark` (text or a logo), `music`, `voiceover`, `meta` defaults and `limits`. Your name,
watermark and default tags belong in config, not in code. Every option is explained in
[`yamlreel/templates/config.example.yaml`](https://github.com/nsijwali/yamlreel/blob/main/yamlreel/templates/config.example.yaml).

A few settings widen what a spec can touch, so yamlreel only reads them from your **user** config or the command
line, never from a spec or project config you downloaded: `allow_paths`, `allow_private_hosts`,
`chromium_path`, `font_dir` and `voice_server`. See [SECURITY.md](https://github.com/nsijwali/yamlreel/blob/main/SECURITY.md).

## Troubleshooting

| You see | Fix |
| --- | --- |
| `ffmpeg not found` | `pip install "yamlreel[ffmpeg]"`, or install ffmpeg with your package manager. |
| `font 'DejaVuSans-Bold.ttf' not found` | Install DejaVu (see *Install details*), or point `font_dir` in your user config at the folder that holds them. |
| `scene 0 has narration but no voice server is configured` | Start VoiceStudio and set `voice_server` in your user config, pass `--voice-url`, or render with `--no-voice`. |
| `cannot reach the voice server … Is VoiceStudio running?` | Start VoiceStudio and check that the URL ends in `/mcp/`. |
| `Client error '403 Forbidden' for url '…/generate'` | Accept the OmniVoice and tokenizer licences in VoiceStudio's Model Catalogue. |
| `Server error '500'` from the voice server, or the server is killed | The machine is out of memory. Close other apps, and use a 5 to 10 second voice sample: a longer reference needs over 2 GB on the CPU. |
| `voice server reply had no audio` | VoiceStudio is set to `OMNIVOICE_MCP_OUTPUT_MODE=files`. yamlreel needs the default inline mode. |
| `capturing a URL needs playwright` | `pip install "yamlreel[capture]"`, then `playwright install chromium`. |
| `could not capture … Executable doesn't exist` | Run `playwright install chromium`, or set `chromium_path` in your user config. |
| `… outside the folders this run may read` | Move the file next to your spec, or pass `--allow-path DIR`. For localhost pages, add `--allow-private-hosts`. |
| A narration line still uses an old voice | The cache cannot see a change behind the same server URL (new engine, model, account or voice recording). Run `rm "<output folder>"/.cache/narration-*.wav`. |
| Homebrew fails to build ffmpeg or Python | Older macOS versions get no prebuilt packages. Use the `[ffmpeg]` extra for ffmpeg, and [python.org](https://www.python.org/downloads/) or `uv` for Python. |

## Built with

| | Used for |
| --- | --- |
| [Pillow](https://python-pillow.org), [NumPy](https://numpy.org), [PyYAML](https://pyyaml.org) | drawing every frame, generating the music, reading specs |
| [FFmpeg](https://ffmpeg.org) (or [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)) | encoding the video |
| [Playwright](https://playwright.dev/python/) | capturing web pages for `shot` scenes (optional) |
| [DejaVu fonts](https://dejavu-fonts.github.io) | all on-screen text |
| [VoiceStudio](https://github.com/debpalash/VoiceStudio) with [OmniVoice](https://huggingface.co/k2-fsa/OmniVoice) | the optional local voiceover, including voice cloning (not bundled) |
| [Claude Code](https://claude.com/claude-code) | building, testing and reviewing yamlreel |

If you publish a video voiced with OmniVoice, credit it and keep it non-commercial: its licence (CC-BY-NC)
requires both, and VoiceStudio's audio tokenizer has its own terms (see *Licences* above). yamlreel puts a
spec's `meta.credits` into `share-copy.txt` for you.

## Why yamlreel

- **Manim** is excellent for math animation, but each animation is code you write. yamlreel is a spec: you describe scenes, not frames.
- **Remotion** renders video from React and is very flexible. yamlreel trades that flexibility for zero JavaScript and a fixed set of scene types.
- **MoviePy** edits and composes video clips you already have. yamlreel generates frames and music itself and adds an optional voiceover.
- **Online AI video tools** are quick but run in the cloud, usually cost money, and your script and voice leave your machine. yamlreel renders locally and is MIT licensed; your narration stays local only if your voice server is.

## Contributing

Bug reports, new scene types, examples and docs are welcome. See [CONTRIBUTING.md](https://github.com/nsijwali/yamlreel/blob/main/CONTRIBUTING.md)
for setup and the pull request flow.

## Development

```bash
git clone https://github.com/nsijwali/yamlreel.git
cd yamlreel
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,ffmpeg]'
pytest -q
```

Tests never read your own user or project config. Checks that need `ffprobe`, Playwright or Chromium are skipped
when those tools are missing. CI runs on Ubuntu with Python 3.10 and 3.12.

## Security and licence

Specs are treated as untrusted input. File access is confined, URL captures are checked, and settings that widen
access come only from you. [SECURITY.md](https://github.com/nsijwali/yamlreel/blob/main/SECURITY.md) covers the details and how to report a vulnerability.

yamlreel is [MIT licensed](https://github.com/nsijwali/yamlreel/blob/main/LICENSE). Tools you pair it with keep their own licences.

## Author

yamlreel is created and maintained by **Nitin Sijwali** ([@nsijwali](https://github.com/nsijwali)), a software
engineer in Bangalore, India. It started as a way to post a short system-design explainer every day without
editing videos by hand. Project website: [nsijwali.github.io/yamlreel](https://nsijwali.github.io/yamlreel/).
If you use yamlreel in your work, GitHub's **Cite this repository** button gives a ready-made citation.
