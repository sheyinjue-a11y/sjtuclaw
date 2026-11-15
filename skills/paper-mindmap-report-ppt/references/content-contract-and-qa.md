# Content Contract and QA

## Contract Purpose

Use a text contract when the user says wording must remain unchanged. The contract covers native PowerPoint text boxes and table cells. Text baked into a paper figure image must be verified visually against the source figure.

For spelling-only work, first create a replacement ledger in `source-notes.txt`:

```text
chat-assitant -> chat assistant
retrival -> retrieval
```

Apply only the listed replacements. Build the manifest from that corrected source copy. Do not use a general grammar, punctuation, capitalization, spacing, or terminology cleanup pass.

## Contract Mode Selection

Use `strict` only when the user explicitly locks all native visible text and provides or approves the complete copy. No additional native text is allowed.

Use `required_subset` when only specific titles, labels, quotations, terms, or phrases are immutable, while additional source-grounded presentation text is allowed.

If the user permits ordinary summarization or rewriting and specifies no immutable copy, do not create a content contract.

Infer the most appropriate contract mode from the user鈥檚 materials and requirements. Before building the deck, state the inferred mode and briefly ask the user to confirm it. If the user has already selected a mode explicitly, follow that selection without asking again.

If the user has not provided a mind map or an equivalent source outline, ask them to provide either the mind map itself or a written description of its structure, main branches, content priorities, and presentation requirements before creating the source outline or content contract.

Do not create a contract until the presentation structure and the scope of immutable text are sufficiently clear.

## Manifest Format

Create `content-contract.json` from the source outline, not from the generated deck:

```json
{
  "schema_version": 1,
  "mode": "strict",
  "normalize_whitespace": true,
  "expected": [
    {"text": "Exact title from the source", "count": 1},
    {"text": "Exact body text assigned to one text box", "count": 1}
  ]
}
```

Use `mode: "strict"` when all native visible text is locked. Use `mode: "required_subset"` when only selected outline labels or user-specified phrases are immutable and the deck may contain additional paper-grounded explanations.

In `strict` mode, every piece of native visible text must be represented in the manifest. The generated deck may not contain missing, altered, duplicated, or additional native text.

In `required_subset` mode, each expected string and its minimum count must be present. Unrelated native text is allowed, provided that any added factual or explanatory content is grounded in the source materials.

Split long source passages into exact strings assigned to individual text boxes or table cells. Line breaks may move because whitespace is normalized. Characters, words, terms, punctuation, and duplicate counts must still match.

Include slide titles, table headers, footers, page labels, navigation labels, and other visible native text when they fall within the locked copy.

Keep decorative shapes and figure frames text-free unless their wording comes from the source or is explicitly permitted by the selected contract mode.

## Source Contract Rules

Build the manifest from the approved source outline or corrected source copy before generating the deck.

Do not build the manifest by extracting text from the generated deck. Doing so would allow altered wording to validate itself.

When only part of the source is immutable, include only those exact strings in a `required_subset` manifest.

When all visible copy is immutable, include all planned native text in a `strict` manifest, including repeated strings with their correct counts.

If the same text appears multiple times, record the intended total occurrence count:

```json
{
  "text": "Results",
  "count": 3
}
```

The contract unit should generally correspond to one text box or one table cell. If a source paragraph will be divided across multiple text containers, split it into the exact strings that will appear in those containers before creating the manifest.

Do not alter wording merely to make the contract easier to satisfy. Solve layout and density issues through slide structure and visual design.

## Checker

Debug actual text extraction:

```powershell
python "<this-skill>\scripts\pptx_text_contract.py" extract `
  --pptx "<final.pptx>" `
  --out "<workspace>\tmp\actual-text.json"
```

The `extract` output is diagnostic only. Do not use it as the source contract; that would make altered wording validate itself.

Run the contract comparison:

```powershell
python "<this-skill>\scripts\pptx_text_contract.py" check `
  --pptx "<final.pptx>" `
  --manifest "<workspace>\tmp\content-contract.json"
```

In `strict` mode, any missing, extra, altered, or incorrectly duplicated string is a failure.

In `required_subset` mode, any missing required string or insufficient occurrence count is a failure. Additional native text is permitted.

When a check fails, compare the extracted diagnostic text with the approved source contract. Correct the deck rather than weakening or regenerating the contract from the deck.

## Visual QA

Inspect both montage and full-size slide PNGs. Check:

* one-line titles do not wrap;
* body text remains readable at presentation distance;
* no clipping, overlap, isolated bullets, or orphaned lines;
* tables have enough row height and stable columns;
* figure frames do not cover source text;
* figures are sharp and labels are legible;
* page chrome, margins, and color hierarchy remain consistent;
* no internal production notes or placeholder labels are visible.

For locked copy, solve density by splitting slides or changing layout. Do not shorten text, paraphrase wording, remove content, or substitute punctuation.

When text is not locked, ordinary editing may be used only to the extent permitted by the user鈥檚 requirements and the selected contract mode.

## Figure Text QA

The text contract covers only native PowerPoint text boxes and table cells.

Text embedded inside screenshots, charts, diagrams, or paper figure images is not reliably covered by native text extraction. Verify such text visually against the source figure.

Check that:

* the correct source figure was used;
* no labels or legends were cropped;
* figure text remains readable;
* overlays and frames do not hide source text;
* the figure has not been stretched or distorted;
* any native caption added outside the figure follows the selected contract mode.

Do not recreate or paraphrase figure labels as native PowerPoint text unless the user permits it.

## Traceability QA

For each slide, verify three links:

1. the requested outline branch is present;
2. factual claims and selected figures point to source or paper evidence;
3. visible native text matches the approved source copy after listed corrections.

Treat a missing outline branch, unsupported added claim, or unlisted wording change as a failed build even when visual QA passes.

In `strict` mode, any additional native text not represented in the manifest is also a failed build.

In `required_subset` mode, additional text is allowed only when it remains consistent with the user鈥檚 requested structure and is grounded in the provided source materials.

## Completion Criteria

Do not consider the deck complete until:

* the contract mode has been inferred and confirmed, unless the user selected it explicitly;
* the mind map, source outline, or equivalent structure has been provided or established;
* the manifest was created from the approved source copy;
* the contract checker passes;
* montage and full-size visual QA pass;
* figure text has been visually verified;
* every requested outline branch is present;
* every added factual claim is traceable to source evidence;
* no unapproved wording changes are visible.
