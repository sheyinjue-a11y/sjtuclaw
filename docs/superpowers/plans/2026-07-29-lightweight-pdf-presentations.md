# Lightweight PDF and Presentations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add default, automatically routed PDF and basic PPTX creation/checking capabilities to SJTUClaw without new APIs or bypassing workspace approvals.

**Architecture:** Two self-contained Python resource scripts live in local Skill snapshots and are copied into the approved workspace before execution. A shared Runtime router adds immutable `pdf` and `presentations` snapshots before a Run is persisted, preserves explicit selections, and records automatic selections distinctly. The Web composer hides only these default Skills from its manual insertion menu.

**Tech Stack:** Python 3.12, pypdf, reportlab, pdfplumber, python-pptx, optional Poppler (`pdftoppm`), optional LibreOffice (`soffice`), FastAPI runtime, React/Vite/Vitest.

## Global Constraints

- Add `reportlab`, `pdfplumber`, and `python-pptx` to `pyproject.toml`; retain existing `pypdf`.
- Built-in Skill IDs are exactly `pdf` and `presentations`; each `SKILL.md` uses parser-valid single-line frontmatter and a `version`.
- Resource files must be UTF-8 text and each stay below the existing 24,000-byte Skill-resource limit.
- Generated script inputs and outputs are workspace-relative; scripts reject absolute paths and traversal outside their current working directory.
- `pdftoppm` and `soffice` are optional `PATH` commands. Missing renderers produce stable errors and never imply visual QA passed.
- Keep existing Gateway API, JSON session compatibility, attachment types, approvals, and download workflow. Current attachments remain text/PDF only.
- Automatic route precedence: preserve explicit references, append non-duplicate automatic references in `pdf`, `presentations` order until the existing three-Skill limit.

---

### Task 1: Deliver self-contained PDF Skill resources

**Files:**
- Modify: `pyproject.toml`
- Create: `skills/pdf/SKILL.md`
- Create: `skills/pdf/references/input-schema.md`
- Create: `skills/pdf/scripts/pdf_workflow.py`
- Test: `tests/unit/test_pdf_workflow.py`

**Interfaces:**
- Consumes: workspace-relative CLI arguments and JSON specs.
- Produces: `pdf_workflow.py` commands `inspect`, `create`, `fill`, and `render`; JSON stdout for inspection; stable nonzero errors on stderr.
- Uses: `pypdf`, `reportlab`, `pdfplumber`, and optional `pdftoppm`.

- [ ] **Step 1: Write failing subprocess tests for the four CLI operations**

```python
def _run(
    script: Path, *args: str, cwd: Path, env: dict[str, str] | None = None
) -> CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args], text=True, capture_output=True, cwd=cwd, env=env
    )

def test_create_then_inspect_pdf(tmp_path: Path) -> None:
    spec = tmp_path / "report.json"
    spec.write_text('{"title":"实验报告","sections":[{"heading":"结论","paragraphs":["可验证内容"]}]}', encoding="utf-8")
    output = tmp_path / "report.pdf"
    assert _run(SCRIPT, "create", "--spec", "report.json", "--output", "report.pdf", cwd=tmp_path).returncode == 0
    inspected = _run(SCRIPT, "inspect", "--input", "report.pdf", cwd=tmp_path)
    assert json.loads(inspected.stdout)["page_count"] == 1
```

Add tests that create a ReportLab AcroForm text field then call `fill`, and set `PATH=""` while calling `render` to assert `PDF_RENDERER_UNAVAILABLE`.

- [ ] **Step 2: Run the PDF test before implementation**

Run: `py -3.12 -m pytest tests/unit/test_pdf_workflow.py -q`

Expected: FAIL because `skills/pdf/scripts/pdf_workflow.py` and declared dependencies do not exist.

- [ ] **Step 3: Declare runtime packages and implement the bounded CLI**

Add these project dependencies:

```toml
"reportlab>=4,<5",
"pdfplumber>=0.11,<1",
"python-pptx>=1,<2",
```

Implement this public entry point and path guard:

```python
def main(argv: Sequence[str] | None = None) -> int: ...

def workspace_path(raw: str) -> Path:
    root = Path.cwd().resolve()
    candidate = (root / raw).resolve()
    if Path(raw).is_absolute() or not candidate.is_relative_to(root):
        raise ArtifactError("ARTIFACT_INPUT_INVALID", "paths must stay inside the workspace")
    return candidate
```

