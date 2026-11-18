# card-stack

**Use when** a list or option set is revealed one item per beat (three ways to count, the closing choice), or a
before/after pair (WITHOUT vs WITH). Items slide in from one side, evenly spaced, with a pop sound each.

```yaml
- type: motion
  seconds: 4.0
  narration: "Three common ways to count. Fixed. Sliding. Token bucket."
  elements:
    - &way {kind: group, x: 0.5, y: 0.24, at: 0.25, enter: slide-left, children: [{kind: rect, w: 780, h: 130, radius: 30, fill: card, stroke: line, stroke_width: 3}, {kind: circle, r: 34, fill: accent, stroke: none, x: -0.29}, {kind: text, text: "1", size: 40, color: bg, x: -0.29}, {kind: text, text: "FIXED WINDOW", size: 46, x: 0.05}]}
    - {<<: *way, y: 0.36, at: 0.9, children: [{kind: rect, w: 780, h: 130, radius: 30, fill: card, stroke: line, stroke_width: 3}, {kind: circle, r: 34, fill: accent, stroke: none, x: -0.29}, {kind: text, text: "2", size: 40, color: bg, x: -0.29}, {kind: text, text: "SLIDING WINDOW", size: 46, x: 0.05}]}
    - {<<: *way, y: 0.48, at: 1.55, children: [{kind: rect, w: 780, h: 130, radius: 30, fill: card, stroke: accent, stroke_width: 3}, {kind: circle, r: 34, fill: accent, stroke: none, x: -0.29}, {kind: text, text: "3", size: 40, color: bg, x: -0.29}, {kind: text, text: "TOKEN BUCKET", size: 46, x: 0.05, color: accent}]}
  sfx:
    - {at: 0.0, sound: whoosh, gain: 0.7}
    - {at: 0.25, sound: pop}
    - {at: 0.9, sound: pop, pitch: 2}
    - {at: 1.55, sound: pop, pitch: 4}
```

**Safe zone.** Vertical card rows y 0.24, 0.36, 0.48 (step 0.12, height 130 px = 0.068), last bottom ~0.52; up to
5 rows still ends <= 0.69 at step 0.11 from 0.2. Headline above at y <= 0.13. Landscape: rows <= 0.76.

**Timing.**
- Each card `at` = its spoken word + 0.3 s; pop sound (pitch up 0, 2, 4) on the same `at`; whoosh on the first.
- Group children default to `enter: none`, so the label arrives with the card: no separate text timing.
- Highlight the answer card with `stroke: accent` and `color: accent`; hold the full stack >= 2 s.
- For a question under the stack use `split: words` with `stagger: 0.12`, 0.8 s after the last card.

**Mistakes to avoid**
- Headline over chips: keep the headline y <= 0.13 and the first card y >= 0.24.
- Label inside a card drawn with its own `at`: it appears before the card finishes entering. Use group children.
- More than 6 cards per scene: split into two beats; leaves count (a card is 3 to 4).
- All cards at the same `at`: it reads as one block; space them by the narration.
