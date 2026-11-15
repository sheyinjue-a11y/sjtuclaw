# Varka Resting Motion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Varka static in normal non-running states while preserving startup, running, and drag feedback animations.

**Architecture:** The pet state module remains the source of truth for atlas rows and frame timing, with an added explicit `loop` flag per state. The React sprite component uses that flag to decide whether a frame timer exists; rendering, state mapping, and Electron interaction stay unchanged.

**Tech Stack:** TypeScript, React 19, Vitest, Testing Library, Vite, Electron desktop build.

## Global Constraints

- Keep Varka atlas rows, session state mapping, reminders, Quick Chat, and Electron IPC unchanged.
- `idle`, `waiting`, `review`, and `failed` always show frame 0.
- Only `waving`, `running`, `running-left`, and `running-right` advance frames; `waving` remains bounded by the existing eight-second greeting.
- Respect `prefers-reduced-motion` by rendering frame 0 as before.
- Do not add dependencies or modify `project_structure.md`; no directories move or are added.

---

### Task 1: Make Resting States Static

**Files:**
- Modify: `web/src/pet/state.ts:25-52`
- Modify: `web/src/pet/state.test.ts:36-44`
- Modify: `web/src/pet/PetApp.tsx:50-75`
- Modify: `web/src/pet/PetApp.test.tsx:1-147`

**Interfaces:**
- Consumes: `animationFor(state: PetState): AnimationRow`.
- Produces: `AnimationRow.loop: boolean`; a non-looping sprite never schedules a frame advance and remains on frame 0.

- [ ] **Step 1: Add failing state-policy tests**

In `web/src/pet/state.test.ts`, extend the atlas-state test with explicit loop expectations:

```ts
expect(animationFor("idle").loop).toBe(false);
expect(animationFor("waiting").loop).toBe(false);
expect(animationFor("review").loop).toBe(false);
expect(animationFor("failed").loop).toBe(false);
expect(animationFor("waving").loop).toBe(true);
expect(animationFor("running").loop).toBe(true);
expect(animationFor("running-left").loop).toBe(true);
expect(animationFor("running-right").loop).toBe(true);
```

- [ ] **Step 2: Verify the state-policy test fails**

Run from `web`:

```powershell
npm test -- --run src/pet/state.test.ts
```

Expected: FAIL because `AnimationRow` has no `loop` property.

- [ ] **Step 3: Add failing renderer-timer tests**

In `web/src/pet/PetApp.test.tsx`, use `vi.useFakeTimers()` and `act()` to pass the eight-second greeting. Assert a static idle model stays on `pet-sprite--frame-0` after two seconds, while a running model changes from frame 0 after its 120ms duration:

```ts
expect(sprite.className).toContain("pet-sprite--frame-0");
act(() => vi.advanceTimersByTime(2_000));
expect(sprite.className).toContain("pet-sprite--frame-0");
```

- [ ] **Step 4: Verify the renderer-timer test fails**

Run from `web`:

```powershell
npm test -- --run src/pet/PetApp.test.tsx
```

Expected: FAIL because idle still advances through its six frames.

- [ ] **Step 5: Add the minimal animation policy**

In `web/src/pet/state.ts`, extend the exact interface and all entries:

```ts
export type AnimationRow = {
  row: number;
  durations: readonly number[];
  frameCount: number;
  loop: boolean;
};
```

Set `loop: false` on `idle`, `waiting`, `review`, and `failed`; set `loop: true` on `waving`, `running`, `running-left`, and `running-right`. Keep every existing row, duration, and frame count unchanged.

In `web/src/pet/PetApp.tsx`, prevent the existing timer effect from creating a timeout for non-looping animations:

```ts
if (isReducedMotion || !animation.loop) return undefined;
```

The reset effect continues to set frame 0 whenever the state changes.

- [ ] **Step 6: Verify focused tests pass**

Run from `web`:

```powershell
npm test -- --run src/pet/state.test.ts src/pet/PetApp.test.tsx
```

Expected: PASS, including static idle and advancing running coverage.

- [ ] **Step 7: Run regression build verification**

Run from `web`:

```powershell
npm test -- --run
npm run build
npm run build:desktop
```

Expected: all Vitest suites pass and both production bundles compile.

- [ ] **Step 8: Inspect and commit the focused change**

Run from repository root:

```powershell
git diff --check
git status --short
git add web/src/pet/state.ts web/src/pet/state.test.ts web/src/pet/PetApp.tsx web/src/pet/PetApp.test.tsx
git commit -m "fix: keep Varka resting states static"
```

Expected: only the four listed production/test files are committed; the design and plan documents remain separate commits.

