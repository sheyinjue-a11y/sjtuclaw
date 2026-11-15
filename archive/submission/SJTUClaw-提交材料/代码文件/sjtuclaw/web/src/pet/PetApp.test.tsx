// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { PetApp } from "./PetApp";
import type { PetViewModel } from "./state";

const idleModel: PetViewModel = {
  sessionId: "s1",
  sessionTitle: "演示",
  activeRunId: null,
  state: "idle",
  reminder: null,
};

function fakeController(options: {
  send: (text: string) => Promise<void>;
  connected?: boolean;
  model?: PetViewModel;
}) {
  return {
    start(listener: (snapshot: { connected: boolean; model: PetViewModel }) => void) {
      listener({ connected: options.connected ?? true, model: options.model ?? idleModel });
    },
    stop: vi.fn(),
    send: options.send,
    requestBrowserToken: vi.fn().mockResolvedValue("b".repeat(43)),
  };
}

function fakeBridge() {
  return {
    setInteractive: vi.fn(),
    openChat: vi.fn(),
    startDrag: vi.fn(),
    moveDrag: vi.fn(),
    endDrag: vi.fn(),
    getChatSide: vi.fn().mockResolvedValue("left" as const),
    openWorkbench: vi.fn().mockResolvedValue(undefined),
  };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it("opens the independent chat window on right click", () => {
  const bridge = fakeBridge();
  render(<PetApp controller={fakeController({ send: vi.fn() })} bridge={bridge} />);

  fireEvent.contextMenu(screen.getByRole("img", { name: "法尔伽桌宠" }));

  expect(bridge.openChat).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("textbox")).toBeNull();
});

it("starts a drag only from the pet sprite with the left button", () => {
  const bridge = fakeBridge();
  render(<PetApp controller={fakeController({ send: vi.fn() })} bridge={bridge} />);
  const sprite = screen.getByRole("img", { name: "法尔伽桌宠" });

  fireEvent.pointerDown(sprite, { button: 2, screenX: 12, screenY: 34 });
  expect(bridge.startDrag).not.toHaveBeenCalled();

  fireEvent.pointerDown(sprite, { button: 0, screenX: 12, screenY: 34 });
  expect(bridge.startDrag).toHaveBeenCalledWith(12, 34);
});

it("restores click-through after a released drag leaves the pet sprite", () => {
  const bridge = fakeBridge();
  render(<PetApp controller={fakeController({ send: vi.fn() })} bridge={bridge} />);
  const sprite = screen.getByRole("img", { name: "法尔伽桌宠" });
  vi.spyOn(sprite, "matches").mockImplementation((selector) => selector === ":hover");

  fireEvent.pointerEnter(sprite);
  fireEvent.pointerDown(sprite, { button: 0, pointerId: 1, screenX: 12, screenY: 34 });
  fireEvent.pointerUp(sprite, { button: 0, pointerId: 1, screenX: 12, screenY: 34 });
  fireEvent.pointerLeave(sprite);

  expect(bridge.endDrag).toHaveBeenCalledTimes(1);
  expect(bridge.setInteractive).toHaveBeenNthCalledWith(2, true);
  expect(bridge.setInteractive).toHaveBeenLastCalledWith(false);
});

it("marks only the pet sprite and open controls as interactive", () => {
  const bridge = fakeBridge();
  render(<PetApp controller={fakeController({ send: vi.fn() })} bridge={bridge} />);
  const sprite = screen.getByRole("img", { name: "法尔伽桌宠" });

  fireEvent.pointerEnter(sprite);
  fireEvent.pointerLeave(sprite);
  expect(bridge.setInteractive).toHaveBeenNthCalledWith(1, true);
  expect(bridge.setInteractive).toHaveBeenNthCalledWith(2, false);
});

it("keeps the idle sprite on its resting frame after the greeting", () => {
  vi.useFakeTimers();
  render(<PetApp controller={fakeController({ send: vi.fn() })} bridge={fakeBridge()} />);

  act(() => vi.advanceTimersByTime(8_000));
  const sprite = screen.getByRole("img");
  expect(sprite.className).toContain("pet-sprite--frame-0");

  act(() => vi.advanceTimersByTime(2_000));
  expect(sprite.className).toContain("pet-sprite--frame-0");
});

it("advances the running sprite after the greeting", () => {
  vi.useFakeTimers();
  render(<PetApp controller={fakeController({
    send: vi.fn(),
    model: { ...idleModel, state: "running" },
  })} bridge={fakeBridge()} />);

  act(() => vi.advanceTimersByTime(8_000));
  const sprite = screen.getByRole("img");
  expect(sprite.className).toContain("pet-sprite--frame-0");

  act(() => vi.advanceTimersByTime(120));
  expect(sprite.className).toContain("pet-sprite--frame-1");
});

it("dismisses only the current generic reminder", async () => {
  const model: PetViewModel = {
    ...idleModel,
    state: "waiting",
    reminder: { id: "approval:r1", kind: "approval", text: "需要审批，请在工作台处理" },
  };
  render(<PetApp controller={fakeController({ send: vi.fn(), model })} bridge={fakeBridge()} />);

  await userEvent.click(screen.getByRole("button", { name: "关闭提醒" }));

  expect(screen.queryByRole("status")).toBeNull();
});

it("makes the approval action interactive without blocking pet drag", () => {
  const bridge = fakeBridge();
  const model: PetViewModel = {
    ...idleModel,
    state: "waiting",
    reminder: { id: "approval:r1", kind: "approval", text: "需要审批，请在工作台处理" },
  };
  render(<PetApp controller={fakeController({ send: vi.fn(), model })} bridge={bridge} />);

  const approval = screen.getByRole("button", { name: "打开工作台" });
  fireEvent.pointerEnter(approval);
  fireEvent.pointerLeave(approval);

  expect(bridge.setInteractive).toHaveBeenNthCalledWith(1, true);
  expect(bridge.setInteractive).toHaveBeenNthCalledWith(2, false);
});
