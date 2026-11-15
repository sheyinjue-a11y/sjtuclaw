import { describe, expect, it, vi } from "vitest";

import { setPetMouseInteractivity } from "./mouseEvents";

describe("pet mouse interaction", () => {
  it("passes forward only while restoring click-through", () => {
    const window = { setIgnoreMouseEvents: vi.fn() };

    setPetMouseInteractivity(window, true);
    expect(window.setIgnoreMouseEvents).toHaveBeenCalledWith(false);

    setPetMouseInteractivity(window, false);
    expect(window.setIgnoreMouseEvents).toHaveBeenLastCalledWith(true, { forward: true });
  });
});
