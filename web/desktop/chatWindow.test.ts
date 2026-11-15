import { describe, expect, it, vi } from "vitest";

import { chatWindowBounds, focusExistingChatWindow } from "./chatWindow";

describe("chat window", () => {
  it("restores and focuses the existing singleton", () => {
    const window = {
      isDestroyed: () => false,
      isMinimized: () => true,
      restore: vi.fn(),
      show: vi.fn(),
      focus: vi.fn(),
    };

    expect(focusExistingChatWindow(window)).toBe(true);
    expect(window.restore).toHaveBeenCalledTimes(1);
    expect(window.show).toHaveBeenCalledTimes(1);
    expect(window.focus).toHaveBeenCalledTimes(1);
  });

  it("declines a destroyed singleton", () => {
    const window = {
      isDestroyed: () => true,
      isMinimized: () => false,
      restore: vi.fn(),
      show: vi.fn(),
      focus: vi.fn(),
    };

    expect(focusExistingChatWindow(window)).toBe(false);
    expect(window.focus).not.toHaveBeenCalled();
  });

  it("keeps the new window inside the pet display work area", () => {
    const bounds = chatWindowBounds(
      { x: 900, y: 650, width: 520, height: 320 },
      { x: 0, y: 0, width: 1280, height: 720 },
    );

    expect(bounds).toEqual({ x: 408, y: 120, width: 480, height: 600 });
  });
});
