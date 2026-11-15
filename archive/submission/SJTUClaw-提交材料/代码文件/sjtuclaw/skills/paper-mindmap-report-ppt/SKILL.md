---
name: paper-mindmap-report-ppt
description: Build and verify an academic PowerPoint from a research-paper PDF and a mind map, outline, notes, or mind-map image.
version: 1.0.0
---

# Paper + Mind Map Report PPT

Create a presentation in which the outline controls the story and the paper
controls factual evidence. Treat user instructions as the highest authority.

## Required Skills

Use the local `$pdf` and `$presentations` Skills before processing inputs or
authoring slides. SJTUClaw routes these two Skills automatically for relevant
PDF and presentation requests; do not require the user to add them manually.

## Workflow

### 1. Resolve Scope and Sources

Identify:

- paper PDF;
- mind map or outline (`.xmind`, `.mm`, `.md`, `.txt`, PDF, image, or pasted text);
- audience, duration, language, output path, and visual direction;
- whether visible wording is editable, lightly editable, or immutable;
- whether corrections are unrestricted, terminology-only, or spelling-only;
- figure policy: use paper figures, redraw selected diagrams, or leave blank areas.

Do not stop for details that can be inferred safely. Record assumptions in
`source-notes.txt`.

If the user asks for "faithful" output without defining the edit boundary,
default to immutable visible wording. In spelling-only mode, preserve unusual
phrasing, jokes, mixed Chinese-English style, punctuation, capitalization, and
terminology unless each change is an obvious spelling error.

Use this source precedence:

1. explicit user requirements;
2. immutable visible wording from the outline;
3. paper evidence;
4. design judgment.

If the outline contradicts the paper, do not silently correct immutable wording.
Keep required visible wording and record the conflict for the user.

### 2. Inspect Both Inputs Completely

Parse the mind-map hierarchy, preserving branch order and nesting. For image
mind maps, inspect at full resolution and transcribe uncertain nodes explicitly.
For `.xmind`, inspect ZIP contents and parse `content.json` or `content.xml`.

Extract PDF text for search, then render relevant pages for layout, figures,
tables, formulas, captions, and footnotes. Never infer figure content from text
extraction alone.

Create these intermediate files under the presentation workspace:

- `source-notes.txt`: outline nodes, paper pages, figures, tables, and evidence;
- `slide-plan.txt`: slide purpose, exact visible copy, outline branch, paper
  evidence, and layout;
- `asset-plan.txt`: selected paper figures or blank figure regions;
- `qa-ledger.txt`: content and visual checks;
- `content-contract.json`: required native PowerPoint text when wording is locked.

Read [references/input-routing.md](references/input-routing.md) for format and
figure handling.

### 3. Build the Narrative

Translate the mind-map hierarchy into a coherent academic story. Default order:

1. title and research question;
2. motivation and gap;
3. method, benchmark, or system;
4. experimental setup;
5. main results;
6. analysis and implications;
7. limitations and extensions.

Keep every requested branch. Split dense branches across slides. Do not compress
away required wording. Use the paper to check claims, names, metrics, figure
numbers, and terminology.

### 4. Freeze Visible Copy

When wording is immutable:

- copy source text without paraphrasing or punctuation substitution;
- allow only splitting, line wrapping, indentation, and relocation;
- derive every native text box and table-cell string from source text;
- do not add visible placeholder labels, production notes, or explanatory copy;
- use blank bordered regions for figures the user will add later;
- store every expected text box or cell in `content-contract.json`.

When only selected outline labels or phrases are immutable, use
`mode: "required_subset"` in `content-contract.json`. Use strict mode only when
all native visible text is locked.

When only spelling fixes are allowed:

- list every accepted replacement as `before -> after` in `source-notes.txt`;
- derive `content-contract.json` from the corrected source copy, not the deck;
- make no unlisted consistency edits, including terminology normalization,
  capitalization, punctuation, spacing, or rephrasing;
- preserve deliberate humor and informal wording.

Read [references/content-contract-and-qa.md](references/content-contract-and-qa.md)
before authoring any locked-copy deck.

### 5. Design and Author

Follow the local `$presentations` Skill for workspace selection, narrative
quality, font minimums, visual routing, and creation/inspection workflow.

For a custom academic style:

- use 16:9 unless the user specifies otherwise;
- use a very light tinted background rather than pure white;
- use one dark academic primary color and at most two restrained accents;
- keep body text near-black and maintain high contrast;
- use tables, timelines, process diagrams, and section bands only when they
  clarify the research story;
- avoid marketing layouts, oversized decoration, nested cards, and repeated
  dashboard panels;
- reserve large stable rectangles for requested paper figures.

Use original paper figures as high-resolution crops when requested. Preserve
their aspect ratio and caption/legend legibility. Use blank no-text frames when
the user will paste figures later.

### 6. Verify Before Delivery

Run all checks:

1. export the `.pptx`;
2. render every slide to PNG with `render_slides.py`;
3. create a montage for deck-level consistency;
4. inspect every slide individually at full size;
5. run `slides_test.py` and fix all overflow;
6. run the text-contract checker for locked-copy decks;
7. compare claims, metrics, and figures with `source-notes.txt`;
8. confirm every requested outline branch appears in `slide-plan.txt` and the
   rendered deck;
9. confirm spelling-only changes match the recorded replacement ledger;
10. confirm no unresolved placeholder text, internal notes, clipping, overlap,
   title wrapping, unreadable captions, or inconsistent page chrome.

Use:

```powershell
python "<this-skill>\scripts\pptx_text_contract.py" check `
  --pptx "<final.pptx>" `
  --manifest "<workspace>\tmp\content-contract.json"
```

Do not weaken checks to make them pass. Fix source mapping or slide layout.

### 7. Deliver

Return exactly one link to the final `.pptx`, plus a concise summary of source
use and checks completed. Keep builders, manifests, previews, and QA files in
scratch unless requested.

## Failure Handling

- Text mismatch: inspect the reported slide and shape text; correct the slide or
  contract chunk derived from source. Never rewrite source to match the deck.
- Overflow: split the slide or enlarge the content region before reducing font.
- Missing Chinese glyphs: use an installed CJK font and re-render.
- Blurry paper figure: render the PDF page at higher DPI and crop again.
- Unreadable mind map: request the original file or a higher-resolution export.
- Conflicting sources: preserve explicit user requirements and report conflict.
- Artifact-tool setup checks `<project>/.cache`: do not rewrite `HOME`. Resolve
  the actual user profile directory, rerun the setup helper from that working
  directory, and keep the presentation workspace unchanged.
- Poppler wrapper cannot find its executable: use the absolute Poppler path
  returned by the loaded workspace dependencies, then record the fallback in
  `qa-ledger.txt`.
