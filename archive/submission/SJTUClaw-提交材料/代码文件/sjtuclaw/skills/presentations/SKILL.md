---
name: presentations
version: 1.0.0
description: Create compact, inspectable PowerPoint decks from workspace-local JSON specifications.
---

# PPTX workflow

Use the workflow script for lightweight PowerPoint deliverables. Build the JSON specification according to [references/input-schema.md](references/input-schema.md), keep visible copy audience-facing, and use workspace-relative paths only.

```powershell
py pptx_workflow.py create --spec artifacts/deck.json --output output/deck.pptx
py pptx_workflow.py inspect --input output/deck.pptx
py pptx_workflow.py render --input output/deck.pptx --output-dir tmp/deck-pages
```

Keep body text at 16pt or larger: shorten text or split content across slides instead of shrinking it. The workflow uses a high-contrast light background, dark text, and Microsoft YaHei for East Asian text.

Run `inspect` after creating or editing a deck. It checks shapes outside the slide canvas and conservatively estimates text-frame overflow from text length, font size, and geometry, but does not prove visual balance or catch overlap between shapes. Render pages and inspect them visually when renderers are available.

Rendering needs both LibreOffice (`soffice`) and Poppler (`pdftoppm`) on `PATH`. If either is unavailable, the command exits with:

`PRESENTATION_RENDERER_UNAVAILABLE: render requires both soffice and pdftoppm on PATH; install LibreOffice and Poppler, then retry`
