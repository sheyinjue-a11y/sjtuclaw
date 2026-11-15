export type PetState =
  | "idle"
  | "running"
  | "waiting"
  | "failed"
  | "review"
  | "waving"
  | "running-left"
  | "running-right";

export type PetReminder = {
  id: string;
  kind: "input" | "approval" | "completed" | "failed";
  text: string;
};

export type PetViewModel = {
  sessionId: string;
  sessionTitle: string;
  activeRunId: string | null;
  state: Exclude<PetState, "waving" | "running-left" | "running-right">;
  reminder: PetReminder | null;
};

export type AnimationRow = {
  row: number;
  durations: readonly number[];
  frameCount: number;
  loop: boolean;
};

const approvalText = "需要审批，请在工作台处理";
const failedText = "任务失败，请打开工作台查看";
const completedText = "任务已完成";
const inputText = "有新回复，可继续输入";

const animations: Record<PetState, AnimationRow> = {
  idle: { row: 0, durations: [280, 110, 110, 140, 140, 320], frameCount: 6, loop: false },
  "running-right": {
    row: 1,
    durations: [120, 120, 120, 120, 120, 120, 120, 220],
    frameCount: 8,
    loop: true,
  },
  "running-left": {
    row: 2,
    durations: [120, 120, 120, 120, 120, 120, 120, 220],
    frameCount: 8,
    loop: true,
  },
  waving: { row: 3, durations: [140, 140, 140, 280], frameCount: 4, loop: true },
  failed: { row: 5, durations: [140, 140, 140, 140, 140, 140, 140, 240], frameCount: 8, loop: false },
  waiting: { row: 6, durations: [150, 150, 150, 150, 150, 260], frameCount: 6, loop: false },
  running: { row: 7, durations: [120, 120, 120, 120, 120, 220], frameCount: 6, loop: true },
  review: { row: 8, durations: [150, 150, 150, 150, 150, 280], frameCount: 6, loop: false },
};

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function reminder(
  id: string,
  kind: PetReminder["kind"],
  text: string,
): PetReminder {
  return { id, kind, text };
}

export function toPetViewModel(raw: unknown): PetViewModel {
  const session = asRecord(raw);
  const runs = Array.isArray(session.runs) ? session.runs.map(asRecord) : [];
  const active = runs.find((run) => run.status === "started" || run.status === "queued");
  const latest = runs.at(-1);
  const approval = session.pendingApproval !== null && typeof session.pendingApproval === "object";
  const status = typeof latest?.status === "string" ? latest.status : null;
  const state = approval
    ? "waiting"
    : active
      ? "running"
      : status === "failed" || status === "interrupted"
        ? "failed"
        : status === "completed"
          ? "review"
          : "idle";
  const approvalId = typeof asRecord(session.pendingApproval).approvalId === "string"
    ? asRecord(session.pendingApproval).approvalId
    : "";
  const runId = typeof latest?.runId === "string" ? latest.runId : "";
  const modelReminder = approval
    ? reminder(`approval:${approvalId}`, "approval", approvalText)
    : state === "failed"
      ? reminder(`failed:${runId}`, "failed", failedText)
      : state === "review"
        ? reminder(`completed:${runId}`, "completed", completedText)
        : null;

  return {
    sessionId: typeof session.sessionId === "string" ? session.sessionId : "",
    sessionTitle: typeof session.title === "string" ? session.title : "当前会话",
    activeRunId: active && typeof active.runId === "string" ? active.runId : null,
    state,
    reminder: modelReminder,
  };
}

export function reducePetEvent(
  model: PetViewModel,
  event: { type: string; runId: string },
): PetViewModel {
  if (event.type === "approval_required") {
    return {
      ...model,
      state: "waiting",
      reminder: reminder(`approval:${event.runId}`, "approval", approvalText),
    };
  }
  if (event.type === "run_completed") {
    return {
      ...model,
      activeRunId: null,
      state: "review",
      reminder: model.reminder?.kind === "input"
        ? model.reminder
        : reminder(`completed:${event.runId}`, "completed", completedText),
    };
  }
  if (event.type === "run_failed") {
    return {
      ...model,
      activeRunId: null,
      state: "failed",
      reminder: reminder(`failed:${event.runId}`, "failed", failedText),
    };
  }
  if (event.type === "run_cancelled") {
    return { ...model, activeRunId: null, state: "idle", reminder: null };
  }
  if (event.type === "run_started") {
    return { ...model, activeRunId: event.runId, state: "running", reminder: null };
  }
  if (event.type === "assistant_message") {
    return {
      ...model,
      state: "review",
      reminder: reminder(`input:${event.runId}`, "input", inputText),
    };
  }
  return model;
}

export function animationFor(state: PetState): AnimationRow {
  return animations[state];
}

export function dragAnimation(deltaX: number): "running-left" | "running-right" {
  return deltaX < 0 ? "running-left" : "running-right";
}
