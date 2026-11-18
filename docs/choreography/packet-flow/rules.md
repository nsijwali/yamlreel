# packet-flow

**Use when** a request moves between two boxes (client to API) and either passes a gate or bounces off it.
One packet per narrated beat; a stream is several packets 0.15 s apart (count them: 60-leaf limit).

```yaml
- type: motion
  seconds: 5.0
  narration: "A client sends a request. The limiter lets it through, then bounces the next one."
  elements:
    - {id: client, kind: group, x: 0.5, y: 0.21, at: 0, enter: none, children: [{kind: rect, w: 600, h: 150, radius: 30, fill: card, stroke: line, stroke_width: 3}, {kind: text, text: "CLIENT", size: 46}]}
    - {kind: path, points: [[0.5, 0.262], [0.5, 0.494]], color: line, stroke_width: 4, at: 0, enter: none}
    - {kind: rect, w: 380, h: 22, radius: 11, fill: accent, stroke: none, x: 0.5, y: 0.385, at: 0.3, enter: slide-left}
    - {id: api, kind: group, x: 0.5, y: 0.56, at: 0, enter: none, children: [{kind: rect, w: 600, h: 170, radius: 30, fill: card, stroke: line, stroke_width: 3}, {kind: text, text: "API", size: 58}]}
    - {kind: circle, r: 13, fill: accent, stroke: none, x: 0.5, y: 0.275, at: 1.0, enter: none, end: 1.5, exit: {type: fade-out, seconds: 0.06}, moves: [{at: 1.0, seconds: 0.5, y: 0.49, easing: linear}]}
    - {kind: circle, r: 13, fill: accent2, stroke: none, x: 0.5, y: 0.275, at: 2.5, enter: none, end: 3.3, exit: {type: fade-out, seconds: 0.1}, moves: [{at: 2.5, seconds: 0.22, y: 0.372, easing: linear}, {at: 2.72, seconds: 0.4, x: 0.28, y: 0.30, opacity: 0, easing: out}]}
  sfx:
    - {at: 1.0, sound: tick}
    - {at: 2.72, sound: deny, gain: 0.7}
```

**Safe zone.** Vertical: client y 0.21, lane 0.26 to 0.49, API y 0.56 (box bottom ~0.605), all <= 0.69. Landscape:
row y 0.40, lane y 0.62, boxes at x 0.14 / 0.50 / 0.86, nothing below 0.76.

**Timing.**
- Packet `at` = the word it illustrates + 0.3 s; travel 0.4 to 0.6 s, `easing: linear`.
- Tick sfx at the packet's `at`; `deny` where a bounced packet turns (the second move starts).
- A bounce is two moves back to back: second `at` = first `at + seconds`, fade via `opacity: 0`.
- The scene holds >= 0.5 s after the last packet.

**Mistakes to avoid**
- Packet ending on the box label: stop at y 0.49, above the box top (~0.515), and `fade-out`.
- `enter` on a mover with `moves`: use `enter: none` so the move can start at `at`.
- Overlapping `moves`: next `at` >= previous `at + seconds`.
- Streams of 20 copies: they count toward 60 leaves; use at most one packet per beat unless the beat is a flood.
- No sound on the packet (rule 3 in ../README.md).
