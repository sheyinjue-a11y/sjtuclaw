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
