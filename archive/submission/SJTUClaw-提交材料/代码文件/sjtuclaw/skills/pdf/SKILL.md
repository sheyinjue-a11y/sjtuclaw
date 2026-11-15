---
name: pdf
description: Create, inspect, fill, render, and visually review workspace-local PDF artifacts.
---

# PDF workflow

Use `read_skill_resource` to load the requested source/specification, then `write_file` to place JSON input in the workspace. Obtain user approval before `run_command` creates or changes PDF artifacts. Use `create_download` only after validation succeeds.

Run commands from the workspace root:

```powershell
py skills/pdf/scripts/pdf_workflow.py create --spec artifacts/report.json --output output/report.pdf
py skills/pdf/scripts/pdf_workflow.py inspect --input output/report.pdf
py skills/pdf/scripts/pdf_workflow.py render --input output/report.pdf --output-dir tmp/report-pages
```

Use `fill` for AcroForms:

```powershell
py skills/pdf/scripts/pdf_workflow.py fill --input artifacts/form.pdf --values artifacts/values.json --output output/filled.pdf
```

All CLI paths must be relative to the current workspace. `inspect` prints JSON. Errors use stable codes on stderr. `render` requires `pdftoppm`; a missing renderer reports `PDF_RENDERER_UNAVAILABLE`.

Visual review is complete only after `render` returns PNGs and those images are inspected. Confirm no clipping, overlap, unreadable text, or table/layout defects before download.

See [input schema](references/input-schema.md) for `create` and `fill` JSON documents.
