export type ActiveSessionSummary = {
  sessionId: string;
  title?: string;
};

export type ActiveSessionApi = <T>(path: string, init?: RequestInit) => Promise<T>;

export function chooseInitialSession(
  activeId: string | null,
  sessions: readonly ActiveSessionSummary[],
): string | null {
  return sessions.some((item) => item.sessionId === activeId)
    ? activeId
    : sessions[0]?.sessionId ?? null;
}

export async function readActiveSession(api: ActiveSessionApi): Promise<ActiveSessionSummary> {
  return (await api<{ session: ActiveSessionSummary }>("/ui/active-session")).session;
}

export async function writeActiveSession(api: ActiveSessionApi, sessionId: string): Promise<void> {
  await api("/ui/active-session", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId }),
  });
}
