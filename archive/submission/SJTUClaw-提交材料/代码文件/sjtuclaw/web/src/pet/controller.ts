import { reducePetEvent, toPetViewModel, type PetViewModel } from "./state";
import type { PetActiveSession, PetSendTarget } from "./api";

export type PetEventSource = {
  addEventListener(type: string, listener: (event: MessageEvent) => void): void;
  close(): void;
  onerror: (() => void) | null;
};

export type PetGatewayClientLike = {
  getActiveSession(): Promise<PetActiveSession>;
  getSession(sessionId: string): Promise<unknown>;
  eventUrl(sessionId: string, runId: string): string;
  send(target: PetSendTarget, text: string): Promise<void>;
  requestBrowserToken(): Promise<string>;
};

export type PetConnectionSnapshot = {
  connected: boolean;
  model: PetViewModel;
};

export type PetSessionControllerOptions = {
  client: PetGatewayClientLike;
  eventSource: (url: string) => PetEventSource;
  pollMilliseconds?: number;
};

const eventTypes = [
  "run_started",
  "assistant_message",
  "approval_required",
  "run_completed",
  "run_failed",
  "run_cancelled",
] as const;

const initialSnapshot: PetConnectionSnapshot = {
  connected: false,
  model: toPetViewModel({}),
};

export class PetSessionController {
  private listener: ((snapshot: PetConnectionSnapshot) => void) | null = null;
  private source: PetEventSource | null = null;
  private streamKey: string | null = null;
  private timer: ReturnType<typeof globalThis.setInterval> | null = null;
  private snapshot = initialSnapshot;
  private refreshInFlight: Promise<void> | null = null;
  private lifecycle = 0;

  constructor(private readonly options: PetSessionControllerOptions) {}

  start(listener: (snapshot: PetConnectionSnapshot) => void): void {
    this.stop();
    this.listener = listener;
    void this.refresh();
    this.timer = globalThis.setInterval(
      () => void this.refresh(),
      this.options.pollMilliseconds ?? 1_000,
    );
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

  stop(): void {
    this.lifecycle += 1;
    if (this.timer !== null) globalThis.clearInterval(this.timer);
    this.timer = null;
    this.source?.close();
    this.source = null;
    this.streamKey = null;
    this.refreshInFlight = null;
    this.listener = null;
  }

  async send(text: string): Promise<void> {
    const active = await this.options.client.getActiveSession();
    const model = toPetViewModel(await this.options.client.getSession(active.sessionId));
    await this.options.client.send(
      { sessionId: active.sessionId, activeRunId: model.activeRunId },
      text,
    );
  }

  requestBrowserToken(): Promise<string> {
    return this.options.client.requestBrowserToken();
  }

  private async refreshSnapshot(lifecycle: number): Promise<void> {
    try {
      const active = await this.options.client.getActiveSession();
      const model = toPetViewModel(await this.options.client.getSession(active.sessionId));
      if (lifecycle !== this.lifecycle) return;

      const streamKey = model.activeRunId ? `${model.sessionId}:${model.activeRunId}` : null;
      if (streamKey !== this.streamKey) {
        this.source?.close();
        this.source = null;
        this.streamKey = streamKey;
        if (model.activeRunId) this.connectEvents(model.sessionId, model.activeRunId);
      }

      this.snapshot = { connected: true, model };
      this.publish();
    } catch {
      if (lifecycle !== this.lifecycle) return;
      this.snapshot = { ...this.snapshot, connected: false };
      this.publish();
    }
  }

  private connectEvents(sessionId: string, runId: string): void {
    const source = this.options.eventSource(this.options.client.eventUrl(sessionId, runId));
    this.source = source;
    for (const type of eventTypes) {
      source.addEventListener(type, () => {
        if (this.source !== source) return;
        this.snapshot = {
          ...this.snapshot,
          model: reducePetEvent(this.snapshot.model, { type, runId }),
        };
        this.publish();
        void this.refresh();
      });
    }
    source.onerror = () => {
      if (this.source !== source) return;
      this.snapshot = { ...this.snapshot, connected: false };
      this.publish();
    };
  }

  private publish(): void {
    this.listener?.(this.snapshot);
  }
}
