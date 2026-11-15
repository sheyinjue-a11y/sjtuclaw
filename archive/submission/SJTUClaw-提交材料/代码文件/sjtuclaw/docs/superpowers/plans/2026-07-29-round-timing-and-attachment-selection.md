# Round Timing and Attachment Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Let users clear all attachments selected for the next request and show a completed round's duration above its assistant reply only when it is at least one second.

**Architecture:** Keep attachment clearing local to React state. Reuse SessionDetail.runs, whose Gateway payload already supplies assistantMessageId and durationMs, to map completed runs to their final assistant message. Put the threshold and second-formatting rule in a tiny pure web utility so Vitest covers it independently of the large chat component.

**Tech Stack:** React 19, TypeScript 7, Vite 8, Vitest 4, existing Gateway session payload.

## Global Constraints

- Do not delete attachment metadata, attachment files, or historical message attachment references.
- Do not add or change Gateway APIs, persistence schemas, or Python runtime behavior.
- Show round time only for a completed run with both assistantMessageId and durationMs and only when durationMs >= 1000.
- Use whole elapsed seconds rounded down; 23_999 milliseconds displays as 23s.
- Do not render a placeholder, explanation, or empty space when round time is unavailable or shorter than one second.
- Keep archived sessions read-only.

---

### Task 1: Add a tested round-duration presentation rule

**Files:**

- Create: web/src/roundTiming.ts
- Create: web/src/roundTiming.test.ts

**Interfaces:**

- Consumes: persisted Run durationMs, typed as number | null.
- Produces: formatRoundDuration(durationMs: number | null): string | null. Null means caller renders nothing.

- [ ] **Step 1: Write the failing test**

~~~
import { describe, expect, it } from "vitest";
import { formatRoundDuration } from "./roundTiming";

describe("formatRoundDuration", () => {
  it("hides unavailable and sub-second durations", () => {
    expect(formatRoundDuration(null)).toBeNull();
    expect(formatRoundDuration(999)).toBeNull();
  });

  it("shows whole seconds for durations of at least one second", () => {
    expect(formatRoundDuration(1_000)).toBe("1s");
    expect(formatRoundDuration(23_999)).toBe("23s");
  });
});
~~~

- [ ] **Step 2: Run the focused test and verify it fails because the module does not exist**

Run: npm test -- --run src/roundTiming.test.ts

Expected: FAIL with a module-resolution error for ./roundTiming.

- [ ] **Step 3: Write the minimal formatter**

~~~
export function formatRoundDuration(durationMs: number | null): string | null {
  if (durationMs === null || durationMs < 1_000) return null;
  return String(Math.floor(durationMs / 1_000)) + "s";
}
~~~

- [ ] **Step 4: Run the focused test and verify it passes**

Run: npm test -- --run src/roundTiming.test.ts

Expected: PASS with 2 tests.

- [ ] **Step 5: Commit the isolated formatter and tests**

~~~
git add -- web/src/roundTiming.ts web/src/roundTiming.test.ts
git commit -m "feat: format visible round durations"
~~~

### Task 2: Render completed round time above final assistant replies

**Files:**

- Modify: web/src/App.tsx:1-20 to import formatRoundDuration.
- Modify: web/src/App.tsx:630-634 to derive an assistant-message-to-duration map from detail.runs.
- Modify: web/src/App.tsx:2167-2184 to render the compact time line before an assistant message group.
- Modify: web/src/styles.css:600-607 to style the compact time line.

**Interfaces:**

- Consumes: formatRoundDuration(durationMs) from web/src/roundTiming.ts.
- Consumes: Run.status, Run.assistantMessageId, and Run.durationMs from the existing SessionDetail payload.
- Produces: a round-duration element immediately before the assistant bubble only when the formatter returns a label.

- [ ] **Step 1: Keep the existing test boundary explicit**

The web project has only pure Vitest unit tests and no React render-test dependency. The formatter test from Task 1 owns the threshold rule; Task 2 uses TypeScript build plus manual browser checks. Do not add a test library for this UI-only condition.

- [ ] **Step 2: Derive the completed duration map in App**

Place this near activeRun so it recalculates from the current detail snapshot:

