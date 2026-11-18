# lyric-breath

**Use when** a sung (or slow spoken) line must sit on screen while it is sung: the line large, its
transliteration small beneath, a translation fainter a beat later, all fading in and out like a breath over quiet
art. Built for the `dusk` theme; one block of three lines per sung line, reused for a repeated couplet.

```yaml
- type: motion
  seconds: 16
  elements:
    # a shaped script (Devanagari, Arabic, ...) comes in as a PNG at its on-screen size, never resampled
    - {kind: image, source: lines/line-1.png, w: 814, x: 0.5, y: 0.575, at: 0.2, enter: {type: fade, seconds: 1.0, easing: in_out},
       moves: [{at: 7.4, seconds: 1.0, opacity: 0}, {at: 8.7, seconds: 0.9, opacity: 1}], end: 15.0, exit: {type: fade-out, seconds: 0.8}}
    - {kind: text, text: "Main line, transliterated", size: 30, color: fg, opacity: 0.82, x: 0.5, y: 0.632, at: 0.2,
       enter: {type: fade, seconds: 1.0, easing: in_out},
       moves: [{at: 7.4, seconds: 1.0, opacity: 0}, {at: 8.7, seconds: 0.9, opacity: 0.82}], end: 15.0, exit: {type: fade-out, seconds: 0.8}}
    - {kind: text, text: "The line in English", size: 26, font: body, color: muted, opacity: 0.85, x: 0.5, y: 0.666, at: 0.9,
       enter: {type: fade, seconds: 1.0, easing: in_out},
       moves: [{at: 7.4, seconds: 1.0, opacity: 0}, {at: 9.4, seconds: 0.9, opacity: 0.85}], end: 15.0, exit: {type: fade-out, seconds: 0.8}}
```

**Safe zone.** Main line centre 0.575, transliteration 0.632, translation 0.666: the bottom of the stack stays
under 0.69 in vertical. Keep the art (windows, figures, chair legs) above about 0.53 so nothing dark crosses the
glyphs, and keep the main line at most 0.8 W wide (about 860 px) so it clears the Shorts buttons on the right.

**Timing.**
- Times come from the audio: the 100 ms RMS envelope gives the line onsets and ends; Whisper (sung, so fuzzy)
  confirms which line is which. Line-level timing only.
- Fade in 0.3 s before the line is sung (`enter` 0.8 to 1.0 s, `in_out`); the translation follows 0.7 s later.
- A repeated line: one element, faded out with an opacity move after the first pass and in again before the second
  (dip to 0.3 instead of 0 when the breath between passes is under 1 s).
- Two different lines in the same slot: the first `end + exit.seconds` must be at or before the next `at`.
- Every exit ends at least 0.2 s before the scene ends: a fade transition starts from the old scene's last frame,
  so a half-faded line there ghosts through the next scene's first second.

**Mistakes to avoid**
- Drawing Devanagari or Arabic with a `text` element when Pillow has no raqm: conjuncts break and matras move.
- A title or the next line fading in while the last line is still fading out: they ghost over each other.
- A camera move on a lyric scene: it zooms and softens the text. Scale the art instead (`moves: scale`).
- Fading the art out over the theme gradient: put an opaque black image under it so the end reads black.
