# number-chain

**Use when** a back-of-envelope result is derived on screen (100M a month, divided by 2.6M seconds, is about 40 a
second) or two quantities are compared to scale (1 write against 100 reads). Input chips pop in order, arrows draw
between them, the result lands last with a pulse. State rounded numbers as "about" and check the arithmetic first.

```yaml
- type: motion
  seconds: 7.0
  narration: "A hundred million links a month, over two point six million seconds, is about forty writes a second."
  elements:
    - {kind: text, text: "BACK-OF-THE-ENVELOPE MATH", size: 40, color: accent, x: 0.5, y: 0.1, at: 0, enter: none}
    - {id: a, kind: group, x: 0.28, y: 0.22, at: 1.0, enter: pop, children: [{kind: rect, w: 400, h: 110, radius: 26, fill: card, stroke: line, stroke_width: 3}, {kind: text, text: "100M / month", size: 38, font: mono}]}
    - {id: b, kind: group, x: 0.72, y: 0.22, at: 2.2, enter: pop, children: [{kind: rect, w: 360, h: 110, radius: 26, fill: card, stroke: line, stroke_width: 3}, {kind: text, text: "÷ 2.6M sec", size: 38, font: mono, color: muted}]}
    - {kind: arrow, from: a, to: b, color: muted, stroke_width: 4, at: 2.4, enter: draw}
    - {kind: image, source: assets/studio/glow-cyan.png, w: 800, h: 300, x: 0.5, y: 0.4, at: 4.0, enter: fade, opacity: 0.6, layer: -1}
    - {kind: text, text: "≈ 40 / sec", size: 120, color: accent, x: 0.5, y: 0.4, at: 4.0, enter: {type: pop, seconds: 0.35}, emphasis: {type: pulse, at: 4.8, repeat: 1}}
    - {kind: path, points: [[0.09, 0.55], [0.0983, 0.55]], color: accent, stroke_width: 40, at: 5.0, enter: {type: draw, seconds: 0.3}}
    - {kind: path, points: [[0.09, 0.62], [0.9233, 0.62]], color: accent2, stroke_width: 40, at: 5.4, enter: {type: draw, seconds: 1.1, easing: linear}}
  sfx:
    - {at: 1.0, sound: pop}
    - {at: 2.2, sound: pop, pitch: 2}
    - {at: 3.9, sound: hit, gain: 0.8}
```

**Safe zone.** Landscape (1920 wide): chips at x 0.17 / 0.50 / 0.83, y 0.25, arrows between; bars at y 0.46 and 0.55;
result y 0.69 (size 100), bottom under 0.76. Vertical: two chips per row, result below. Stroke width tops out at 40.

**Timing.**
- Each chip `at` = its spoken word + 0.3 s, pop sound pitch rising; arrow `at` = the next chip's word + 0.1 s.
- Result `at` = the word "about" or the number + 0.3 s, `hush` 0.8 s before and `hit` 0.1 s before it.
- To-scale bars: 16 px and 1600 px of a 1920 frame is exactly 1 : 100; draw the long one in 1.1 s, `linear`.
- Hold the result >= 2 s; there is no tail beyond that.

**Mistakes to avoid**
- Unrounded results: say "about forty", show "≈ 40", and keep the spoken and shown numbers the same.
- A stroke_width above 40: validation error; use a rect for a fatter bar.
- Chips and result with different units on screen and in speech (per day vs per second).
- Hook claims that contradict the chain (100M a day in the hook, 100M a month in the maths): derive, then write the hook.
