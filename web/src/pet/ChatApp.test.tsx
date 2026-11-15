// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { ChatApp } from "./ChatApp";
import type { ChatSnapshot } from "./chatController";

const snapshot: ChatSnapshot = {
  connected: true,
  sessionId: "s1",
  sessionTitle: "最近会话",
  messages: [
    { id: "m1", role: "user", content: "进度怎么样？" },
    { id: "m2", role: "assistant", content: "已经完成。" },
  ],
};

function controller(send = vi.fn().mockResolvedValue(undefined)) {
  return {
    start(listener: (value: ChatSnapshot) => void) {
      listener(snapshot);
    },
    stop: vi.fn(),
    send,
  };
}

afterEach(cleanup);

it("renders the recent session messages", () => {
  render(<ChatApp controller={controller()} />);

  expect(screen.getByText("最近会话")).not.toBeNull();
  expect(screen.getByText("进度怎么样？")).not.toBeNull();
  expect(screen.getByText("已经完成。")).not.toBeNull();
  expect(screen.getByText("已连接")).not.toBeNull();
});

it("sends with Enter and keeps Shift Enter for a newline", async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  render(<ChatApp controller={controller(send)} />);
  const input = screen.getByRole("textbox", { name: "发送消息" });

  await userEvent.type(input, "第一行");
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect(send).not.toHaveBeenCalled();

  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(send).toHaveBeenCalledWith("第一行"));
  await waitFor(() => expect((input as HTMLTextAreaElement).value).toBe(""));
});

it("keeps the draft when sending fails", async () => {
  const send = vi.fn().mockRejectedValue(new Error("offline"));
  render(<ChatApp controller={controller(send)} />);
  const input = screen.getByRole("textbox", { name: "发送消息" });

  await userEvent.type(input, "请保留");
  fireEvent.keyDown(input, { key: "Enter" });

  expect((await screen.findByRole("alert")).textContent).toBe("发送失败，请重试");
  expect((input as HTMLTextAreaElement).value).toBe("请保留");
});
