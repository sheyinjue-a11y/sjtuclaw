import { describe, expect, it, vi } from "vitest";
import { chooseInitialSession, readActiveSession, writeActiveSession } from "./activeSession";

describe("active session sync", () => {
  it("prefers the gateway active session and falls back to the newest list item", () => {
    const sessions = [{ sessionId: "new" }, { sessionId: "old" }];

    expect(chooseInitialSession("old", sessions)).toBe("old");
    expect(chooseInitialSession("missing", sessions)).toBe("new");
    expect(chooseInitialSession(null, [])).toBeNull();
  });

  it("uses the fixed active-session routes", async () => {
    const api = vi.fn().mockResolvedValue({ session: { sessionId: "s1" } });

    expect((await readActiveSession(api)).sessionId).toBe("s1");
    expect(api).toHaveBeenLastCalledWith("/ui/active-session");
    await writeActiveSession(api, "s2");

    expect(api).toHaveBeenLastCalledWith(
      "/ui/active-session",
      expect.objectContaining({ method: "PUT" }),
    );
  });
});