`create` accepts `title`, optional `subtitle`, and `sections` containing `heading`, `paragraphs`, `bullets`, and `table`. Use ReportLab `UnicodeCIDFont("STSong-Light")` for Chinese. `inspect` returns `page_count`, `encrypted`, `text_chars`, and `field_names`. `fill` rejects encrypted input and raises `PDF_FIELD_NOT_FOUND` for any requested name absent from `PdfReader.get_fields()`. `render` obtains `pdftoppm` from `shutil.which` and otherwise exits with `PDF_RENDERER_UNAVAILABLE`.

- [ ] **Step 4: Add Skill instructions and JSON schema reference**

Document `read_skill_resource` -> `write_file` -> approval -> `run_command` -> `create_download`. State that visual review is complete only after `render` returns PNGs and those images are inspected. Include exact commands such as:

```powershell
py pdf_workflow.py create --spec artifacts/report.json --output output/report.pdf
py pdf_workflow.py inspect --input output/report.pdf
py pdf_workflow.py render --input output/report.pdf --output-dir tmp/report-pages
```

- [ ] **Step 5: Run focused checks**

Run: `py -3.12 -m pytest tests/unit/test_pdf_workflow.py -q`

Expected: PASS, including the no-renderer error path.

- [ ] **Step 6: Commit the PDF Skill**

```powershell
git add pyproject.toml skills/pdf tests/unit/test_pdf_workflow.py
git commit -m "feat: add PDF skill resources"
```

### Task 2: Deliver self-contained PPTX Skill resources

**Files:**
- Create: `skills/presentations/SKILL.md`
- Create: `skills/presentations/references/input-schema.md`
- Create: `skills/presentations/scripts/pptx_workflow.py`
- Test: `tests/unit/test_pptx_workflow.py`

**Interfaces:**
- Consumes: a workspace-relative JSON spec with `title` and `slides`.
- Produces: `pptx_workflow.py` commands `create`, `inspect`, and `render`.
- Uses: `python-pptx`, optional `soffice`, and optional `pdftoppm`.

- [ ] **Step 1: Write failing PPTX CLI tests**

```python
def test_create_then_inspect_presentation(tmp_path: Path) -> None:
    spec = tmp_path / "deck.json"
    spec.write_text('{"title":"课程汇报","slides":[{"title":"问题","bullets":["背景","目标"]}]}', encoding="utf-8")
    assert _run(SCRIPT, "create", "--spec", "deck.json", "--output", "deck.pptx", cwd=tmp_path).returncode == 0
    result = _run(SCRIPT, "inspect", "--input", "deck.pptx", cwd=tmp_path)
    assert json.loads(result.stdout)["slide_count"] == 2
```

Create a deliberately out-of-bounds textbox with `python-pptx` and assert `PRESENTATION_BOUNDS_ERROR`; set `PATH=""` and assert `PRESENTATION_RENDERER_UNAVAILABLE` from `render`.

- [ ] **Step 2: Run the new test before implementation**

Run: `py -3.12 -m pytest tests/unit/test_pptx_workflow.py -q`

Expected: FAIL because the presentation resource script does not exist.

- [ ] **Step 3: Implement deterministic creation, inspection, and optional rendering**

Implement:

```python
def create_presentation(spec: Mapping[str, object], output: Path) -> None: ...
def inspect_presentation(source: Path) -> dict[str, object]: ...
def render_presentation(source: Path, output_dir: Path) -> None: ...
```

Create a 16:9 deck with one title slide and one slide per `slides` element. Each slide accepts `title`, `body`, `bullets`, and optional workspace-relative `image`. Use a high-contrast light background, dark text, and `Microsoft YaHei` as the East Asian font name. `inspect` rejects any shape with negative coordinates or a right/bottom edge outside `Presentation.slide_width`/`slide_height`. `render` requires both `soffice` and `pdftoppm`; run LibreOffice headlessly into an isolated temporary directory before rasterizing its PDF.

- [ ] **Step 4: Add Skill instructions and schema reference**

Require the model to shorten text instead of shrinking body text below 16pt; treat `inspect` as structural QA only. Document the exact renderer fallback message and commands:

```powershell
py pptx_workflow.py create --spec artifacts/deck.json --output output/deck.pptx
py pptx_workflow.py inspect --input output/deck.pptx
py pptx_workflow.py render --input output/deck.pptx --output-dir tmp/deck-pages
```

- [ ] **Step 5: Run focused checks**

Run: `py -3.12 -m pytest tests/unit/test_pptx_workflow.py -q`

Expected: PASS, including bounds and no-renderer coverage.

- [ ] **Step 6: Commit the presentation Skill**

```powershell
git add skills/presentations tests/unit/test_pptx_workflow.py
git commit -m "feat: add presentations skill resources"
```

### Task 3: Route default Skills before request persistence

