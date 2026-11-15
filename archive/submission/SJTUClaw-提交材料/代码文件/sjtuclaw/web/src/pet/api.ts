export type PetActiveSession = {
  sessionId: string;
  title?: string;
};

export type PetSendTarget = {
  sessionId: string;
  activeRunId: string | null;
};

export type PetFetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export class PetGatewayClient {
  private readonly apiBase: string;

  constructor(
    origin: string,
    private readonly fetcher: PetFetcher,
  ) {
    const base = origin.replace(/\/+$/, "");
    this.apiBase = base.endsWith("/api/v1") ? base : `${base}/api/v1`;
  }

  async exchangeBootstrapFragment(fragment = window.location.hash): Promise<void> {
    const token = new URLSearchParams(fragment.replace(/^#/, "")).get("bootstrap");
    if (!token) return;

    await this.request<void>("/auth/bootstrap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  }

  async getActiveSession(): Promise<PetActiveSession> {
    const payload = await this.request<{ session?: unknown }>("/ui/active-session");
    const session = payload.session;
    if (!session || typeof session !== "object" || typeof (session as PetActiveSession).sessionId !== "string") {
      throw new Error("Gateway returned an invalid active session");
    }
    return session as PetActiveSession;
  }

  async getSession(sessionId: string): Promise<unknown> {
    const payload = await this.request<{ session?: unknown }>(`/sessions/${encodeURIComponent(sessionId)}`);
    if (!payload.session || typeof payload.session !== "object") {
      throw new Error("Gateway returned an invalid session snapshot");
    }
    return payload.session;
  }

  eventUrl(sessionId: string, runId: string): string {
    return `${this.apiBase}/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/events`;
  }

  async send(target: PetSendTarget, text: string): Promise<void> {
    const value = text.trim();
    if (!value) throw new Error("Message cannot be empty");

    if (target.activeRunId) {
      const id = crypto.randomUUID();
      await this.request(`/sessions/${encodeURIComponent(target.sessionId)}/runs/${encodeURIComponent(target.activeRunId)}/steer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ steerId: id, idempotencyKey: id, text: value }),
      }, false);
      return;
    }

    await this.request(`/sessions/${encodeURIComponent(target.sessionId)}/requests`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: value,
        attachmentRefs: [],
        idempotencyKey: crypto.randomUUID(),
      }),
    }, false);
  }

  async requestBrowserToken(): Promise<string> {
    const payload = await this.request<{ token?: unknown }>("/auth/bootstrap-token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ purpose: "browser" }),
    });
    if (typeof payload.token !== "string") {
      throw new Error("Gateway returned an invalid browser token");
    }
    return payload.token;
  }

  private async request<T>(path: string, init: RequestInit = {}, readBody = true): Promise<T> {
    const response = await this.fetcher(`${this.apiBase}${path}`, {
      ...init,
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new Error(`Gateway request failed (${response.status})`);
    }
    return !readBody || response.status === 204 ? undefined as T : response.json() as Promise<T>;
  }
}
