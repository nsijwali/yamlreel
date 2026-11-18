# counter-tick

**Use when** a number changes as events land (97, 98, 99, 100) or a total jumps (CHARGES: 1 to 2). The engine has no
number counter, so stack one text per value at the same x/y and swap them with `end` + `exit`.

```yaml
- type: motion
  seconds: 4.5
  narration: "Each request counts. Ninety-eight, ninety-nine, one hundred."
  elements:
    - {id: api, kind: group, x: 0.5, y: 0.56, at: 0, enter: none, children: [{kind: rect, w: 600, h: 170, radius: 30, fill: card, stroke: line, stroke_width: 3}, {kind: text, text: "API", size: 58, x: -0.14}]}
    - {kind: text, text: "97", size: 52, font: mono, color: muted, x: 0.6, y: 0.56, at: 0, enter: none, end: 1.5, exit: {type: fade-out, seconds: 0.05}}
    - {kind: text, text: "98", size: 52, font: mono, color: muted, x: 0.6, y: 0.56, at: 1.5, enter: none, end: 2.2, exit: {type: fade-out, seconds: 0.05}}
    - {kind: text, text: "99", size: 52, font: mono, color: fg, x: 0.6, y: 0.56, at: 2.2, enter: none, end: 2.9, exit: {type: fade-out, seconds: 0.05}}
    - {kind: text, text: "100", size: 52, font: mono, color: accent, x: 0.6, y: 0.56, at: 2.9, enter: pop, emphasis: {type: pulse, at: 3.4, repeat: 1}}
  sfx:
    - {at: 1.5, sound: tick}
    - {at: 2.2, sound: tick, pitch: 2}
    - {at: 2.9, sound: tick, pitch: 5}
```

**Safe zone.** Counter beside its box, vertical y <= 0.69; big final numbers (size 100 to 250) at y 0.2 to 0.25 so
they clear the captions. Same x/y for every step so nothing jumps.

**Timing.**
- Step `at` = the spoken number's word time + 0.3 s; sfx on the same `at`, pitch rises 0, 2, 5 (a rising tick).
- Value N ends exactly where N+1 starts (`end` = next `at`); `exit` 0.05 s so there is no flicker.
- The last value uses `enter: pop`, and `emphasis: pulse` at `at + 0.35` or later (pop takes 0.45 s by default).
- Hold the final value >= `letters/15 + 1.5` s, a minimum of 2 s.

**Mistakes to avoid**
- Pulse before the pop finished: validation error. Use `at + enter.seconds` or later.
- Different x/y between steps: the number jitters.
- Fading values with a slow exit: two numbers overlap on screen. Keep exit 0.05 s.
- Counting every integer 1..100: leaf limit is 60; show 3 to 4 steps near the end.
- No tick sound per step.
