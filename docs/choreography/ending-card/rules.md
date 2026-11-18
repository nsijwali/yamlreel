# ending-card

**Use when** a series part ends (every part): a spoken ask plus an on-screen card that names the next part, then the
call to follow and subscribe, with the channel name last. One ask only, no comment question.

```yaml
- type: motion
  seconds: 7.5
  narration: "Next up, part two. The API and the redirect. Follow and subscribe so you do not miss it."
  elements:
    - {kind: group, x: 0.5, y: 0.2, at: 0.85, enter: pop, children: [{kind: rect, w: 340, h: 84, radius: 42, fill: card, stroke: line, stroke_width: 3}, {kind: text, text: "NEXT: PART 2/7", size: 34, color: muted}]}
    - {kind: group, x: 0.5, y: 0.33, at: 1.7, enter: pop, children: [{kind: rect, w: 900, h: 190, radius: 34, fill: card, stroke: accent, stroke_width: 3}, {kind: text, text: "THE API AND THE REDIRECT", size: 52, color: accent, max_w: 0.7}]}
    - {kind: group, x: 0.5, y: 0.5, at: 3.5, enter: pop, children: [{kind: rect, w: 640, h: 112, radius: 56, fill: accent, stroke: none}, {kind: text, text: "FOLLOW + SUBSCRIBE", size: 46, color: bg}]}
    - {kind: text, text: "scale in 60 sec", size: 60, color: fg, x: 0.5, y: 0.62, at: 5.4, enter: {type: pop, seconds: 0.35}}
  sfx:
    - {at: 0.85, sound: pop}
    - {at: 1.7, sound: pop, pitch: 2}
    - {at: 3.0, sound: hush, seconds: 0.45}
    - {at: 3.5, sound: stinger}
    - {at: 5.4, sound: pop, pitch: 4}
```

**Safe zone.** Landscape rows at y 0.23 / 0.43 / 0.61 and the brand at 0.715 (bottom under 0.76, captions at 0.88).
Vertical rows at y 0.2 / 0.33 / 0.5 / 0.62. The small "PART n/7" series label comes from the spec `watermark:`.

**Timing.**
- Part chip on "part" + 0.3 s, title card on the title's first word, pill on "Follow", brand on the last clause.
- `hush` 0.5 s before the pill, `stinger` on it; end the scene >= 2 s after the brand lands (tail 2.2 s).
- Spell the number for the voice ("part two"), put digits on screen.

**Mistakes to avoid**
- Asking for a comment as well: one ask at the close.
- The channel name before the ask: it goes last.
- Digits or a colon in the narration ("Part 2:"): the retake check may flag it; write "part two" and full stops.
- Brand text below y 0.76 in landscape: it meets the caption band.
