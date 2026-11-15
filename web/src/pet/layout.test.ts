import { describe, expect, it } from "vitest";

import {
  PET_SPRITE_WIDTH,
  PET_WINDOW_WIDTH,
  QUICK_CHAT_WIDTH,
  REMINDER_WIDTH,
  chatLayout,
} from "./layout";

describe("Quick Chat layout", () => {
  it("places Quick Chat to the right of the sprite at the screen left edge", () => {
    const layout = chatLayout("right");

    expect(layout.spriteLeft + PET_SPRITE_WIDTH).toBeLessThanOrEqual(layout.chatLeft);
  });

  it("places Quick Chat to the left of the sprite away from the screen edge", () => {
    const layout = chatLayout("left");

    expect(layout.chatLeft + QUICK_CHAT_WIDTH).toBeLessThanOrEqual(layout.spriteLeft);
  });

  it("keeps the reminder directly below the sprite and inside the pet window", () => {
    for (const side of ["left", "right"] as const) {
      const layout = chatLayout(side);

      expect(layout.reminderLeft).toBeGreaterThanOrEqual(0);
      expect(layout.reminderLeft + REMINDER_WIDTH).toBeLessThanOrEqual(PET_WINDOW_WIDTH);
      expect(layout.reminderLeft).toBeLessThanOrEqual(layout.spriteLeft + PET_SPRITE_WIDTH);
    }
  });
});
