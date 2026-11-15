import { describe, expect, it } from "vitest";
import { animationFor, dragAnimation, reducePetEvent, toPetViewModel } from "./state";

describe("pet state", () => {
  it("maps public run states and approval without retaining tool data", () => {
    const model = toPetViewModel({
      sessionId: "s1",
      title: "演示",
      runs: [{ runId: "r1", status: "started" }],
      pendingApproval: { approvalId: "a1", toolName: "run_command", preview: "secret" },
      messages: [{ toolName: "write_file", content: "secret output" }],
    });

    expect(model).toEqual(expect.objectContaining({ sessionId: "s1", state: "waiting" }));
    expect(JSON.stringify(model)).not.toMatch(/run_command|write_file|secret/);
  });

  it("maps terminal events to one generic latest reminder", () => {
    const base = toPetViewModel({ sessionId: "s1", title: "演示", runs: [], pendingApproval: null });
    const completed = reducePetEvent(base, { type: "run_completed", runId: "r1" });

    expect(completed.state).toBe("review");
    expect(completed.reminder?.kind).toBe("completed");
    expect(reducePetEvent(completed, { type: "run_cancelled", runId: "r1" }).state).toBe("idle");
    expect(reducePetEvent(base, { type: "approval_required", runId: "r1" }).state).toBe("waiting");
    expect(reducePetEvent(base, { type: "assistant_message", runId: "r1" }).reminder?.kind).toBe("input");
  });

  it("keeps an input reminder when its run completes", () => {
    const base = toPetViewModel({ sessionId: "s1", title: "演示", runs: [], pendingApproval: null });
    const input = reducePetEvent(base, { type: "assistant_message", runId: "r1" });

    expect(reducePetEvent(input, { type: "run_completed", runId: "r1" }).reminder).toEqual(
      input.reminder,
    );
  });

  it("uses exact v2 rows and horizontal drag direction", () => {
    expect(animationFor("idle")).toMatchObject({ row: 0, frameCount: 6 });
    expect(animationFor("waiting")).toMatchObject({ row: 6, frameCount: 6 });
    expect(animationFor("review")).toMatchObject({ row: 8, frameCount: 6 });
    expect(dragAnimation(-1)).toBe("running-left");
    expect(dragAnimation(1)).toBe("running-right");
  });

  it("keeps resting states static and transient states animated", () => {
    expect(animationFor("idle").loop).toBe(false);
    expect(animationFor("waiting").loop).toBe(false);
    expect(animationFor("review").loop).toBe(false);
    expect(animationFor("failed").loop).toBe(false);
    expect(animationFor("waving").loop).toBe(true);
    expect(animationFor("running").loop).toBe(true);
    expect(animationFor("running-left").loop).toBe(true);
    expect(animationFor("running-right").loop).toBe(true);
  });
});
