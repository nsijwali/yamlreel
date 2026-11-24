# tile-map

**Use when** the idea is "what is loaded right now": a map, a cache or a buffer split into tiles, a block of them
in memory around a cursor (a car, a reader), the next column fetched ahead, the oldest one dropped. Keep the same
grid at the same pixels across beats and cut between scenes, so the tricks read as one canvas.

```yaml
- type: motion
  seconds: 5.0
  narration: "Only the tiles around your car stay in memory. Floor it, and the next column loads first."
  elements: [
    {kind: text, text: "KEEP 9 TILES", size: 60, y: 0.135, at: 0.3, enter: slide-up},
    &t {kind: rect, w: 140, h: 140, radius: 14, fill: card, stroke: line, stroke_width: 2, x: 0.2037, y: 0.3567, at: 0, enter: none, opacity: 0.28},
    {<<: *t, x: 0.3519, opacity: 1}, {<<: *t, x: 0.5, opacity: 1}, {<<: *t, x: 0.6481, opacity: 1},
    {<<: *t, x: 0.7963, moves: [{at: 3.1, seconds: 0.35, opacity: 1}]},
    {<<: *t, y: 0.44}, {<<: *t, y: 0.44, x: 0.3519, opacity: 1}, {<<: *t, y: 0.44, x: 0.5, opacity: 1},
    {<<: *t, y: 0.44, x: 0.6481, opacity: 1}, {<<: *t, y: 0.44, x: 0.7963, moves: [{at: 3.2, seconds: 0.35, opacity: 1}]},
    {<<: *t, y: 0.5233}, {<<: *t, y: 0.5233, x: 0.3519, opacity: 1}, {<<: *t, y: 0.5233, x: 0.5, opacity: 1},
    {<<: *t, y: 0.5233, x: 0.6481, opacity: 1}, {<<: *t, y: 0.5233, x: 0.7963, moves: [{at: 3.3, seconds: 0.35, opacity: 1}]},
    {kind: path, points: [[0.139, 0.44], [0.861, 0.44]], color: muted, stroke_width: 6, opacity: 0.55, at: 0, enter: none},
    {kind: rect, w: 46, h: 28, radius: 8, fill: accent, stroke: none, x: 0.5, y: 0.44, at: 0, enter: none,
     moves: [{at: 3.7, seconds: 0.5, x: 0.6481, easing: in_out}]},
    {kind: rect, w: 478, h: 478, radius: 20, fill: none, stroke: accent, stroke_width: 5, x: 0.5, y: 0.44, at: 0.9,
     enter: {type: fade, seconds: 0.3}, moves: [{at: 3.7, seconds: 0.5, x: 0.6481, easing: in_out}]},
    {kind: rect, w: 158, h: 478, radius: 20, fill: none, stroke: accent, stroke_width: 4, x: 0.7963, y: 0.44, at: 3.1,
     enter: {type: fade, seconds: 0.25}, end: 4.5, exit: {type: fade-out, seconds: 0.2}}
  ]
  sfx:
    - {at: 0.9, sound: click}
    - {at: 3.1, sound: tick}
    - {at: 3.7, sound: whoosh, seconds: 0.4}
```

**Safe zone.** Tile 140 px on a 160 px pitch (20 px gap): columns x 0.2037 to 0.7963 (edges 0.139 to 0.861, clear of
the Shorts buttons), rows from y 0.2733 on a 0.0833 step. Five rows end at 0.643, three at 0.56. Headline y 0.135,
one mono label at y 0.19, nothing else between them and the grid.

**Timing.**
- In memory = opacity 1, on disk = 0.28. Change state with an opacity `move` (0.35 s) on the spoken word.
- The ring (`fill: none`) arrives on "around"; the prefetched column fades up 0.1 s apart per row on "loads";
  the car and ring move together on "before you get there" (`in_out`, 0.5 s).
- Eviction: the oldest column gets a red ring and an `OLDEST` label, then drops to 0.28 on "dropped".
- A memory bar over the grid: a `line` track plus an `accent2` path drawn on "fills"; swap to a shorter `accent`
  path when a column is dropped.

**Mistakes to avoid**
- Background-coloured cut lines over the map read as dark stripes on the gradient: after the cut, cross-fade to
  25 separate tiles.
- Tiles listed after the car hide it: list the road and the car after the tiles.
- Speed lines started at the car's centre sat on top of it: start them behind the car (x minus half its width).
- 25 tiles plus a ring, car, road and labels is about 35 leaves; stay under 60 in total.
