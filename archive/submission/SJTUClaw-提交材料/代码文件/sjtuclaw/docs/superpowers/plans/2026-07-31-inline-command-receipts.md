# Inline command receipts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Render transient slash-command input and its receipt together in the current Web conversation timeline.

**Architecture:** Keep command timeline state entirely in React.  A small pure
`commandTimeline.ts` module owns creation, completion, failure, and optional
cross-Session relocation of a transient command item.  `App.tsx` records a
pending item before posting `/commands`, updates the item with the outcome, and
renders the item pair where detached command cards are rendered today.

**Tech Stack:** React, TypeScript, Vitest, Vite.

## Global Constraints

- Do not change Session persistence, Gateway API contracts, Runtime events, or
  slash-command execution semantics.
- A browser refresh clears all transient command entries.
- Keep LLM Tool and Skill timeline rendering unchanged.
- Do not parse command text to create a URL or change Host Tool-resource
  rendering.
- Update `project_structure.md` for every new source/test module.

---

### Task 1: Model transient command timeline state

**Files:**

- Create: `web/src/commandTimeline.ts`
- Create: `web/src/commandTimeline.test.ts`
- Modify: `project_structure.md`

**Interfaces:**

- Produces `CommandReceipt`, `TransientCommandItem`, `newTransientCommand`, and
  `completeTransientCommand` from
  `commandTimeline.ts`.
- `CommandReceipt` is `{ command: string; lines: string[]; isError: boolean }`.
- `TransientCommandItem` is `{ id: string; command: string; receipt: CommandReceipt | null }`.
- Completion helpers take immutable
  `Record<string, readonly TransientCommandItem[]>` state and return a new
  record.  A target Session ID different from the source moves the same item to
  the target Session while preserving its position relative to its receipt.

- [x] **Step 1: Write the failing unit tests**

```ts
import { describe, expect, it } from "vitest";
import {
  completeTransientCommand,
  failTransientCommand,
  newTransientCommand,
} from "./commandTimeline";

describe("transient command timeline", () => {
  it("keeps a command and its successful receipt adjacent", () => {
    const pending = newTransientCommand("command_1", "/help");
    const state = { session_1: [pending] };
    expect(completeTransientCommand(state, "session_1", "session_1", "command_1", {
      command: "/help", lines: ["available commands"], isError: false,
    })).toEqual({
      session_1: [{
        id: "command_1", command: "/help",
        receipt: { command: "/help", lines: ["available commands"], isError: false },
      }],
    });
  });

  it("moves the triggering item and its receipt to a selected Session", () => {
    const state = { session_old: [newTransientCommand("command_2", "/new")] };
    expect(completeTransientCommand(state, "session_old", "session_new", "command_2", {
      command: "/new", lines: ["created"], isError: false,
    })).toEqual({
      session_old: [],
      session_new: [{
        id: "command_2", command: "/new",
        receipt: { command: "/new", lines: ["created"], isError: false },
      }],
    });
  });

  it("starts with no entries after a fresh browser state", () => {
    expect({}).toEqual({});
  });
});
```

- [x] **Step 2: Run the test to verify it fails**

Run:

```powershell
Set-Location web
npm test -- --run src/commandTimeline.test.ts
```

Expected: FAIL because `./commandTimeline` does not exist.

- [x] **Step 3: Implement the pure immutable helpers**

```ts
export type CommandReceipt = {
  command: string;
  lines: string[];
  isError: boolean;
};

export type TransientCommandItem = {
  id: string;
  command: string;
  receipt: CommandReceipt | null;
};

export function newTransientCommand(id: string, command: string): TransientCommandItem {
  return { id, command, receipt: null };
}
```

Implement completion through one private helper: remove the matching item from
its source Session, append the item with the supplied receipt to its target
Session, and preserve all unrelated Session lists without mutation.  Do not
write to `localStorage` or any API.

Add `commandTimeline.ts / commandTimeline.test.ts` to the Web tree and describe
them as transient slash-command timeline state in `project_structure.md`.

- [x] **Step 4: Run the focused tests to verify they pass**

Run:

