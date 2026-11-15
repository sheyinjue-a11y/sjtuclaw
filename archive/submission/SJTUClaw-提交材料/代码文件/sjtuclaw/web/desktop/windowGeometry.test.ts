import { describe, expect, it } from "vitest";
import { chatSideForBounds, clampWindowPosition, toIntegerPoint } from "./windowGeometry";

const workArea = { x: 0, y: 0, width: 1920, height: 1040 };

describe("window geometry", () => {
  it("clamps a restored window into the current work area", () => {
    expect(clampWindowPosition({ x: -900, y: 2000 }, { width: 520, height: 320 }, workArea))
      .toEqual({ x: 0, y: 720 });
  });

  it("places chat to the right only at the left edge", () => {
    expect(chatSideForBounds({ x: 0, width: 520 }, workArea)).toBe("right");
    expect(chatSideForBounds({ x: 800, width: 520 }, workArea)).toBe("left");
  });

  it("rounds pointer coordinates before they reach native window APIs", () => {
    expect(toIntegerPoint(12.6, 34.4)).toEqual({ x: 13, y: 34 });
    expect(toIntegerPoint(Number.NaN, 34)).toBeNull();
  });
});
