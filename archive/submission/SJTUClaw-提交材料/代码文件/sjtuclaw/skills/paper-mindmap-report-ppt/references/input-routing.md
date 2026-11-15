# Input Routing

## Mind Maps and Outlines

- `.md` / `.txt`: preserve heading and list order.
- `.xmind`: treat as ZIP; prefer `content.json`, fall back to `content.xml`.
- `.mm`: parse XML nodes and nesting.
- PDF mind map: render pages and inspect visually.
- Image mind map: inspect at original resolution; use OCR only as an aid.
- Pasted outline: preserve hierarchy and punctuation.

Do not flatten nested branches until slide planning. Keep node identifiers or
paths such as `2 > 2.3 > limitation` in `source-notes.txt`.

## Paper PDF

Use text extraction for search and page localization. Use rendered pages for:

- figure and table content;
- formulas and special symbols;
- captions, legends, footnotes, and multi-column reading order;
- visual verification of exact terminology.

Record each slide's evidence as page, section, figure, or table references.

## Figure Policy

Choose one policy per requested figure:

- **Use original**: render page at high DPI, crop, preserve aspect ratio, and
  keep caption/legend readable.
- **Redraw**: use only when requested and when a simpler diagram improves the
  talk. Preserve semantics and labels from the paper.
- **Leave blank**: add a stable, lightly bordered, no-text region. Do not add
  "placeholder", "paste figure", or production notes to visible slide content.

If wording is locked, prefer original figures as images or blank frames. Native
chart labels and diagram copy otherwise expand the visible-text contract.

## Source Conflicts

When outline wording and paper evidence differ:

1. keep user-required visible text unchanged;
2. record the conflict in `source-notes.txt`;
3. avoid adding a silent correction to the slide;
4. mention the conflict at delivery if it can affect interpretation.