~~~
const roundDurationByAssistantMessageId = new Map(
  (detail?.runs ?? []).flatMap((run) => (
    run.status === "completed"
      && run.assistantMessageId !== null
      && run.durationMs !== null
      ? [[run.assistantMessageId, run.durationMs] as const]
      : []
  )),
);
~~~

- [ ] **Step 3: Render the time line only when it has a label**

In the historic assistant-message branch, compute the label from the message ID and add it before AssistantBubble:

~~~
const roundDuration = formatRoundDuration(
  roundDurationByAssistantMessageId.get(message.messageId) ?? null,
);

{roundDuration && <div className="round-duration">已处理 {roundDuration}</div>}
~~~

Keep tool-call rendering unchanged. Do not add a time line for liveItems, failed runs, cancelled runs, or unmatched historic messages.

- [ ] **Step 4: Add restrained styles**

Add round-duration beside timeline styles: muted text, small font, compact vertical margin, and tabular numerals. It remains a non-interactive information line and uses no unexplained chevron.

- [ ] **Step 5: Build and manually verify message placement**

Run: npm run build

Manual browser checks:

1. Send a request taking at least one second; confirm 已处理 Ns appears immediately above the persisted assistant response.
2. Open an older message with no matching Run; confirm no blank time row exists.
3. Confirm the existing top status timer still updates while a live reply streams.

Expected: build succeeds; only eligible final replies have the compact time line.

- [ ] **Step 6: Commit the round-time rendering**

~~~
git add -- web/src/App.tsx web/src/styles.css
git commit -m "feat: show completed round durations"
~~~

### Task 3: Add an explicit right-sidebar clear-selection action

**Files:**

- Modify: web/src/App.tsx:2460-2472 to add the selection count and clear button in the attachment title row.
- Modify: web/src/styles.css:872-876 to lay out and focus-style the attachment title action.

**Interfaces:**

- Consumes: existing selectedAttachmentIds, setSelectedAttachmentIds, and isArchivedSession state in App.
- Produces: a local onClick that sets selectedAttachmentIds to an empty array; it makes no API call.

- [ ] **Step 1: Preserve current single-item behavior**

The attachment row already calls toggleAttachment(attachmentId). Keep that handler unchanged so users can still select or deselect one file at a time.

- [ ] **Step 2: Add the explicit clear action**

Replace the attachment title's standalone count with a small action group. Show 已选 N and render this button only when selectedAttachmentIds.length > 0:

~~~
<button
  className="attachment-selection__clear"
  type="button"
  onClick={() => setSelectedAttachmentIds([])}
  disabled={isArchivedSession}
>
  取消本轮选择
</button>
~~~

Keep total attachment count accessible with an aria-label on the existing count element.

- [ ] **Step 3: Style the title action without changing file-row layout**

Use a flex action group in the title row. Make the clear control a muted text button with a visible focus-visible outline. Retain disabled appearance. Do not add per-file delete buttons.

- [ ] **Step 4: Build and manually verify attachment safety**

Run: npm run build

Manual browser checks:

1. Select two ready attachments in the right sidebar; confirm 已选 2 and 取消本轮选择 appear.
2. Click clear; confirm selected styling and composer chips disappear.
3. Confirm the files remain in the sidebar and selecting one still works.
4. Archive a session; confirm the action cannot modify selection in the read-only view.

Expected: build succeeds; clear selection never deletes an attachment or sends a request.

- [ ] **Step 5: Run all web verification and commit**

Run: npm test -- --run

Run: npm run build

Expected: all Vitest tests and the production TypeScript/Vite build pass.

~~~
git add -- web/src/App.tsx web/src/styles.css
git commit -m "feat: clear selected session attachments"
~~~

## Plan self-review

- Spec coverage: Task 1 covers one-second hide rule. Task 2 covers persisted completed Run mapping, placement, and no-placeholder behavior. Task 3 covers local-only attachment clearing and archived-session read-only behavior.
- Placeholder scan: no unresolved placeholder or deferred implementation step remains.
- Type consistency: Task 1 exports formatRoundDuration(number | null): string | null. Task 2 imports exactly that name. Task 2 consumes existing Run fields from SessionDetail. Task 3 consumes existing React state without API changes.
