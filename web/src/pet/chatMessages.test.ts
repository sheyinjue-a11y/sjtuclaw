import { describe, expect, it } from "vitest";

import { toRecentChatMessages } from "./chatMessages";

describe("toRecentChatMessages", () => {
  it("returns only the last ten user and assistant text messages", () => {
    const visible = Array.from({ length: 12 }, (_, index) => ({
      messageId: `m${index + 1}`,
      role: index % 2 === 0 ? "user" : "assistant",
      content: `message ${index + 1}`,
      sequenceNumber: index + 1,
      reasoningContent: "private reasoning",
      toolCalls: [{ name: "secret_tool" }],
    }));
    const session = {
      messages: [
        { messageId: "system", role: "system", content: "hidden", sequenceNumber: 0 },
        { messageId: "tool", role: "tool", content: "tool output", sequenceNumber: 1 },
        ...visible,
        { messageId: "blank", role: "assistant", content: "   ", sequenceNumber: 99 },
      ],
    };

    expect(toRecentChatMessages(session)).toEqual(
      visible.slice(-10).map((message) => ({
        id: message.messageId,
        role: message.role,
        content: message.content,
      })),
    );
  });

  it("returns an empty list for malformed snapshots", () => {
    expect(toRecentChatMessages(null)).toEqual([]);
    expect(toRecentChatMessages({ messages: "invalid" })).toEqual([]);
  });
});
