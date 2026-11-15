export type ToolResource = {
  kind: "download";
  name: string;
  href: string;
  mimeType: string | null;
  expiresAt: string | null;
};

const DOWNLOAD_HREF = /^\/downloads\/[A-Za-z0-9_-]{1,256}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function normalizeToolResources(value: unknown): ToolResource[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item) || item.kind !== "download") return [];
    if (typeof item.name !== "string" || !item.name) return [];
    if (typeof item.href !== "string" || !DOWNLOAD_HREF.test(item.href)) return [];
    return [{
      kind: "download" as const,
      name: item.name,
      href: item.href,
      mimeType: typeof item.mimeType === "string" ? item.mimeType : null,
      expiresAt: typeof item.expiresAt === "string" ? item.expiresAt : null,
    }];
  });
}

export function visibleToolResources(
  resources: ToolResource[],
  status: "running" | "success" | "error",
): ToolResource[] {
  return status === "error" ? [] : resources;
}