**Files:**
- Create: `src/sjtuclaw/runtime/skill_routing.py`
- Modify: `src/sjtuclaw/models/runtime.py`
- Modify: `src/sjtuclaw/models/session.py`
- Modify: `src/sjtuclaw/infrastructure/persistence/json_store.py`
- Modify: `src/sjtuclaw/runtime/service.py`
- Modify: `src/sjtuclaw/runtime/agent_loop.py`
- Modify: `src/sjtuclaw/bootstrap.py`
- Modify: `src/sjtuclaw/runtime/__init__.py`
- Test: `tests/unit/test_skill_routing.py`
- Test: `tests/unit/test_runtime_service.py`
- Test: `tests/unit/test_json_store.py`
- Test: `tests/unit/test_agent_loop.py`

**Interfaces:**
- Produces: `candidate_default_skill_ids(text: str, attachments: tuple[Attachment, ...]) -> tuple[str, ...]`.
- Produces: `merge_default_skill_refs(request: RuntimeRequest, candidates: tuple[str, ...], skills: SkillService) -> RuntimeRequest`.
- Extends: `RuntimeRequest`, `PendingRequest`, and `RunCheckpoint` with `automatic_skill_ids: tuple[str, ...]` that is unique and a subset of `skill_refs`.

- [ ] **Step 1: Write failing router and persistence tests**

```python
def test_pdf_attachment_routes_pdf_and_presentation_text_routes_presentations() -> None:
    assert candidate_default_skill_ids("请生成 PPT 汇报", ()) == ("presentations",)
    assert candidate_default_skill_ids("总结附件", (ready_pdf_attachment(),)) == ("pdf",)

def test_explicit_refs_win_and_auto_refs_fill_only_remaining_slots() -> None:
    request = request_with_refs("course-report", "material-summary", "presentation-outline")
    routed = merge_default_skill_refs(request, ("pdf", "presentations"), skills)
    assert routed.skill_refs == request.skill_refs
    assert routed.automatic_skill_ids == ()
```

Add JSON round-trip coverage for pending requests/checkpoints with `automaticSkillIds`, plus an AgentLoop assertion that an automatically routed reference creates `SkillUsageSource.AUTO` and an explicit reference remains `EXPLICIT`.

- [ ] **Step 2: Run focused router tests before implementation**

Run: `py -3.12 -m pytest tests/unit/test_skill_routing.py tests/unit/test_runtime_service.py tests/unit/test_json_store.py tests/unit/test_agent_loop.py -q`

Expected: FAIL because routing interfaces and persisted `automaticSkillIds` do not exist.

- [ ] **Step 3: Implement pure candidate detection and snapshot merge**

In `skill_routing.py`, match case-folded PDF/PPT keywords; use `AttachmentKind.PDF` for uploaded files. Treat text mentioning `.pptx` as a workspace-file request without accessing the file system. Resolve candidates one-by-one from `SkillService.list()` so unavailable defaults are skipped, then append at most enough references to reach three. Never replace explicit references or add an ID twice.

- [ ] **Step 4: Carry source metadata through Runtime and JSON recovery**

Add `automatic_skill_ids` to the three request/checkpoint models and validate it with:

```python
if any(skill_id not in {ref.skill_id for ref in self.skill_refs} for skill_id in self.automatic_skill_ids):
    raise ValueError("automatic skill ids must be selected skill ids")
```

Write `automaticSkillIds` for new pending requests and checkpoints. Introduce the next session schema version, while all earlier schema versions deserialize the new field as `()`. Carry the field through `_persist_pending`, `_resume_pending`, approval resume, idempotency comparison, and every `RuntimeRequest` reconstruction.

- [ ] **Step 5: Apply route once in `RuntimeHost.submit` and record sources correctly**

Store `skills` on `RuntimeHost`. Before `_persist_pending`, obtain ready attachment metadata when references exist, replace the request with merged default references, then persist that exact request. In `AgentLoop`, replace `_explicit_skill_usages` with an initial-usage builder: IDs in `request.automatic_skill_ids` use `SkillUsageSource.AUTO` and reason `automatic <skill-id> routing`; all other IDs retain current explicit/scheduled-explicit behavior. Set `Session.explicit_skill_refs` to only the non-automatic references.

Refresh Skill snapshots during bootstrap only when the current list is empty or lacks either default ID, so an upgraded local installation discovers these built-ins without refreshing every user Skill on every startup.

- [ ] **Step 6: Run focused checks**

Run: `py -3.12 -m pytest tests/unit/test_skill_routing.py tests/unit/test_runtime_service.py tests/unit/test_json_store.py tests/unit/test_agent_loop.py tests/unit/test_bootstrap_tools.py -q`

