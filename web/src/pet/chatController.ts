import type { PetActiveSession, PetSendTarget } from "./api";
import { toRecentChatMessages, type ChatMessage } from "./chatMessages";
import { toPetViewModel } from "./state";

export type ChatSnapshot = {
  connected: boolean;
  sessionId: string;
  sessionTitle: string;
  messages: ChatMessage[];
};

export type ChatClient = {
  getActiveSession(): Promise<PetActiveSession>;
  getSession(sessionId: string): Promise<unknown>;
  send(target: PetSendTarget, text: string): Promise<void>;
};

export class ChatSessionController {
  private listener: ((snapshot: ChatSnapshot) => void) | null = null;
  private timer: ReturnType<typeof globalThis.setInterval> | null = null;
  private refreshInFlight: Promise<void> | null = null;
  private lifecycle = 0;
  private snapshot: ChatSnapshot = {
    connected: false,
    sessionId: "",
    sessionTitle: "当前会话",
    messages: [],
  };

  constructor(private readonly options: { client: ChatClient; pollMilliseconds?: number }) {}

  subscribe(listener: (snapshot: ChatSnapshot) => void): void {
    this.listener = listener;
  }

  start(listener: (snapshot: ChatSnapshot) => void): void {
    this.stop();
    this.listener = listener;
    void this.refresh();
    this.timer = globalThis.setInterval(
      () => void this.refresh(),
      this.options.pollMilliseconds ?? 1_000,
    );
  }

  stop(): void {
    this.lifecycle += 1;
    if (this.timer !== null) globalThis.clearInterval(this.timer);
    this.timer = null;
    this.refreshInFlight = null;
    this.listener = null;
  }

  async refresh(): Promise<void> {
    if (this.refreshInFlight) return this.refreshInFlight;
    const refresh = this.refreshSnapshot(this.lifecycle);
    this.refreshInFlight = refresh;
    try {
      await refresh;
    } finally {
      if (this.refreshInFlight === refresh) this.refreshInFlight = null;
    }
  }

  private async refreshSnapshot(lifecycle: number): Promise<void> {
    try {
      const active = await this.options.client.getActiveSession();
      const session = await this.options.client.getSession(active.sessionId);
      if (lifecycle !== this.lifecycle) return;
      const model = toPetViewModel(session);
      this.snapshot = {
        connected: true,
        sessionId: active.sessionId,
        sessionTitle: active.title ?? model.sessionTitle,
        messages: toRecentChatMessages(session),
      };
    } catch {
      if (lifecycle !== this.lifecycle) return;
      this.snapshot = { ...this.snapshot, connected: false };
    }
    this.listener?.(this.snapshot);
  }

  async send(text: string): Promise<void> {
    const active = await this.options.client.getActiveSession();
    const model = toPetViewModel(await this.options.client.getSession(active.sessionId));
    await this.options.client.send(
      { sessionId: active.sessionId, activeRunId: model.activeRunId },
      text,
    );
    await this.refresh();
  }
}
