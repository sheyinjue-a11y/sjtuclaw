import { describe, expect, it, vi } from "vitest";

import {
  PetSessionController,
  type PetGatewayClientLike,
  type PetEventSource,
} from "./controller";

class FakeEventSource implements PetEventSource {
  closed = false;
  onerror: (() => void) | null = null;
  private readonly listeners = new Map<string, Array<(event: MessageEvent) => void>>();

  constructor(readonly url: string) {}

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data = "secret event payload"): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data } as MessageEvent);
    }
  }
}

function clientWith(
  overrides: Partial<PetGatewayClientLike> = {},
): PetGatewayClientLike {
  return {
    getActiveSession: vi.fn().mockResolvedValue({ sessionId: "s1", title: "one" }),
    getSession: vi.fn().mockResolvedValue({
      sessionId: "s1",
      title: "one",
      runs: [{ runId: "r1", status: "started" }],
      pendingApproval: null,
    }),
    eventUrl: (sessionId: string, runId: string) => `/sessions/${sessionId}/runs/${runId}/events`,
    send: vi.fn().mockResolvedValue(undefined),
    requestBrowserToken: vi.fn().mockResolvedValue("b".repeat(43)),
    ...overrides,
  };
}

describe("PetSessionController", () => {
  it("closes the previous EventSource when active Session changes", async () => {
    const sources: FakeEventSource[] = [];
    const client = clientWith({
      getActiveSession: vi.fn()
        .mockResolvedValueOnce({ sessionId: "s1", title: "one" })
        .mockResolvedValueOnce({ sessionId: "s2", title: "two" }),
      getSession: vi.fn(async (sessionId: string) => ({
        sessionId,
        title: sessionId,
        runs: [{ runId: sessionId === "s1" ? "r1" : "r2", status: "started" }],
        pendingApproval: null,
      })),
    });
    const controller = new PetSessionController({
      client,
      eventSource: (url) => {
        const source = new FakeEventSource(url);
        sources.push(source);
        return source;
      },
      pollMilliseconds: 1_000,
    });

    await controller.refresh();
    await controller.refresh();

    expect(sources[0]?.closed).toBe(true);
    expect(sources[1]?.url).toContain("/sessions/s2/runs/r2/events");
    controller.stop();
    expect(sources[1]?.closed).toBe(true);
  });

  it("re-reads the active session and snapshot before sending", async () => {
    const client = clientWith({
      getActiveSession: vi.fn().mockResolvedValue({ sessionId: "s2", title: "two" }),
      getSession: vi.fn().mockResolvedValue({
        sessionId: "s2",
        title: "two",
        runs: [{ runId: "r2", status: "started" }],
        pendingApproval: null,
      }),
    });
    const controller = new PetSessionController({ client, eventSource: () => new FakeEventSource("/events") });

    await controller.send("more");

    expect(client.send).toHaveBeenCalledWith({ sessionId: "s2", activeRunId: "r2" }, "more");
  });

  it("publishes generic event state only and restores connected after a successful poll", async () => {
    const sources: FakeEventSource[] = [];
    const listener = vi.fn();
    const controller = new PetSessionController({
      client: clientWith(),
      eventSource: (url) => {
        const source = new FakeEventSource(url);
        sources.push(source);
        return source;
      },
    });

    controller.start(listener);
    await controller.refresh();
    sources[0]?.emit("assistant_message");

    expect(JSON.stringify(listener.mock.lastCall?.[0])).not.toContain("secret event payload");
    expect(listener.mock.lastCall?.[0]).toEqual(
      expect.objectContaining({ connected: true, model: expect.objectContaining({ reminder: expect.any(Object) }) }),
    );

    sources[0]?.onerror?.();
    expect(listener.mock.lastCall?.[0]).toEqual(expect.objectContaining({ connected: false }));
    await controller.refresh();
    expect(listener.mock.lastCall?.[0]).toEqual(expect.objectContaining({ connected: true }));
    controller.stop();
  });
});