Expected: PASS; direct PDF/PPT requests enter the first model context with selected snapshots and correct `AUTO` usage records.

- [ ] **Step 7: Commit automatic routing**

```powershell
git add src/sjtuclaw tests/unit
git commit -m "feat: auto-route PDF and presentation skills"
```

### Task 4: Hide default Skills from manual Web insertion and connect paper workflow

**Files:**
- Create: `web/src/skillMenu.ts`
- Create: `web/src/skillMenu.test.ts`
- Modify: `web/src/App.tsx`
- Modify: `skills/paper-mindmap-report-ppt/SKILL.md`
- Modify: `project_structure.md`
- Modify: `README.md`

**Interfaces:**
- Produces: `manualSkillOptions<T extends SkillMenuItem>(skills: readonly T[]): T[]`.
- Consumes: API-provided Skill manifests; removes only IDs `pdf` and `presentations` from the manual menu.

- [ ] **Step 1: Write failing Web helper tests**

```ts
it("hides only default artifact Skills", () => {
  expect(manualSkillOptions([
    { skillId: "pdf" }, { skillId: "presentations" }, { skillId: "course-report" },
  ]).map((item) => item.skillId)).toEqual(["course-report"]);
});
```

- [ ] **Step 2: Run the Web test before implementation**

Run: `Set-Location web; npm run test -- --run skillMenu.test.ts`

Expected: FAIL because `skillMenu.ts` does not exist.

- [ ] **Step 3: Implement the menu filter without changing API data**

Export this pure helper so `App.tsx` keeps its local API type:

```ts
export type SkillMenuItem = { skillId: string };
export const AUTOMATIC_SKILL_IDS = new Set(["pdf", "presentations"]);
export const manualSkillOptions = <T extends SkillMenuItem>(skills: readonly T[]): T[] =>
  skills.filter((skill) => !AUTOMATIC_SKILL_IDS.has(skill.skillId));
```

In `App.tsx`, render `manualSkillOptions(skills)` in the existing add-menu branch. Before editing, inspect the current user diff in `web/src/App.tsx`; preserve unrelated attachment-selection changes exactly.

- [ ] **Step 4: Update the paper workflow and documentation**

Replace the paper Skill's references to unavailable capitalized `Presentations` with built-in `$pdf` and `$presentations`; state they are auto-routed for relevant requests. Update README capability/install notes and `project_structure.md` to list six local Skills and the two resource-script packages.

- [ ] **Step 5: Run Web and documentation checks**

Run: `Set-Location web; npm run test -- --run skillMenu.test.ts; npm run build`

Expected: PASS. Then run: `py -3.12 -m pytest tests/unit/test_skill_registry.py tests/unit/test_skill_service.py -q`

Expected: PASS with `pdf` and `presentations` available.

- [ ] **Step 6: Commit UI and documentation integration**

```powershell
git add web/src/skillMenu.ts web/src/skillMenu.test.ts web/src/App.tsx skills/paper-mindmap-report-ppt README.md project_structure.md
git commit -m "feat: make artifact skills default in composer"
```

### Task 5: Run complete offline verification and manual artifact smoke checks

**Files:**
- Modify: `docs/acceptance.md`

**Interfaces:**
- Consumes: completed resource scripts, auto-routing runtime, and existing approval/download flow.
- Produces: reproducible local commands and explicit optional-renderer coverage status.

- [ ] **Step 1: Add acceptance commands**

Document a workspace-bound request that says `根据这份 PDF 生成 5 页 PPT 汇报并导出` without `$skill`; expected first Run displays `pdf` and `presentations` in selected Skill references. Include a minimal direct script smoke sequence for PDF and PPTX generation.

- [ ] **Step 2: Run Python quality gates**

Run:

```powershell
py -3.12 -m pytest -q
py -3.12 -m ruff check .
py -3.12 -m mypy src tests
py -3.12 -m compileall -q src
```

Expected: all commands pass without loading `.env` or calling a live model.

- [ ] **Step 3: Run Web quality gates**

Run:

```powershell
Set-Location web
npm run test -- --run
npm run build
```

Expected: all tests and production build pass.

- [ ] **Step 4: Run conditional renderer smoke checks**

Run `Get-Command pdftoppm` and `Get-Command soffice`. If a command is absent, run the corresponding unit test that asserts its stable unavailable error and record visual rendering as unverified. If present, run each resource script's `render` command and inspect generated PNGs for clipping and unreadable text.

- [ ] **Step 5: Commit acceptance documentation**

```powershell
git add docs/acceptance.md
git commit -m "docs: add artifact skill acceptance checks"
```
