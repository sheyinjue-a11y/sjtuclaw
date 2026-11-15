import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import type { ChatSnapshot } from "./chatController";

type ChatController = {
  start(listener: (snapshot: ChatSnapshot) => void): void;
  stop(): void;
  send(text: string): Promise<void>;
};

const initialSnapshot: ChatSnapshot = {
  connected: false,
  sessionId: "",
  sessionTitle: "当前会话",
  messages: [],
};

export function ChatApp({ controller }: { controller: ChatController }) {
  const [snapshot, setSnapshot] = useState(initialSnapshot);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const messageEnd = useRef<HTMLDivElement>(null);

  useEffect(() => {
    controller.start(setSnapshot);
    return () => controller.stop();
  }, [controller]);

  useEffect(() => {
    messageEnd.current?.scrollIntoView?.({ block: "end" });
  }, [snapshot.messages]);

  const send = async () => {
    const value = draft.trim();
    if (!value || sending || !snapshot.connected) return;
    setSending(true);
    setError(null);
    try {
      await controller.send(value);
      setDraft("");
    } catch {
      setError("发送失败，请重试");
    } finally {
      setSending(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void send();
  };

  return (
    <div className="mini-chat">
      <header className="mini-chat__header">
        <p>当前会话</p>
        <h1>{snapshot.sessionTitle}</h1>
      </header>

      <div className="mini-chat__status" role="status">
        <span aria-hidden="true" />
        {snapshot.connected ? "已连接" : "正在重连"}
      </div>

      <main className="mini-chat__messages" aria-live="polite">
        {snapshot.messages.length === 0 && (
          <p className="mini-chat__empty">这里会显示最近 10 条消息。</p>
        )}
        {snapshot.messages.map((message) => (
          <article
            className={`mini-chat__message mini-chat__message--${message.role}`}
            key={message.id}
          >
            <span>{message.role === "user" ? "你" : "SJTUClaw"}</span>
            <p>{message.content}</p>
          </article>
        ))}
        <div ref={messageEnd} />
      </main>

      <footer className="mini-chat__composer">
        <textarea
          aria-label="发送消息"
          placeholder="给 SJTUClaw 发消息…"
          value={draft}
          disabled={!snapshot.connected || sending}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="mini-chat__composer-row">
          <span>Enter 发送 · Shift+Enter 换行</span>
          <button
            type="button"
            disabled={!draft.trim() || !snapshot.connected || sending}
            onClick={() => void send()}
          >
            {sending ? "发送中" : "发送"}
          </button>
        </div>
        {error && <p className="mini-chat__error" role="alert">{error}</p>}
      </footer>
    </div>
  );
}
