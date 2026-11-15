export type MemoryCategory = "identity" | "preference" | "project" | "constraint" | "event" | "other";

export type MemoryEntry = {
  memoryId: string;
  content: string;
  category: MemoryCategory;
  importance: number;
  sourceKind: "automatic" | "manual";
  sourceSessionId: string | null;
  sourceMessageId: string | null;
  originalTimeText: string | null;
  eventAt: string | null;
  eventTimezone: string | null;
  createdAt: string;
  updatedAt: string | null;
  revision: number;
};

export type MemoryPage = {
  items: MemoryEntry[];
  nextCursor: string | null;
};

export const memoryCategories: Array<{ value: MemoryCategory; label: string }> = [
  { value: "identity", label: "身份" },
  { value: "preference", label: "偏好" },
  { value: "project", label: "项目" },
  { value: "constraint", label: "约束" },
  { value: "event", label: "事件" },
  { value: "other", label: "其他" },
];

export function categoryLabel(category: MemoryCategory): string {
  return memoryCategories.find((item) => item.value === category)?.label ?? category;
}

export function memoryMoment(memory: MemoryEntry): string {
  return memory.eventAt ?? memory.createdAt;
}

export function sortMemories(memories: MemoryEntry[]): MemoryEntry[] {
  return [...memories].sort((left, right) => {
    const timeDifference = new Date(memoryMoment(right)).valueOf() - new Date(memoryMoment(left)).valueOf();
    return timeDifference || left.memoryId.localeCompare(right.memoryId);
  });
}

export function mergeMemories(current: MemoryEntry[], incoming: MemoryEntry[]): MemoryEntry[] {
  const merged = new Map(current.map((item) => [item.memoryId, item]));
  incoming.forEach((item) => merged.set(item.memoryId, item));
  return sortMemories([...merged.values()]);
}

export function groupedMemories(memories: MemoryEntry[]): Array<{ label: string; items: MemoryEntry[] }> {
  const groups = new Map<string, { label: string; items: MemoryEntry[] }>();
  memories.forEach((memory) => {
    const date = new Date(memoryMoment(memory));
    const key = Number.isNaN(date.valueOf()) ? "unknown" : [date.getFullYear(), date.getMonth(), date.getDate()].join("-");
    const label = Number.isNaN(date.valueOf())
      ? "时间未知"
      : new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "short" }).format(date);
    const group = groups.get(key) ?? { label, items: [] };
    group.items.push(memory);
    groups.set(key, group);
  });
  return [...groups.values()];
}

export function formatMemoryTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatRelativeMemoryTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "时间未知";
  const seconds = Math.round((date.valueOf() - Date.now()) / 1_000);
  const formatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
  const days = Math.round(hours / 24);
  if (Math.abs(days) < 30) return formatter.format(days, "day");
  const months = Math.round(days / 30);
  if (Math.abs(months) < 12) return formatter.format(months, "month");
  return formatter.format(Math.round(months / 12), "year");
}

export function toDateTimeLocal(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  const local = new Date(date.valueOf() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

export function eventTimePayload(value: string): { eventAt: string | null; eventTimezone: string | null } {
  if (!value) return { eventAt: null, eventTimezone: null };
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return { eventAt: null, eventTimezone: null };
  return {
    eventAt: date.toISOString(),
    eventTimezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  };
}
