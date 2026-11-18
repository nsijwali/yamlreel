# error-flash

**Use when** this is the payoff: a refusal or failure the viewer should feel (429, DOWN, DUPLICATE). Build it as
near-silence, a riser, then one hit that lands with a red glow, a big code and a tiny camera shake.

```yaml
- type: motion
  seconds: 5.0
  narration: "Go over, and you get four hundred twenty-nine. Too Many Requests."
  elements:
    - {kind: rect, w: 380, h: 22, radius: 11, fill: accent, stroke: none, x: 0.5, y: 0.385, at: 0, enter: none}
    - {kind: rect, w: 380, h: 22, radius: 11, fill: accent2, stroke: none, x: 0.5, y: 0.385, at: 1.4, enter: {type: fade, seconds: 0.05}, end: 2.4}
    - {id: api, kind: group, x: 0.5, y: 0.56, at: 0, enter: none, children: [{kind: rect, w: 600, h: 170, radius: 30, fill: card, stroke: line, stroke_width: 3}, {kind: text, text: "API", size: 58, x: -0.14}]}
    - {kind: circle, r: 46, fill: bg, stroke: accent2, stroke_width: 6, x: 0.8, y: 0.56, at: 1.5, enter: pop}
    - {kind: text, text: "429", size: 250, color: accent2, x: 0.5, y: 0.205, at: 1.4, enter: {type: pop, seconds: 0.35}, emphasis: {type: pulse, at: 2.2, repeat: 1}}
    - {kind: text, text: "Too Many Requests", size: 52, color: fg, x: 0.5, y: 0.315, at: 3.1, enter: slide-up}
  camera:
    - {at: 1.4, seconds: 0.35, zoom: 1.1, focus: [0.5, 0.45], easing: out}
    - {at: 1.75, seconds: 0.04, zoom: 1.1, focus: [0.507, 0.447]}
    - {at: 1.79, seconds: 0.05, zoom: 1.1, focus: [0.494, 0.453]}
    - {at: 1.84, seconds: 0.08, zoom: 1.1, focus: [0.5, 0.45]}
    - {at: 3.6, seconds: 1.0, zoom: 1.0, focus: [0.5, 0.5], easing: in_out}
  sfx:
    - {at: 0.0, sound: hush, seconds: 1.3}
    - {at: 0.25, sound: riser, seconds: 1.15}
    - {at: 1.4, sound: hit}
    - {at: 3.1, sound: click}
```

**Safe zone.** Code at y 0.2 to 0.25 (size up to 250), sub-label y 0.315, details <= 0.46; all far above 0.69 even at
zoom 1.1 with focus 0.45 (formula in ../zoom-focus/rules.md). Landscape: same rows, nothing below 0.76.

**Timing.**
- hush from the scene start, riser ends where the hit lands; hit `at` = the code's `at` (impact 0.1 s before the
  spoken word when the word needs to stay clear).
- Red shape (`accent2`) swaps in at the hit: fade 0.05 s, `end` after about 1 s back to `accent`.
- Pulse at `at + enter.seconds` or later (pop 0.35 s here, pulse 0.8 s later).
- Shake: three keys totalling 0.17 s after the push; pull back after the sub-label has been read.
- Sub-label hold >= `letters/15 + 1.5` s.

**Mistakes to avoid**
- A ring drawn filled: use `fill: bg` with a stroke, not the default accent fill.
- Pulse before the pop finished: validation error.
- Shake keys overlapping the push key: next `at` >= previous `at + seconds`.
- Flash, glow and code all at different times: they land on the same `at`, with one sound.
- Sound without `mix:` in the spec: silent.
