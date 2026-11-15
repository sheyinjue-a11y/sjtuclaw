import { describe, expect, it, vi } from "vitest";

import { PetGatewayClient } from "./api";

describe("PetGatewayClient", () => {
  it("sends a request without an active run and steer with an active run", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response("{}", { status: 202 }));
    const client = new PetGatewayClient("http://127.0.0.1:8765", fetcher);

    await client.send({ sessionId: "s1", activeRunId: null }, "hello");
    expect(fetcher.mock.calls[0]?.[0]).toContain("/sessions/s1/requests");

    await client.send({ sessionId: "s1", activeRunId: "r1" }, "more");
    expect(fetcher.mock.calls[1]?.[0]).toContain("/sessions/s1/runs/r1/steer");
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual(
      expect.objectContaining({ text: "more" }),
    );
  });

  it("keeps the draft when the server rejects send", async () => {
    const client = new PetGatewayClient(
      "http://127.0.0.1:8765",
      vi.fn().mockResolvedValue(new Response("{}", { status: 409 })),
    );

    await expect(client.send({ sessionId: "s1", activeRunId: null }, "keep me")).rejects.toThrow();
  });

  it("uses same-origin credentials for every fetch", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ session: { sessionId: "s1" } }), { status: 200 }),
    );
    const client = new PetGatewayClient("http://127.0.0.1:8765", fetcher);

    await client.getActiveSession();

    expect(fetcher).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/v1/ui/active-session",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });
});
