export type ChatSide = "left" | "right";

export const PET_SPRITE_WIDTH = 96;
export const PET_WINDOW_WIDTH = 520;
export const QUICK_CHAT_WIDTH = 344;
export const REMINDER_WIDTH = 220;
const CHAT_GAP = 8;

export function chatLayout(side: ChatSide): {
  spriteLeft: number;
  chatLeft: number;
  reminderLeft: number;
} {
  if (side === "right") {
    return { spriteLeft: 0, chatLeft: PET_SPRITE_WIDTH + CHAT_GAP, reminderLeft: 0 };
  }
  const spriteLeft = QUICK_CHAT_WIDTH + CHAT_GAP;
  return {
    spriteLeft,
    chatLeft: 0,
    reminderLeft: Math.min(
      PET_WINDOW_WIDTH - REMINDER_WIDTH,
      spriteLeft + (PET_SPRITE_WIDTH - REMINDER_WIDTH) / 2,
    ),
  };
}
