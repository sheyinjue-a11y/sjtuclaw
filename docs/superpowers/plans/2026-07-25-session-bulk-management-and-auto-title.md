# Session Bulk Management and Auto Title Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit lasso-based bulk session deletion, LLM-generated first-turn titles, a neutral composer focus state, and a single attachment/Token divider.

**Architecture:** A focused `SessionTitleGenerator` performs the extra no-tool LLM call and sanitizes its output. `RuntimeHost` invokes it after the first Gateway response is persisted but before publishing the terminal event. The React workbench keeps bulk-selection and lasso state locally and reuses existing single-session DELETE endpoints.

**Tech Stack:** Python 3.12, asyncio, FastAPI runtime services, React 19, TypeScript 7, CSS, pytest, Vite.

## Global Constraints

- Only new sessions whose title is exactly `新会话` may receive an automatic title.
- Title input contains only first user text and Assistant Final response.
- Title output is plain text, at most 20 Unicode characters, with a bounded timeout and first-question fallback.
- Manual titles are never overwritten.
- Bulk deletion reuses `DELETE /api/v1/sessions/{session_id}` and reports partial failure.
- No new frontend test framework or unrelated refactor.
- Preserve all unrelated uncommitted work.

---

### Task 1: LLM Session Title Generator

**Files:**
- Create: `src/sjtuclaw/runtime/session_title.py`
- Create: `tests/unit/test_session_title.py`

**Interfaces:**
- Consumes: `LLMClient.complete(LLMRequest)`, `Message`, `LLMUsage`.
- Produces: `SessionTitleResult(title: str, usage: LLMUsage | None)` and `SessionTitleGenerator.generate(user_text: str, assistant_text: str) -> SessionTitleResult`.

- [ ] **Step 1: Write failing generator tests**

Cover normal output, prefix/quote/newline cleanup, 20-character truncation, no tools, empty output fallback, provider exception, and timeout:

```python
result = await generator.generate("如何整理课程重点", "按章节归纳并制作复习清单。")
assert result.title == "课程重点整理方法"
assert result.usage == usage
assert llm.requests[0].tools == ()
assert [message.role for message in llm.requests[0].messages] == [
    MessageRole.SYSTEM,
    MessageRole.USER,
]
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
py -3.12 -m pytest tests/unit/test_session_title.py -q
```

Expected: collection fails because `sjtuclaw.runtime.session_title` does not exist.

- [ ] **Step 3: Implement the generator**

Create:

```python
@dataclass(frozen=True, slots=True)
class SessionTitleResult:
    title: str
    usage: LLMUsage | None


class SessionTitleGenerator:
    def __init__(
        self,
        *,
        llm: LLMClient,
        clock: Clock,
        timeout_seconds: float = 5.0,
        max_chars: int = 20,
    ) -> None: ...

    async def generate(
        self,
        user_text: str,
        assistant_text: str,
    ) -> SessionTitleResult: ...
```

Use `asyncio.wait_for`, a system instruction that treats conversation text as data, and an `LLMRequest` with two messages and no tools. Normalize whitespace, remove paired quotes and `标题：`/`Title:` prefixes, truncate with `…`, and fall back to normalized user text. If the response exists but its content is invalid, retain its usage while using the fallback.

- [ ] **Step 4: Run generator tests and verify GREEN**

Run:

```powershell
py -3.12 -m pytest tests/unit/test_session_title.py -q
```

Expected: all generator tests pass.

---

### Task 2: Runtime Title Integration and Token Accounting

**Files:**
- Modify: `src/sjtuclaw/runtime/service.py`
- Modify: `src/sjtuclaw/bootstrap.py`
- Modify: `tests/unit/test_runtime_host.py`
- Modify: `tests/unit/test_bootstrap_tools.py`

**Interfaces:**
- Consumes: `SessionTitleGenerator.generate(...)` from Task 1 and `accumulate_token_usage(session, usage)`.
- Produces: `RuntimeHost(..., session_titles: SessionTitleGenerator | None = None)` and an updated Session before the terminal event.

- [ ] **Step 1: Replace old title tests with failing LLM-title tests**

Use a separate fake title generator so AgentLoop response ordering remains explicit:

```python
class FakeSessionTitleGenerator:
    async def generate(self, user_text: str, assistant_text: str) -> SessionTitleResult:
        self.calls.append((user_text, assistant_text))
        return SessionTitleResult("课程复习重点整理", LLMUsage(3, 2, 5))
```

Assert:

- first successful Gateway turn passes user text and final response;
- the generated title is stored before `RUN_COMPLETED`;
- title usage is added to Session totals;
- custom titles and second turns do not call the generator;
- generator failure/fallback does not fail the run.

- [ ] **Step 2: Run focused Runtime tests and verify RED**

Run:

```powershell
py -3.12 -m pytest tests/unit/test_runtime_host.py -k "title" -q
```

Expected: constructor or title assertions fail because `session_titles` is not integrated.

- [ ] **Step 3: Integrate title generation**

Add `session_titles` to `RuntimeHost`. Remove `_automatic_session_title`. After `AgentLoop.run_locked` completes in both normal and approval-resume paths:

