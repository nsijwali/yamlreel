# timeline-burst

**Use when** events sit on a time axis: a fixed window, a reset line, bursts either side, then the total
(fixed-window rate limiting: 100 at 0:59 and 100 at 1:01 = 200 in 2 seconds).

```yaml
- type: motion
  seconds: 7.0
  narration: "A hundred a minute. A burst at fifty-nine seconds. Another at one-oh-one. Two hundred get through."
  elements:
    - {kind: text, text: "1  FIXED WINDOW", size: 34, color: accent, x: 0.5, y: 0.105, at: 0.2, enter: fade}
    - {kind: path, points: [[0.07, 0.42], [0.93, 0.42]], color: muted, stroke_width: 4, at: 0.1, enter: {type: draw, seconds: 0.6}}
    - {kind: rect, w: 440, h: 160, radius: 22, fill: card, stroke: line, stroke_width: 3, x: 0.29, y: 0.42, at: 0.35, enter: pop}
    - {kind: rect, w: 440, h: 160, radius: 22, fill: card, stroke: line, stroke_width: 3, x: 0.71, y: 0.42, at: 0.5, enter: pop}
    - {kind: rect, w: 8, h: 210, radius: 4, fill: accent, stroke: none, x: 0.5, y: 0.42, at: 1.4, enter: {type: fade, seconds: 0.2}}
    - {kind: text, text: "RESET", size: 28, color: accent, x: 0.5, y: 0.355, at: 1.6, enter: fade, end: 3.0}
    - {kind: text, text: "0:59", size: 30, font: mono, color: accent2, x: 0.425, y: 0.355, at: 3.2, enter: fade}
    - &b1 {kind: rect, w: 22, h: 22, radius: 6, fill: accent2, stroke: none, x: 0.395, y: 0.42, at: 3.22, enter: pop}
    - {<<: *b1, x: 0.425, at: 3.28}
    - {<<: *b1, x: 0.455, at: 3.34}
    - {kind: text, text: "1:01", size: 30, font: mono, color: accent2, x: 0.575, y: 0.355, at: 4.6, enter: fade}
    - {<<: *b1, x: 0.545, at: 4.62}
    - {<<: *b1, x: 0.575, at: 4.68}
    - {<<: *b1, x: 0.605, at: 4.74}
    - {kind: text, text: "200", size: 230, color: accent2, x: 0.5, y: 0.215, at: 5.4, enter: {type: pop, seconds: 0.35}}
  camera:
    - {at: 2.4, seconds: 0.7, zoom: 1.35, focus: [0.5, 0.42], easing: in_out}
    - {at: 5.0, seconds: 0.4, zoom: 1.0, focus: [0.5, 0.5], easing: out}
  sfx:
    - {at: 1.4, sound: tick, pitch: 7}
    - {at: 3.22, sound: tick, gain: 0.7}
    - {at: 4.62, sound: tick, gain: 0.7, pitch: 3}
    - {at: 5.3, sound: hit, gain: 0.8}
```

**Safe zone.** Axis at y 0.42, labels at 0.355 / 0.50, memory or log row <= 0.62. During the zoom (1.35, focus
0.42) the 0.50 labels land at 0.61; nothing may sit below ~0.56 when zoomed. Big total at y 0.215, above the zoom.

**Timing.**
- Draw the axis first (0.6 s), then windows, then the reset line on the word "reset" + 0.3 s.
- Burst items 0.06 s apart; sfx tick every 2nd item, pitch rising; `hush` 0.4 s before the total, `hit` on it.
- Zoom in (0.7 s) just before the first burst, out (0.4 s) before the total; no packet moves during the zoom.
- Hold the total >= 2 s.

**Mistakes to avoid**
- Chips colliding with the headline: headline y <= 0.13, chips y >= 0.25.
- Burst squares beyond 12 per burst: leaf limit 60. Use 6 and label "100 in".
- Camera zoom pushing the labels past 0.69.
- Timeline labels exiting before the zoom ends.
