# zoom-focus

**Use when** one part of a finished diagram must dominate for a beat (a counter, a gate, a payload), then the frame
pulls back. A camera move is the only move in its beat; do not start packets inside it.

```yaml
- type: motion
  seconds: 6.0
  narration: "Watch the counter. At one hundred, the gate closes."
  elements:
    - {id: gate, kind: rect, w: 380, h: 22, radius: 11, fill: accent, stroke: none, x: 0.5, y: 0.385, at: 0, enter: none}
    - {id: api, kind: group, x: 0.5, y: 0.56, at: 0, enter: none, children: [{kind: rect, w: 600, h: 170, radius: 30, fill: card, stroke: line, stroke_width: 3}, {kind: text, text: "API", size: 58}]}
    - {kind: text, text: "100", size: 52, font: mono, color: accent, x: 0.6, y: 0.56, at: 1.5, enter: pop}
  camera:
    - {at: 1.2, seconds: 0.35, zoom: 1.1, focus: [0.5, 0.45], easing: out}
    - {at: 4.4, seconds: 1.0, zoom: 1.0, focus: [0.5, 0.5], easing: in_out}
  sfx:
    - {at: 1.2, sound: whoosh, seconds: 0.45}
```

**Safe zone.** The camera maps a point to `screen = (y - fy) * zoom + 0.5`. For vertical keep every element that must
stay visible at `y <= fy + 0.19 / zoom` (bottom edge, not centre) and `y >= fy - 0.5 / zoom`. Example: focus 0.42,
zoom 1.35 allows bottoms up to 0.56. Reference zooms: 1.05 to 1.1 drift, 1.35 for one close-up. Landscape (content limit 0.76):
bottoms `<= fy + 0.26 / zoom`.

**Timing.**
- Push 0.35 to 0.7 s (`easing: out` for a punch, `in_out` for a glide); hold >= 1 s; pull back 0.4 to 1.0 s.
- Push `at` = the word it emphasises (impact 0.1 s earlier); whoosh on the push.
- Keys must not overlap: next `at` >= previous `at + seconds`. At most 16 keys.
- A shake is 3 tiny keys 0.04 to 0.08 s long, focus moved by about 0.007 (see error-flash).

**Mistakes to avoid**
- Zoom cutting off the diagram edge: re-check with the formula, or lower the zoom.
- Zoom pushing content under the captions: bottoms above the limit after zoom, not before.
- Camera push and a packet move in the same beat: one move per beat.
- Forgetting the pull-back: the next beat starts at zoom 1.0 unless you key it.
- Overlapping camera keys: validation error.
