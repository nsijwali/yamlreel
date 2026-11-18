# Failures we hit (one line each, with the fix)

- Emphasis (pulse) started before the entry finished: validation error. Start it at or after `at + enter.seconds`.
- A move started before its element entered: error. Give movers `enter: none`, or start the move after the entry.
- Consecutive moves overlapped (`moves[1].at` before move 0 ended): error. Next `at` >= previous `at + seconds`.
- A packet covered a box label: end the packet above the box (box centre 0.56, packet ends y 0.49, box top ~0.515) and fade it out.
- Zoom cut off the diagram: keep `y <= fy + 0.19/zoom` for every element that must stay above the caption line.
- Zoom pushed content into the caption band: same formula; lower the zoom or move the focus down.
- A ring rendered filled: circles default to `fill: accent`. Use `fill: bg` with a `stroke` for a ring.
- A label collided with the scene `title` (drawn at 0.09 H): use either `title:` or your own label, not both at y ~0.1.
- A headline collided with the chips under it: headline centre y <= 0.13, first chip row y >= 0.25.
- More than 60 elements in a scene: error counts leaves after groups and split words. Reuse one mover with `moves`.
- A move or emphasis started before the element's entry ended, so it jumped: see rule 6 in README.md.
- SFX had no effect: `sfx:` requires a `mix:` block in the spec.
- Debreath cut consonants: set `debreath: false` and deliver the voice with `shorts/voicedeliver.py`.
- The first frame was empty: frame 0 is the poster frame. Put the hook on screen at `at: 0`, `enter: none`.
- Frames looked fine at one time stamp only: check 0.04 s, 0.5 s, 1 s and every scene start and end.
- Text was replaced before it could be read: hold >= letters/15 + 1.5 s.
- Loudness guessed: measure it (-14 LUFS, <= -1 dBTP), and say measured versus assumed.
