import { describe, expect, it, vi } from "vitest";

import { ChatSessionController, type ChatSnapshot } from "./chatController";

describe("ChatSessionController", () => {
  it("publishes the active session and retains it while reconnecting", async () => {
    const client = {
      getActiveSession: vi.fn().mockResolvedValue({ sessionId: "s1", title: "最近会话" }),
      getSession: vi.fn()
        .mockResolvedValueOnce({
          sessionId: "s1",
          title: "最近会话",
          runs: [],
          messages: [{ messageId: "m1", role: "assistant", content: "完成", sequenceNumber: 1 }],
        })
        .mockRejectedValueOnce(new Error("offline")),
      send: vi.fn(),
    };
    const controller = new ChatSessionController({ client });
    const snapshots: unknown[] = [];
    controller.subscribe((snapshot) => snapshots.push(snapshot));

    await controller.refresh();
    await controller.refresh();

    expect(snapshots).toEqual([
      {
        connected: true,
        sessionId: "s1",
        sessionTitle: "最近会话",
        messages: [{ id: "m1", role: "assistant", content: "完成" }],
      },
      {
        connected: false,
        sessionId: "s1",
        sessionTitle: "最近会话",
        messages: [{ id: "m1", role: "assistant", content: "完成" }],
      },
    ]);
  });

  it("sends to the latest active session and refreshes", async () => {
    const client = {
      getActiveSession: vi.fn().mockResolvedValue({ sessionId: "s2", title: "最新" }),
      getSession: vi.fn().mockResolvedValue({
        sessionId: "s2",
        title: "最新",
        runs: [{ runId: "r2", status: "started" }],
        messages: [],
      }),
      send: vi.fn().mockResolvedValue(undefined),
    };
    const controller = new ChatSessionController({ client });

    await controller.send("继续");

    expect(client.send).toHaveBeenCalledWith(
      { sessionId: "s2", activeRunId: "r2" },
      "继续",
    );
    expect(client.getSession).toHaveBeenCalledTimes(2);
  });

  it("ignores a stale refresh after stop and restart", async () => {
    let resolveFirst!: (value: { sessionId: string; title: string }) => void;
    const first = new Promise<{ sessionId: string; title: string }>((resolve) => {
      resolveFirst = resolve;
    });
    const client = {
      getActiveSession: vi.fn()
        .mockReturnValueOnce(first)
        .mockResolvedValueOnce({ sessionId: "new", title: "新会话" }),
      getSession: vi.fn().mockImplementation((sessionId: string) => Promise.resolve({
        sessionId,
        title: sessionId === "new" ? "新会话" : "旧会话",
        runs: [],
        messages: [{ messageId: sessionId, role: "assistant", content: sessionId, sequenceNumber: 1 }],
      })),
      send: vi.fn(),
    };
    const controller = new ChatSessionController({ client, pollMilliseconds: 60_000 });
    const snapshots: ChatSnapshot[] = [];

    controller.start((snapshot) => snapshots.push(snapshot));
    controller.stop();
    controller.start((snapshot) => snapshots.push(snapshot));
    await vi.waitFor(() => expect(snapshots.at(-1)?.sessionId).toBe("new"));
    resolveFirst({ sessionId: "old", title: "旧会话" });
    await Promise.resolve();
    await Promise.resolve();
    controller.stop();

    expect(snapshots.map((snapshot) => snapshot.sessionId)).toEqual(["new"]);
  });
});
