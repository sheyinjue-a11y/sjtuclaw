export type Rectangle = { x: number; y: number; width: number; height: number };

export type ChatWindowLike = {
  isDestroyed(): boolean;
  isMinimized(): boolean;
  restore(): void;
  show(): void;
  focus(): void;
};

const CHAT_SIZE = { width: 480, height: 600 };
const WINDOW_GAP = 12;

export function focusExistingChatWindow(window: ChatWindowLike | null): boolean {
  if (!window || window.isDestroyed()) return false;
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
  return true;
}

export function chatWindowBounds(pet: Rectangle, workArea: Rectangle): Rectangle {
  const width = Math.min(CHAT_SIZE.width, workArea.width);
  const height = Math.min(CHAT_SIZE.height, workArea.height);
  const right = pet.x + pet.width + WINDOW_GAP;
  const left = pet.x - width - WINDOW_GAP;
  const x = right + width <= workArea.x + workArea.width
    ? right
    : left >= workArea.x
      ? left
      : workArea.x + Math.floor((workArea.width - width) / 2);
  return {
    x,
    y: Math.max(workArea.y, Math.min(pet.y, workArea.y + workArea.height - height)),
    width,
    height,
  };
}