```python
title_usage = await self._apply_automatic_session_title(request)
if title_usage is not None:
    usage = _combine_llm_usage(usage, title_usage)
```

`_apply_automatic_session_title` must load the completed run and Assistant message, verify the Session title is still `新会话`, verify exactly one completed Gateway run, call the generator, recheck the title, accumulate title usage, and save. Catch generator failures and retain the first-question fallback without failing the run.

Define `_combine_llm_usage(left: LLMUsage, right: LLMUsage) -> LLMUsage` in `service.py`. Sum prompt, completion, and total tokens. Sum cache hit/miss only when both usages provide a complete cache breakdown; otherwise set both cache fields to `None`.

Construct `SessionTitleGenerator(llm=llm, clock=clock)` in `bootstrap.py` and inject it into `RuntimeHost`.

- [ ] **Step 4: Run Runtime and bootstrap tests**

Run:

```powershell
py -3.12 -m pytest tests/unit/test_runtime_host.py tests/unit/test_bootstrap_tools.py -q
```

Expected: all pass.

---

### Task 3: Bulk Session Selection and Deletion

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/styles.css`

**Interfaces:**
- Consumes: existing `api`, `selectSession`, create-session flow, and single-session DELETE endpoint.
- Produces: explicit bulk mode, click selection, lasso selection, select-all, cancel, confirmation, and partial-failure handling.

- [ ] **Step 1: Add pure selection helpers and state**

Add:

```ts
type SelectionRect = {
  left: number;
  top: number;
  right: number;
  bottom: number;
};

function rectsIntersect(left: SelectionRect, right: SelectionRect): boolean {
  return left.left <= right.right
    && left.right >= right.left
    && left.top <= right.bottom
    && left.bottom >= right.top;
}
```

State includes `bulkMode`, `selectedSessionIds`, `selectionRect`, `bulkDeleteConfirm`, and `bulkDeleting`. Refs include the session navigation element, row elements, and pointer-drag baseline.

- [ ] **Step 2: Implement batch toolbar and accessible row selection**

Render “批量管理” beside “最近会话”. In bulk mode render selected count, full select, delete, and cancel. Session rows use `aria-pressed`, a visible check marker, and click-to-toggle. Suppress session navigation and context menus while bulk mode is active. Add `Escape` cleanup.

- [ ] **Step 3: Implement lasso**

Start only when pointer down does not originate from a button. Use pointer capture and a 5px movement threshold. Store the drag box in navigation-content coordinates, render a clipped translucent rectangle, and replace the selection with intersecting visible rows.

- [ ] **Step 4: Implement confirmed partial-success deletion**

After confirmation:

```ts
const results = await Promise.allSettled(
  ids.map((sessionId) => api<void>(`/sessions/${sessionId}`, { method: "DELETE" })),
);
```

Remove fulfilled IDs, retain failed IDs selected, report failure count, close stale SSE if the active Session was deleted, select the first remaining Session, or create one `新会话` when none remain.

- [ ] **Step 5: Build the frontend**

Run:

```powershell
Set-Location web
npm run build
```

Expected: TypeScript and Vite succeed.

---

### Task 4: Focus Styling, Divider Cleanup, Structure Docs, and Verification

**Files:**
- Modify: `web/src/styles.css`
- Modify: `project_structure.md`
- Test: existing Python and frontend suites.

**Interfaces:**
- Consumes: class names introduced in Task 3.
- Produces: neutral focus style, one Token divider, documented title module, and verified page behavior.

- [ ] **Step 1: Apply scoped styles**

Add explicit neutral composer focus styling:

```css
.composer:focus-within {
  border-color: #c8d0dc;
  box-shadow: 0 8px 22px rgba(31, 41, 55, .07);
}

.composer textarea:focus,
.composer textarea:focus-visible {
  border: 0;
  outline: none;
  box-shadow: none;
}

.detail-section.attachments {
  margin-bottom: 0;
  padding-bottom: 0;
  border-bottom: 0;
}
```

Style the batch toolbar, selected rows, check markers, and lasso rectangle with neutral tones and a restrained danger action.

- [ ] **Step 2: Update project structure**

Add `runtime/session_title.py` to the Runtime tree and module mapping. Do not change unrelated documentation.

- [ ] **Step 3: Run focused and full verification**

Run:

```powershell
py -3.12 -m pytest tests/unit/test_session_title.py tests/unit/test_runtime_host.py tests/unit/test_bootstrap_tools.py -q
py -3.12 -m pytest --ignore=tests/live -q
Set-Location web
npm run build
```

Expected: zero failures. Existing third-party deprecation warnings may remain.

- [ ] **Step 4: Browser QA**

Start:

```powershell
py -3.12 -m sjtuclaw.entrypoints.gateway
```

Verify desktop and narrow layouts:

- bulk mode enter/cancel and `Esc`;
- click, select-all, and rectangle selection;
- confirmation dialog and active-session deletion;
- partial failure keeps failed rows;
- composer focus has no blue rectangle;
- attachment section has no redundant bottom line;
- first completed new Session receives the generated title;
- no overflow, overlap, duplicate cards, or console errors.
