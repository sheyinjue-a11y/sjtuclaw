export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? value as Record<string, unknown> : {};
}

export function toRecentChatMessages(snapshot: unknown): ChatMessage[] {
  const messages = record(snapshot).messages;
  if (!Array.isArray(messages)) return [];

  return messages.flatMap((value): ChatMessage[] => {
    const message = record(value);
    const role = message.role;
    const content = typeof message.content === "string" ? message.content.trim() : "";
    if ((role !== "user" && role !== "assistant") || !content) return [];
    return [{
      id: typeof message.messageId === "string"
        ? message.messageId
        : `${role}:${String(message.sequenceNumber ?? content)}`,
      role,
      content,
    }];
  }).slice(-10);
}