```powershell
Set-Location web
npm test -- --run src/commandTimeline.test.ts
```

Expected: PASS; the source Session keeps unrelated items, same-Session outcomes
remain adjacent, target-Session outcomes relocate the pair, and no stored state
exists for a fresh page.

- [x] **Step 5: Commit the model and tests**

```powershell
git add web/src/commandTimeline.ts web/src/commandTimeline.test.ts project_structure.md
git commit -m "feat: model transient command timeline"
```

### Task 2: Render command input and receipts in the conversation flow

**Files:**

- Modify: `web/src/App.tsx`
- Modify: `web/src/commandTimeline.ts`
- Test: `web/src/commandTimeline.test.ts`

**Interfaces:**

- Consumes all exports from `commandTimeline.ts` from Task 1.
- Replaces `Record<string, CommandCard[]>` state with
  `Record<string, readonly TransientCommandItem[]>` state.
- Adds `failTransientCommand`, a thin typed wrapper over completion that creates
  `{ command, lines: [messageOf(error)], isError: true }` for a failed HTTP
  request.

- [x] **Step 1: Extend the failing tests for rendering state**

```ts
it("retains a pending command until its local error receipt arrives", () => {
  const state = { session_1: [newTransientCommand("command_3", "/unknown")] };
  const next = failTransientCommand(
    state, "session_1", "command_3", "/unknown", "request failed",
  );
  expect(next.session_1[0].receipt?.isError).toBe(true);
  expect(next.session_1[0].command).toBe("/unknown");
});
```

- [x] **Step 2: Run the test to verify it fails**

Run:

```powershell
Set-Location web
npm test -- --run src/commandTimeline.test.ts
```

Expected: FAIL because `failTransientCommand` is not exported yet.

- [x] **Step 3: Integrate state creation, outcome completion, and rendering**

In `App.tsx`:

1. Add `failTransientCommand()` to `commandTimeline.ts` as a wrapper around
   `completeTransientCommand()` that keeps the item in its source Session and
   gives it `{ command, lines: [error], isError: true }`.
2. Import the Task 1 types/helpers and replace `CommandCard`/`commandCards` with
   `TransientCommandItem`/`commandTimeline` state.
3. At the start of `executeCommand()`, create `anchorId = crypto.randomUUID()`
   and append `newTransientCommand(anchorId, command)` to the source Session
   before calling the API.
4. When the response returns, call `completeTransientCommand()` with source
   Session, `selectSessionId ?? sourceSession`, the anchor, and the existing
   `response.outcome` values.  On the catch path, call `failTransientCommand()`
   with the source Session, anchor, command, and existing `messageOf(caught)`
   text.
5. In the history renderer, replace the detached `commandCards` map with a map
   over `commandTimeline[selectedSessionId]`.  Render a user-style article for
   `item.command`; immediately render one `command-card` below it.  Pending
   entries render the existing card style with `运行中` and `等待命令结果`.
6. Do not add state hydration.  `useState({})` is the refresh-clearing behavior.

- [x] **Step 4: Run focused and full Web verification**

Run:

```powershell
Set-Location web
npm test -- --run src/commandTimeline.test.ts
npm test -- --run
npm run build
```

Expected: all Web tests pass and TypeScript builds.  Verify manually in the
running page that `/help` shows a command entry followed immediately by its
receipt, a failed command shows its error receipt in the same place, and a
browser refresh clears both transient entries.

- [x] **Step 5: Commit the UI integration**

```powershell
git add web/src/App.tsx web/src/commandTimeline.test.ts
git commit -m "feat: inline transient command receipts"
```

## Plan self-review

- Spec coverage: Task 1 owns transient state, Session scoping, relocation, and
  refresh-clearing semantics; Task 2 owns Web submission, error handling, and
  presentation.  No persistence, API, runtime, Tool, Skill, or resource behavior
  is changed.
- Placeholder scan: no unfinished implementation markers remain.
- Type consistency: Task 1's `CommandReceipt` and `TransientCommandItem` are the
  only state types consumed by Task 2; helper parameters identify source Session,
  target Session, and the one generated anchor ID consistently.
